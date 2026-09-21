"""Terminal styling for DataTable cell content.

Separate from view_settings.py (which owns *persisted* per-view preferences
and the plain-string width/truncation math) because this is purely about how
an already-projected, already-truncated value is painted.

Today the only styled cells are enum-like columns (Column.options), rendered
in the provider's own option colors - a Notion select/status/multi_select
value shows up in the same color Notion itself shows it."""
from rich.text import Text

# Notion's option palette -> Rich style strings. Four of Notion's ten names
# (brown, orange, purple, pink) aren't Rich color names at all, hence an
# explicit map rather than passing the color through. "default" means "no
# color" in Notion, so it inherits the table's own foreground.
OPTION_STYLES = {
    "default": "",
    "gray": "grey62",
    "grey": "grey62",
    "brown": "dark_goldenrod",
    "orange": "dark_orange",
    "yellow": "yellow",
    "green": "green",
    "blue": "dodger_blue1",
    "purple": "medium_purple",
    "pink": "hot_pink",
    "red": "red",
}


def option_style(color) -> str:
    """Rich style for a provider's option color name. An unrecognized color
    (a palette dbman hasn't seen, or None) renders unstyled rather than
    raising - a new Notion color shouldn't break a page load."""
    return OPTION_STYLES.get(color or "default", "")


def option_text(name: str, color) -> Text:
    return Text(str(name), style=option_style(color))


def stylize_row(display_columns: list, rendered_row: list, source_row: list) -> list:
    """Paint a rendered row's enum-valued cells in their option colors,
    leaving every other cell exactly as it was.

    Must run *after* view_settings.truncate_rows: truncation slices plain
    `str` values by length, so styling first would either be sliced apart or
    (as a Text object) skip truncation entirely. Styling second keeps the
    width math operating on plain strings and only wraps at the very end.

    `source_row` is the same row *before* truncation, and is what option
    names are matched against - an option long enough to be truncated (e.g.
    Notion's "P2 - Friction / should fix before launch") still gets its
    color, with the shortened string carrying it.

    A cell is styled only when its untruncated value equals exactly one of
    the column's option names. That's also what makes multi-value columns
    behave sanely: a Notion multi_select holding one option gets that
    option's color, while one holding several renders as the plain
    comma-joined string, which matches no single option."""
    out = list(rendered_row)
    for i, col in enumerate(display_columns):
        options = getattr(col, "options", ())
        if not options or i >= len(out) or i >= len(source_row):
            continue
        value = source_row[i]
        if not isinstance(value, str):
            continue
        match = next((o for o in options if o.name == value), None)
        if match is not None:
            out[i] = Text(str(out[i]), style=option_style(match.color))
    return out
