# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`dbman` is a vim-like terminal UI (TUI) database browser built with [Textual](https://textual.textualize.io/). It connects to SQLite (primary target) and in theory PostgreSQL/MySQL via SQLAlchemy Core, or to a CouchDB database via its HTTP API, letting you browse tables/views (or documents/design-doc views), edit cells, run ad-hoc SQL (or JS map/reduce views), and — for relational backends — view a force-directed ER diagram of foreign-key relationships, all from the terminal.

## Running

```bash
./dbman <path-to-sqlite-file-or-db-url>          # e.g. ./dbman garden.sqlite
./dbman couchdb://user:pass@host:5984/dbname     # CouchDB, one database per invocation
```

`./dbman` is a wrapper script that invokes `venv/bin/python3 dbman.py "$@"`, so it always runs inside the project's own virtualenv regardless of the caller's shell environment. Prefer running through this wrapper rather than `python3 dbman.py` directly, or make sure the venv is activated first.

Connection string dispatch happens in `providers/__init__.py:create_provider()`: a `couchdb://`/`couchdbs://` URL selects `CouchDBProvider`; anything else that doesn't already start with `sqlite://`, `postgresql://`, or `mysql://` is treated as a filesystem path and turned into a `sqlite:///<abspath>` URL, then handed to `SqlAlchemyProvider`.

Dependencies (`requirements.txt`): `textual`, `sqlalchemy`, `requests`. Install into the venv with `venv/bin/pip install -r requirements.txt`. `requests` is only imported lazily inside `providers/couchdb_provider.py` (itself only imported when a `couchdb://` URL is used), so a SQL-only environment never needs it at import time.

## Tests

There is no test runner configured (pytest is not installed in `venv`). `test_layout.py`, `test_lines.py`, and `test_ortho.py` are standalone algorithm prototypes (force-directed graph layout, Bresenham line drawing, orthogonal line routing) with no assertions — they just `print()` output for manual inspection. Run them individually with `venv/bin/python3 test_layout.py` etc. when iterating on diagram-rendering math in isolation before porting the logic into `DiagramView` in `dbman.py`.

There's also no automated coverage of the app itself — verify changes by actually running `./dbman` against `garden.sqlite` (and, for provider-layer changes, against a local CouchDB — see "Testing the CouchDB provider" below).

## Architecture

