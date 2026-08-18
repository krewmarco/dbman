"""Notion provider: exposes each Notion database that lives directly under a
given page as one dbman "table". See CLAUDE.md for the architecture writeup.

Pinned to the classic Notion REST API (Notion-Version: 2022-06-28), where one
database has exactly one flat property schema and one query endpoint.
Notion's newer multi-data-source database model (2025-09-03+, where a single
database can fan out into several independently-queryable "data sources") is
out of scope for now: this provider assumes each discovered database has
exactly one implicit data source, true for every database that hasn't been
explicitly split into multiple sources.
"""
import json
from urllib.parse import urlsplit

from .base import Provider, Column, RowKey, RowPage, Capabilities, DiagramModel

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Property types dbman can both display and write back as a plain scalar
# cell. Everything else (relation, multi_select, people, files, formula,
# rollup, status, created/last_edited_*, unique_id, ...) is display-only for
# now - see _read_property/_write_property.
_EDITABLE_TYPES = {
    "title", "rich_text", "number", "checkbox", "url", "email",
    "phone_number", "date", "select",
}


def _rich_text_to_str(rich_text: list) -> str:
    return "".join(t.get("plain_text", "") for t in (rich_text or []))


def _rich_text_payload(value) -> list:
    if not value:
        return []
    return [{"type": "text", "text": {"content": str(value)}}]


def _read_property(prop: dict):
    """Best-effort flatten of a Notion page property value to a display cell."""
    ptype = prop.get("type")
    body = prop.get(ptype)
    if ptype in ("title", "rich_text"):
        return _rich_text_to_str(body)
    if ptype in ("select", "status"):
        return body.get("name") if body else None
    if ptype == "multi_select":
        return ", ".join(o.get("name", "") for o in (body or []))
    if ptype in ("number", "checkbox", "url", "email", "phone_number"):
        return body
    if ptype == "date":
        if not body:
            return None
        start, end = body.get("start"), body.get("end")
        return f"{start} -> {end}" if end else start
    if ptype == "people":
        return ", ".join(p.get("name") or p.get("id", "") for p in (body or []))
    if ptype == "files":
        return ", ".join(f.get("name", "") for f in (body or []))
    if ptype == "relation":
        return ", ".join(r.get("id", "") for r in (body or []))
    if ptype in ("created_time", "last_edited_time"):
        return body
    if ptype in ("created_by", "last_edited_by"):
        return (body.get("name") or body.get("id")) if body else None
    if ptype == "formula":
        inner_type = body.get("type") if body else None
        return body.get(inner_type) if inner_type else None
    if ptype == "rollup":
        inner_type = body.get("type") if body else None
        inner = body.get(inner_type) if inner_type else None
        if inner_type == "array":
            return ", ".join(str(_read_property(p)) for p in (inner or []))
        return inner
    if ptype == "unique_id":
        number = body.get("number") if body else None
        if number is None:
            return None
        return f"{body.get('prefix') or ''}{number}"
    # Unknown/rare type (verification, button, ...): fall back to raw JSON
    # rather than crashing the row fetch over one unrecognized property.
    return json.dumps(body)


def _write_property(ptype: str, value) -> dict:
    if ptype == "title":
        return {"title": _rich_text_payload(value)}
    if ptype == "rich_text":
        return {"rich_text": _rich_text_payload(value)}
    if ptype == "number":
        return {"number": None if value in (None, "") else float(value)}
    if ptype == "checkbox":
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "y")
        return {"checkbox": bool(value)}
    if ptype in ("url", "email", "phone_number"):
        return {ptype: value or None}
    if ptype == "date":
        return {"date": {"start": value} if value else None}
    if ptype == "select":
        return {"select": {"name": value} if value else None}
    raise ValueError(f"Notion property type {ptype!r} is read-only in dbman")


