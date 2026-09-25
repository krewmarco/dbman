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
import fnmatch
import json
from urllib.parse import urlsplit

from .base import (
    Provider, Column, ColumnOption, RowKey, RowPage, Capabilities, DiagramModel,
    FILTER_EMPTY, FILTER_NOT_EMPTY,
)

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Property types dbman can display and write back. Everything else
# (relation, people, files, formula, rollup, created/last_edited_*,
# unique_id, ...) is display-only - see _read_property/_write_property.
_EDITABLE_TYPES = {
    "title", "rich_text", "number", "checkbox", "url", "email",
    "phone_number", "date", "select", "status", "multi_select",
}

# Enum-like property types: their schema declares a fixed option set (with a
# per-option color), which get_schema surfaces as Column.options so the UI
# can offer a picker rather than a free-text box. That's a correctness fix,
# not just ergonomics - writing an arbitrary name to a `select` or
# `multi_select` silently *creates* a new option on the user's real
# database, while the same on a `status` 400s (Notion's API can't create
# status options at all). Picking from the declared set can do neither.
_CHOICE_TYPES = {"select", "status", "multi_select"}

# Notion's query API has no native "manual row order" - it can only sort by
# a page's own properties. To support persistent shift+j/k reordering
# (capabilities.reorder_row), dbman lazily adds this real `number` property
# to the database the first time a reorder is attempted, and always sorts by
# it once present. It's excluded from get_schema's returned columns so it
# never shows up as a regular editable column - but unlike _dbman_layout for
# SQLite, it can't be tucked away in a side table: Notion has no
# join/sidecar concept, so this necessarily becomes a real, visible property
# on the user's database (see CLAUDE.md's Notion provider notes).
_ORDER_PROPERTY = "_dbman_order"
_ORDER_STEP = 1000

# Notion's query filter API is per-property-type (the condition lives under
# a key named after the type, e.g. {"rich_text": {"contains": ...}}), unlike
# SqlAlchemyProvider/CouchDBProvider where one clause shape fits every
# column. These two sets say which types accept a free-text substring/exact
# match vs. only the is_empty/is_not_empty keywords.
_TEXT_FILTER_TYPES = {"title", "rich_text", "url", "email", "phone_number"}
_EMPTY_CAPABLE_TYPES = _TEXT_FILTER_TYPES | {
    "number", "select", "status", "multi_select", "date", "people", "files", "relation",
}


def _notion_filter_condition(ptype: str, val: str):
    """Build the type-specific inner filter condition for one column's
    filter-box value, mirroring the null/not null/empty/not empty keyword
    convention used by SqlAlchemyProvider._build_filter_clause and
    CouchDBProvider._mango_selector. Returns None if this property type
    can't express the given value - skipped rather than sent to the API and
    guaranteed to error or never match (e.g. checkbox has no is_empty;
    people/files/relation's "contains" needs a raw id, not the display name
    dbman's filter box actually holds; dates have no substring match)."""
    low = val.lower()
    if low in ("null", "empty"):
        return {"is_empty": True} if ptype in _EMPTY_CAPABLE_TYPES else None
    if low in ("not null", "!null", "not empty"):
        return {"is_not_empty": True} if ptype in _EMPTY_CAPABLE_TYPES else None
    if ptype in _TEXT_FILTER_TYPES:
        return {"contains": val}
    if ptype in ("select", "status"):
        return {"equals": val}
    if ptype == "multi_select":
        return {"contains": val}
    if ptype == "number":
        try:
            return {"equals": float(val)}
        except ValueError:
            return None
    if ptype == "checkbox":
        return {"equals": low in ("1", "true", "yes", "y")}
    return None


def _notion_choice_condition(ptype: str, val: str):
    """The inner condition for one entry of a *list* filter value - the
    picker path for enum-like columns (Column.options). Unlike the free-text
    path this is always an exact match, because the entry came from the
    property's own declared option set."""
    if val == FILTER_EMPTY:
        return {"is_empty": True} if ptype in _EMPTY_CAPABLE_TYPES else None
    if val == FILTER_NOT_EMPTY:
        return {"is_not_empty": True} if ptype in _EMPTY_CAPABLE_TYPES else None
    if ptype == "multi_select":
        # A multi_select cell holds several options, so "has this one" is
        # `contains`, not `equals`.
        return {"contains": val}
    if ptype in ("select", "status"):
        return {"equals": val}
    return None