`dbman.py` is the Textual `App` — UI only (sidebar, modals, keybindings, `DataTable`/diagram rendering). All backend-specific logic (SQLAlchemy Core, CouchDB's HTTP API) lives behind a **provider abstraction** in `providers/`, so the UI never touches SQLAlchemy or `requests` directly. `plugins/lookup.py` is the one plugin. There's no build step.

### Provider abstraction (`providers/`)
- `providers/base.py` defines the `Provider` ABC plus its data shapes: `Column`, `RowKey` (an opaque per-row identity — an int rowid for SQLite, `{"_id", "_rev"}` for CouchDB), `RowPage` (columns + rows + row_keys + pagination cursor/`has_more`, plus optional `raw_rows` for whole-document editing), and `Capabilities` (per-provider feature flags: `diagram`, `lookup_plugin`, `truncate_column`, `whole_row_edit`, `create_definition`, `delete_item`). `DbMan` reads `self.provider.capabilities.*` before wiring up diagram mode, the lookup plugin, truncate, whole-document edit, view create/edit, and delete — this is how the UI degrades gracefully per backend instead of crashing on `NotImplementedError`.
- `providers/__init__.py`'s `create_provider(db_url)` is the only dispatch point. **To add a new provider**: implement `Provider`, set `capabilities` honestly, add one `elif` branch here. There's deliberately no plugin registry/discovery — this one-`elif` extension point is intentional, not a stopgap.
- `providers/sqlalchemy_provider.py` (`SqlAlchemyProvider`) is the relational-DB implementation — see below.
- `providers/couchdb_provider.py` (`CouchDBProvider`) is the CouchDB implementation — see below.

### `SqlAlchemyProvider` (relational: SQLite primarily, Postgres/MySQL best-effort)
- A single SQLAlchemy `engine` is created once and shared for the provider's lifetime. There is no ORM layer — all queries go through SQLAlchemy Core (`MetaData()`/`Table(..., autoload_with=engine)`, `select()`, `update()`, `text()`), and tables/columns are *reflected live* on every read rather than cached, so schema changes made through the app (or externally) show up immediately.
- SQLite-specific behavior is special-cased throughout (`self.engine.dialect.name == "sqlite"`): rowid-based editing (`WHERE rowid = :rid`), `sqlite_master` for raw `CREATE` SQL, `ON CONFLICT DO UPDATE` upserts (in `LookupPlugin`). Postgres/MySQL paths exist but are noticeably thinner (no PK-based UPDATE fallback is implemented yet — see the `# PK logic needed for non-sqlite` comment in `update_cell`) — don't assume feature parity across dialects.
- Paging (`get_page`) is real offset-based paging: it fetches `page_size + 1` rows to detect `has_more` without a separate `COUNT(*)`, and `cursor` is just `str(offset)`.
- dbman stores its own metadata *inside the target database*, in tables prefixed `_dbman_` (`_dbman_layout` for diagram node positions, written directly by `DiagramView` via `provider.sqlalchemy_engine()`; `_dbman_lookup_config` for FK lookup display-column config, owned by `plugins/lookup.py`). These are filtered out of the sidebar/table lists everywhere alongside `sqlite_*` internal tables.
- `capabilities`: everything is `True` (diagram, lookup_plugin, truncate_column, create_definition, delete_item) except `whole_row_edit` (rows are flat/typed, not JSON documents, so there's no "edit whole row as JSON" concept here).

### `CouchDBProvider` (document DB)
- **Concept mapping**: a `couchdb://` URL names exactly *one* CouchDB database (consistent with "one DB per dbman invocation"). That database itself is dbman's single sidebar "table" entry (all documents, browsed via Mango `_find`); each design-doc map/reduce view is a sidebar "view" entry named `"ddoc/view"`.
- **Schema is inferred, not declared**: `get_schema`/`get_page` sample up to 200 documents and infer a flat column set (union of top-level keys, `_id`/`_rev` first, ranked by frequency, capped at 15 columns) with a per-field type guess (`"mixed"` if a field's type varies across sampled docs). Nested (`dict`/`list`) values are stored as their raw Python object in the `DataTable` cell (not stringified) so `action_edit_cell` can detect them.
- **Paging** deliberately avoids `_all_docs?skip=N` (skip-based paging is O(skip) in CouchDB). Documents use Mango `_find`'s `bookmark` cursor; design-doc views use `startkey`/`startkey_docid` — but since those bounds are *inclusive*, a cursored view page re-fetches and drops one duplicate leading row (see the `skip_duplicate` handling in `_get_view_page`).
- **Editing**: `update_cell`/`update_row_json` always re-`GET` the document immediately before `PUT` to get the freshest `_rev`, rather than trusting a value captured at page-load time — this isn't full optimistic-concurrency conflict handling (a genuine race can still 409), but it's a deliberate, low-effort match to the SQL path's equally non-transactional update-by-rowid risk posture. `E` (`action_edit_document` in `dbman.py`) opens the *whole* document as JSON (seeded from `RowPage.raw_rows`, so fields outside the inferred column set are editable/addable); plain `e` on a `dict`/`list`-valued cell auto-routes to the same whole-document editor instead of the single-line `EditCellScreen`.
- **View create/edit** reuses the same `EditTextScreen` modal as SQL's "Create View"/"Edit SQL", but with a plain-text template format (`DESIGN_DOC:` / `VIEW_NAME:` / `MAP:` / `REDUCE:`) parsed by `_parse_view_definition` and written as a design-doc via `_put_design_view` — there's no real "SQL" here, just a text format chosen to fit the existing modal.
- **Capabilities that are permanently off** (not "not implemented yet", but conceptually N/A and gated in `dbman.py`'s action methods): `diagram` (no FK-equivalent — `DiagramView` shows "Diagram not available for this provider" and `m` mode-cycling skips diagram entirely), `lookup_plugin` (FK-lookup is inherently relational), `truncate_column` (no sane "bulk-mutate one field across heterogeneous docs" semantics), `delete_item` (the one "table" entry *is* the whole database — deleting it is out of scope for a cell-level browser).
- **Future CouchDB-specific persisted state** (if a diagram-equivalent or similar feature is ever added for CouchDB) should follow the same pattern as `_dbman_layout`/`_dbman_lookup_config`: a sibling `_dbman_meta` CouchDB database, with docids scoped `"<source-db>:<namespace>"` to avoid collisions across multiple dbman-browsed databases on the same server. This isn't implemented — it's the documented convention to follow when it's needed.

### UI structure (`DbMan(App)`)
- Layout: a sidebar `ListView` (Views / Tables / Plugins sections) + a `ContentSwitcher` that swaps between three widgets: `#data-table` (a `DataTable`), `#sql-view` (a `Static` showing the item's "definition" — raw `CREATE` SQL, or a CouchDB view's map/reduce source), and `#diagram-view` (`DiagramView`).
- `self.mode` is one of `"view" | "schema" | "sql" | "diagram"`, cycled with `m` (skipping `"diagram"` if `provider.capabilities.diagram` is `False`); `d`/`ctrl+d` jumps straight to diagram mode (or notifies it's unavailable). `load_item()` is the central dispatcher that reads `self.mode` + the selected sidebar item's type (`"table" | "view" | "plugin"`) and populates the right widget via `self.provider.*` calls.
- Row identity for editing flows through `self.row_keys: dict[str, RowKey]` (DataTable string row-key → the provider's `RowKey`, rebuilt on every `load_item`) and `self.raw_docs: dict[str, Any]` (same keying, populated only when `RowPage.raw_rows` is present — i.e. only for CouchDB's document table, feeding whole-document edits).
- View-mode paging state (`self.page_cursor`, `self.page_history` as a back-stack, `self.page_size`, `self.page_has_more`) is reset whenever the selected sidebar item or `self.filters` changes (see `reset_paging()`); `]`/`[` step forward/back and the title bar shows a `[page N+]` indicator when more rows exist.
- `self.select_mode` is one of `"field" | "row" | "column"` (View mode only), rotated with `s` (`action_rotate_select_mode`) and mapped 1:1 onto Textual `DataTable`'s native `cursor_type` reactive (`"cell" | "row" | "column"`) via `DbMan.CURSOR_TYPE_BY_SELECT_MODE` — row/column highlighting and cursor movement semantics come from Textual for free. `load_item()` re-applies `cursor_type` after every rebuild (resetting to `"cell"` outside View mode); the title bar shows a `[ROW]`/`[COLUMN]` suffix via `update_title()`. It determines what `e` (`action_edit_cell`) operates on: field mode edits the focused cell (unchanged); row mode routes to `action_edit_document` if `provider.capabilities.whole_row_edit` (else notifies it's unsupported — switch to field mode); column mode currently has no `e` action (`t`/truncate and `option+left`/`option+right` already act at column granularity). In column mode, `option+left`/`option+right` (`alt+left`/`alt+right` — `action_reorder_column`) swap the selected column with its neighbor and persist the new order to `ViewSettingsStore` (`settings.order`), superseding the earlier grab/drop design (issue #4); `ctrl+left`/`ctrl+right` was deliberately avoided since macOS reserves those for Mission Control space-switching. See issue #7.
- All interactive dialogs (edit cell, edit whole document, filter, confirm delete, truncate column, edit text [SQL or CouchDB view JS/JSON], export CSV, lookup select) are `ModalScreen` subclasses pushed with `self.push_screen(Screen(...), callback)` — the callback receives the dismissed value. Follow this pattern for new modal interactions rather than inlining prompts.
- Keybindings are declared as class-level `BINDINGS` lists (see `DbMan.BINDINGS` and the in-app `?`/`ctrl+p` shortcuts panel in `ShortcutsScreen` — **keep these two in sync** when adding/changing a binding).

### Diagram mode (`DiagramView`)
- Only meaningful for providers with `capabilities.diagram = True` (currently just `SqlAlchemyProvider`); `DiagramView` is constructed with the `provider`, not a raw engine, and calls `provider.get_diagram_model()` to get nodes/edges.
- Renders tables as absolutely-positioned `TableDiagram` widgets (one `Panel` each) over a background `Static` containing hand-drawn orthogonal connector lines (`draw_lines`, using box-drawing characters directly on a character grid — not a Textual layout).
- Initial layout for never-before-positioned tables uses `layout_graph`: a simple radial placement (not true force-directed simulation — that experiment lives only in `test_layout.py` and was not ported in). Positions are persisted per-table to `_dbman_layout`, reached via `provider.sqlalchemy_engine()` — this SQL-specific detail lives in `DiagramView` rather than the `Provider` ABC since no other provider currently supports diagram mode; revisit if that changes.
- Diagram mode hides the sidebar entirely (`sidebar.display = False`) since it needs the full screen.

### Plugins (`plugins/lookup.py`)
- `LookupPlugin` is the only plugin; it appears as a synthetic sidebar entry (item_type `"plugin"`) rather than a real table/view. It auto-detects FK relationships via `inspector.get_foreign_keys()` and lets the user configure a "display column" from the related table (stored in `_dbman_lookup_config`), which then renders as a `Select` dropdown instead of a free-text `Input` when editing a matching cell in `action_edit_cell`.
- It's inherently relational, so it's only ever instantiated when `provider.capabilities.lookup_plugin` is `True`: `DbMan.__init__` sets `self.lookup_plugin = LookupPlugin(provider.sqlalchemy_engine()) if provider.capabilities.lookup_plugin else None`, and every call site (`refresh_sidebar`'s plugin list, `action_edit_cell`'s plugin branch, `action_toggle_mode`) guards on `self.lookup_plugin is not None` first.
- There's no formal plugin interface/registry — adding a new plugin currently means importing it directly in `dbman.py` and hardcoding it into `refresh_sidebar()`'s `plugins = [...]` list and the `item_type == "plugin"` branches in `load_item()` / `action_edit_cell()`.

### View settings (`view_settings.py`)
- Per-table/per-view display preferences (hidden columns, explicit widths, column order) persist to a local, provider-agnostic file: `<cwd>/.dbman/<db-name>.json`, gitignored since it's local machine state rather than shared config. `<db-name>` comes from `derive_db_name()` (last URL path segment, extension stripped) — works uniformly across bare sqlite paths, `sqlite://`/`postgresql://`/`mysql://` URLs, and `couchdb://` URLs, so it lives outside `providers/` rather than inside any one provider.
- `DbMan.__init__` builds one `ViewSettingsStore` per connection (`self.view_settings`). `load_item`'s `"view"` mode branch calls `apply_view_settings(page.columns, page.rows, view_settings)` to project the fetched `RowPage` through hidden/order before rendering, and passes `view_settings.widths.get(col.name)` to `DataTable.add_column` — this applies uniformly across providers since it operates on the already-fetched column/row list, not provider internals.
- There's no in-app UI to *set* these yet — `ViewSettingsStore.save()` exists and is exercised by tests but has no caller in `dbman.py`. It's the landing point for hide/unhide, column-width, and column-reorder UI (tracked separately; see the project's GitHub issues).

## Testing the CouchDB provider

There's no fixture/CI setup for this — spin up a throwaway local CouchDB in Docker, on a port that won't collide with any other CouchDB container you might already have running:

```bash
docker run -d --name dbman-couch -p 5985:5984 \
  -e COUCHDB_USER=admin -e COUCHDB_PASSWORD=admin couchdb:latest
curl -u admin:admin -X PUT http://localhost:5985/plants
curl -u admin:admin -X POST http://localhost:5985/plants -H "Content-Type: application/json" \
  -d '{"name":"Tomato","color":"red"}'
curl -u admin:admin -X PUT http://localhost:5985/plants/_design/by_name \
  -d '{"views":{"all":{"map":"function(doc){ emit(doc.name, doc.color); }"}}}'
./dbman couchdb://admin:admin@localhost:5985/plants
```

Seed a mix of flat and nested/heterogeneous documents to exercise column inference and the whole-document JSON editor (`E`, or plain `e` on a `dict`/`list` cell).

## Notes
- Uncaught exceptions are logged to `dbman_crash.log` in the cwd via a global `sys.excepthook` (`dbman.py:94`) — check this file first when the TUI exits unexpectedly.
- `garden.sqlite` in the repo root is sample/dev data used for manual testing of the app, not fixture data for an automated suite.
