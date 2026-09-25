"""Workspace-level connection registry and last-session state, persisted in
`dbman.sqlite` (meta_db.py) in the directory dbman was invoked from: the
`connections`, `sessions` and `workspace` tables. Distinct from
`view_settings.py`'s per-item display settings in the same file -- this is
the project-level "what connections does this directory have, and where was
I" record, keyed by a short connection name. See CLAUDE.md and
github.com/krewmarco/dbman issue #22.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from meta_db import MetaDb
from view_settings import derive_db_name


@dataclass
class ConnectionSession:
    item_name: Optional[str] = None
    item_type: Optional[str] = None
    mode: str = "view"
    select_mode: str = "field"
    cursor_row: Optional[int] = None
    cursor_column: Optional[int] = None


class WorkspaceStore:
    """Named connections plus each one's last-viewed item/cursor, in
    `<base_dir>/dbman.sqlite`. `resolve()` turns a CLI argument (or none,
    for a bare `dbman` invocation) into a `(url, name)` pair without
    writing; `upsert_connection` and `save_session` are the two write
    paths, called after a successful connect and on clean quit respectively.

    Rows with no name or no url are skipped everywhere: a blank row from 'a'
    when the connections table is browsed directly, or an orphan's parent
    row (see meta_db), neither of which can be connected to yet.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self.db = MetaDb(base_dir)
        self.path = self.db.path

    def _connections(self) -> dict[str, str]:
        """name -> url, in the order they were first saved."""
        with self.db.read() as conn:
            if conn is None:
                return {}
            rows = conn.execute(
                "SELECT name, url FROM connections"
                " WHERE name IS NOT NULL AND name != '' AND url IS NOT NULL AND url != '' ORDER BY id"
            ).fetchall()
        return dict(rows)

    def _last_connection(self) -> Optional[str]:
        with self.db.read() as conn:
            if conn is None:
                return None
            row = conn.execute("SELECT last_connection FROM workspace WHERE id = 1").fetchone()
        return row[0] if row else None

    def resolve(self, arg: Optional[str], name_override: Optional[str] = None) -> Optional[tuple[str, str]]:
        """Returns (url, connection_name), or None if `arg` is None and
        there's nothing saved yet to fall back to.

        `name_override` (dbman's `--name`/`-n`) only takes effect when `arg`
        is a fresh url/path being saved for the first time -- it's a
        friendly alternative to the auto-derived name (`derive_db_name`),
        which for e.g. a `notion://` url is an unfriendly page-id UUID. It's
        silently ignored when `arg` already names a saved connection, since
        renaming an existing entry is a different operation than this."""
        connections = self._connections()

        if arg is None:
            if not connections:
                return None
            name = self._last_connection()
            if name not in connections:
                name = next(iter(connections))
            return connections[name], name

        if arg in connections:
            return connections[arg], arg

        # Not a saved connection name -- treat as a url/path exactly like
        # today, deriving a name to (maybe newly) save it under.
        base_name = name_override or derive_db_name(arg)
        name = base_name
        suffix = 2
        while name in connections and connections[name] != arg:
            name = f"{base_name}-{suffix}"
            suffix += 1
        return arg, name

    def list_connections(self) -> list[str]:
        return list(self._connections())

    def upsert_connection(self, name: str, url: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO connections (name, url, last_used_at) VALUES (?, ?, CURRENT_TIMESTAMP)"
                " ON CONFLICT(name) DO UPDATE SET url = excluded.url, last_used_at = excluded.last_used_at",
                (name, url),
            )
            conn.execute(
                "INSERT INTO workspace (id, last_connection) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_connection = excluded.last_connection",
                (name,),
            )

    def save_session(self, name: str, session: ConnectionSession) -> None:
        # Only while the connection still exists: browsing dbman.sqlite, the
        # user can delete (cascading) the very connection they're on, and
        # the quit-time save shouldn't then fail the foreign key - or
        # resurrect the row they just removed.
        with self.db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (connection, item_name, item_type, mode, select_mode,"
                " cursor_row, cursor_column)"
                " SELECT ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM connections WHERE name = ?)",
                (name, session.item_name, session.item_type, session.mode, session.select_mode,
                 session.cursor_row, session.cursor_column, name),
            )

    def get_session(self, name: str) -> Optional[ConnectionSession]:
        with self.db.read() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT item_name, item_type, mode, select_mode, cursor_row, cursor_column"
                " FROM sessions WHERE connection = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        item_name, item_type, mode, select_mode, cursor_row, cursor_column = row
        return ConnectionSession(
            item_name=item_name,
            item_type=item_type,
            mode=mode or "view",
            select_mode=select_mode or "field",
            cursor_row=cursor_row,
            cursor_column=cursor_column,
        )