def _wildcard_filters(col_types: dict, filters: dict) -> dict:
    """The filter entries routed around Notion's server-side filter and
    matched client-side with fnmatch instead (see _row_matches_wildcards).

    Restricted to _TEXT_FILTER_TYPES on purpose. Globbing only earns its
    keep where the server-side alternative is a plain substring `contains`
    with no anchoring; an enum-like column is picked from a dropdown now, so
    a glob there is neither producible from the UI nor meaningful - Notion's
    select/status filter is an exact `equals` against a predefined option.
    Shared by _build_notion_filter and get_page so the two can't disagree
    about which entries the server is handling."""
    return {
        c: v for c, v in filters.items()
        if isinstance(v, str) and "*" in v and col_types.get(c) in _TEXT_FILTER_TYPES
    }


def _row_matches_wildcards(row: list, col_names: list, wildcard_filters: dict) -> bool:
    """Glob-match a wildcard filter box value (e.g. "*Timeline*") against a
    row's already-flattened display values (the same strings the DataTable
    renders) - a filter shape Notion's per-type filter API can't express
    server-side, see _build_notion_filter. Case-insensitive, consistent
    with SQL's default LIKE behavior on SQLite. A None value (empty cell)
    never matches a wildcard - use the existing 'empty'/'not empty'
    keywords for that instead."""
    for col_name, pattern in wildcard_filters.items():
        try:
            idx = col_names.index(col_name)
        except ValueError:
            continue
        value = row[idx]
        if value is None or not fnmatch.fnmatch(str(value).lower(), pattern.lower()):
            return False
    return True


def _rich_text_to_str(rich_text: list) -> str:
    return "".join(t.get("plain_text", "") for t in (rich_text or []))


def _rich_text_payload(value) -> list:
    if not value:
        return []
    return [{"type": "text", "text": {"content": str(value)}}]


