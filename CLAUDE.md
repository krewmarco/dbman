# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`dbman` is a vim-like terminal UI (TUI) database browser built with [Textual](https://textual.textualize.io/) and SQLAlchemy Core. It connects to SQLite (primary target), and in theory PostgreSQL/MySQL, letting you browse tables/views, edit cells, run ad-hoc SQL, and view a force-directed ER diagram of foreign-key relationships — all from the terminal.

## Running

```bash
./dbman <path-to-sqlite-file-or-db-url>   # e.g. ./dbman garden.sqlite
```

`./dbman` is a wrapper script that invokes `venv/bin/python3 dbman.py "$@"`, so it always runs inside the project's own virtualenv regardless of the caller's shell environment. Prefer running through this wrapper rather than `python3 dbman.py` directly, or make sure the venv is activated first.

If the argument doesn't start with `sqlite://`, `postgresql://`, or `mysql://`, it's treated as a filesystem path and turned into a `sqlite:///<abspath>` URL (`dbman.py:816`).

Dependencies (`requirements.txt`): `textual`, `sqlalchemy`. Install into the venv with `venv/bin/pip install -r requirements.txt`.

## Tests

There is no test runner configured (pytest is not installed in `venv`). `test_layout.py`, `test_lines.py`, and `test_ortho.py` are standalone algorithm prototypes (force-directed graph layout, Bresenham line drawing, orthogonal line routing) with no assertions — they just `print()` output for manual inspection. Run them individually with `venv/bin/python3 test_layout.py` etc. when iterating on diagram-rendering math in isolation before porting the logic into `DiagramView` in `dbman.py`.

## Architecture

Everything lives in two files: `dbman.py` (the app) and `plugins/lookup.py` (the one plugin). There's no build step — it's a single Textual `App` subclass.

### Data layer
- A single SQLAlchemy `engine` is created once in `DbMan.__init__` and shared everywhere. There is no ORM layer — all queries go through SQLAlchemy Core (`MetaData().reflect` / `Table(..., autoload_with=engine)`, `select()`, `update()`, `text()`).
- Tables/columns are *reflected live* on every read rather than cached, so schema changes made through the app (or externally) show up immediately.
- SQLite-specific behavior is special-cased throughout (`self.engine.dialect.name == "sqlite"`): rowid-based editing (`WHERE rowid = :rid`), `sqlite_master` for raw `CREATE` SQL, `ON CONFLICT DO UPDATE` upserts. Postgres/MySQL paths exist but are noticeably thinner (no PK-based UPDATE fallback is implemented yet — see the `# PK logic needed for non-sqlite` comments) — don't assume feature parity across dialects.
- dbman stores its own metadata in the target database itself, in tables prefixed `_dbman_` (`_dbman_layout` for diagram node positions, `_dbman_lookup_config` for FK lookup display-column config). These are filtered out of the sidebar/table lists everywhere alongside `sqlite_*` internal tables.

### UI structure (`DbMan(App)`)
- Layout: a sidebar `ListView` (Views / Tables / Plugins sections) + a `ContentSwitcher` that swaps between three widgets: `#data-table` (a `DataTable`), `#sql-view` (a `Static` showing raw SQL), and `#diagram-view` (`DiagramView`).
- `self.mode` is one of `"view" | "schema" | "sql" | "diagram"`, cycled with `m`; `d`/`ctrl+d` jumps straight to diagram mode. `load_item()` is the central dispatcher that reads `self.mode` + the selected sidebar item's type (`"table" | "view" | "plugin"`) and populates the right widget.
- All interactive dialogs (edit cell, filter, confirm delete, truncate column, edit SQL, export CSV, lookup select) are `ModalScreen` subclasses pushed with `self.push_screen(Screen(...), callback)` — the callback receives the dismissed value. Follow this pattern for new modal interactions rather than inlining prompts.
- Keybindings are declared as class-level `BINDINGS` lists (see `DbMan.BINDINGS` and the in-app `?`/`ctrl+p` shortcuts panel in `ShortcutsScreen` — **keep these two in sync** when adding/changing a binding).

### Diagram mode (`DiagramView`)
- Renders tables as absolutely-positioned `TableDiagram` widgets (one `Panel` each) over a background `Static` containing hand-drawn orthogonal connector lines (`draw_lines`, using box-drawing characters directly on a character grid — not a Textual layout).
- Initial layout for never-before-positioned tables uses `layout_graph`: a simple radial placement (not true force-directed simulation — that experiment lives only in `test_layout.py` and was not ported in). Positions are persisted per-table to `_dbman_layout` in the target DB the first time a table is dragged (arrow keys while a `TableDiagram` has focus emit a `TableDiagram.Moved` message handled by `DiagramView.on_table_diagram_moved`).
- Diagram mode hides the sidebar entirely (`sidebar.display = False`) since it needs the full screen.

### Plugins (`plugins/lookup.py`)
- `LookupPlugin` is the only plugin; it appears as a synthetic sidebar entry (item_type `"plugin"`) rather than a real table/view. It auto-detects FK relationships via `inspector.get_foreign_keys()` and lets the user configure a "display column" from the related table (stored in `_dbman_lookup_config`), which then renders as a `Select` dropdown instead of a free-text `Input` when editing a matching cell in `action_edit_cell`.
- There's no formal plugin interface/registry — adding a new plugin currently means importing it directly in `dbman.py` and hardcoding it into `refresh_sidebar()`'s `plugins = [...]` list and the `item_type == "plugin"` branches in `get_item_data()` / `action_edit_cell()`.

## Notes
- Uncaught exceptions are logged to `dbman_crash.log` in the cwd via a global `sys.excepthook` (`dbman.py:94`) — check this file first when the TUI exits unexpectedly.
- `garden.sqlite` in the repo root is sample/dev data used for manual testing of the app, not fixture data for an automated suite.
