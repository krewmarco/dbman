import json
from urllib.parse import urlsplit, urlunsplit

from .base import (
    Provider, Column, RowKey, RowPage, Capabilities,
    DiagramModel,
)

SAMPLE_SIZE = 200
MAX_INFERRED_COLUMNS = 15
VIEW_TEMPLATE = (
    'DESIGN_DOC: my_design\n'
    'VIEW_NAME: my_view\n'
    'MAP:\n'
    'function(doc) {\n'
    '  emit(doc._id, doc);\n'
    '}\n'
    'REDUCE:\n'
    '\n'
)


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "mixed"


class CouchDBProvider(Provider):
    """Document-database provider for a single CouchDB database.

    Concept mapping: the CouchDB *database* named in the URL is dbman's one
    "table" (all documents, browsed/paged via Mango `_find`). Each design-doc
    map/reduce view is exposed as a "view" named "ddoc/view". There is no
    fixed schema and no foreign keys, so schema/diagram/lookup are either
    inferred (schema) or unavailable (diagram, lookup, truncate, delete).
    """

    def __init__(self, db_url: str):
        import requests  # lazy: only CouchDB users need this dependency

        parts = urlsplit(db_url)
        scheme = "https" if parts.scheme == "couchdbs" else "http"
        path_segments = [p for p in parts.path.split("/") if p]
        if len(path_segments) != 1:
            raise ValueError(
                f"CouchDB URL must name exactly one database, e.g. "
                f"couchdb://host:5984/mydb (got path {parts.path!r})"
            )
        self.db_name = path_segments[0]
        netloc = parts.hostname or "localhost"
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        self.server_url = urlunsplit((scheme, netloc, "", "", ""))
        self.db_url_base = f"{self.server_url}/{self.db_name}"

        self.session = requests.Session()
        if parts.username:
            self.session.auth = (parts.username, parts.password or "")

        # Fail fast with a clear error if the database/server is unreachable.
        resp = self.session.get(self.db_url_base, timeout=10)
        resp.raise_for_status()

        self.capabilities = Capabilities(
            definition_pane=True,
            create_definition=True,
            diagram=False,
            lookup_plugin=False,
            truncate_column=False,
            whole_row_edit=True,
            delete_item=False,
        )

    # -- helpers -----------------------------------------------------------

    def _get(self, path, **kwargs):
        resp = self.session.get(f"{self.db_url_base}{path}", timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, payload, **kwargs):
        resp = self.session.post(f"{self.db_url_base}{path}", json=payload, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path, payload, **kwargs):
        resp = self.session.put(f"{self.db_url_base}{path}", json=payload, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _get_or_none(self, path):
        resp = self.session.get(f"{self.db_url_base}{path}", timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _split_view_name(self, name: str):
        ddoc, view = name.split("/", 1)
        return ddoc, view

    def _sample_docs(self, limit=SAMPLE_SIZE):
        result = self._post("/_find", {"selector": {}, "limit": limit})
        return result.get("docs", [])

    def _infer_columns(self, docs) -> list[Column]:
        seen_types: dict[str, set] = {}
        order: list[str] = []
        counts: dict[str, int] = {}
        for doc in docs:
            for key, value in doc.items():
                if key not in counts:
                    order.append(key)
                counts[key] = counts.get(key, 0) + 1
                seen_types.setdefault(key, set()).add(_type_name(value))

        def sort_key(k):
            priority = {"_id": -2, "_rev": -1}.get(k, 0)
            return (priority, -counts[k], k)

        ranked = sorted(order, key=sort_key)[:MAX_INFERRED_COLUMNS]
        columns = []
        for key in ranked:
            types = seen_types[key]
            type_name = types.pop() if len(types) == 1 else "mixed"
            columns.append(Column(name=key, type_name=type_name, inferred=True))
        return columns

    # -- Provider interface --------------------------------------------------

    def list_tables(self) -> list[str]:
        return [self.db_name]

    def list_views(self) -> list[str]:
        result = self._get("/_design_docs", params={"include_docs": "true"})
        views = []
        for row in result.get("rows", []):
            doc = row.get("doc") or {}
            ddoc_name = row["id"].split("/", 1)[1]
            for view_name in (doc.get("views") or {}):
                views.append(f"{ddoc_name}/{view_name}")
        return views

    def get_schema(self, name, item_type) -> list[Column]:
        if item_type == "view":
            return [
                Column(name="key", type_name="any", inferred=True),
                Column(name="value", type_name="any", inferred=True),
                Column(name="id", type_name="string", inferred=True),
            ]
        docs = self._sample_docs()
        return self._infer_columns(docs)

    def get_page(self, name, item_type, filters, cursor, page_size) -> RowPage:
        if item_type == "view":
            return self._get_view_page(name, cursor, page_size)
        return self._get_documents_page(filters, cursor, page_size)

    def _mango_selector(self, filters: dict[str, str]) -> dict:
        selector = {}
        for col_name, val in filters.items():
            if val.lower() == "null":
                selector[col_name] = None
            elif val.lower() == "empty":
                selector["$or"] = [{col_name: None}, {col_name: ""}]
            else:
                selector[col_name] = {"$regex": val}
        return selector

    def _get_documents_page(self, filters, cursor, page_size) -> RowPage:
        columns = self._infer_columns(self._sample_docs())
        col_names = [c.name for c in columns]

        payload = {"selector": self._mango_selector(filters)}
        if page_size:
            payload["limit"] = page_size
        if cursor:
            payload["bookmark"] = cursor

        result = self._post("/_find", payload)
        docs = result.get("docs", [])
        bookmark = result.get("bookmark")

        rows = [[doc.get(c) for c in col_names] for doc in docs]
        row_keys = [RowKey({"_id": doc.get("_id"), "_rev": doc.get("_rev")}) for doc in docs]
        # Mango bookmarks are opaque continuation tokens for "resume after
        # exactly this batch" - unlike the view path, we can't over-fetch by
        # one and trim without breaking the bookmark's position. So a full
        # batch is treated as "maybe more"; if the collection ends exactly on
        # a page boundary this costs one extra fetch that returns 0 rows,
        # never a skipped or duplicated row.
        has_more = bool(page_size) and len(docs) == page_size
        next_cursor = bookmark if has_more else None
        return RowPage(columns, rows, row_keys, next_cursor=next_cursor, has_more=has_more, raw_rows=docs)

    def _get_view_page(self, name, cursor, page_size) -> RowPage:
        ddoc, view = self._split_view_name(name)
        columns = self.get_schema(name, "view")

        # startkey/startkey_docid bounds are inclusive, so a cursor page
        # re-returns the last row of the previous page as its first row.
        skip_duplicate = bool(cursor)
        params = {"include_docs": "false"}
        if page_size:
            params["limit"] = page_size + 1 + (1 if skip_duplicate else 0)
        if cursor:
            start = json.loads(cursor)
            params["startkey"] = json.dumps(start["key"])
            params["startkey_docid"] = start["id"]

        result = self._get(f"/_design/{ddoc}/_view/{view}", params=params)
        result_rows = result.get("rows", [])
        if skip_duplicate and result_rows:
            result_rows = result_rows[1:]

        has_more = bool(page_size) and len(result_rows) > page_size
        if has_more:
            result_rows = result_rows[:page_size]

        rows = [[r.get("key"), r.get("value"), r.get("id")] for r in result_rows]
        row_keys = [RowKey(None) for _ in result_rows]
        next_cursor = None
        if has_more:
            last = result_rows[-1]
            next_cursor = json.dumps({"key": last.get("key"), "id": last.get("id")})
        return RowPage(columns, rows, row_keys, next_cursor=next_cursor, has_more=has_more)

    def get_definition(self, name, item_type) -> str:
        if item_type == "view":
            ddoc, view = self._split_view_name(name)
            try:
                doc = self._get(f"/_design/{ddoc}")
            except Exception as e:
                return f"Error fetching view definition: {e}"
            view_def = (doc.get("views") or {}).get(view, {})
            lines = [
                f"DESIGN_DOC: {ddoc}",
                f"VIEW_NAME: {view}",
                "MAP:",
                view_def.get("map", ""),
                "REDUCE:",
                view_def.get("reduce", ""),
            ]
            return "\n".join(lines)
        try:
            info = self._get("")
            doc_count = info.get("doc_count", "?")
            return f"-- CouchDB database '{self.db_name}', {doc_count} documents, no fixed schema"
        except Exception as e:
            return f"Error fetching database info: {e}"

    def definition_language(self, item_type) -> str:
        return "javascript"

    def default_definition_template(self) -> str:
        return VIEW_TEMPLATE

    def get_diagram_model(self) -> DiagramModel:
        return DiagramModel()

    def delete_item(self, name, item_type) -> None:
        raise NotImplementedError("Deleting the whole database is not exposed from dbman")

    def update_cell(self, name, item_type, row_key: RowKey, column, value) -> RowKey:
        doc_id = row_key.value["_id"]
        # Re-fetch immediately before writing so we always PUT with the
        # freshest _rev, rather than trusting a value captured at page-load time.
        doc = self._get(f"/{doc_id}")
        doc[column] = value
        resp = self._put(f"/{doc_id}", doc)
        return RowKey({"_id": doc_id, "_rev": resp["rev"]})

    def update_row_json(self, name, item_type, row_key: RowKey, new_json_text) -> RowKey:
        doc_id = row_key.value["_id"]
        new_doc = json.loads(new_json_text)
        fresh = self._get(f"/{doc_id}")
        new_doc["_id"] = doc_id
        new_doc["_rev"] = fresh["_rev"]
        resp = self._put(f"/{doc_id}", new_doc)
        return RowKey({"_id": doc_id, "_rev": resp["rev"]})

    def _parse_view_definition(self, definition_text):
        ddoc = None
        view = None
        section = None
        map_lines, reduce_lines = [], []
        for line in definition_text.split("\n"):
            if line.startswith("DESIGN_DOC:"):
                ddoc = line.split(":", 1)[1].strip()
                section = None
            elif line.startswith("VIEW_NAME:"):
                view = line.split(":", 1)[1].strip()
                section = None
            elif line.strip() == "MAP:":
                section = "map"
            elif line.strip() == "REDUCE:":
                section = "reduce"
            elif section == "map":
                map_lines.append(line)
            elif section == "reduce":
                reduce_lines.append(line)
        if not ddoc or not view:
            raise ValueError("Definition must include DESIGN_DOC: and VIEW_NAME: lines")
        return ddoc, view, "\n".join(map_lines).strip(), "\n".join(reduce_lines).strip()

    def _put_design_view(self, ddoc, view, map_fn, reduce_fn):
        doc = self._get_or_none(f"/_design/{ddoc}") or {"_id": f"_design/{ddoc}"}
        doc.setdefault("views", {})
        view_def = {"map": map_fn}
        if reduce_fn:
            view_def["reduce"] = reduce_fn
        doc["views"][view] = view_def
        self._put(f"/_design/{ddoc}", doc)

    def create_view(self, definition_text) -> None:
        ddoc, view, map_fn, reduce_fn = self._parse_view_definition(definition_text)
        self._put_design_view(ddoc, view, map_fn, reduce_fn)

    def update_view_definition(self, name, definition_text) -> None:
        ddoc, view, map_fn, reduce_fn = self._parse_view_definition(definition_text)
        self._put_design_view(ddoc, view, map_fn, reduce_fn)
