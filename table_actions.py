"""Actions over dbman's current table, run from the command palette
(ctrl+p) rather than a key of their own - things done now and then, where
the finite key namespace isn't worth spending.

An action is an `async` function taking a `TableContext`: async because
asking the user something mid-action is `await ctx.prompt(...)`, which
needs Textual's push_screen_wait and so a worker. TableContext is the
whole surface an action sees, so it's what a user-scriptable action API
would grow from - deliberately internal until a few more actions have
shaped it.
"""
import math
import random
from typing import Optional

PAGE_SIZE = 500


def parse_sample_size(text: str, total: int) -> tuple[Optional[int], Optional[str]]:
    """'10' or '10%' -> (rows to sample, None), else (None, error). A
    percentage rounds up, so 10% of 5 rows is 1, never 0."""
    text = text.strip()
    percent = text.endswith("%")
    number = text[:-1].strip() if percent else text
    try:
        value = float(number) if percent else int(number)
    except ValueError:
        return None, "Enter a row count (10) or a percentage (10%)"
    if value <= 0:
        return None, "The sample needs at least one row"
    if percent:
        if value > 100:
            return None, "A percentage can't be over 100%"
        return math.ceil(total * value / 100), None
    return int(value), None


def key_column(columns, rows) -> Optional[str]:
    """The column a sample is filtered on: one whose values tell every
    matching row apart, since the sample is stored as a filter on it. The
    provider's key column if it does (it nearly always will), else the
    first column that happens to (a CSV's `id`), else None."""
    def identifies_rows(index):
        values = [row[index] for row in rows]
        if any(v is None or v == "" or isinstance(v, (dict, list)) for v in values):
            return False
        return len({str(v) for v in values}) == len(values)

    ordered = sorted(range(len(columns)), key=lambda i: not columns[i].primary_key)
    for index in ordered:
        if identifies_rows(index):
            return columns[index].name
    return None


class TableContext:
    """The current table as an action sees it: what's loaded, every row
    the filters match, and the few things an action can do about it."""

    def __init__(self, app):
        self.app = app
        self.item = app.current_item
        self.item_type = app.current_type
        self.filters = dict(app.filters)

    def notify(self, message, severity="information"):
        self.app.notify(message, severity=severity)

    def matching_rows(self, filters: dict) -> tuple[list, list]:
        """(columns, rows) of every row `filters` match, across all pages -
        not just the page on screen. Pages through the provider's own
        cursor, since page_size=None isn't "everything" on every backend
        (Mango _find and Notion's query both default to a page)."""
        provider = self.app.provider
        columns, rows, cursor = [], [], None
        while True:
            page = provider.get_page(self.item, self.item_type, filters, cursor=cursor, page_size=PAGE_SIZE)
            columns = page.columns
            rows.extend(page.rows)
            if not page.has_more or not page.next_cursor:
                return columns, rows
            cursor = page.next_cursor

    async def prompt(self, title: str, value: str, parse) -> Optional[object]:
        """Ask for one line of text. `parse(text)` returns (result, error);
        an error keeps the prompt open with what was typed. Returns the
        result, or None if cancelled."""
        from virtual_table import PromptScreen, RowEdit
        parsed = []

        def apply(text):
            result, error = parse(text)
            if error:
                return error
            parsed.append(result)
            return None

        accepted = await self.app.push_screen_wait(PromptScreen(RowEdit(title, value, apply)))
        return parsed[-1] if accepted and parsed else None

    def apply_filters(self, filters: dict, sample=None):
        self.app.filters = filters
        self.app.sample = sample
        self.app.reset_paging()
        self.app.load_item(self.item, self.item_type)


async def sample_rows(ctx: TableContext):
    """Spot-check: narrow the table to a random N (or N%) of the rows the
    current filters match. The picks are stored as a filter on the key
    column, so the sample stays put while its records are edited - a
    random ORDER BY would reshuffle on every reload. Running it again on a
    sampled table draws a fresh sample from the same filters."""
    active = ctx.app.active_sample()
    base_filters = dict(ctx.filters)
    if active:
        base_filters.pop(active["column"], None)
    try:
        columns, rows = ctx.matching_rows(base_filters)
    except Exception as e:
        ctx.notify(f"Sample failed: {e}", severity="error")
        return
    if not rows:
        ctx.notify("No rows match the current filters", severity="error")
        return
    column = key_column(columns, rows)
    if column is None:
        ctx.notify("No column tells these rows apart, so a sample can't be filtered on", severity="error")
        return
    total = len(rows)
    size = await ctx.prompt(
        f"Sample how many of {total} rows? (a count, or a percentage like 10%)",
        str(active["size"]) if active else "10",
        lambda text: parse_sample_size(text, total),
    )
    if size is None:
        return
    size = min(size, total)
    index = [c.name for c in columns].index(column)
    picks = [str(row[index]) for row in random.sample(rows, size)]
    ctx.apply_filters(
        {**base_filters, column: picks},
        sample={"column": column, "ids": picks, "size": size, "total": total},
    )
    ctx.notify(f"Showing a random {size} of {total} rows (filtered on '{column}'; F clears)")