def _split_multi(value) -> list:
    """Inverse of _read_property's multi_select join. Only a fallback for a
    plain-string write - an option name containing ", " wouldn't survive the
    round trip, which is why the picker passes a list instead."""
    if value in (None, ""):
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


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
        prefix = body.get("prefix") if body else None
        return f"{prefix}-{number}" if prefix else str(number)
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
    if ptype in ("select", "status"):
        return {ptype: {"name": value} if value else None}
    if ptype == "multi_select":
        # The UI's multi-choice picker hands over a real list; the string
        # fallback mirrors _read_property's ", " join so a value that came
        # back out of a cell can go straight back in.
        names = value if isinstance(value, (list, tuple)) else _split_multi(value)
        return {"multi_select": [{"name": n} for n in names if n]}
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
        path_segments = [p for p in parts.path.split("/") if p]
        # Page id lives in the URL *path* (like CouchDB's dbname), not the
        # host, specifically so derive_db_name() (view_settings.py) can pull
        # a secret-free name from it - a bare host+userinfo shape would leave
        # the path empty and fall back to hashing the whole URL, token
        # included, into the saved connection name.
        if not token or len(path_segments) != 1:
            raise ValueError(
                "Notion URL must be notion://<integration-token>@notion.so/<page-id>"
            )
        page_id = path_segments[0]
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
        # Per-database_id cache of whether _ORDER_PROPERTY exists yet -
        # populated for free by get_schema's own database fetch, so a plain
        # get_page doesn't need an extra round trip just to check.
        self._order_ready: dict = {}

        self.capabilities = Capabilities(
            definition_pane=True,
            create_definition=False,
            diagram=False,
            lookup_plugin=False,
            truncate_column=False,
            whole_row_edit=False,
            open_in_browser=True,
            delete_item=False,
            add_row=True,
            delete_row=True,
            reorder_row=True,
            sort_column=True,
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
        properties = db.get("properties", {})
        self._order_ready[database_id] = _ORDER_PROPERTY in properties
        columns = []
        for prop_name, prop in properties.items():
            if prop_name == _ORDER_PROPERTY:
                continue  # dbman-owned sort ordinal, not a real column - see reorder_row
            ptype = prop.get("type")
            # The option set (and each option's color) is already in this
            # same GET /databases response - surfacing it costs no extra
            # round trip, and get_schema is called on every get_page.
            options = ()
            if ptype in _CHOICE_TYPES:
                options = tuple(
                    ColumnOption(o.get("name", ""), o.get("color"))
                    for o in (prop.get(ptype) or {}).get("options", [])
                )
            columns.append(Column(
                name=prop_name,
                type_name=ptype,
                primary_key=(ptype == "title"),
                read_only=(ptype not in _EDITABLE_TYPES),
                options=options,
                multi_value=(ptype == "multi_select"),
            ))
        # Notion doesn't guarantee properties are returned in display order;
        # put title first since it's the row's de facto identity, like a PK.
        columns.sort(key=lambda c: (not c.primary_key, c.name))
        return columns

    def _build_notion_filter(self, columns: list, filters: dict):
        """Translate dbman's filter map into a Notion `filter` payload
        object. Two value shapes, per get_page's contract in base.py:

          * a list - an enum-like column's picker selection, OR'd together
            via _notion_choice_condition. An `and` of `or`s is two levels,
            which is exactly the 2022-06-28 compound-filter API's maximum
            nesting depth; don't nest further.
          * a str - the free-text path, via _notion_filter_condition.

        Skipped entirely: any column/value combination
        _notion_filter_condition can't express (see its docstring), and the
        text-column wildcard filters _wildcard_filters claims, which get_page
        applies client-side instead - Notion's text `contains` is a plain
        substring with no anchoring, so a glob has to be matched here."""
        col_types = {c.name: c.type_name for c in columns}
        handled_client_side = _wildcard_filters(col_types, filters)
        conditions = []
        for col_name, val in filters.items():
            if not val or col_name in handled_client_side:
                continue
            ptype = col_types.get(col_name)
            if not ptype:
                continue
            if isinstance(val, list):
                subs = [
                    {"property": col_name, ptype: cond}
                    for cond in (_notion_choice_condition(ptype, v) for v in val)
                    if cond is not None
                ]
                if subs:
                    conditions.append(subs[0] if len(subs) == 1 else {"or": subs})
                continue
            condition = _notion_filter_condition(ptype, val)
            if condition is None:
                continue
            conditions.append({"property": col_name, ptype: condition})
        if not conditions:
            return None
        return conditions[0] if len(conditions) == 1 else {"and": conditions}

    def get_page(self, name, item_type, filters, cursor, page_size, sort=None) -> RowPage:
        database_id = self._database_ids[name]
        columns = self.get_schema(name, item_type)
        col_names = [c.name for c in columns]

        payload = {}
        if page_size:
            payload["page_size"] = page_size
        if cursor:
            payload["start_cursor"] = cursor
        if sort:
            col_name, direction = sort
            payload["sorts"] = [{
                "property": col_name,
                "direction": "ascending" if direction != "desc" else "descending",
            }]
        elif self._order_ready.get(database_id):
            # No explicit column sort requested - fall back to dbman's own
            # manual row-order property (see reorder_row) rather than
            # Notion's default (creation-time) ordering.
            payload["sorts"] = [{"property": _ORDER_PROPERTY, "direction": "ascending"}]
        notion_filter = self._build_notion_filter(columns, filters)
        if notion_filter:
            payload["filter"] = notion_filter
        wildcard_filters = _wildcard_filters({c.name: c.type_name for c in columns}, filters)

        result = self._post(f"/databases/{database_id}/query", payload)
        pages = result.get("results", [])

        rows = []
        row_keys = []
        for page in pages:
            props = page.get("properties", {})
            row = [_read_property(props[c]) if c in props else None for c in col_names]
            if wildcard_filters and not _row_matches_wildcards(row, col_names, wildcard_filters):
                continue
            rows.append(row)
            row_keys.append(RowKey(page["id"]))

        # Applying wildcard_filters here (after the page's already been
        # fetched) means has_more/next_cursor still reflect Notion's
        # unfiltered pagination, not this narrowed row count - a page can
        # come back with fewer rows than page_size, but repeatedly paging
        # forward still converges on every match. Acceptable given
        # page_size is 500 and most Notion databases browsed here are far
        # smaller than that.
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

    def get_row_url(self, name, item_type, row_key: RowKey) -> str:
        # A row is a Notion page in its own right, and a bare no-dash page
        # id resolves directly (no title slug needed) - verified against a
        # real page. This opens in the Notion desktop app if it's installed
        # and registered for notion.so links, else the web app in a browser
        # - either way dbman hands the whole row (properties + body) off to
        # Notion's own editor rather than trying to represent it itself.
        return f"https://www.notion.so/{row_key.value.replace('-', '')}"

    def delete_item(self, name, item_type) -> None:
        raise NotImplementedError(
            "Deleting a Notion database isn't exposed from dbman - archive it "
            "in Notion directly"
        )

    def delete_row(self, name, item_type, row_key: RowKey) -> None:
        # Notion's API has no permanent-delete for pages - archiving is
        # exactly what the "Delete" button in Notion's own UI does (moves it
        # to Trash, recoverable there), so this is a faithful, non-destructive
        # match rather than a workaround.
        self._patch(f"/pages/{row_key.value}", {"archived": True})

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
        if self._order_ready.get(database_id):
            # Append past the current max so a freshly added row sorts last
            # rather than landing with no ordinal (which would push it
            # first/last unpredictably once _dbman_order is the sort key).
            properties[_ORDER_PROPERTY] = {"number": self._max_order(database_id) + _ORDER_STEP}
        resp = self._post("/pages", {
            "parent": {"database_id": database_id},
            "properties": properties,
        })
        return RowKey(resp["id"])

    # -- row reordering (capabilities.reorder_row) -----------------------------

    def _ensure_order_property(self, database_id: str) -> None:
        """Lazily add _ORDER_PROPERTY to the database and backfill existing
        rows the first time a reorder is attempted against it. A no-op once
        the property exists (cached in self._order_ready, or freshly
        discovered via a schema fetch)."""
        if self._order_ready.get(database_id):
            return
        db = self._get(f"/databases/{database_id}")
        if _ORDER_PROPERTY not in db.get("properties", {}):
            self._patch(f"/databases/{database_id}", {
                "properties": {_ORDER_PROPERTY: {"number": {}}}
            })
            self._backfill_order(database_id)
        self._order_ready[database_id] = True

    def _backfill_order(self, database_id: str) -> None:
        """One-time migration: assign every existing row a spaced ordinal
        (1000, 2000, ...) in whatever order Notion's default query returns
        them, so _dbman_order has a value to sort/swap against from then on.
        N+1 PATCH calls, but this only ever runs once per database."""
        order = 0
        cursor = None
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            result = self._post(f"/databases/{database_id}/query", payload)
            for page in result.get("results", []):
                order += _ORDER_STEP
                self._patch(f"/pages/{page['id']}", {
                    "properties": {_ORDER_PROPERTY: {"number": order}}
                })
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")

    def _max_order(self, database_id: str) -> float:
        result = self._post(f"/databases/{database_id}/query", {
            "sorts": [{"property": _ORDER_PROPERTY, "direction": "descending"}],
            "page_size": 1,
        })
        results = result.get("results", [])
        if not results:
            return 0
        return results[0]["properties"][_ORDER_PROPERTY]["number"] or 0

    def reorder_rows(self, name, item_type, ordered_row_keys: list) -> None:
        """Batch counterpart to the (now-removed) single-step move: takes
        the new desired sequence for a run of rows that moved locally and
        reassigns exactly the order values those rows already hold, in
        their new sequence - a pure permutation of known-fresh values read
        once via point GETs (/pages/{id}, always read-your-writes
        consistent), never re-derived from a query mid-batch. That query-
        based neighbor lookup is what the original swap-based move_row used
        internally, and it broke under rapid replay: Notion's
        POST /databases/{id}/query index lagged behind very recent writes
        by a couple of seconds in testing, so a second move issued right
        after a first could still see the pre-first-move ordering and
        silently redo the same swap instead of progressing. Point GETs on
        /pages/{id} showed no such lag."""
        database_id = self._database_ids[name]
        self._ensure_order_property(database_id)
        if len(ordered_row_keys) < 2:
            return

        current_order = {}
        for row_key in ordered_row_keys:
            page = self._get(f"/pages/{row_key.value}")
            current_order[row_key.value] = page["properties"][_ORDER_PROPERTY]["number"] or 0

        new_values = sorted(current_order.values())
        for row_key, new_value in zip(ordered_row_keys, new_values):
            if current_order[row_key.value] != new_value:
                self._patch(f"/pages/{row_key.value}", {
                    "properties": {_ORDER_PROPERTY: {"number": new_value}}
                })
