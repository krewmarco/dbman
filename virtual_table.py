"""Virtual tables: table-shaped UI over rows that don't come from a Provider.

dbman's premise is that everything is a table, so the fast, already-learned
vocabulary (row cursor, j/k, filter, an opener on the selected row) should
apply to things that aren't database rows at all - the option set of an enum
column, a column's own display settings, a plugin's config.

Deliberately *not* built on the `Provider` ABC. A Provider is ~25 methods of
schema reflection, cursor paging, capabilities and write paths; a list of 18
Notion select options needs none of it, and making every future virtual
table implement that surface would be a tax with no payer. The contract here
is four methods, only two of them required.

The "opener" is `space`, following PLANNING_space-vs-edit-keybinding.md's
split of space ("see it more truly") from `e` ("change it"). What opening
*means* is the table's business: for an option picker it toggles the row's
checkbox, which is why open_row returns whether anything changed.
"""
import fnmatch
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label

from cell_render import option_text
from providers.base import Column


@dataclass
class Prompt:
    """Returned by open_row when opening a row means asking for a value
    rather than flipping one - a column's width, say, next to rows that
    just toggle. The screen collects the text and hands it to `apply`;
    a cancelled prompt never calls it."""
    title: str
    value: str
    apply: Any  # Callable[[str], None]


@dataclass
class VirtualRow:
    """`key` is the row's stable identity - it survives filtering and
    re-rendering, and is what open_row is handed. `cells` are positionally
    aligned with columns() and may be plain values or Rich renderables."""
    key: Any
    cells: list


def cell_matches(value, term: str) -> bool:
    """Does one cell match the on-screen search term? See matches() for the
    semantics; this is the per-cell half, shared with dbman's table search
    ('/') so the two can't drift apart."""
    term = term.strip().lower()
    if not term:
        return True
    text = (value.plain if isinstance(value, Text) else str(value if value is not None else "")).lower()
    return fnmatch.fnmatch(text, f"*{term}*")


def matches(cells: list, term: str) -> bool:
    """Filter one row against the search box, case-insensitively, against
    the rendered text of every cell - so a term can hit any column without
    the user picking one first.

    The term is matched as a fragment with '*'/'?' acting as wildcards
    inside it, i.e. implicitly surrounded by '*'. So "13" finds
    "13. Auth & Collaboration" and so does "auth*", which is the useful
    reading of both in a search box.

    Note this is deliberately *looser* than the whole-string glob the main
    table's text filter box uses, where "*Timeline*" needs its own stars.
    That one builds a stored, re-applied query over rows the app may never
    have seen; this one narrows a short list already on screen, where making
    someone anchor a search they can see the results of is just friction."""
    if not term.strip():
        return True
    return any(cell_matches(cell, term) for cell in cells)


class VirtualTable(ABC):
    """Implement this, hand it to VirtualTableScreen. See OptionPickerTable."""

    title = "Table"
    # Shown under the title; a one-liner telling the user what space does here.
    hint = "space toggles · enter saves"
    # True when the opener writes through immediately rather than the screen
    # collecting a value to hand back on save. Such a table has nothing to
    # cancel - closing it can only report what already happened - so the
    # screen drops the Save/Clear buttons and dismisses result() however it
    # is closed, escape included.
    commits_immediately = False

    @abstractmethod
    def columns(self) -> list[Column]:
        ...

    @abstractmethod
    def rows(self) -> list[VirtualRow]:
        ...

    def open_row(self, key):
        """The opener - what `space` does to the selected row. Return True
        if the table's contents changed and it should be re-rendered, or a
        Prompt to ask the user for a value first."""
        return False

    def clear(self) -> None:
        """What the Clear button does. No-op unless the table has a notion
        of being emptied (a picker's selection, say)."""

    def result(self):
        """The value the screen dismisses with on save."""
        return None


