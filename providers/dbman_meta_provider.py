"""The provider for dbman's own config database, `dbman.sqlite` (meta_db.py).

Browsing it is ordinary SQLite browsing - paging, filters, sort, search,
cell edits, row add/delete, the diagram, all inherited unchanged. What this
adds:

- a guard, so the grid can't take the config apart. Without it, `d` on the
  `connections` table (outside row mode) drops it, and with foreign keys on
  that cascades through every child table: all saved config, gone behind
  one confirm dialog.
- secrets in connection urls masked wherever the grid shows them.
- opening a `connections` row (space, row select mode) connects to it.

A subclass for now. The intended end state is provider hooks (issue #25),
so each override below is written in the shape of a hook point rather than
as free-form subclass logic, and the class holds no state of its own -
converting it later means lifting these methods into a plugin:

  on_connect         -> _on_connect: bring the schema current, recreate
                        anything missing
  capabilities       -> _capability_overrides: provider-wide switches
  before item delete -> _check_delete_item: veto dropping a config table
  column annotation  -> _read_only_columns: columns dbman maintains
  display mask       -> _masked_columns + mask_url: hide url secrets
  before cell write  -> update_cell: re-checks read-only, and restores a
                        masked secret the edit left in place
  open row           -> can_open_row / open_row: row -> connection
"""
from dataclasses import replace
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import text

import meta_db

from .base import RowKey, RowTarget
from .sqlalchemy_provider import SqlAlchemyProvider

# Set by dbman, never by hand: `id` is the rowid, and the timestamps record
# what dbman did. Editing them would only make them lie.
_READ_ONLY = {
    "connections": {"id", "created_at", "last_used_at"},
}

# Columns whose values carry credentials.
_MASKED = {
    "connections": {"url"},
}

MASK = "••••"

# Schemes whose URL carries a bare token in the *username* slot
# (`notion://<token>@notion.so/...`, `github://<token>@github.com/...`).
# Everywhere else a lone username is just a name (`postgresql://marco@host`)
# and only a password is secret.
_TOKEN_AS_USERNAME = {"notion", "github"}


def split_url_secret(url: str) -> tuple[str, Optional[str]]:
    """(url with its secret replaced by MASK, the secret) - or (url, None)
    when there's nothing to hide. Only the userinfo is touched, and the
    rest of the string is kept byte-for-byte rather than re-assembled, so
    masking never normalizes a url the user typed. A plain file path has no
    `scheme://` and passes through untouched."""
    parts = urlsplit(url)
    prefix = f"{parts.scheme}://"
    if not parts.scheme or "@" not in parts.netloc or not url.startswith(prefix + parts.netloc):
        return url, None
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user, has_password, password = userinfo.partition(":")
    if has_password and password:
        secret, masked_userinfo = password, f"{user}:{MASK}"
    elif not has_password and user and parts.scheme in _TOKEN_AS_USERNAME:
        secret, masked_userinfo = user, MASK
    else:
        return url, None
    rest = url[len(prefix + parts.netloc):]
    return f"{prefix}{masked_userinfo}@{hostport}{rest}", secret


def mask_url(url):
    if not isinstance(url, str):
        return url
    return split_url_secret(url)[0]


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

    @staticmethod
    def _masked_columns(name: str) -> set[str]:
        """Masked in everything get_page returns, which is everything the
        grid shows: cells, `/` search (it runs over the loaded page), CSV
        export, and the edit dialog's starting value. This guards against a
        screen share or an exported file, not against the user - `f` on the
        column still matches the real value in SQL, and dbman.sqlite itself
        is plain text on disk (gitignored for that reason)."""
        return _MASKED.get(name, set())

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
        masked = self._masked_columns(name)
        positions = [i for i, c in enumerate(page.columns) if c.name in masked]
        if positions:
            for row in page.rows:
                for i in positions:
                    row[i] = mask_url(row[i])
        return page

    def update_cell(self, name, item_type, row_key: RowKey, column, value) -> RowKey:
        if column in self._read_only_columns(name):
            raise ValueError(f"'{column}' is maintained by dbman and is read-only")
        if column in self._masked_columns(name) and isinstance(value, str) and MASK in value:
            value = self._restore_secret(name, row_key, column, value)
        return super().update_cell(name, item_type, row_key, column, value)

    def _restore_secret(self, name, row_key: RowKey, column, value: str) -> str:
        """The edit dialog is seeded with the masked url, so an edit that
        leaves the mask in place (changing the host or page id, say) must
        put the real secret back rather than save the bullets over it. To
        replace the secret, type a whole new url."""
        with self.engine.connect() as conn:
            current = conn.execute(
                text(f'SELECT "{column}" FROM "{name}" WHERE rowid = :rid'), {"rid": row_key.value}
            ).scalar()
        _, secret = split_url_secret(current or "")
        if secret is None:
            raise ValueError(f"no stored secret to restore for '{MASK}' - type the full url")
        return value.replace(MASK, secret, 1)

    def can_open_row(self, name, item_type) -> bool:
        return item_type == "table" and name == "connections"

    def open_row(self, name, item_type, row_key: RowKey) -> RowTarget:
        if not self.can_open_row(name, item_type):
            raise ValueError(f"rows of '{name}' don't open to anything")
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT name, url FROM connections WHERE rowid = :rid"), {"rid": row_key.value}
            ).fetchone()
        if row is None:
            raise ValueError("this connection no longer exists - reload")
        conn_name, url = row
        if not conn_name or not url:
            raise ValueError("give this connection a name and a url first")
        return RowTarget("connection", conn_name)
