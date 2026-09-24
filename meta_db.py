"""dbman's own configuration database: `<cwd>/dbman.sqlite`.

One SQLite file holds what used to be split across `dbman.json` (the
connection registry and last session, `workspace.py`) and `.dbman/<name>.json`
(per-item display settings, `view_settings.py`). It is also an ordinary
database - `./dbman dbman.sqlite` browses it like any other - which is why its
tables are *not* `_dbman_`-prefixed: the sidebar filters that prefix out, so
they'd hide themselves. See issue #22.

The stores never cache: every read goes to the file, and every write is one
short transaction. dbman can be browsing this very file through
SqlAlchemyProvider while the stores write to it, and an in-memory copy would
silently overwrite edits made in the grid the next time it saved.

Uses the stdlib `sqlite3` module rather than SQLAlchemy - this is dbman's
bookkeeping, not a provider, and doesn't need reflection or dialects.
"""
import json
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_FILENAME = "dbman.sqlite"
LEGACY_WORKSPACE = "dbman.json"
LEGACY_SETTINGS_DIR = ".dbman"

SCHEMA_VERSION = 1

# Every column in `connections` has a default (or is nullable) so the table
# can take a blank row from 'a' once it's browsable, like any SQLite table
# (see SqlAlchemyProvider.add_row). That's why `name` is a nullable UNIQUE
# rather than the primary key: a blank row has no name yet, and SQLite lets
# any number of rows share a NULL under UNIQUE. Unnamed rows are skipped by
# WorkspaceStore until one is given.
SCHEMA = """
CREATE TABLE connections (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,
    url TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT,
    notes TEXT
);
-- Separate from `connections` so the on-quit session save never rewrites
-- the row a user edits by hand.
CREATE TABLE sessions (
    connection TEXT PRIMARY KEY,
    item_name TEXT,
    item_type TEXT,
    mode TEXT,
    select_mode TEXT,
    cursor_row INTEGER,
    cursor_column INTEGER
);
CREATE TABLE item_settings (
    connection TEXT NOT NULL,
    item TEXT NOT NULL,
    sort_column TEXT,
    sort_direction TEXT,
    PRIMARY KEY (connection, item)
);
-- One row per column: the relational form of ViewSettings' parallel
-- hidden/widths/order/no_color collections. `position` is the column's
-- index in the saved order, NULL when it has no explicit place.
CREATE TABLE column_settings (
    connection TEXT NOT NULL,
    item TEXT NOT NULL,
    column_name TEXT NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    width INTEGER,
    position INTEGER,
    no_color INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (connection, item, column_name)
);
CREATE TABLE workspace (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""
# Settings reference connections by name, deliberately without a foreign
# key: orphaned settings exist in the wild (a `.dbman/<uuid>.json` written
# before --name existed, or a connection since removed), and they should
# import and keep working rather than fail a constraint.


class MetaDb:
    """Opens `<base_dir>/dbman.sqlite` on demand. Reads against a file that
    doesn't exist yet return nothing rather than creating it, so a bare
    `dbman` in an empty directory still leaves no trace; the first write
    creates it."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path.cwd()
        self.path = self.base_dir / DB_FILENAME
        migrate_legacy(self.base_dir)

    @contextmanager
    def read(self):
        """Yields a connection, or None when the file doesn't exist yet."""
        if not self.path.exists():
            yield None
            return
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self):
        """One transaction: committed on a clean exit, rolled back if the
        body raises. Creates the file (and schema) on first use."""
        conn = _open_initialized(self.path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def _open_initialized(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        with conn:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def migrate_legacy(base_dir: Path) -> bool:
    """Import `dbman.json` and `.dbman/*.json` into a new `dbman.sqlite`,
    then rename them with a `.bak` suffix. Runs only when `dbman.sqlite`
    doesn't exist yet and there's something to import; returns whether it
    did.

    Built in a temp file and moved into place with one rename, so a crash
    mid-import leaves no half-built database - the next run starts over. The
    legacy files are renamed only after that, so a crash in between leaves
    them in place but ignored (dbman.sqlite exists), never lost."""
    target = base_dir / DB_FILENAME
    workspace_json = base_dir / LEGACY_WORKSPACE
    settings_dir = base_dir / LEGACY_SETTINGS_DIR
    if target.exists():
        return False
    if not workspace_json.is_file() and not settings_dir.is_dir():
        return False

    tmp = base_dir / (DB_FILENAME + ".migrating")
    tmp.unlink(missing_ok=True)
    conn = _open_initialized(tmp)
    try:
        with conn:
            _import_workspace(conn, _read_json(workspace_json))
            if settings_dir.is_dir():
                for f in sorted(settings_dir.glob("*.json")):
                    _import_settings(conn, f.stem, _read_json(f))
    finally:
        conn.close()
    tmp.replace(target)

    for legacy in (workspace_json, settings_dir):
        if legacy.exists():
            legacy.rename(_free_backup_path(legacy))
    return True


def _read_json(path: Path) -> dict:
    # Same forgiveness as the JSON stores had: a missing or corrupt file
    # reads as empty rather than blocking startup.
    try:
        loaded = json.loads(path.read_text())
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _free_backup_path(path: Path) -> Path:
    """`<path>.bak`, or `<path>.bak.2`, ... if an earlier migration's
    backup is already there - never overwrite a backup."""
    candidate = path.with_name(path.name + ".bak")
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak.{n}")
        n += 1
    return candidate


def _import_workspace(conn: sqlite3.Connection, data: dict) -> None:
    connections = data.get("connections") or {}
    for name, entry in connections.items():
        if not isinstance(entry, dict):
            continue
        conn.execute("INSERT INTO connections (name, url) VALUES (?, ?)", (name, entry.get("url", "")))
        session = entry.get("session")
        if isinstance(session, dict):
            conn.execute(
                "INSERT INTO sessions (connection, item_name, item_type, mode, select_mode,"
                " cursor_row, cursor_column) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, session.get("item_name"), session.get("item_type"), session.get("mode"),
                 session.get("select_mode"), session.get("cursor_row"), session.get("cursor_column")),
            )
    last = data.get("last_connection")
    if last:
        conn.execute("INSERT INTO workspace (key, value) VALUES ('last_connection', ?)", (last,))


def _import_settings(conn: sqlite3.Connection, connection: str, data: dict) -> None:
    # Local import: view_settings imports this module.
    from view_settings import ViewSettings, write_view_settings
    for item, raw in data.items():
        if not isinstance(raw, dict):
            continue
        settings = ViewSettings(
            hidden=list(raw.get("hidden", [])),
            widths=dict(raw.get("widths", {})),
            order=list(raw.get("order", [])),
            no_color=list(raw.get("no_color", [])),
            sort_column=raw.get("sort_column"),
            sort_direction=raw.get("sort_direction"),
        )
        write_view_settings(conn, connection, item, settings)