class OptionPickerTable(VirtualTable):
    """Pick from an enum column's declared option set (Column.options).

    One table serves both arities. In single mode, toggling a row clears the
    others - so `select`/`status` editing, `multi_select` editing, and
    filtering any of them are the same screen rather than three.

    Selections live here, in a set keyed by option name, rather than being
    read back off the widget at save time. That's what makes filtering safe:
    narrowing the view to "auth*" can't drop a tick made under "13", which
    is exactly what a widget-owned selection would have done."""

    def __init__(self, title, options, selected, multi: bool, allow_none: bool = True):
        self.title = title
        self.options = list(options)
        self.multi = multi
        self.allow_none = allow_none and not multi
        names = {o.name for o in self.options}
        self.selected = {s for s in selected if s in names}
        # A Value column only earns its place when some option's value
        # actually differs from its display name (a relational FK lookup's
        # (display, key) pairs). For Notion's options the name *is* the
        # value, and a column repeating Name on every row is pure noise.
        self._show_value = any(getattr(o, "value", o.name) != o.name for o in self.options)
        self.hint = (
            "space toggles · f filters · enter saves"
            if multi else
            "space picks · f filters · enter saves"
        )

    NONE_KEY = "\x00none"

    def columns(self) -> list[Column]:
        cols = [Column("Sel", "bool"), Column("Name", "text")]
        if self._show_value:
            cols.append(Column("Value", "text"))
        return cols

    def rows(self) -> list[VirtualRow]:
        out = []
        for opt in self.options:
            cells = [
                "✓" if opt.name in self.selected else "",
                option_text(opt.name, getattr(opt, "color", None)),
            ]
            if self._show_value:
                cells.append(str(getattr(opt, "value", opt.name)))
            out.append(VirtualRow(opt.name, cells))
        if self.allow_none:
            cells = ["✓" if not self.selected else "", Text("(none)", style="italic dim")]
            if self._show_value:
                cells.append("")
            out.append(VirtualRow(self.NONE_KEY, cells))
        return out

    def open_row(self, key) -> bool:
        if key == self.NONE_KEY:
            self.selected.clear()
        elif self.multi:
            self.selected.symmetric_difference_update({key})
        else:
            # Re-picking the highlighted option clears it, so a single-select
            # cell can be emptied without reaching for the (none) row.
            self.selected = set() if self.selected == {key} else {key}
        return True

    def clear(self) -> None:
        self.selected.clear()

    def result(self):
        if self.multi:
            return [o.name for o in self.options if o.name in self.selected]
        return next((o.name for o in self.options if o.name in self.selected), "")


