"""Per-view display settings (hidden columns, widths, column order, color,
sort), persisted in `dbman.sqlite`'s `item_settings` and `column_settings`
tables (meta_db.py), keyed by connection name then table/view name.
Provider-agnostic: works the same for SqlAlchemyProvider tables and
CouchDBProvider's inferred columns. See CLAUDE.md and github.com/krewmarco/dbman
issues #1 and #22."""
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from meta_db import MetaDb


def derive_db_name(db_url: str) -> str:
    """A stable, short name for a connection string, used as its default
    connection name. Bare sqlite paths, sqlite:// URLs, and couchdb://
    URLs all carry their meaningful identifier in the last path segment."""
    path = urlsplit(db_url).path or db_url
    return Path(path).stem or "db"


@dataclass
class ViewSettings:
    hidden: list[str] = field(default_factory=list)
    widths: dict[str, int] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    # Columns whose enum values should render *without* their option colors.
    # Per-column rather than a global switch because the answer differs by
    # column: a Status worth spotting at a glance vs. a Section whose every
    # row is colored, where the color is repetition rather than signal.
    no_color: list[str] = field(default_factory=list)
    sort_column: Optional[str] = None
    sort_direction: Optional[str] = None  # "asc" | "desc", meaningless if sort_column is None


class ViewSettingsStore:
    """Loads/saves ViewSettings for every table/view of one connection, in
    `<base_dir>/dbman.sqlite`. Reads the file on every get() rather than
    caching - see meta_db.py for why."""

    def __init__(self, db_name: str, base_dir: Optional[Path] = None):
        self.connection = db_name
        self.db = MetaDb(base_dir)

    def get(self, item_name: str) -> ViewSettings:
        settings = ViewSettings()
        with self.db.read() as conn:
            if conn is None:
                return settings
            sort = conn.execute(
                "SELECT sort_column, sort_direction FROM item_settings"
                " WHERE connection = ? AND item = ?",
                (self.connection, item_name),
            ).fetchone()
            if sort:
                settings.sort_column, settings.sort_direction = sort
            # rowid order is write order: saved column order first (see
            # write_view_settings). `hidden`/`no_color` are only ever used
            # as sets, so they come back in that order rather than the
            # order columns were hidden in - the unhide picker lists them
            # in column order, which reads better anyway.
            rows = conn.execute(
                "SELECT column_name, hidden, width, position, no_color FROM column_settings"
                " WHERE connection = ? AND item = ? ORDER BY rowid",
                (self.connection, item_name),
            ).fetchall()
        positioned = []
        for column, hidden, width, position, no_color in rows:
            if hidden:
                settings.hidden.append(column)
            if width is not None:
                settings.widths[column] = width
            if position is not None:
                positioned.append((position, column))
            if no_color:
                settings.no_color.append(column)
        settings.order = [column for _, column in sorted(positioned)]
        return settings

    def save(self, item_name: str, settings: ViewSettings) -> None:
        with self.db.write() as conn:
            write_view_settings(conn, self.connection, item_name, settings)


def write_view_settings(conn: sqlite3.Connection, connection: str, item: str, settings: ViewSettings) -> None:
    """Replace one item's rows wholesale. Shared with meta_db's JSON import
    so both write the same shape. The caller owns the transaction."""
    conn.execute("DELETE FROM item_settings WHERE connection = ? AND item = ?", (connection, item))
    conn.execute("DELETE FROM column_settings WHERE connection = ? AND item = ?", (connection, item))
    if settings.sort_column is not None:
        conn.execute(
            "INSERT INTO item_settings (connection, item, sort_column, sort_direction) VALUES (?, ?, ?, ?)",
            (connection, item, settings.sort_column, settings.sort_direction),
        )
    hidden, no_color = set(settings.hidden), set(settings.no_color)
    position = {column: i for i, column in enumerate(settings.order)}
    # Every column any setting mentions, once, in first-mention order.
    columns = dict.fromkeys([*settings.order, *settings.hidden, *settings.widths, *settings.no_color])
    conn.executemany(
        "INSERT INTO column_settings (connection, item, column_name, hidden, width, position, no_color)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (connection, item, column, int(column in hidden), settings.widths.get(column),
             position.get(column), int(column in no_color))
            for column in columns
        ],
    )


def apply_view_settings(columns: list, rows: list[list], settings: ViewSettings):
    """Project a RowPage's columns/rows through hidden/order settings.
    `rows` must be positionally aligned with `columns`. Returns
    (display_columns, display_rows)."""
    display_columns = [c for c in columns if c.name not in settings.hidden]
    if settings.order:
        order_index = {name: i for i, name in enumerate(settings.order)}
        display_columns.sort(key=lambda c: order_index.get(c.name, len(settings.order)))
    indices = [columns.index(c) for c in display_columns]
    display_rows = [[row[i] for i in indices] for row in rows]
    return display_columns, display_rows


def compute_column_widths(
    display_columns: list, display_rows: list[list], settings: ViewSettings,
    padding: int = 2, min_width: int = 6, max_width: int = 40,
) -> dict[str, int]:
    """Width per column: an explicit override from `settings.widths` if set,
    otherwise the average string length of the currently-loaded page's
    values for that column (+ padding), clamped to [min_width, max_width]
    and never narrower than the header label. Using the average rather than
    the longest value is the point: one huge outlier value shouldn't blow
    out the whole column."""
    widths = {}
    for i, col in enumerate(display_columns):
        if col.name in settings.widths:
            widths[col.name] = settings.widths[col.name]
            continue
        lengths = [len(str(row[i])) for row in display_rows if row[i] is not None]
        avg = (sum(lengths) / len(lengths)) if lengths else 0
        widths[col.name] = max(min_width, len(col.name), min(max_width, round(avg) + padding))
    return widths


def truncate_display_value(value, width: Optional[int]):
    """Shorten an overlong string value to fit `width`, marked with a
    trailing '..'. Only `str` values are touched — dict/list cells (e.g.
    CouchDB nested fields) must stay as raw Python objects so action_edit_cell
    can keep detecting them; other scalar types are left alone too."""
    if width is None or not isinstance(value, str) or len(value) <= width:
        return value
    return value[:max(width - 2, 1)] + ".."


def truncate_rows(display_columns: list, display_rows: list[list], widths: dict[str, int]) -> list[list]:
    return [
        [truncate_display_value(value, widths.get(col.name)) for col, value in zip(display_columns, row)]
        for row in display_rows
    ]
