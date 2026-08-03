"""Per-view display settings (hidden columns, widths, column order),
persisted to a local `.dbman/<db-name>.json` file relative to the directory
dbman was invoked from. Provider-agnostic: keyed by table/view name, so it
works the same for SqlAlchemyProvider tables and CouchDBProvider's inferred
columns. See CLAUDE.md and github.com/krewmarco/dbman issue #1."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


def derive_db_name(db_url: str) -> str:
    """A stable, filesystem-safe name for a connection string, used to key
    its settings file. Bare sqlite paths, sqlite:// URLs, and couchdb://
    URLs all carry their meaningful identifier in the last path segment."""
    path = urlsplit(db_url).path or db_url
    return Path(path).stem or "db"


@dataclass
class ViewSettings:
    hidden: list[str] = field(default_factory=list)
    widths: dict[str, int] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


class ViewSettingsStore:
    """Loads/saves ViewSettings for every table/view in one database, from
    `<base_dir>/.dbman/<db_name>.json`."""

    def __init__(self, db_name: str, base_dir: Optional[Path] = None):
        self.path = (base_dir or Path.cwd()) / ".dbman" / f"{db_name}.json"
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, item_name: str) -> ViewSettings:
        raw = self._data.get(item_name, {})
        return ViewSettings(
            hidden=list(raw.get("hidden", [])),
            widths=dict(raw.get("widths", {})),
            order=list(raw.get("order", [])),
        )

    def save(self, item_name: str, settings: ViewSettings) -> None:
        self._data[item_name] = {
            "hidden": settings.hidden,
            "widths": settings.widths,
            "order": settings.order,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))


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