class VirtualTableScreen(ModalScreen):
    """Render a VirtualTable modally with dbman's own table vocabulary.

    Row-select by default, because the row is the unit a virtual table deals
    in - there's no cell to edit. j/k mirror the app's navigation; they need
    an explicit binding here because Textual's ModalScreen doesn't fall
    through to App-level bindings at all (see ConnectionSwitcherScreen, which
    hit the same thing). `f` focuses the filter box, matching the main app's
    filter key, and enter/escape from it hands focus back to the table so
    j/k/space stay immediate."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("space", "open_row", "Toggle", show=False),
        Binding("f", "focus_filter", "Filter", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    CSS = """
    VirtualTableScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #vt-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 64;
        height: auto;
        max-height: 80%;
    }
    #vt-title { text-style: bold; }
    #vt-hint { color: $text-muted; margin-bottom: 1; }
    #vt-filter { margin-bottom: 1; }
    #vt-table { height: auto; max-height: 18; }
    #vt-buttons { align: right middle; margin-top: 1; }
    #vt-buttons Button { margin-left: 1; }
    """

    def __init__(self, table: VirtualTable):
        super().__init__()
        self.table = table
        self._keys: list = []  # visible row keys, positionally aligned with the DataTable

    def compose(self) -> ComposeResult:
        with Vertical(id="vt-dialog"):
            yield Label(self.table.title, id="vt-title")
            yield Label(self.table.hint, id="vt-hint")
            # Hidden until `f`, like the main table's filter: an always-on
            # box is a permanent row of chrome for something most visits to
            # this screen never use, and it makes the list start lower.
            filter_box = Input(placeholder="Filter (try 'auth*' or '13')", id="vt-filter")
            filter_box.display = False
            yield filter_box
            yield DataTable(id="vt-table", cursor_type="row")
            with Horizontal(id="vt-buttons"):
                if self.table.commits_immediately:
                    yield Button("Close", variant="primary", id="vt-cancel")
                else:
                    yield Button("Cancel", id="vt-cancel")
                    yield Button("Clear", variant="warning", id="vt-clear")
                    yield Button("Save", variant="success", id="vt-save")

    def on_mount(self):
        self._populate()
        self.query_one("#vt-table", DataTable).focus()

    def _populate(self, keep_key=None):
        """Rebuild the DataTable from the VirtualTable, honouring the filter
        box and keeping the cursor on `keep_key` if it's still visible.

        Named _populate, not _render: Widget._render is Textual's own
        internal "turn render() into a Visual" hook, and shadowing it on a
        Screen makes the whole screen render as None - which surfaces a long
        way from the cause, as an AttributeError on NoneType.render_strips
        deep inside the styles cache."""
        table = self.query_one("#vt-table", DataTable)
        term = self.query_one("#vt-filter", Input).value
        table.clear(columns=True)
        for col in self.table.columns():
            table.add_column(col.name, key=col.name, width=3 if col.type_name == "bool" else None)
        self._keys = []
        for row in self.table.rows():
            if not matches(row.cells, term):
                continue
            table.add_row(*row.cells, key=str(len(self._keys)))
            self._keys.append(row.key)
        if keep_key in self._keys:
            table.cursor_coordinate = Coordinate(self._keys.index(keep_key), 0)

    def _selected_key(self):
        table = self.query_one("#vt-table", DataTable)
        index = table.cursor_coordinate.row
        return self._keys[index] if 0 <= index < len(self._keys) else None

    def action_cursor_down(self):
        self.query_one("#vt-table", DataTable).action_cursor_down()

    def action_cursor_up(self):
        self.query_one("#vt-table", DataTable).action_cursor_up()

    def action_open_row(self):
        key = self._selected_key()
        if key is None:
            return
        outcome = self.table.open_row(key)
        if isinstance(outcome, Prompt):
            def done(value):
                if value is not None:
                    outcome.apply(value)
                    self._populate(keep_key=key)
            self.app.push_screen(PromptScreen(outcome), done)
        elif outcome:
            self._populate(keep_key=key)

    def action_focus_filter(self):
        box = self.query_one("#vt-filter", Input)
        box.display = True
        box.focus()

    def _hide_filter_if_empty(self):
        """Leaving an empty box puts the chrome away again; a box with a
        term in it stays visible, since it's explaining the shortened list."""
        box = self.query_one("#vt-filter", Input)
        if not box.value.strip():
            box.display = False

    def action_cancel(self):
        box = self.query_one("#vt-filter", Input)
        if box.has_focus:
            # Escape in the filter box backs out of filtering, not out of
            # the whole screen - losing a half-made selection to a stray
            # escape would be a nasty way to learn the difference.
            box.value = ""
            box.display = False
            self.query_one("#vt-table", DataTable).focus()
            return
        self.dismiss(self.table.result() if self.table.commits_immediately else None)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._populate(keep_key=self._selected_key())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._hide_filter_if_empty()
        self.query_one("#vt-table", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self.table.commits_immediately:
            # Enter is the opener's twin here, not a commit - there's
            # nothing left to commit, and closing on it would make the row
            # cursor feel like a trapdoor.
            self.action_open_row()
            return
        # Enter on a row saves, matching the main app's "enter commits" feel.
        self.dismiss(self.table.result())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "vt-save":
            self.dismiss(self.table.result())
        elif event.button.id == "vt-clear":
            self.table.clear()
            self.dismiss(self.table.result())
        else:
            self.dismiss(None)

    def key_escape(self) -> None:
        self.action_cancel()


class PromptScreen(ModalScreen):
    """One-line input for a Prompt returned by an opener. Local to this
    module rather than reusing dbman's EditCellScreen, which would make
    virtual_table import dbman and close an import cycle."""

    CSS = """
    PromptScreen { background: rgba(0, 0, 0, 0.3); align: center middle; }
    #prompt-dialog {
        background: $panel; border: thick $primary;
        padding: 1 2; width: 50; height: auto;
    }
    #prompt-title { text-style: bold; margin-bottom: 1; }
    """

    def __init__(self, prompt: Prompt):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-dialog"):
            yield Label(self.prompt.title, id="prompt-title")
            yield Input(value=self.prompt.value, id="prompt-input")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss(None)
