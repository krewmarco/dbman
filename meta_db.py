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

SCHEMA_VERSION = 2

# The whole schema, and the one place it lives. Browsing dbman.sqlite shows
# the same DDL in SQL mode ('m'), and these foreign keys are what the
# diagram draws.
#
# Every column in `connections` has a default (or is nullable) so the table
# can take a blank row from 'a' once it's browsable, like any SQLite table
# (see SqlAlchemyProvider.add_row). That's why `name` is a nullable UNIQUE
# rather than the primary key: a blank row has no name yet, and SQLite lets
# any number of rows share a NULL under UNIQUE. Unnamed rows are skipped by
# WorkspaceStore until one is given. A UNIQUE column is a valid foreign-key
# target, so children still reference the *name* - which is what makes a
# rename cascade (ON UPDATE CASCADE) rather than needing to be propagated
# by hand.
CONNECTIONS_DDL = """
CREATE TABLE connections (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,
    url TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT,
    notes TEXT
);
"""
CHILD_DDL = """
-- Separate from `connections` so the on-quit session save never rewrites
-- the row a user edits by hand.
CREATE TABLE sessions (
    connection TEXT PRIMARY KEY
        REFERENCES connections (name) ON DELETE CASCADE ON UPDATE CASCADE,
    item_name TEXT,
    item_type TEXT,
    mode TEXT,
    select_mode TEXT,
    cursor_row INTEGER,
    cursor_column INTEGER
);
-- One row per table/view that has any saved settings: the parent of its
-- column_settings, so deleting it (or its connection) takes those along.
CREATE TABLE item_settings (
    connection TEXT NOT NULL
        REFERENCES connections (name) ON DELETE CASCADE ON UPDATE CASCADE,
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
    PRIMARY KEY (connection, item, column_name),
    FOREIGN KEY (connection, item)
        REFERENCES item_settings (connection, item) ON DELETE CASCADE ON UPDATE CASCADE
);
-- Exactly one row. A typed column rather than a key/value table so the
-- last-used connection is a real relation: a rename follows it, and
-- deleting that connection clears it instead of leaving a dangling name.
CREATE TABLE workspace (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_connection TEXT
        REFERENCES connections (name) ON DELETE SET NULL ON UPDATE CASCADE
);
"""
# Orphaned settings exist in the wild (a `.dbman/<uuid>.json` written before
# --name existed, or a connection since removed). Rather than drop them to
# satisfy the foreign keys, they get a url-less `connections` row: visible
# (and deletable, cascading) when browsing, ignored by the `c` switcher since
# there's nothing to connect to, and adopted if a connection of that name is
# later saved.


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
        """Yields a connection, or None when the file doesn't exist yet. An
        existing file is brought up to the current schema first, so a read
        never sees an older table shape."""
        if not self.path.exists():
            yield None
            return
        conn = _open_initialized(self.path)
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
            with _transaction(conn):
                yield conn
        finally:
            conn.close()


@contextmanager
def _transaction(conn: sqlite3.Connection):
    # Explicit, because connections run in autocommit mode (see
    # _open_initialized) and sqlite3's implicit transactions don't cover DDL.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _execute_ddl(conn: sqlite3.Connection, ddl: str) -> None:
    """Run a multi-statement DDL string inside the caller's transaction.
    Not `executescript`, which commits any open transaction before it runs
    and so would split a schema build or upgrade into separately-committed
    halves."""
    statement = ""
    for line in ddl.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""


def _open_initialized(path: Path) -> sqlite3.Connection:
    """A connection with the schema current and foreign keys enforced.
    SQLite ignores foreign keys unless each connection opts in, and the
    pragma is a no-op inside a transaction - hence autocommit mode and
    setting it only after any schema work."""
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        with _transaction(conn):
            _execute_ddl(conn, CONNECTIONS_DDL + CHILD_DDL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 1:
        _upgrade_v1(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _upgrade_v1(conn: sqlite3.Connection) -> None:
    """v1 had the same tables with no foreign keys, and `workspace` as
    key/value rows. SQLite can't add a foreign key to an existing table, so
    the four child tables are rebuilt: renamed aside, recreated from
    CHILD_DDL, copied back, dropped. `connections` is unchanged.

    Foreign keys stay off for the rebuild (the default for a fresh
    connection) and are checked once at the end, inside the transaction, so
    a violation rolls the whole upgrade back rather than leaving it half
    done."""
    children = ("sessions", "item_settings", "column_settings", "workspace")
    with _transaction(conn):
        for t in children:
            conn.execute(f"ALTER TABLE {t} RENAME TO v1_{t}")
        _execute_ddl(conn, CHILD_DDL)
        # Orphans get a url-less parent row (see the note under CHILD_DDL),
        # and every column_settings group gets its item_settings parent,
        # which v1 only wrote when a sort was set.
        conn.execute(
            "INSERT OR IGNORE INTO connections (name)"
            " SELECT connection FROM v1_sessions UNION SELECT connection FROM v1_item_settings"
            " UNION SELECT connection FROM v1_column_settings"
        )
        conn.execute("INSERT INTO sessions SELECT * FROM v1_sessions")
        conn.execute("INSERT INTO item_settings SELECT * FROM v1_item_settings")
        conn.execute(
            "INSERT OR IGNORE INTO item_settings (connection, item)"
            " SELECT DISTINCT connection, item FROM v1_column_settings"
        )
        conn.execute("INSERT INTO column_settings SELECT * FROM v1_column_settings")
        conn.execute(
            "INSERT INTO workspace (id, last_connection)"
            " SELECT 1, value FROM v1_workspace WHERE key = 'last_connection'"
            " AND value IN (SELECT name FROM connections)"
        )
        for t in children:
            conn.execute(f"DROP TABLE v1_{t}")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"dbman.sqlite upgrade left foreign key violations: {violations}")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


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
        with _transaction(conn):
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
    if last in connections:
        conn.execute("INSERT INTO workspace (id, last_connection) VALUES (1, ?)", (last,))


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
