"""The provider for dbman's own config database, `dbman.sqlite` (meta_db.py).

Browsing it is ordinary SQLite browsing - paging, filters, sort, search,
cell edits, row add/delete, the diagram, all inherited unchanged. What this
adds is a guard: the grid can't take the config apart. Without it, `d` on the
`connections` table (outside row mode) drops it, and with foreign keys on
that cascades through every child table: all saved config, gone behind one
confirm dialog.

A subclass for now. The intended end state is provider hooks (issue #25),
so each override below is written in the shape of a hook
point rather than as free-form subclass logic, and the class holds no state
of its own - converting it later means lifting these methods into a plugin:

  on_connect         -> _on_connect: bring the schema current, recreate
                        anything missing
  capabilities       -> _capability_overrides: provider-wide switches
  before item delete -> _check_delete_item: veto dropping a config table
  column annotation  -> _read_only_columns: columns dbman maintains
  before cell write  -> update_cell re-checks the same, so a write that
                        slips past the UI still fails readably
"""
from dataclasses import replace

import meta_db

from .base import RowKey
from .sqlalchemy_provider import SqlAlchemyProvider

# Set by dbman, never by hand: `id` is the rowid, and the timestamps record
# what dbman did. Editing them would only make them lie.
_READ_ONLY = {
    "connections": {"id", "created_at", "last_used_at"},
}


class DbmanMetaProvider(SqlAlchemyProvider):

    def __init__(self, db_url: str):
        super().__init__(db_url)
        self._on_connect()
        self.capabilities = replace(self.capabilities, **self._capability_overrides())

    # ---- hook-shaped overrides ----

    def _on_connect(self) -> None:
        # The file is already known to be a config db (see create_provider),
        # so this upgrades or repairs it, never creates one from nothing.
        meta_db.ensure_schema(meta_db.sqlite_url_path(self.db_url))

    @staticmethod
    def _capability_overrides() -> dict:
        # No new tables (the schema is meta_db's to define), and no bulk
        # truncation - cutting every url to N characters is never the goal.
        # Views stay: one over the config is harmless and useful, and its
        # DDL can only add to the file.
        return {"create_table": False, "truncate_column": False}

    @staticmethod
    def _check_delete_item(name: str, item_type: str) -> None:
        if item_type == "table" and name in meta_db.CONFIG_TABLES:
            raise ValueError(
                f"'{name}' is part of dbman's config and can't be dropped - "
                "delete its rows instead (row select mode, then 'd')"
            )

    @staticmethod
    def _read_only_columns(name: str) -> set[str]:
        return _READ_ONLY.get(name, set())

    # ---- Provider methods, routed through the hooks ----

    def delete_item(self, name, item_type) -> None:
        self._check_delete_item(name, item_type)
        super().delete_item(name, item_type)

    def _mark_read_only(self, name, columns):
        read_only = self._read_only_columns(name)
        return [replace(c, read_only=True) if c.name in read_only else c for c in columns]

    def get_schema(self, name, item_type):
        return self._mark_read_only(name, super().get_schema(name, item_type))

    def get_page(self, name, item_type, filters, cursor, page_size, sort=None):
        page = super().get_page(name, item_type, filters, cursor, page_size, sort)
        page.columns = self._mark_read_only(name, page.columns)
        return page

    def update_cell(self, name, item_type, row_key: RowKey, column, value) -> RowKey:
        if column in self._read_only_columns(name):
            raise ValueError(f"'{column}' is maintained by dbman and is read-only")
        return super().update_cell(name, item_type, row_key, column, value)
