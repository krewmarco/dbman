"""Workspace-level connection registry and last-session state, persisted to
a flat `dbman.json` file in the directory dbman was invoked from. Distinct
from `view_settings.py`'s per-connection `.dbman/<db-name>.json` (display
settings) -- this file is the project-level "what connections does this
directory have, and where was I" record, keyed by a short connection name
so a workspace can eventually hold more than one connection. See CLAUDE.md
and github.com/krewmarco/dbman.
"""
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

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
    """Loads/saves `<base_dir>/dbman.json`: named connections plus each
    one's last-viewed item/cursor. `resolve()` turns a CLI argument (or
    none, for a bare `dbman` invocation) into a `(url, name)` pair without
    touching disk; `upsert_connection` and `save_session` are the two write
    paths, called after a successful connect and on clean quit respectively.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self.path = (base_dir or Path.cwd()) / "dbman.json"
        self._data: dict = {"version": 1, "last_connection": None, "connections": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    self._data.update(loaded)
            except (json.JSONDecodeError, OSError):
                pass
        self._data.setdefault("connections", {})

    def resolve(self, arg: Optional[str], name_override: Optional[str] = None) -> Optional[tuple[str, str]]:
        """Returns (url, connection_name), or None if `arg` is None and
        there's nothing saved yet to fall back to.

        `name_override` (dbman's `--name`/`-n`) only takes effect when `arg`
        is a fresh url/path being saved for the first time -- it's a
        friendly alternative to the auto-derived name (`derive_db_name`),
        which for e.g. a `notion://` url is an unfriendly page-id UUID. It's
        silently ignored when `arg` already names a saved connection, since
        renaming an existing entry is a different operation than this."""
        connections = self._data["connections"]

        if arg is None:
            if not connections:
                return None
            name = self._data.get("last_connection")
            if name not in connections:
                name = next(iter(connections))
            return connections[name]["url"], name

        if arg in connections:
            return connections[arg]["url"], arg

        # Not a saved connection name -- treat as a url/path exactly like
        # today, deriving a name to (maybe newly) save it under.
        base_name = name_override or derive_db_name(arg)
        name = base_name
        suffix = 2
        while name in connections and connections[name]["url"] != arg:
            name = f"{base_name}-{suffix}"
            suffix += 1
        return arg, name

    def upsert_connection(self, name: str, url: str) -> None:
        entry = self._data["connections"].setdefault(name, {})
        entry["url"] = url
        self._data["last_connection"] = name
        self._save()

    def save_session(self, name: str, session: ConnectionSession) -> None:
        entry = self._data["connections"].setdefault(name, {"url": ""})
        entry["session"] = asdict(session)
        self._save()

    def get_session(self, name: str) -> Optional[ConnectionSession]:
        entry = self._data["connections"].get(name)
        raw = entry.get("session") if entry else None
        if raw is None:
            return None
        return ConnectionSession(
            item_name=raw.get("item_name"),
            item_type=raw.get("item_type"),
            mode=raw.get("mode", "view"),
            select_mode=raw.get("select_mode", "field"),
            cursor_row=raw.get("cursor_row"),
            cursor_column=raw.get("cursor_column"),
        )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