class NotionProvider(Provider):
    """Document-ish provider over the Notion API.

    Concept mapping: a `notion://<integration-token>@<page-id>` URL names
    exactly one Notion page. Each database that lives directly under that
    page (a `child_database` block, inline or full-page view - both showed
    up identically when this was verified against a real page) becomes one
    dbman "table"; there's no separate "view" concept yet, so list_views()
    is always empty. A database row is a Notion page (RowKey = page id); a
    database column is a Notion property, whose type dictates whether dbman
    can write it back (_EDITABLE_TYPES) or only display it.

    Databases linked onto this page from elsewhere in the workspace (a
    "linked database view" whose true parent page is different) are not
    discovered - only databases actually owned by this page's block tree.
    """

    def __init__(self, db_url: str):
        import requests  # lazy: only Notion users need this dependency

        parts = urlsplit(db_url)
        token = parts.username
        page_id = parts.hostname
        if not token or not page_id:
            raise ValueError(
                "Notion URL must be notion://<integration-token>@<page-id>"
            )
        self.page_id = page_id

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
        })

        # Fail fast with a clear error if the token is bad or the page
        # hasn't been shared with the integration.
        self._get(f"/pages/{page_id}")

        self._database_ids: dict = self._discover_databases(page_id)

        self.capabilities = Capabilities(
            definition_pane=True,
            create_definition=False,
            diagram=False,
            lookup_plugin=False,
            truncate_column=False,
            whole_row_edit=False,
            delete_item=False,
            add_row=True,
        )

    # -- HTTP helpers ---------------------------------------------------------

    def _get(self, path, **kwargs):
        resp = self.session.get(f"{API_BASE}{path}", timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, payload, **kwargs):
        resp = self.session.post(f"{API_BASE}{path}", json=payload, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path, payload, **kwargs):
        resp = self.session.patch(f"{API_BASE}{path}", json=payload, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    # -- database discovery -----------------------------------------------------

    def _discover_databases(self, page_id: str) -> dict:
        databases = {}
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            result = self._get(f"/blocks/{page_id}/children", params=params)
            for block in result.get("results", []):
                if block.get("type") != "child_database":
                    continue
                name = block["child_database"].get("title") or "Untitled"
                unique_name, suffix = name, 2
                while unique_name in databases:
                    unique_name = f"{name}-{suffix}"
                    suffix += 1
                databases[unique_name] = block["id"]
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")
        return databases

    # -- Provider interface -----------------------------------------------------

    def list_tables(self) -> list:
        return list(self._database_ids.keys())

    def list_views(self) -> list:
        return []

    def get_schema(self, name, item_type) -> list:
        database_id = self._database_ids[name]
        db = self._get(f"/databases/{database_id}")
        columns = []
        for prop_name, prop in db.get("properties", {}).items():
            ptype = prop.get("type")
            columns.append(Column(
                name=prop_name,
                type_name=ptype,
                primary_key=(ptype == "title"),
                read_only=(ptype not in _EDITABLE_TYPES),
            ))
        # Notion doesn't guarantee properties are returned in display order;
        # put title first since it's the row's de facto identity, like a PK.
        columns.sort(key=lambda c: (not c.primary_key, c.name))
        return columns

    def get_page(self, name, item_type, filters, cursor, page_size) -> RowPage:
        database_id = self._database_ids[name]
        columns = self.get_schema(name, item_type)
        col_names = [c.name for c in columns]

        payload = {}
        if page_size:
            payload["page_size"] = page_size
        if cursor:
            payload["start_cursor"] = cursor

        result = self._post(f"/databases/{database_id}/query", payload)
        pages = result.get("results", [])

        rows = []
        row_keys = []
        for page in pages:
            props = page.get("properties", {})
            rows.append([_read_property(props[c]) if c in props else None for c in col_names])
            row_keys.append(RowKey(page["id"]))

        has_more = bool(result.get("has_more"))
        next_cursor = result.get("next_cursor") if has_more else None
        return RowPage(columns, rows, row_keys, next_cursor=next_cursor, has_more=has_more)

    def get_definition(self, name, item_type) -> str:
        columns = self.get_schema(name, item_type)
        lines = [f"-- Notion database '{name}'", "-- property : type"]
        for c in columns:
            suffix = "" if not c.read_only else "  -- read-only"
            lines.append(f"{c.name} : {c.type_name}{suffix}")
        return "\n".join(lines)

    def get_diagram_model(self) -> DiagramModel:
        return DiagramModel()

    def delete_item(self, name, item_type) -> None:
        raise NotImplementedError(
            "Deleting a Notion database isn't exposed from dbman - archive it "
            "in Notion directly"
        )

    def update_cell(self, name, item_type, row_key: RowKey, column, value) -> RowKey:
        database_id = self._database_ids[name]
        db = self._get(f"/databases/{database_id}")
        prop = db.get("properties", {}).get(column)
        if prop is None:
            raise ValueError(f"Unknown Notion property {column!r}")
        payload = {"properties": {column: _write_property(prop["type"], value)}}
        self._patch(f"/pages/{row_key.value}", payload)
        return row_key

    def add_row(self, name, item_type) -> RowKey:
        database_id = self._database_ids[name]
        title_col = next((c for c in self.get_schema(name, item_type) if c.primary_key), None)
        properties = {title_col.name: _write_property("title", "")} if title_col else {}
        resp = self._post("/pages", {
            "parent": {"database_id": database_id},
            "properties": properties,
        })
        return RowKey(resp["id"])
