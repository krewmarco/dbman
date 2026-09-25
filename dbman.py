#!/usr/bin/env python3
import argparse
import sys
import traceback
import webbrowser
import math
import random
import csv
import json
from sqlalchemy import text
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, ListView, ListItem, Label, Static, Button, Input, ContentSwitcher, TextArea, Select
from textual.containers import Horizontal, Vertical, Center, VerticalScroll
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.reactive import reactive
from textual.message import Message
from rich.panel import Panel
from rich.table import Table as RichTable
from rich.text import Text

# Import LookupPlugin from plugins folder
from plugins.lookup import LookupPlugin, LookupSelectScreen, LookupConfigScreen
from providers import create_provider
from view_settings import (
    ViewSettings, ViewSettingsStore, derive_db_name, apply_view_settings,
    compute_column_widths, truncate_rows,
)
from cell_render import option_text, stylize_row
from providers.base import Column, ColumnOption, FILTER_EMPTY, FILTER_NOT_EMPTY
from virtual_table import OptionPickerTable, VirtualTableScreen, cell_matches
from column_meta import ColumnMetadataTable
from workspace import WorkspaceStore, ConnectionSession

__version__ = "0.1.0"

# ... (rest of imports unchanged) ...

# ... (ShortcutsScreen, FilterColumnScreen, ConfirmScreen, EditCellScreen, TruncateColumnScreen, DbItem, SidebarHeader unchanged) ...

class TableDiagram(Static, can_focus=True):
    """A widget for displaying a single table and its relationships."""
    
    def __init__(self, table_name, columns, pks, fks, **kwargs):
        super().__init__(**kwargs)
        self.table_name = table_name
        self.columns = columns
        self.pks = pks
        self.fks = fks

    def render(self):
        table = RichTable(show_header=False, box=None, padding=(0, 1), expand=True)
        for col in self.columns:
            name = col.name
            col_type = col.type_name
            pk_marker = "[bold yellow]*[/]" if name in self.pks else " "
            table.add_row(f"{pk_marker} {name}", f"[dim]{col_type}[/]")

        if self.fks:
            table.add_section()
            table.add_row("[italic yellow]Relationships[/]", "")
            for fk in self.fks:
                rel_str = f"{fk.from_column} -> {fk.to_table}({fk.to_column})"
                table.add_row(f"  [dim]↳[/] {rel_str}", "")
                
        return Panel(
            table, 
            title=f"[bold cyan] {self.table_name} [/]", 
            border_style="green" if self.has_focus else "blue",
            expand=False,
            width=50
        )

    BINDINGS = [
        Binding("up", "move(0, -1)", "Up", show=False),
        Binding("down", "move(0, 1)", "Down", show=False),
        Binding("left", "move(-2, 0)", "Left", show=False),
        Binding("right", "move(2, 0)", "Right", show=False),
    ]

    class Moved(Message):
        def __init__(self, table_name, dx, dy):
            self.table_name = table_name
            self.dx = dx
            self.dy = dy
            super().__init__()

    def action_move(self, dx: int, dy: int) -> None:
        self.post_message(self.Moved(self.table_name, dx, dy))

    def action_cursor_up(self): self.action_move(0, -1)
    def action_cursor_down(self): self.action_move(0, 1)
    def action_cursor_left(self): self.action_move(-2, 0)
    def action_cursor_right(self): self.action_move(2, 0)

    def on_focus(self):
        self.refresh()
        self.scroll_visible()

    def on_blur(self):
        self.refresh()

# Crash logging setup
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    with open("dbman_crash.log", "w") as f:
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = handle_exception

COLORS = [
    "cyan", "magenta", "green", "yellow", "blue", "red", 
    "bright_cyan", "bright_magenta", "bright_green", "bright_yellow"
]

class ShortcutsScreen(ModalScreen):
    """A modal screen for displaying shortcuts."""
    CSS = """
    ShortcutsScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #shortcuts-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 60;
        height: auto;
        max-height: 80%;
    }
    #shortcuts-content {
        margin-bottom: 1;
    }
    Button {
        width: 100%;
    }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="shortcuts-dialog"):
            yield Static(
                " [bold]Shortcuts[/]\n\n"
                " [bold]General[/]\n"
                " q: Quit\n"
                " tab: Cycle focus (Sidebar -> Main Area)\n"
                " shift+tab: Jump to next sidebar section\n"
                " m: Toggle View/Schema/SQL/Diag mode\n"
                " r: Reload current table/view from the database\n"
                " c: Switch connection (pick from dbman.sqlite, j/k + enter)\n"
                " ?: Toggle this Shortcuts panel\n"
                " ctrl+p: Open Textual's command palette\n"
                " V: Show version/about screen\n\n"
                " [bold]Navigation[/]\n"
                " j / k: Move down / up\n"
                " h / l: Move left / right (Table only)\n"
                " pgup / pgdn or ctrl+d / ctrl+u: Scroll one screen down / up (Mac: fn + up / fn + down)\n"
                " g / G: Home / End\n"
                " \\] / \\[: Fetch next / previous page of rows from the DB (View mode)\n\n"
                " [bold]Editing & Filtering[/]\n"
                " e: Edit selected cell/row (View mode) or SQL (SQL mode)\n"
                "    Columns with a fixed option set open a picker table (space toggles,\n"
                "    f filters the options, enter saves)\n"
                " E: Edit whole document as JSON (document DB providers)\n"
                " space: Open the selected row (row select mode): a Notion row in the\n"
                "    browser, a dbman.sqlite connections row connects to it\n"
                " a: Add a new row (table) or create a new Table/View (where supported;\n"
                "    also works on a TABLES/VIEWS sidebar header, even when empty)\n"
                " d: Delete selected table/view\n"
                " x: Export current Table/View to CSV\n"
                " /: Search the loaded page - cell values, or column names in column mode;\n"
                "    with the sidebar focused, search table/view/plugin names instead\n"
                " n / N: Step to the next / previous match\n"
                " f: Filter the column under the cursor (field or column select mode)\n"
                "    Option columns open the same picker table (multi-pick, plus\n"
                "    (empty)/(not empty)); text columns accept '*' wildcards\n"
                " F: Clear all filters for current table\n"
                " o: Sort by selected column, cycling asc -> desc -> none (column select mode)\n"
                " t: Truncate/Shorten Column data (View mode only)\n"


                " [bold]Select Mode (View mode only)[/]\n"
                " s: Rotate select mode: field -> row -> column -> field\n"
                " e: In column mode, opens that column's settings (hidden, colored,\n"
                "    width, sort) as a table - space edits the selected property\n"
                " H / L: Move selected column left / right (column mode; option+left/right also works on some terminals)\n"
                " J / K: Move selected row down / up, persisted (row mode; providers that support it)\n"
                " z: Hide selected column (column mode)\n"
                " Z: Unhide a column (column mode, pick from hidden list)\n",
                id="shortcuts-content"
            )
            yield Button("Close", variant="primary", id="close-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-button":
            self.app.pop_screen()

    def key_question_mark(self) -> None:
        self.app.pop_screen()

DBMAN_BANNER = (
    "████   ████   █   █   ███   █   █\n"
    "█   █  █   █  ██ ██  █   █  ██  █\n"
    "█   █  ████   █ █ █  █████  █ █ █\n"
    "█   █  █   █  █   █  █   █  █  ██\n"
    "████   ████   █   █  █   █  █   █"
)

class VersionScreen(ModalScreen):
    """A modal 'about' splash: ASCII banner, tagline, and version - see
    issue #13. Scoped down from that issue's full superhero-mascot concept
    to a plain block-letter banner, since hand-drawing a recognizable
    figure in ASCII without visual iteration is a much bigger, more
    failure-prone undertaking than a text banner."""
    CSS = """
    VersionScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #version-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: auto;
        height: auto;
    }
    #version-content {
        margin-bottom: 1;
        text-align: center;
        width: 100%;
    }
    Button {
        width: 100%;
    }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="version-dialog"):
            yield Static(
                f"{DBMAN_BANNER}\n\n"
                f"Master the data!\n\n"
                f"v{__version__}  ·  github.com/krewmarco/dbman",
                id="version-content",
            )
            yield Button("Close", variant="primary", id="close-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-button":
            self.app.pop_screen()

    def key_escape(self) -> None:
        self.app.pop_screen()

class FilterColumnScreen(ModalScreen):
    """Free-text filter for one column. An enum-like column (Column.options)
    doesn't come here at all - action_filter_column sends it to an
    OptionPickerTable instead, since picking from the declared option set is
    both the expected gesture and the only one a per-option filter API can
    actually answer."""
    CSS = """
    FilterColumnScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #filter-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 50;
        height: auto;
    }
    Label {
        margin-bottom: 1;
        text-style: bold;
    }
    Input {
        margin-bottom: 1;
    }
    #filter-buttons {
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """
    def __init__(self, column_name, current_filter=""):
        super().__init__()
        self.column_name = column_name
        self.current_filter = current_filter if isinstance(current_filter, str) else ""

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-dialog"):
            yield Label(f"Filter Column: {self.column_name}")
            yield Static(
                "Enter search term ('null'/'not null', 'empty'/'not empty', "
                "'*' wildcards, or free text):",
                id="small-label",
            )
            yield Input(value=self.current_filter, id="filter-input", placeholder="Filter...")
            with Horizontal(id="filter-buttons"):
                yield Button("Cancel", id="cancel-filter")
                yield Button("Clear", variant="warning", id="clear-filter")
                yield Button("Apply", variant="success", id="apply-filter")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply-filter":
            self.dismiss(self.query_one(Input).value)
        elif event.button.id == "clear-filter":
            # Falsy-but-not-None: clear this column's filter. None is cancel.
            self.dismiss("")
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss(None)

class SearchScreen(ModalScreen):
    """'/' - find text in the page that's loaded, and move the cursor to it.

    Distinct from the filter box: a filter narrows the rowset and costs a
    provider round trip, while this only moves the cursor. That split is
    what lets search be cross-column and instant - it runs over the rows
    already in memory, so it behaves identically on all three backends
    instead of inheriting each one's filter vocabulary."""
    CSS = """
    SearchScreen { background: rgba(0, 0, 0, 0.3); align: center middle; }
    #search-dialog {
        background: $panel; border: thick $primary;
        padding: 1 2; width: 50; height: auto;
    }
    #search-dialog Label { text-style: bold; margin-bottom: 1; }
    #search-hint { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, scope: str, current: str = ""):
        super().__init__()
        self.scope = scope
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Label(f"Search {self.scope}")
            yield Input(value=self.current, placeholder="Text, or a '*' pattern", id="search-input")
            yield Static("n / N step through matches · empty clears", id="search-hint")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen):
    """A modal screen for confirmation."""
    CSS = """
    ConfirmScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #confirm-dialog {
        background: $panel;
        border: thick $error;
        padding: 1 2;
        width: 40;
        height: auto;
    }
    Label {
        margin-bottom: 1;
        text-align: center;
        width: 100%;
    }
    #confirm-buttons {
        align: center middle;
    }
    Button {
        margin: 0 1;
    }
    """
    def __init__(self, message, button_label="Delete"):
        super().__init__()
        self.message = message
        self.button_label = button_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.message)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel-confirm")
                yield Button(self.button_label, variant="error", id="ok-confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

class EditCellScreen(ModalScreen):
    """A minimal modal screen for editing a cell in-place."""
    CSS = """
    EditCellScreen {
        background: rgba(0, 0, 0, 0.3);
        align: center middle;
    }
    #edit-input {
        width: 50%;
        border: double $primary;
        background: $surface;
    }
    """
    def __init__(self, current_value):
        super().__init__()
        self.current_value = str(current_value) if current_value is not None else ""

    def compose(self) -> ComposeResult:
        yield Input(value=self.current_value, id="edit-input")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss(None)

class TruncateColumnScreen(ModalScreen):
    """A modal screen for truncating a column with live row count."""
    CSS = """
    TruncateColumnScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #truncate-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 50;
        height: auto;
    }
    Label {
        margin-bottom: 1;
        text-style: bold;
    }
    Input {
        margin-bottom: 1;
    }
    #stats-label {
        color: $text-muted;
        margin-bottom: 1;
    }
    #truncate-buttons {
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """
    
    affected_count = reactive(0)

    def __init__(self, table_name, column, current_max, suggested_len, provider):
        super().__init__()
        self.table_name = table_name
        self.column = column
        self.current_max = current_max
        self.suggested_len = suggested_len
        self.provider = provider

    def compose(self) -> ComposeResult:
        with Vertical(id="truncate-dialog"):
            yield Label(f"Truncate {self.column} (Max: {self.current_max})")
            yield Label("Target length:", id="small-label")
            yield Input(value=str(self.suggested_len), id="truncate-input")
            yield Static("", id="stats-label")
            with Horizontal(id="truncate-buttons"):
                yield Button("Cancel", id="cancel-truncate")
                yield Button("Apply", variant="success", id="apply-truncate")

    def on_mount(self):
        self.query_one(Input).focus()
        self.update_stats(str(self.suggested_len))

    def on_input_changed(self, event: Input.Changed) -> None:
        self.update_stats(event.value)

    def update_stats(self, value):
        try:
            target_len = int(value)
            count = self.provider.count_over_length(self.table_name, self.column, target_len)
            self.query_one("#stats-label").update(f"Will affect [bold red]{count}[/] rows")
        except:
            self.query_one("#stats-label").update("Invalid length")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply-truncate":
            self.dismiss(self.query_one(Input).value)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

class UnhideColumnScreen(ModalScreen):
    """A modal screen for picking a hidden column to bring back. Mode-
    independent (unlike hiding, which is column-select-mode-scoped) since a
    hidden column isn't reachable via column-mode cursor selection."""
    CSS = """
    UnhideColumnScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #unhide-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 50;
        height: auto;
    }
    Label {
        margin-bottom: 1;
        text-style: bold;
    }
    Select {
        margin-bottom: 1;
    }
    #unhide-buttons {
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """

    def __init__(self, hidden_columns: list[str]):
        super().__init__()
        self.hidden_columns = hidden_columns

    def compose(self) -> ComposeResult:
        with Vertical(id="unhide-dialog"):
            yield Label("Unhide column")
            yield Select(
                [(c, c) for c in self.hidden_columns],
                value=self.hidden_columns[0],
                id="unhide-select",
            )
            with Horizontal(id="unhide-buttons"):
                yield Button("Cancel", id="cancel-unhide")
                yield Button("Unhide", variant="success", id="apply-unhide")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply-unhide":
            self.dismiss(self.query_one(Select).value)
        else:
            self.dismiss(None)

class ConnectionListItem(ListItem):
    def __init__(self, name: str, is_current: bool) -> None:
        super().__init__(Label(f" {name} {'(current)' if is_current else ''}"))
        self.connection_name = name

class ConnectionSwitcherScreen(ModalScreen):
    """A modal listing every connection saved in dbman.sqlite (workspace.py),
    for fast switching without restarting the app - see 'c'/action_switch_
    connection. Enter (ListView's native binding) dismisses with the chosen
    connection name; Escape cancels with None.

    j/k need an explicit local BINDINGS entry here rather than relying on
    DbMan's own app-level j/k -> focused-widget dispatch (the mechanism the
    sidebar's ListView rides for free): Textual's ModalScreen deliberately
    does not fall through to App-level bindings at all (confirmed live -
    app.screen.active_bindings drops every App binding, including j/k, the
    moment a ModalScreen is on top of the stack) - otherwise typing "d" into
    a modal's Input would trigger DbMan's unrelated delete_item action.
    ListView's own native up/down bindings still work (they're bound on the
    focused widget itself, inside the modal's own DOM), just not j/k."""
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]
    CSS = """
    ConnectionSwitcherScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #connection-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 50;
        height: auto;
        max-height: 80%;
    }
    #connection-dialog Label {
        margin-bottom: 1;
        text-style: bold;
    }
    #connection-list {
        height: auto;
        max-height: 20;
    }
    """

    def __init__(self, names: list[str], current_name: str) -> None:
        super().__init__()
        self.names = names
        self.current_name = current_name

    def compose(self) -> ComposeResult:
        with Vertical(id="connection-dialog"):
            yield Label("Switch connection")
            with ListView(id="connection-list"):
                for name in self.names:
                    yield ConnectionListItem(name, name == self.current_name)

    def on_mount(self):
        list_view = self.query_one("#connection-list", ListView)
        list_view.focus()
        if self.current_name in self.names:
            list_view.index = self.names.index(self.current_name)

    def action_cursor_down(self):
        self.query_one("#connection-list", ListView).action_cursor_down()

    def action_cursor_up(self):
        self.query_one("#connection-list", ListView).action_cursor_up()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if isinstance(event.item, ConnectionListItem):
            self.dismiss(event.item.connection_name)

    def key_escape(self) -> None:
        self.dismiss(None)

class EditTextScreen(ModalScreen):
    """A modal screen for editing a block of text: SQL, a CouchDB view's
    JS map/reduce definition, or a whole JSON document."""
    CSS = """
    EditTextScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #edit-sql-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 80%;
        height: 80%;
    }
    Label {
        margin-bottom: 1;
        text-style: bold;
    }
    TextArea {
        margin-bottom: 1;
        height: 1fr;
    }
    #edit-sql-buttons {
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """
    def __init__(self, title, initial_text="", language="sql"):
        super().__init__()
        self.title_text = title
        self.initial_text = initial_text
        self.language = language

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-sql-dialog"):
            yield Label(self.title_text)
            yield TextArea(self.initial_text, id="sql-editor", language=self.language)
            with Horizontal(id="edit-sql-buttons"):
                yield Button("Cancel", id="cancel-edit-sql")
                yield Button("Execute", variant="success", id="apply-edit-sql")

    def on_mount(self):
        self.query_one(TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply-edit-sql":
            self.dismiss(self.query_one(TextArea).text)
        else:
            self.dismiss(None)

class ExportCsvScreen(ModalScreen):
    """A modal screen for exporting to CSV."""
    CSS = """
    ExportCsvScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #export-csv-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    Label {
        margin-bottom: 1;
        text-style: bold;
    }
    Input {
        margin-bottom: 1;
    }
    #export-csv-buttons {
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """
    def __init__(self, default_filename):
        super().__init__()
        self.default_filename = default_filename

    def compose(self) -> ComposeResult:
        with Vertical(id="export-csv-dialog"):
            yield Label("Export to CSV")
            yield Static("Enter filename (absolute path or relative to project root):", id="small-label")
            yield Input(value=self.default_filename, id="export-filename-input")
            with Horizontal(id="export-csv-buttons"):
                yield Button("Cancel", id="cancel-export")
                yield Button("Export", variant="success", id="apply-export")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply-export":
            self.dismiss(self.query_one(Input).value)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

class DbItem(ListItem):
    def __init__(self, name: str, item_type: str) -> None:
        super().__init__(Label(f" {name} "))
        self.item_name = name
        self.item_type = item_type # "table", "view", "plugin"

class SidebarHeader(ListItem):
    """Section header ('TABLES'/'VIEWS'/'PLUGINS'). Selectable like any other
    sidebar item (arrow-key navigable) rather than disabled, so it's a
    natural target for 'a' (Add) to create a new object of that section's
    type even when the section is currently empty."""
    def __init__(self, title: str, section_type: str) -> None:
        super().__init__(Label(f" {title} "))
        self.title = title
        self.section_type = section_type  # "table", "view", "plugin"

class DiagramView(VerticalScroll, can_focus=True):
    """A container for displaying table diagrams using a force-directed layout."""
    
    def __init__(self, provider, **kwargs):
        super().__init__(**kwargs)
        self.provider = provider
        self.positions = {}
        self.edges = []
        self.width = 150
        self.height = 50

    def get_saved_positions(self):
        saved = {}
        try:
            engine = self.provider.sqlalchemy_engine()
            with engine.connect() as conn:
                res = conn.execute(text("SELECT table_name, x, y FROM _dbman_layout"))
                for row in res:
                    saved[row[0]] = [row[1], row[2]]
        except Exception:
            pass # Table probably doesn't exist, or provider has no diagram-position storage
        return saved

    def save_position(self, table_name, x, y):
        try:
            engine = self.provider.sqlalchemy_engine()
            with engine.connect() as conn:
                conn.execute(text("CREATE TABLE IF NOT EXISTS _dbman_layout (table_name TEXT PRIMARY KEY, x INTEGER, y INTEGER)"))
                conn.execute(text("DELETE FROM _dbman_layout WHERE table_name = :name"), {"name": table_name})
                conn.execute(text("INSERT INTO _dbman_layout (table_name, x, y) VALUES (:name, :x, :y)"), 
                             {"name": table_name, "x": x, "y": y})
                conn.commit()
        except Exception:
            pass

    def layout_graph(self, nodes, edges, width, height, iterations=None):
        positions = self.get_saved_positions()
        
        remaining = [n for n in nodes if n not in positions]
        if not remaining:
            return positions
            
        degrees = {n: 0 for n in remaining}
        for u, v in edges:
            if u in remaining:
                degrees[u] += 1
            if v in remaining:
                degrees[v] += 1
                
        remaining.sort(key=lambda n: degrees[n], reverse=True)
        
        center_x = width // 2 - 25
        center_y = height // 2 - 5
        
        radius = 20.0
        angle = 0.0
        for idx, node in enumerate(remaining):
            if idx == 0:
                positions[node] = [center_x, center_y]
            else:
                x = center_x + int(radius * math.cos(angle) * 2)
                y = center_y + int(radius * math.sin(angle))
                
                x = max(0, min(width - 50, x))
                y = max(0, min(height - 10, y))
                positions[node] = [x, y]
                
                angle += 2.4  # radians step
                radius += 3.0 # expand outwards
                
        return positions

    def draw_lines(self, width, height, edges, positions):
        # We'll use a Text object for rich text with styles directly so it's performant
        grid_chars = [[' ' for _ in range(int(width))] for _ in range(int(height))]
        
        def plot_hline(x0, x1, y):
            if not (0 <= y < int(height)): return
            start_x, end_x = min(x0, x1), max(x0, x1)
            for x in range(start_x, end_x + 1):
                if 0 <= x < int(width):
                    if grid_chars[y][x] == ' ': grid_chars[y][x] = '─'
        
        def plot_vline(x, y0, y1):
            if not (0 <= x < int(width)): return
            start_y, end_y = min(y0, y1), max(y0, y1)
            for y in range(start_y, end_y + 1):
                if 0 <= y < int(height):
                    if grid_chars[y][x] in (' ', '─'): grid_chars[y][x] = '│'
        
        def plot_corner(x, y, char):
            if 0 <= y < int(height) and 0 <= x < int(width):
                grid_chars[y][x] = char
                
        for u, v in edges:
            if u in positions and v in positions:
                x0, y0 = positions[u]
                x1, y1 = positions[v]
                
                # Offset by half the panel width/height approximately to center lines
                x0, y0 = int(round(x0)) + 25, int(round(y0)) + 5
                x1, y1 = int(round(x1)) + 25, int(round(y1)) + 5
                
                mid_x = (x0 + x1) // 2
                
                plot_hline(x0, mid_x, y0)
                plot_vline(mid_x, y0, y1)
                plot_hline(mid_x, x1, y1)
                
                # Plot corners accurately based on direction
                c1 = '┐' if (x0 < mid_x and y0 < y1) or (x0 > mid_x and y0 > y1) else '┌'
                c2 = '└' if (x1 > mid_x and y1 > y0) or (x1 < mid_x and y1 < y0) else '┘'
                
                if y0 <= y1:
                    if x0 <= x1:
                        c1, c2 = '┐', '└'
                    else:
                        c1, c2 = '┌', '┘'
                else:
                    if x0 <= x1:
                        c1, c2 = '┘', '┌'
                    else:
                        c1, c2 = '└', '┐'
                
                plot_corner(mid_x, y0, c1)
                plot_corner(mid_x, y1, c2)
                
        text = Text()
        for row in grid_chars:
            text.append("".join(row) + "\n", style="dim cyan")
        return text

    def update_lines(self):
        bg_text = self.draw_lines(self.width, self.height, self.edges, self.positions)
        # Use query instead of query_one to avoid crash and update all if multiple exist temporarily
        for bg in self.query(".diagram-bg"):
            bg.update(bg_text)

    def on_table_diagram_moved(self, event: TableDiagram.Moved):
        widget = None
        for w in self.query(TableDiagram):
            if w.table_name == event.table_name:
                widget = w
                break
        
        if widget and widget.table_name in self.positions:
            x, y = self.positions[widget.table_name]
            new_x = max(0, min(self.width - 20, x + event.dx))
            new_y = max(0, min(self.height - 5, y + event.dy))
            self.positions[widget.table_name] = [new_x, new_y]
            
            widget.styles.offset = (int(new_x), int(new_y))
            
            self.save_position(widget.table_name, int(new_x), int(new_y))
            self.update_lines()

    def refresh_diagram(self):
        try:
            if not self.provider.capabilities.diagram:
                self.query(TableDiagram).remove()
                bg_query = self.query(".diagram-bg")
                msg = "Diagram not available for this provider."
                if bg_query:
                    bg_query.first().update(msg)
                else:
                    self.mount(Static(msg, classes="diagram-bg"))
                return

            # Remove existing diagrams
            self.query(TableDiagram).remove()

            model = self.provider.get_diagram_model()

            table_widgets = []
            nodes = []
            edges = []

            for node in model.nodes:
                node_fks = [e for e in model.edges if e.from_table == node.name]
                table_widgets.append(TableDiagram(node.name, node.columns, node.primary_keys, node_fks))
                nodes.append(node.name)

            for e in model.edges:
                edges.append((e.from_table, e.to_table))

            self.width = max(self.app.console.size.width, 150)
            self.height = max(self.app.console.size.height, 50)
            self.edges = edges

            if not table_widgets:
                bg_text = "No tables found."
            else:
                self.positions = self.layout_graph(nodes, edges, self.width, self.height)
                bg_text = self.draw_lines(self.width, self.height, self.edges, self.positions)
            
            # Reuse existing background widget if possible to avoid DuplicateIds or stacking
            bg_query = self.query(".diagram-bg")
            if bg_query:
                bg_query.first().update(bg_text)
                # If we somehow got duplicates, remove the extras
                for other in bg_query[1:]:
                    other.remove()
            else:
                self.mount(Static(bg_text, classes="diagram-bg"))

            for w in table_widgets:
                if w.table_name in self.positions:
                    x, y = self.positions[w.table_name]
                    w.styles.position = "absolute"
                    w.styles.offset = (int(x), int(y))
                    w.styles.margin = 0
                self.mount(w)
                
        except Exception as e:
            bg_query = self.query(".diagram-bg")
            if bg_query:
                bg_query.first().update(f"Error generating diagram: {e}")
            else:
                self.mount(Static(f"Error generating diagram: {e}", classes="diagram-bg"))

def _ctx(modes=None, select_modes=None, item_types=None, capability=None):
    """Build a check_action predicate for the common case: an AND of
    mode/select_mode/item_type/capability checks. `None` for any parameter
    means "any value is fine" on that axis. See issue #10."""
    modes = frozenset(modes) if modes is not None else None
    select_modes = frozenset(select_modes) if select_modes is not None else None
    item_types = frozenset(item_types) if item_types is not None else None

    def predicate(app):
        if modes is not None and app.mode not in modes:
            return False
        if select_modes is not None and app.select_mode not in select_modes:
            return False
        if item_types is not None and app.current_type not in item_types:
            return False
        if capability is not None and not getattr(app.provider.capabilities, capability, False):
            return False
        return True

    return predicate


def _edit_cell_ctx(app):
    """'e' is polymorphic: SQL mode edits the View's SQL, the lookup plugin
    edits its own config, and in View mode what it edits depends on
    select_mode (field: the cell, row: the whole row/document, or a hand-off
    to an external app/page for providers with open_in_browser; column: that
    column's own display settings). Too many cross-cutting branches to
    express as a plain _ctx() AND."""
    if app.mode == "sql":
        return app.current_type == "view" and app.provider.capabilities.create_definition
    if app.current_type == "plugin":
        return app.current_item == "lookup" and app.lookup_plugin is not None
    if app.mode != "view":
        return False
    if app.select_mode == "row":
        return app.provider.capabilities.whole_row_edit or app.provider.capabilities.open_in_browser
    if app.select_mode == "column":
        # Unlike the branches above this doesn't need a writable row - it
        # edits dbman's own per-column display settings, which a read-only
        # view has just as much as a table does.
        return app.current_type in ("table", "view")
    return app.current_type == "table" and app.rows_editable


def _open_row_ctx(app):
    """space = "see it more truly" (PLANNING_space-vs-edit-keybinding.md),
    for now only its row-mode half: open the selected row wherever its
    provider says it leads. Per item via can_open_row, not a Capabilities
    flag, since in dbman.sqlite only `connections` rows open anywhere."""
    if app.mode != "view" or app.select_mode != "row" or not app.current_item:
        return False
    if not isinstance(app.focused, DataTable) or not app.rows_editable:
        return False
    return app.provider.can_open_row(app.current_item, app.current_type)


def _filter_column_ctx(app):
    """Kept enabled across select modes and item types (unlike the
    column-scoped z/w/H/L/F actions) since filtering is basic/core usage -
    see the footer decluttering note. Only fully hidden when there's nothing
    loaded to filter at all. check_action returning False/None doesn't just
    grey out the footer, it blocks the keypress from ever reaching
    action_filter_column (see Textual's App.run_action) - so this must stay
    True whenever View mode has an item loaded, letting action_filter_column's
    own notify() explain *why* filtering isn't available right now (wrong
    select mode, or a non-filterable item like a CouchDB view) instead of the
    key silently doing nothing."""
    return app.mode == "view" and bool(app.current_item)


def _sidebar_has_focus(app):
    return app.focused is not None and app.focused.id == "sidebar-list"


def _search_ctx(app):
    """'/' is polymorphic on focus: with the sidebar focused it searches
    object names (any mode - the sidebar is on screen in all but Diagram,
    which hides it), otherwise it searches the loaded page, View mode only."""
    if _sidebar_has_focus(app):
        return True
    return app.mode == "view"


def _search_step_ctx(app):
    """n/N are only meaningful with a search running. Greyed (None) rather
    than hidden so the keys stay discoverable once '/' has been used, and
    inert - check_action returning None also stops the keypress. Follows
    whichever search '/' would start: object names if the sidebar has focus,
    else the loaded page."""
    if _sidebar_has_focus(app):
        return True if app.sidebar_search_term else None
    if app.mode != "view" or not app.current_item:
        return False
    return True if app.search_matches else None


def _sort_column_ctx(app):
    """Mirrors _filter_column_ctx: kept dispatchable across select modes/item
    types whenever View mode has an item loaded, so action_sort_column's own
    notify() can explain *why* sorting isn't available right now (wrong
    select mode, or a non-sortable item/provider) instead of the key
    silently doing nothing."""
    return app.mode == "view" and bool(app.current_item)


def _truncate_column_ctx(app):
    return (
        app.mode == "view"
        and app.current_type == "table"
        and bool(app.current_item)
        and app.provider.capabilities.truncate_column
    )


def _delete_item_ctx(app):
    """'d' is polymorphic like 'e': in View mode with the DataTable focused
    in row-select mode, it deletes the currently selected row instead of the
    whole table/view (gated on capabilities.delete_row, not delete_item)."""
    if app.current_type == "plugin":
        return False
    if app.mode == "view" and isinstance(app.focused, DataTable) and app.select_mode == "row":
        return bool(app.current_item) and app.rows_editable and app.provider.capabilities.delete_row
    return bool(app.current_type) and app.provider.capabilities.delete_item


def _toggle_mode_ctx(app):
    return app.current_type != "plugin"


def _unhide_column_ctx(app):
    """Column-select-mode-scoped, like hiding. Greys out (rather than hides)
    'u' when there's nothing to unhide."""
    if app.mode != "view" or app.select_mode != "column" or not app.current_item:
        return False
    return True if app.view_settings.get(app.current_item).hidden else None


def _export_csv_ctx(app):
    return app.current_type in ("table", "view")


def _highlighted_sidebar_header(app):
    """The SidebarHeader currently highlighted in the sidebar ListView, if
    any (headers are selectable so 'a' can create a new object of that
    section's type even when the section is empty)."""
    try:
        sidebar_list = app.query_one("#sidebar-list", ListView)
    except Exception:
        return None
    child = sidebar_list.highlighted_child
    return child if isinstance(child, SidebarHeader) else None


def _add_ctx(app):
    """'a' is polymorphic like 'e': add a row on a Table, create a new View
    on a View, or - when a section header is highlighted - create a new
    Table/View for that section. OR-of-branches, not a plain _ctx() AND."""
    header = _highlighted_sidebar_header(app)
    if header is not None:
        if header.section_type == "table":
            return app.provider.capabilities.create_table
        if header.section_type == "view":
            return app.provider.capabilities.create_definition
        return False
    if app.mode != "view" or not app.current_item:
        return False
    if app.current_type == "table":
        return app.provider.capabilities.add_row
    if app.current_type == "view":
        return app.provider.capabilities.create_definition
    return False


def _clear_filters_ctx(app):
    """Paired with 'f', and like it dispatchable in every select mode -
    clearing all filters names no column, so it needs no rotation. Greys
    out (rather than hides) when there's genuinely nothing to clear."""
    if app.mode != "view" or not bool(app.current_item):
        return False
    return True if app.filters else None


def _switch_connection_ctx(app):
    """Hidden entirely with no workspace.json (e.g. programmatic use);
    greyed out (rather than hidden) once a workspace exists but there's
    nothing else saved yet to switch to."""
    if app.workspace is None:
        return False
    return True if len(app.workspace.list_connections()) > 1 else None


class DbMan(App):
    """A vim-like database browser powered by SQLAlchemy."""

    TITLE = "dbman"
    CSS = """
    Screen {
        background: $surface;
    }
    #sidebar {
        width: 25;
        background: $panel;
        border-right: solid $primary;
    }
    ListView {
        height: 1fr;
        background: $panel;
    }
    SidebarHeader {
        background: $accent;
        color: $text;
        text-style: bold;
        padding: 0 1;
        border-bottom: solid $accent;
        border-top: solid $accent;
    }
    SidebarHeader:first-child {
        border-top: none;
    }
    DataTable {
        height: 1fr;
    }
    DataTable:focus {
        border: double $accent;
    }
    #sql-view, #diagram-view {
        height: 1fr;
        padding: 1 2;
        background: $surface;
        color: $text;
        overflow-x: scroll;
        overflow-y: scroll;
    }
    #sql-view:focus, #diagram-view:focus {
        border: double $accent;
    }
    TableDiagram {
        width: auto;
        height: auto;
    }
    .diagram-bg {
        width: auto;
        height: auto;
    }
    ListItem {
        padding: 0 1;
    }
    #sidebar-list > ListItem.-highlight {
        background: $primary;
        color: $text;
    }
    #sidebar-list:focus > ListItem.-highlight {
        background: $primary;
        color: $text;
    }
    #small-label {
        margin-bottom: 0;
        color: $text-muted;
    }
    """
    
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("h", "cursor_left", "Left", show=False),
        Binding("l", "cursor_right", "Right", show=False),
        Binding("pageup", "page_up", "PgUp", show=False),
        Binding("pagedown", "page_down", "PgDn", show=False),
        Binding("ctrl+u", "page_up", "PgUp", show=False),
        Binding("ctrl+d", "page_down", "PgDn", show=False),
        Binding("g", "scroll_home", "Home", show=False),
        Binding("G", "scroll_end", "End", show=False),
        Binding("tab", "switch_focus", "Sidebar/Main"),
        Binding("shift+tab", "jump_section", "Jump Section"),
        Binding("m", "toggle_mode", "View/Schema/SQL/Diag Mode", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("c", "switch_connection", "Switch Connection"),
        Binding("d", "delete_item", "Delete"),
        Binding("?", "toggle_shortcuts", "Shortcuts"),
        Binding("V", "toggle_version", "Version", show=False),
        Binding("e", "edit_cell", "Edit"),
        Binding("space", "open_row", "Open"),
        Binding("E", "edit_document", "Edit Document", show=False),
        Binding("a", "add", "Add"),
        Binding("x", "export_csv", "Export CSV", show=False),
        Binding("/", "search", "Search"),
        Binding("n", "search_next", "Next Match", show=False),
        Binding("N", "search_prev", "Prev Match", show=False),
        Binding("f", "filter_column", "Filter Column"),
        Binding("F", "clear_filters", "Clear Filters"),
        Binding("o", "sort_column", "Sort Column"),
        Binding("t", "truncate_column", "Shorten Column"),
        Binding("s", "rotate_select_mode", "Select Mode"),
        Binding("z", "hide_column", "Hide Column"),
        Binding("Z", "unhide_column", "Unhide Column"),
        Binding("alt+left", "reorder_column(-1)", "Move Column Left", show=False),
        Binding("alt+right", "reorder_column(1)", "Move Column Right", show=False),
        Binding("H", "reorder_column(-1)", "Move Column Left"),
        Binding("L", "reorder_column(1)", "Move Column Right"),
        Binding("K", "move_row(-1)", "Move Row Up"),
        Binding("J", "move_row(1)", "Move Row Down"),
        Binding("]", "next_page", "Next Page", show=False),
        Binding("[", "prev_page", "Prev Page", show=False),
    ]

    SELECT_MODES = ["field", "row", "column"]
    CURSOR_TYPE_BY_SELECT_MODE = {"field": "cell", "row": "row", "column": "column"}

    # Per-action visibility/enablement predicates for the footer, keyed by
    # action name (not by key — see _ctx/_edit_cell_ctx etc. above). Actions
    # not listed here are always shown+enabled. See issue #10.
    ACTION_CONTEXTS = {
        "edit_cell": _edit_cell_ctx,
        "open_row": _open_row_ctx,
        "edit_document": _ctx(modes={"view"}, capability="whole_row_edit"),
        "filter_column": _filter_column_ctx,
        "sort_column": _sort_column_ctx,
        "search": _search_ctx,
        "search_next": _search_step_ctx,
        "search_prev": _search_step_ctx,
        "truncate_column": _truncate_column_ctx,
        "rotate_select_mode": _ctx(modes={"view"}),
        "reorder_column": _ctx(modes={"view"}, select_modes={"column"}),
        "move_row": _ctx(modes={"view"}, select_modes={"row"}, capability="reorder_row"),
        "hide_column": _ctx(modes={"view"}, select_modes={"column"}),
        "unhide_column": _unhide_column_ctx,
        "delete_item": _delete_item_ctx,
        "toggle_mode": _toggle_mode_ctx,
        "export_csv": _export_csv_ctx,
        "add": _add_ctx,
        "clear_filters": _clear_filters_ctx,
        "switch_connection": _switch_connection_ctx,
    }

    def check_action(self, action, parameters):
        ctx = self.ACTION_CONTEXTS.get(action)
        if ctx is None:
            return True
        return ctx(self)

    def __init__(self, db_url, workspace=None, workspace_name=None):
        super().__init__()
        self.db_url = db_url
        self.workspace = workspace
        self.workspace_name = workspace_name
        self.current_item = None
        self.current_type = None
        self.rows_editable = False
        self.row_keys = {}
        self.raw_docs = {}
        self.row_values = {}
        self.column_widths = {}
        self._sidebar_rows = []  # see _rebuild_sidebar/_highlight_sidebar_item
        self._sidebar_rebuilding = False
        self._sidebar_rebuild_id = 0
        # name -> provider Column for the page on screen. The DataTable
        # only carries column name strings, but action_edit_cell and
        # action_filter_column need the column's options/read_only, and
        # re-reading the schema is a network call for Notion.
        self.columns_by_name = {}
        self.mode = "view"
        self.select_mode = "field"
        self.filters = {}
        self.sort = []  # [(column_name, "asc" | "desc")] - single-column for now, see action_sort_column
        # '/' search over the loaded page. search_cells is every matching
        # cell (what gets highlighted); search_matches is the subset the
        # cursor steps through, which differs by select mode.
        self.search_term = None
        self.search_cells = []
        self.search_matches = []
        self.search_index = 0
        # '/' with the sidebar focused: a name search over its objects. Only
        # the term is kept - matches are recomputed against _sidebar_rows on
        # each step, so a sidebar rebuild can't leave stale row indices.
        self.sidebar_search_term = None
        self.page_size = 500
        self.page_cursor = None
        self.page_history = []
        self.page_has_more = False
        self._next_cursor = None
        # Row-reorder batching (capabilities.reorder_row - see
        # action_move_row): self.row_order/rendered_rows mirror the
        # DataTable's current row sequence so J/K can reorder it locally and
        # redraw instantly, without a network round trip per keystroke. Once
        # the local order settles (_ROW_ORDER_SYNC_DELAY of no further
        # moves), the touched rows' new sequence is sent to
        # provider.reorder_rows in one batch call, in a background thread,
        # so a burst of J/K presses feels instant and only pays the network
        # cost once.
        self.row_order = []
        self.rendered_rows = {}
        self._row_order_dirty = False
        self._row_order_sync_target = None
        self._row_order_timer = None
        try:
            self.provider = create_provider(db_url)
            self.lookup_plugin = (
                LookupPlugin(self.provider.sqlalchemy_engine())
                if self.provider.capabilities.lookup_plugin else None
            )
            # Prefer the friendly saved connection name (may be a --name
            # override) over the raw url-derived one, so e.g. a notion://
            # connection's view settings don't land in a page-id-UUID file.
            self.view_settings = ViewSettingsStore(self.workspace_name or derive_db_name(db_url))
            if self.workspace is not None:
                self.workspace.upsert_connection(self.workspace_name, db_url)
            self.sub_title = self.workspace_name or ""
        except Exception as e:
            print(f"Error connecting to database: {e}")
            sys.exit(1)

    def get_tables(self):
        return self.provider.list_tables()

    def get_views(self):
        return self.provider.list_views()

    def get_plugin_data(self, name):
        if name == "lookup":
            columns = ["Table", "ForeignKeyField", "RelatedTable", "RelatedKey", "LookupField"]
            rows = self.lookup_plugin.get_config_data()
            return columns, rows
        return [], []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield ListView(id="sidebar-list")
            with ContentSwitcher(initial="data-table"):
                yield DataTable(id="data-table")
                yield Static("", id="sql-view")
                yield DiagramView(self.provider, id="diagram-view")
        yield Footer()

    def on_mount(self):
        self.refresh_sidebar()
        self.query_one("#sidebar-list").focus()

    def on_descendant_focus(self, event) -> None:
        # '/' and n/N change meaning with focus (sidebar vs table), so the
        # footer has to follow focus moves, not just mode/item changes.
        self.refresh_bindings()

    def on_app_focus(self):
        """Fires when the terminal window regains OS focus (Textual enables
        xterm FocusIn/FocusOut reporting automatically - not supported by
        every terminal/multiplexer, e.g. tmux needs `focus-events on`).
        Auto-refreshes the currently loaded item, since the most common
        reason to tab back in is having just edited something externally -
        e.g. a Notion row opened via `e`/action_open_in_browser. Harmless
        no-op re-fetch if nothing actually changed; load_item's existing
        same-item cursor restore keeps this from disrupting position."""
        if self.mode == "view" and self.current_item:
            self.load_item(self.current_item, self.current_type)

    def action_reload(self):
        """Manual, terminal-independent fallback for on_app_focus: that
        auto-refresh only fires if the terminal actually reports xterm
        FocusIn/FocusOut (e.g. Terminal.app on macOS doesn't), so 'r' is the
        reliable way to pick up an external edit (a Notion row edited via
        'e', a change made outside dbman entirely, ...). Unlike
        on_app_focus, this isn't restricted to View mode - reload whatever's
        currently shown."""
        if not self.current_item:
            return
        self.load_item(self.current_item, self.current_type)
        self.notify("Reloaded")

    def action_switch_connection(self):
        if self.workspace is None:
            return
        names = self.workspace.list_connections()
        if len(names) < 2:
            self.notify("No other saved connections to switch to", severity="warning")
            return
        self.push_screen(
            ConnectionSwitcherScreen(names, self.workspace_name),
            self._on_switch_connection_selected,
        )

    def _on_switch_connection_selected(self, name):
        if name is None or name == self.workspace_name:
            return
        resolved = self.workspace.resolve(name)
        if resolved is None:
            self.notify(f"Unknown connection '{name}'", severity="error")
            return
        new_url, new_name = resolved

        # Build the new connection's provider/plugin/settings *before*
        # touching any current state, so a failed connect (bad credentials,
        # unreachable host) leaves the app exactly as it was.
        try:
            provider = create_provider(new_url)
            lookup_plugin = (
                LookupPlugin(provider.sqlalchemy_engine())
                if provider.capabilities.lookup_plugin else None
            )
            view_settings = ViewSettingsStore(new_name)
        except Exception as e:
            self.notify(f"Connection failed: {e}", severity="error")
            return

        # Flush/save the *old* connection's pending state while self.provider
        # and self.workspace_name still refer to it - same ordering as
        # action_quit.
        self._flush_row_order_sync()
        self._save_workspace_session()

        self.provider = provider
        self.lookup_plugin = lookup_plugin
        self.view_settings = view_settings
        self.db_url = new_url
        self.workspace_name = new_name
        self.workspace.upsert_connection(new_name, new_url)
        self.query_one("#diagram-view", DiagramView).provider = provider

        self.current_item = None
        self.current_type = None
        self.mode = "view"
        self.select_mode = "field"
        self.filters = {}
        self.sort = []
        self._reset_search()
        self.reset_paging()
        self.row_keys = {}
        self.raw_docs = {}
        self.row_values = {}
        self.column_widths = {}
        self.columns_by_name = {}
        self.auto_column_widths = {}
        self.row_order = []
        self.rendered_rows = {}

        self.sub_title = new_name
        self.refresh_sidebar()
        self.refresh_bindings()
        self.notify(f"Switched to '{new_name}'")

    def refresh_sidebar(self):
        """Rebuild the sidebar. Fire-and-forget from the caller's point of
        view, but the rebuild itself has to be async: see _rebuild_sidebar.

        The suppression flag is raised here, synchronously, rather than
        inside the coroutine - a Highlighted queued against the outgoing
        contents would otherwise be delivered in the gap before the worker
        starts, which on a connection switch meant adopting an item that
        belongs to the connection being left."""
        # Generation token, not a plain bool: the worker is exclusive, so a
        # second refresh cancels the first, and the cancelled one still runs
        # its finally - which would otherwise lower the flag out from under
        # the rebuild that superseded it.
        self._sidebar_rebuild_id += 1
        self._sidebar_rebuilding = True
        self.run_worker(
            self._rebuild_sidebar(self._sidebar_rebuild_id),
            exclusive=True, group="sidebar-rebuild",
        )

    async def _rebuild_sidebar(self, rebuild_id):
        sidebar_list = self.query_one("#sidebar-list", ListView)
        # _sidebar_rebuilding is already True (set in refresh_sidebar):
        # rows coming and going make the ListView emit Highlighted for
        # whatever it lands on mid-teardown, including the outgoing
        # connection's rows. Every load that legitimately happens during a
        # rebuild is an explicit load_item call below, so the event is pure
        # noise here. See on_list_view_highlighted.
        try:
            # Awaiting the clear is the whole reason this is async. ListView.
            # clear() removes its children *asynchronously* (it returns an
            # AwaitRemove) while append() lands synchronously, so repopulating
            # without awaiting leaves the widget holding the previous contents
            # followed by the new ones. Anything that then resolves a row
            # position against it - and assigning .index emits Highlighted,
            # which on_list_view_highlighted turns into a load_item - is
            # working against a list that is about to shrink underneath it.
            #
            # Two separate crashes came from that. Switching connections landed
            # .index past the end of the settled list, so the next up/k raised
            # IndexError out of ListView.action_cursor_up; and an index that
            # happened to fall on a *stale* row loaded an item belonging to the
            # previous connection, which the new provider has never heard of
            # (KeyError out of NotionProvider.get_schema). It only looked
            # correct for a same-content refresh (m-cycling), where the stale
            # half is identical to the new one and so any offset into it
            # coincidentally names the right item.
            await sidebar_list.clear()

            views = self.get_views()
            tables = self.get_tables()
            plugins = ["lookup"] if self.lookup_plugin else []

            # One ordered description of the sidebar, recorded as it's built,
            # so a row's position is derived from the same source of truth that
            # produced it rather than scanned back out of the widget. `None`
            # marks a section header; everything else is (name, type).
            self._sidebar_rows = []
            for header, item_type, names in (
                ("TABLES", "table", tables),
                ("VIEWS", "view", views),
                ("PLUGINS", "plugin", plugins),
            ):
                sidebar_list.append(SidebarHeader(header, item_type))
                self._sidebar_rows.append(None)
                for name in names:
                    sidebar_list.append(DbItem(name, item_type))
                    self._sidebar_rows.append((name, item_type))

            if self.current_item:
                # A rebuild that kept its item (m-cycling, a create/delete)
                # must put the highlight back on it - the clear dropped it.
                self._highlight_sidebar_item(self.current_item, self.current_type)
            else:
                restored = self._restore_workspace_session(views, tables)
                if not restored:
                    if tables:
                        self.load_item(tables[0], "table")
                    elif views:
                        self.load_item(views[0], "view")
        finally:
            if rebuild_id == self._sidebar_rebuild_id:
                self._sidebar_rebuilding = False

    def _highlight_sidebar_item(self, name, item_type):
        """Move the sidebar highlight onto one item's row. Safe to call only
        once the sidebar has settled - i.e. from inside _rebuild_sidebar,
        after its awaited clear, or any time no rebuild is in flight."""
        try:
            index = self._sidebar_rows.index((name, item_type))
        except ValueError:
            return
        sidebar_list = self.query_one("#sidebar-list", ListView)
        if index < len(sidebar_list.children):
            sidebar_list.index = index

    def _restore_workspace_session(self, views, tables) -> bool:
        """On first load, re-open the item/mode/select_mode/cursor this
        connection was left on last time it quit cleanly. Returns False
        (falling through to the default first-item selection) if there's
        no saved session or the saved item no longer exists."""
        if self.workspace is None:
            return False
        session = self.workspace.get_session(self.workspace_name)
        if session is None or session.item_name is None:
            return False
        available = {"view": views, "table": tables, "plugin": ["lookup"] if self.lookup_plugin else []}
        if session.item_name not in available.get(session.item_type, []):
            return False

        self.mode = session.mode
        self.select_mode = session.select_mode
        self.load_item(session.item_name, session.item_type)

        self._highlight_sidebar_item(session.item_name, session.item_type)

        if session.cursor_row is not None and session.cursor_column is not None:
            try:
                self.query_one("#data-table", DataTable).move_cursor(
                    row=session.cursor_row, column=session.cursor_column
                )
            except Exception:
                pass
        return True

    async def action_quit(self) -> None:
        self._flush_row_order_sync()
        self._save_workspace_session()
        await super().action_quit()

    def _save_workspace_session(self):
        if self.workspace is None or self.current_item is None:
            return
        cursor_row = cursor_col = None
        if self.mode == "view":
            try:
                coord = self.query_one("#data-table", DataTable).cursor_coordinate
                cursor_row, cursor_col = coord.row, coord.column
            except Exception:
                pass
        self.workspace.save_session(self.workspace_name, ConnectionSession(
            item_name=self.current_item,
            item_type=self.current_type,
            mode=self.mode,
            select_mode=self.select_mode,
            cursor_row=cursor_row,
            cursor_column=cursor_col,
        ))

    def reset_paging(self):
        self.page_cursor = None
        self.page_history = []
        self.page_has_more = False

    def on_list_view_selected(self, event: ListView.Selected):
        item = event.item
        if isinstance(item, DbItem):
            self.filters = {}
            self.sort = []
            self._reset_search()
            self.reset_paging()
            self.load_item(item.item_name, item.item_type, should_focus=True)

    def on_list_view_highlighted(self, event: ListView.Highlighted):
        if self._sidebar_rebuilding:
            return
        item = event.item
        if isinstance(item, DbItem):
            if item.item_name == self.current_item and item.item_type == self.current_type:
                # The highlight landing back on the item that's already
                # loaded is _rebuild_sidebar/_restore_workspace_session
                # putting it there, not the user navigating. Re-fetching
                # would be a wasted round trip (a real one, on Notion) and
                # would throw away the filters/sort below.
                return
            self.filters = {}
            self.sort = []
            self._reset_search()
            self.reset_paging()
            self.load_item(item.item_name, item.item_type, should_focus=False)
        elif isinstance(item, SidebarHeader):
            # Highlighting a header leaves the main view showing whatever
            # was last loaded - just refresh the footer so 'Add' reflects
            # this section's create capability.
            self.refresh_bindings()

    def load_item(self, name, item_type, should_focus=False):
        saved_coord = None
        if self.current_item == name:
            try:
                saved_coord = self.query_one("#data-table").cursor_coordinate
            except:
                pass

        self.current_item = name
        self.current_type = item_type
        table_widget = self.query_one("#data-table", DataTable)
        sql_widget = self.query_one("#sql-view", Static)
        diag_widget = self.query_one("#diagram-view", DiagramView)
        switcher = self.query_one(ContentSwitcher)
        sidebar = self.query_one("#sidebar")
        
        if self.mode == "diagram":
            sidebar.display = False
            switcher.current = "diagram-view"
            diag_widget.refresh_diagram()
            if should_focus:
                diag_widget.focus()
        elif self.mode in ["view", "schema"] or item_type == "plugin":
            sidebar.display = True
            switcher.current = "data-table"
            # A full rebuild is about to replace this table's rows - commit
            # any not-yet-synced local reorder now rather than let it be
            # silently discarded (or, worse, later replayed by a stale
            # debounce against whatever ends up loaded next).
            self._flush_row_order_sync()

            if item_type == "plugin":
                table_widget.clear(columns=True)
                self.row_keys = {}
                self.raw_docs = {}
                self.row_values = {}
                self.columns_by_name = {}
                self.auto_column_widths = {}
                self.row_order = []
                self.rendered_rows = {}
                self.rows_editable = False
                self.page_has_more = False
                columns, rows = self.get_plugin_data(name)
                for i, col in enumerate(columns):
                    color = COLORS[i % len(COLORS)]
                    table_widget.add_column(f"[{color}]{col}[/]", key=col)
                for i, row in enumerate(rows):
                    table_widget.add_row(*row, key=str(i))
            elif self.mode == "view":
                # Seed from this table/view's persisted sort (ViewSettingsStore)
                # only if nothing's been explicitly set yet this session (e.g.
                # just switched to this item) - action_sort_column already
                # keeps self.sort authoritative for the rest of the session,
                # including an explicit clear, so this must not clobber that.
                if not self.sort and self.provider.capabilities.sort_column and self.provider.is_sortable(item_type):
                    saved_settings = self.view_settings.get(name)
                    if saved_settings.sort_column:
                        self.sort = [(saved_settings.sort_column, saved_settings.sort_direction or "asc")]
                try:
                    page = self.provider.get_page(
                        name, item_type, self.filters, cursor=self.page_cursor,
                        page_size=self.page_size, sort=self.sort[0] if self.sort else None,
                    )
                except Exception as e:
                    self.notify(f"Failed to load '{name}': {e}", severity="error")
                    page = None

                # Only tear down the currently-displayed table once a new
                # page has actually loaded successfully - otherwise (e.g. a
                # filter value Notion's API rejects with a 400) the table is
                # left with its previous, internally-consistent columns/rows
                # intact rather than headerless, which used to crash any
                # subsequent column-scoped action (filter/sort/hide/...) on
                # an empty ordered_columns. See issue with 'f' on a Notion
                # select/status column filtered with a non-matching value.
                if page is not None:
                    table_widget.clear(columns=True)
                    self.row_keys = {}
                    self.raw_docs = {}
                    self.row_values = {}
                    self.row_order = []
                    self.rendered_rows = {}
                    self.page_has_more = page.has_more
                    self._next_cursor = page.next_cursor
                    self.rows_editable = bool(page.row_keys) and page.row_keys[0].value is not None

                    view_settings = self.view_settings.get(name)
                    display_columns, display_rows = apply_view_settings(page.columns, page.rows, view_settings)
                    column_widths = compute_column_widths(display_columns, display_rows, view_settings)
                    self.column_widths = column_widths
                    # What each column *would* be without its override, so
                    # ColumnMetadataTable can show "auto (25)". The old
                    # ColumnWidthScreen read this off column_widths, which
                    # already has the override folded in - so it reported
                    # the override back as the auto width.
                    self.auto_column_widths = compute_column_widths(
                        display_columns, display_rows, ViewSettings()
                    )
                    self.columns_by_name = {c.name: c for c in display_columns}
                    rendered_rows = truncate_rows(display_columns, display_rows, column_widths)
                    # Paint enum-valued cells in their option colors. Must
                    # follow truncation - see cell_render.stylize_row.
                    no_color = set(view_settings.no_color)
                    rendered_rows = [
                        stylize_row(display_columns, rendered, source, skip=no_color)
                        for rendered, source in zip(rendered_rows, display_rows)
                    ]

                    for i, col in enumerate(display_columns):
                        color = COLORS[i % len(COLORS)]
                        label = f"[{color}]{col.name}[/]"
                        if col.name in self.filters:
                            label = f"[reverse]{label} (F)[/]"
                        if self.sort and self.sort[0][0] == col.name:
                            label = f"{label} {'▲' if self.sort[0][1] == 'asc' else '▼'}"
                        table_widget.add_column(label, key=col.name, width=column_widths[col.name])

                    for i, (row, rendered_row, row_key) in enumerate(zip(display_rows, rendered_rows, page.row_keys)):
                        if row_key.value is None:
                            key_str = str(i)
                        elif isinstance(row_key.value, dict):
                            key_str = json.dumps(row_key.value, sort_keys=True)
                        else:
                            key_str = str(row_key.value)
                        self.row_keys[key_str] = row_key
                        self.row_values[key_str] = dict(zip((c.name for c in display_columns), row))
                        if page.raw_rows is not None:
                            self.raw_docs[key_str] = page.raw_rows[i]
                        self.row_order.append(key_str)
                        self.rendered_rows[key_str] = rendered_row
                        table_widget.add_row(*rendered_row, key=key_str)
            else:
                # Schema mode
                table_widget.clear(columns=True)
                self.row_keys = {}
                self.raw_docs = {}
                self.row_values = {}
                self.columns_by_name = {}
                self.auto_column_widths = {}
                self.row_order = []
                self.rendered_rows = {}
                self.rows_editable = False
                self.page_has_more = False
                schema_cols = self.provider.get_schema(name, item_type)
                columns = ["name", "type", "nullable", "default", "pk"]
                for i, col in enumerate(columns):
                    color = COLORS[i % len(COLORS)]
                    table_widget.add_column(f"[{color}]{col}[/]", key=col)
                rows = [
                    (c.name, c.type_name, "NOT NULL" if not c.nullable else "NULL", c.default or "", "PK" if c.primary_key else "")
                    for c in schema_cols
                ]
                table_widget.add_rows(rows)

            table_widget.cursor_type = self._cursor_type_for_select_mode() if self.mode == "view" else "cell"

            if should_focus:
                table_widget.focus()
            if saved_coord:
                try:
                    table_widget.move_cursor(row=saved_coord.row, column=saved_coord.column)
                except:
                    pass
        else:
            # SQL mode
            sidebar.display = True
            switcher.current = "sql-view"
            sql_text = self.provider.get_definition(name, item_type)
            sql_widget.update(sql_text)
            if should_focus:
                sql_widget.focus()
                
        if self.search_term and self.mode == "view":
            # load_item redraws every cell, so the highlight has to be laid
            # back down. Don't move the cursor: the reload is usually the
            # tail of an edit, and yanking the user back to match #1 would
            # undo where they had navigated to.
            self._refresh_search(move_cursor=False)
        self.update_title()
        self.refresh_bindings()

    def _cursor_type_for_select_mode(self):
        return self.CURSOR_TYPE_BY_SELECT_MODE[self.select_mode]

    def update_title(self):
        page_indicator = ""
        if self.mode == "view":
            page_num = len(self.page_history) + 1
            if page_num > 1 or self.page_has_more:
                page_indicator = f" [page {page_num}{'+' if self.page_has_more else ''}]"
        select_indicator = (
            f" [{self.select_mode.upper()}]" if self.mode == "view" and self.select_mode != "field" else ""
        )
        sort_indicator = ""
        if self.mode == "view" and self.sort:
            col_name, direction = self.sort[0]
            sort_indicator = f" [sort: {col_name} {'asc' if direction == 'asc' else 'desc'}]"
        search_indicator = ""
        if self.mode == "view" and self.search_term:
            if self.search_matches:
                search_indicator = f" [/{self.search_term} {self.search_index + 1}/{len(self.search_matches)}]"
            else:
                search_indicator = f" [/{self.search_term} no match]"
        self.title = (
            f"dbman - {self.current_item} ({self.mode.upper()})"
            f"{page_indicator}{select_indicator}{sort_indicator}{search_indicator}"
        )

    def action_switch_focus(self):
        if self.query_one("#sidebar").display:
            if self.focused.id == "sidebar-list":
                switcher = self.query_one(ContentSwitcher)
                if switcher.current == "data-table":
                    self.query_one("#data-table").focus()
                elif switcher.current == "sql-view":
                    self.query_one("#sql-view").focus()
                elif switcher.current == "diagram-view":
                    self.query_one("#diagram-view").focus()
            else:
                self.query_one("#sidebar-list").focus()
        else:
            # Sidebar is hidden (Diagram mode)
            # Default Textual focus cycling (Tab) will cycle through focusable widgets
            # Since we consumed Tab with this action, we should manually cycle if needed
            # or just do nothing and let Textual handle it if we remove the binding?
            # But the binding is global. 
            # If sidebar is hidden, we can just focus the next widget in the app.
            self.app.focused.screen.focus_next()

    def action_jump_section(self):
        sidebar_list = self.query_one("#sidebar-list", ListView)
        # No row highlighted yet (nothing selected, or a rebuild in flight):
        # start the search from before the first row rather than raising.
        current_idx = -1 if sidebar_list.index is None else sidebar_list.index
        found = False
        for i in range(current_idx + 1, len(sidebar_list.children)):
            if isinstance(sidebar_list.children[i], SidebarHeader):
                sidebar_list.index = min(i + 1, len(sidebar_list.children)-1)
                found = True
                break
        if not found:
            for i in range(len(sidebar_list.children)):
                if isinstance(sidebar_list.children[i], SidebarHeader):
                    sidebar_list.index = min(i + 1, len(sidebar_list.children)-1)
                    break

    def action_toggle_mode(self):
        if self.current_type == "plugin":
            self.notify("Plugins only have View mode", severity="error")
            return
        modes = ["view", "schema", "sql", "diagram"]
        if not self.provider.capabilities.diagram:
            modes = [m for m in modes if m != "diagram"]
        idx = modes.index(self.mode)
        self.mode = modes[(idx + 1) % len(modes)]
        self.refresh_bindings()
        self.refresh_sidebar()
        if self.current_item:
            self.load_item(self.current_item, self.current_type)

    def action_rotate_select_mode(self):
        """Rotate field -> row -> column -> field. Determines what the cursor
        selects (via DataTable's native cursor_type) and what mode-dependent
        action keys (starting with 'e') operate on. See issue #7."""
        if self.mode != "view":
            self.notify("Select mode only applies in View mode", severity="error")
            return
        if not isinstance(self.focused, DataTable):
            return
        idx = self.SELECT_MODES.index(self.select_mode)
        if self.search_term:
            # Rotating changes what '/' is searching - column mode looks at
            # headers, the other two at values - so drop the old marks
            # before the scope moves out from under them.
            self._paint_search(False)
        self.select_mode = self.SELECT_MODES[(idx + 1) % len(self.SELECT_MODES)]
        self.focused.cursor_type = self._cursor_type_for_select_mode()
        if self.search_term:
            self.search_index = 0
            self._refresh_search(move_cursor=False)
        self.update_title()
        self.refresh_bindings()

    def action_reorder_column(self, direction: int):
        """Shift+H/Shift+L (primary) or alt+left/alt+right (secondary,
        hidden from the footer) in column select mode: swap the selected
        column with its neighbor and persist the new order via
        ViewSettingsStore. H/L reuse the app's own h/l = left/right cursor
        mnemonic (shift = "move the column instead of the cursor"), which
        also sidesteps two dead ends: alt+left/right relies on macOS
        terminals sending the xterm modified-arrow CSI sequence, but they
        commonly send Option+Left/Right as the readline word-jump escape
        instead (Esc+b/Esc+f -> "alt+b"/"alt+f"), so it silently never fired
        for most users; and '<'/'>' (tried first) required Shift+,/Shift+.,
        which is easy to mis-key by visual identification of the unshifted
        comma/period glyphs. ctrl+left/right was avoided from the start
        since macOS reserves those for Mission Control space-switching."""
        if self.mode != "view" or self.select_mode != "column":
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        table_widget = self.focused
        coord = table_widget.cursor_coordinate
        columns = [c.key.value for c in table_widget.ordered_columns]
        idx = coord.column
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(columns):
            return
        columns[idx], columns[new_idx] = columns[new_idx], columns[idx]
        settings = self.view_settings.get(self.current_item)
        settings.order = columns
        self.view_settings.save(self.current_item, settings)
        self.load_item(self.current_item, self.current_type)
        self.query_one("#data-table", DataTable).move_cursor(row=coord.row, column=new_idx)

    # Quiet period after the last J/K before a settled burst of local moves
    # is sent to the provider as one batch - long enough that repeated
    # keystrokes coalesce into one network round trip, short enough that a
    # deliberate pause reads as "done reordering."
    _ROW_ORDER_SYNC_DELAY = 0.6

    def action_move_row(self, direction: int):
        """Shift+J/Shift+K in row select mode: move the selected row
        up/down. The visual move happens immediately and entirely locally
        (self.row_order/rendered_rows, no network call) so a burst of
        keystrokes never blocks on round trips - every provider call so far
        in this app runs synchronously on the UI thread, so waiting on one
        per keystroke is what made this feel unresponsive. Once the local
        order settles (_ROW_ORDER_SYNC_DELAY of no further moves), the
        rows that actually changed position are sent to
        provider.reorder_rows in a single batch call, in a background
        thread (see _schedule_row_order_sync/_sync_row_order). An earlier
        version replayed one provider.move_row call per keystroke instead;
        that broke under a fast burst because each step re-derived the
        moved row's neighbor from a live query, and Notion's query index
        measurably lagged behind its own very recent writes - reorder_rows
        sidesteps that by reassigning already-known order values in one
        pass instead. Only reachable when capabilities.reorder_row is True
        (currently just Notion), gated via ACTION_CONTEXTS."""
        if self.mode != "view" or self.select_mode != "row":
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        if not self.provider.capabilities.reorder_row:
            return
        table_widget = self.focused
        coord = table_widget.cursor_coordinate
        row_id_str = list(table_widget.rows.values())[coord.row].key.value
        if row_id_str not in self.row_order:
            return

        idx = self.row_order.index(row_id_str)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self.row_order):
            return  # already at the top/bottom locally - matches the
                     # provider's own no-op behavior at the real boundary

        if not self._row_order_dirty:
            # Baseline snapshot for this burst, taken before the first move
            # is applied - _sync_row_order diffs this against the settled
            # self.row_order to find exactly which rows moved.
            self._row_order_sync_target = (
                self.current_item, self.current_type, dict(self.row_keys), list(self.row_order)
            )
            self._row_order_dirty = True
        self.row_order[idx], self.row_order[new_idx] = self.row_order[new_idx], self.row_order[idx]

        table_widget.clear(columns=False)
        for key_str in self.row_order:
            table_widget.add_row(*self.rendered_rows[key_str], key=key_str)
        table_widget.move_cursor(row=new_idx, column=coord.column)

        self._schedule_row_order_sync()

    def _schedule_row_order_sync(self):
        if self._row_order_timer is not None:
            self._row_order_timer.stop()
        self._row_order_timer = self.set_timer(self._ROW_ORDER_SYNC_DELAY, self._sync_row_order)

    def _flush_row_order_sync(self):
        """Commit any pending local row-order moves right away instead of
        waiting out the debounce - called whenever the DataTable is about to
        be rebuilt out from under the pending moves (navigating away,
        quitting), so a reorder burst is never silently dropped."""
        if self._row_order_timer is not None:
            self._row_order_timer.stop()
            self._row_order_timer = None
        if self._row_order_dirty:
            self._sync_row_order(blocking=True)

    @staticmethod
    def _touched_row_order_slice(baseline: list, settled: list) -> list:
        """The minimal contiguous run of key-strings (by settled-order
        index) that differs from baseline. A series of adjacent-swap moves
        only ever perturbs a contiguous window around the row's path, so
        this bounds the batch to just the rows that need a new order value
        instead of the whole (possibly much larger) loaded page."""
        if baseline == settled:
            return []
        lo = next(i for i in range(len(settled)) if settled[i] != baseline[i])
        hi = next(i for i in range(len(settled) - 1, -1, -1) if settled[i] != baseline[i])
        return settled[lo:hi + 1]

    def _sync_row_order(self, blocking: bool = False):
        if not self._row_order_dirty:
            return
        self._row_order_dirty = False
        name, item_type, row_keys, baseline_order = self._row_order_sync_target
        self._row_order_sync_target = None

        touched = self._touched_row_order_slice(baseline_order, self.row_order)
        ordered_keys = [row_keys[k] for k in touched if k in row_keys]
        if len(ordered_keys) < 2:
            return

        def call_provider():
            try:
                self.provider.reorder_rows(name, item_type, ordered_keys)
            except Exception as e:
                return e
            return None

        if blocking:
            # Used from _flush_row_order_sync when the table is about to be
            # torn down anyway (navigating away, quitting) - nothing left to
            # reconcile visually, so just persist and report failures.
            error = call_provider()
            if error:
                self.notify(f"Row order sync failed: {error}", severity="error")
            return

        def worker():
            error = call_provider()
            def finish():
                if error:
                    self.notify(f"Row order sync failed: {error}", severity="error")
                elif self.current_item == name and self.current_type == item_type:
                    # Reconcile with the backend's authoritative order - also
                    # self-heals if anything drifted (e.g. a stale row_key
                    # from a concurrent external edit).
                    self.load_item(name, item_type)
            self.call_from_thread(finish)
        self.run_worker(worker, thread=True)

    def action_edit_column(self):
        """'e' in column select mode: open the selected column's own
        settings as a virtual table. The column-mode half of `e`'s
        "edit the selected thing" promise - see ColumnMetadataTable."""
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value
        column = self.columns_by_name.get(column_name)
        if column is None:
            self.notify(f"No metadata for column '{column_name}'", severity="error")
            return

        table = ColumnMetadataTable(
            column, self.current_item, self.view_settings,
            visible_column_count=len(self.focused.ordered_columns),
            auto_width=self.auto_column_widths.get(column_name),
        )

        def done(changed):
            if not changed:
                return
            # Sort lives on the table, not the column, and self.sort is
            # authoritative for the session - so re-seed it from what the
            # metadata table just wrote rather than letting the two drift.
            settings = self.view_settings.get(self.current_item)
            self.sort = (
                [(settings.sort_column, settings.sort_direction or "asc")]
                if settings.sort_column else []
            )
            self.reset_paging()
            self.load_item(self.current_item, self.current_type)

        self.push_screen(VirtualTableScreen(table), done)

    def action_hide_column(self):
        """'z' in column select mode: hide the column-mode-selected column
        from the rendered DataTable (it stays in the underlying RowPage/model
        and is unaffected in any other select mode). Persisted per table/view
        via ViewSettingsStore. See issue #2."""
        if self.mode != "view":
            self.notify("Hide column only allowed in View mode", severity="error")
            return
        if self.select_mode != "column":
            self.notify("Hide column only allowed in column select mode (press 's' to rotate)", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        if len(self.focused.ordered_columns) <= 1:
            self.notify("Cannot hide the last visible column", severity="error")
            return
        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value

        settings = self.view_settings.get(self.current_item)
        if column_name not in settings.hidden:
            settings.hidden.append(column_name)
        self.view_settings.save(self.current_item, settings)
        self.notify(f"Hid column '{column_name}'")
        self.load_item(self.current_item, self.current_type)

    def action_unhide_column(self):
        """'u', column-select-mode-scoped like hiding: pick a previously
        hidden column to bring back. See issue #2."""
        if self.mode != "view" or not self.current_item:
            self.notify("Unhide only allowed in View mode", severity="error")
            return
        if self.select_mode != "column":
            self.notify("Unhide only allowed in column select mode (press 's' to rotate)", severity="error")
            return

        settings = self.view_settings.get(self.current_item)
        if not settings.hidden:
            self.notify("No hidden columns")
            return

        def do_unhide(column_name):
            if column_name:
                settings = self.view_settings.get(self.current_item)
                if column_name in settings.hidden:
                    settings.hidden.remove(column_name)
                    self.view_settings.save(self.current_item, settings)
                    self.notify(f"Unhid column '{column_name}'")
                    self.load_item(self.current_item, self.current_type)

        self.push_screen(UnhideColumnScreen(list(settings.hidden)), do_unhide)

    # -- '/' search over the loaded page -------------------------------------

    def _reset_search(self):
        self.search_term = None
        self.search_cells = []
        self.search_matches = []
        self.search_index = 0

    def _search_scope(self) -> str:
        """What '/' looks at in the current select mode. Column mode
        searches headers because a column's identity is its name; the other
        two search values, and differ only in what the cursor lands on."""
        return "column names" if self.select_mode == "column" else "cell values"

    def _compute_search(self):
        """(every matching cell, the cells the cursor steps through).

        Matching runs against self.row_values - the *untruncated* values -
        not the rendered cells, so text that got shortened to ".." is still
        findable. The cursor can therefore land on a cell whose match isn't
        legible; that's deliberate, the cell is selected and 'e' shows it
        in full, and the alternative is a search that can't find what's
        demonstrably there."""
        table = self.query_one("#data-table", DataTable)
        if not self.search_term or not table.columns:
            return [], []
        names = [c.key.value for c in table.ordered_columns]

        if self.select_mode == "column":
            row = table.cursor_coordinate.row
            hits = [Coordinate(row, i) for i, n in enumerate(names) if cell_matches(n, self.search_term)]
            return [], hits  # the column cursor is its own highlight

        cells = []
        for ri, row_key in enumerate(self.row_order):
            values = self.row_values.get(row_key, {})
            for ci, name in enumerate(names):
                if cell_matches(values.get(name), self.search_term):
                    cells.append(Coordinate(ri, ci))
        if self.select_mode == "row":
            # One stop per row, so n steps row to row rather than crawling
            # across every matching cell within one.
            seen, steps = set(), []
            for coord in cells:
                if coord.row not in seen:
                    seen.add(coord.row)
                    steps.append(coord)
            return cells, steps
        return cells, list(cells)

    def _paint_search(self, on: bool):
        """Mark (or restore) the matching cells. Restoring reads back from
        self.rendered_rows, which holds what load_item actually drew -
        including option colors, which the highlight sits on top of rather
        than replacing."""
        table = self.query_one("#data-table", DataTable)
        for coord in self.search_cells:
            if coord.row >= len(self.row_order):
                continue
            original = self.rendered_rows.get(self.row_order[coord.row])
            if not original or coord.column >= len(original):
                continue
            value = original[coord.column]
            if on:
                text = value if isinstance(value, Text) else Text("" if value is None else str(value))
                value = Text(text.plain, style=f"{text.style} reverse".strip())
            try:
                table.update_cell_at(coord, value)
            except Exception:
                pass  # table rebuilt underneath us; the next load_item repaints

    def _refresh_search(self, move_cursor=True):
        self.search_cells, self.search_matches = self._compute_search()
        self._paint_search(True)
        if self.search_matches:
            self.search_index = min(self.search_index, len(self.search_matches) - 1)
            if move_cursor:
                self._goto_match(self.search_index)
        self.update_title()
        self.refresh_bindings()

    def _goto_match(self, index):
        table = self.query_one("#data-table", DataTable)
        coord = self.search_matches[index]
        # In column mode the row is whatever the cursor was already on -
        # searching for a column shouldn't also move you down the table.
        row = table.cursor_coordinate.row if self.select_mode == "column" else coord.row
        table.cursor_coordinate = Coordinate(row, coord.column)
        self.search_index = index

    def _sidebar_match_rows(self):
        """Indices into the sidebar's rows whose object name matches the
        current sidebar search. Headers are never matched."""
        term = self.sidebar_search_term
        if not term:
            return []
        return [
            i for i, row in enumerate(self._sidebar_rows)
            if row is not None and cell_matches(row[0], term)
        ]

    def _goto_sidebar_row(self, index):
        # Setting .index emits Highlighted, which loads the item - the same
        # thing j/k does, so a search hit opens the object like navigating to it.
        self.query_one("#sidebar-list", ListView).index = index

    def _search_sidebar(self):
        def run(term):
            if term is None:
                return
            self.sidebar_search_term = term.strip() or None
            if self.sidebar_search_term is None:
                self.refresh_bindings()
                return
            matches = self._sidebar_match_rows()
            if not matches:
                self.notify(f"No table/view/plugin matches '{term}'", severity="warning")
                self.refresh_bindings()
                return
            # Start from the highlighted row rather than the top, so the
            # first hit is the nearest one below the cursor (wrapping).
            current = self.query_one("#sidebar-list", ListView).index
            current = -1 if current is None else current
            target = next((i for i in matches if i >= current), matches[0])
            self._goto_sidebar_row(target)
            self.notify(f"{len(matches)} match(es) - n / N to step")
            self.refresh_bindings()

        self.push_screen(SearchScreen("objects by name", self.sidebar_search_term or ""), run)

    def _step_sidebar_search(self, delta):
        matches = self._sidebar_match_rows()
        if not matches:
            self.notify(f"No table/view/plugin matches '{self.sidebar_search_term}'", severity="warning")
            return
        current = self.query_one("#sidebar-list", ListView).index
        current = -1 if current is None else current
        if delta > 0:
            later = [i for i in matches if i > current]
            target, wrapped = (later[0], False) if later else (matches[0], True)
        else:
            earlier = [i for i in matches if i < current]
            target, wrapped = (earlier[-1], False) if earlier else (matches[-1], True)
        self._goto_sidebar_row(target)
        if wrapped:
            self.notify("Wrapped" + (" to top" if delta > 0 else " to bottom"))

    def action_search(self):
        if _sidebar_has_focus(self):
            self._search_sidebar()
            return
        if self.mode != "view":
            self.notify("Search only allowed in View mode", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return

        def run(term):
            if term is None:
                return
            self._paint_search(False)
            self.search_term = term.strip() or None
            self.search_index = 0
            if self.search_term is None:
                self._reset_search()
                self.update_title()
                self.refresh_bindings()
                return
            self._refresh_search()
            if not self.search_matches:
                self.notify(f"No match for '{term}' in {self._search_scope()}", severity="warning")
            else:
                self.notify(f"{len(self.search_matches)} match(es) - n / N to step")

        self.push_screen(SearchScreen(self._search_scope(), self.search_term or ""), run)

    def _step_search(self, delta):
        if not self.search_matches:
            return
        index = self.search_index + delta
        wrapped = not (0 <= index < len(self.search_matches))
        self._goto_match(index % len(self.search_matches))
        self.update_title()
        if wrapped:
            self.notify("Wrapped" + (" to top" if delta > 0 else " to bottom"))

    def action_search_next(self):
        if _sidebar_has_focus(self):
            self._step_sidebar_search(1)
        else:
            self._step_search(1)

    def action_search_prev(self):
        if _sidebar_has_focus(self):
            self._step_sidebar_search(-1)
        else:
            self._step_search(-1)

    def action_filter_column(self):
        if self.mode != "view":
            self.notify("Filtering only allowed in View mode", severity="error")
            return
        if self.select_mode == "row":
            # The cursor keeps a live column coordinate in every select
            # mode, but row mode is the one where nothing on screen says
            # *which* column that is - filtering an invisible target is
            # worse than asking for a rotation. Field and column mode both
            # show the user exactly what they're about to filter.
            self.notify("Filtering needs a visible column - press 's' for field or column mode", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        if not self.provider.is_filterable(self.current_type):
            self.notify("Filtering not available for this item", severity="error")
            return
        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value
        current_filter = self.filters.get(column_name, "")
        def apply_filter(val):
            # None is cancel; anything falsy ("" from the text box, [] from
            # the picker) clears this column's filter. An empty list must
            # never reach a provider - see get_page's contract in base.py.
            if val is not None:
                if not val:
                    self.filters.pop(column_name, None)
                else:
                    self.filters[column_name] = val
                self.reset_paging()
                self.load_item(self.current_item, self.current_type)
        column = self.columns_by_name.get(column_name)
        options = tuple(getattr(column, "options", ()) or ())
        if options:
            # An enum-like column filters by picking from its declared
            # options - the same table the cell editor uses, just multi-pick
            # and with the two empty/not-empty conditions appended as
            # pseudo-options (their sentinels can't collide with a real
            # option name - see providers/base.py).
            picker_options = list(options) + [
                ColumnOption("(empty)", None), ColumnOption("(not empty)", None),
            ]
            label_to_value = {"(empty)": FILTER_EMPTY, "(not empty)": FILTER_NOT_EMPTY}
            value_to_label = {v: k for k, v in label_to_value.items()}
            current = [value_to_label.get(v, v) for v in (current_filter or [])] \
                if isinstance(current_filter, list) else []
            self.push_screen(
                VirtualTableScreen(
                    OptionPickerTable(f"Filter {column_name}", picker_options, current, multi=True)
                ),
                lambda picked: apply_filter(
                    None if picked is None else [label_to_value.get(p, p) for p in picked]
                ),
            )
            return
        self.push_screen(FilterColumnScreen(column_name, current_filter), apply_filter)

    def action_sort_column(self):
        if self.mode != "view":
            self.notify("Sorting only allowed in View mode", severity="error")
            return
        if self.select_mode != "column":
            self.notify("Sorting only allowed in column select mode (press 's' to rotate)", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        if not self.provider.capabilities.sort_column or not self.provider.is_sortable(self.current_type):
            self.notify("Sorting not available for this item", severity="error")
            return
        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value
        current = self.sort[0] if self.sort else None
        if current is not None and current[0] == column_name:
            # Single column, cycling asc -> desc -> none. self.sort is a
            # list so multi-column later is a keybinding change, not a
            # data-shape migration.
            if current[1] == "asc":
                self.sort = [(column_name, "desc")]
            else:
                self.sort = []
        else:
            self.sort = [(column_name, "asc")]

        settings = self.view_settings.get(self.current_item)
        settings.sort_column, settings.sort_direction = (self.sort[0] if self.sort else (None, None))
        self.view_settings.save(self.current_item, settings)

        self.reset_paging()
        self.load_item(self.current_item, self.current_type)
        if self.sort:
            direction = "ascending" if self.sort[0][1] == "asc" else "descending"
            self.notify(f"Sorting '{column_name}' {direction}")
        else:
            self.notify(f"Cleared sort on '{column_name}'")

    def action_clear_filters(self):
        if self.mode != "view":
            self.notify("Filtering only allowed in View mode", severity="error")
            return
        # No select-mode guard at all, unlike its 'f' counterpart: clearing
        # every filter doesn't name a column, so there's nothing for a
        # rotation to disambiguate. Nothing to clear is handled upstream by
        # _clear_filters_ctx returning None, which greys the binding out
        # *and* stops the keypress reaching here - so there's deliberately
        # no "no filters" notify, matching Z/unhide's silent-when-inert.
        self.filters = {}
        self.reset_paging()
        if self.current_item:
            self.load_item(self.current_item, self.current_type)
        self.notify("All filters cleared")

    def action_next_page(self):
        if self.mode != "view" or not self.current_item:
            return
        if not self.page_has_more:
            self.notify("No more rows")
            return
        self.page_history.append(self.page_cursor)
        self.page_cursor = self._next_cursor
        self.load_item(self.current_item, self.current_type)

    def action_prev_page(self):
        if self.mode != "view" or not self.current_item:
            return
        if not self.page_history:
            self.notify("Already at first page")
            return
        self.page_cursor = self.page_history.pop()
        self.load_item(self.current_item, self.current_type)

    def action_delete_item(self):
        if self.current_type == "plugin":
            self.notify("Cannot delete Plugins", severity="error")
            return
        if (
            self.mode == "view"
            and isinstance(self.focused, DataTable)
            and self.select_mode == "row"
            and self.current_item
            and self.rows_editable
        ):
            self._delete_row()
            return
        if not self.provider.capabilities.delete_item:
            self.notify("Deleting is not available for this provider", severity="error")
            return
        sidebar_list = self.query_one("#sidebar-list", ListView)
        if self.focused and self.focused.id == "sidebar-list":
            if sidebar_list.highlighted_child and isinstance(sidebar_list.highlighted_child, DbItem):
                item = sidebar_list.highlighted_child
                name = item.item_name
                item_type = item.item_type
            else:
                return
        elif self.current_item:
            name = self.current_item
            item_type = self.current_type
        else:
            return
        def on_confirm(do_delete):
            if do_delete:
                try:
                    self.provider.delete_item(name, item_type)
                    self.notify(f"{item_type.capitalize()} '{name}' deleted")
                    self.refresh_sidebar()
                except Exception as e:
                    self.notify(f"Delete failed: {e}", severity="error")
        msg = f"Delete {item_type} '{name}'?"
        self.push_screen(ConfirmScreen(msg, "Delete"), on_confirm)

    def _delete_row(self):
        if not self.provider.capabilities.delete_row:
            self.notify("Row delete not supported for this provider — switch to field mode", severity="error")
            return
        coord = self.focused.cursor_coordinate
        row_id_str = list(self.focused.rows.values())[coord.row].key.value
        row_key = self.row_keys.get(row_id_str)
        if row_key is None or row_key.value is None:
            self.notify("Cannot delete this row", severity="error")
            return
        name, item_type = self.current_item, self.current_type
        # Best-effort label for the confirm dialog: the first visible
        # column's value (title/PK-ish, since providers order that first).
        row_vals = self.row_values.get(row_id_str, {})
        label = next(iter(row_vals.values()), None) if row_vals else None
        msg = f"Delete row '{label}'?" if label else "Delete this row?"

        def on_confirm(do_delete):
            if do_delete:
                try:
                    self.provider.delete_row(name, item_type, row_key)
                    self.notify("Row deleted")
                    self.load_item(self.current_item, self.current_type)
                except Exception as e:
                    self.notify(f"Delete failed: {e}", severity="error")
        self.push_screen(ConfirmScreen(msg, "Delete"), on_confirm)

    def action_cursor_down(self):
        if self.focused:
            self.focused.action_cursor_down()

    def action_cursor_up(self):
        if self.focused:
            self.focused.action_cursor_up()

    def action_cursor_left(self):
        if isinstance(self.focused, DataTable):
            self.focused.action_cursor_left()

    def action_cursor_right(self):
        if isinstance(self.focused, DataTable):
            self.focused.action_cursor_right()

    def action_page_up(self):
        if self.focused:
            self.focused.action_page_up()

    def action_page_down(self):
        if self.focused:
            self.focused.action_page_down()

    def action_scroll_home(self):
        if self.focused:
            self.focused.action_scroll_home()

    def action_scroll_end(self):
        if self.focused:
            self.focused.action_scroll_end()

    def action_toggle_shortcuts(self):
        if isinstance(self.screen, ShortcutsScreen):
            self.pop_screen()
        else:
            self.push_screen(ShortcutsScreen())

    def action_toggle_version(self):
        if isinstance(self.screen, VersionScreen):
            self.pop_screen()
        else:
            self.push_screen(VersionScreen())

    def action_edit_cell(self):
        if self.mode == "sql":
            self.action_edit_sql()
            return

        if self.current_type == "plugin" and self.current_item == "lookup" and self.lookup_plugin:
            coord = self.focused.cursor_coordinate
            row_vals = self.focused.get_row_at(coord.row)
            table, fk_col, rel_table, rel_key, current_lookup = row_vals

            cols = [c.name for c in self.provider.get_schema(rel_table, "table")]
            def save_lookup(val):
                if val:
                    self.lookup_plugin.save_config(table, fk_col, rel_table, rel_key, val)
                    self.load_item("lookup", "plugin")
            self.push_screen(LookupConfigScreen(table, fk_col, rel_table, rel_key, cols), save_lookup)
            return

        if self.mode != "view":
            self.notify(f"Editing only allowed in View mode", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return

        if self.select_mode == "row":
            if self.provider.capabilities.whole_row_edit:
                self.action_edit_document()
            elif self.provider.capabilities.open_in_browser:
                self.action_open_in_browser()
            else:
                self.notify("Row edit not supported for this provider — switch to field mode", severity="error")
            return
        if self.select_mode == "column":
            self.action_edit_column()
            return

        if self.current_type == "view":
            self.notify("Cannot edit Views directly (press 'e' in SQL mode to edit View SQL)", severity="error")
            return
        if not self.rows_editable:
            self.notify("Cannot edit tables without identifiable rows (yet)", severity="error")
            return

        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value
        row_id_str = list(self.focused.rows.values())[coord.row].key.value
        row_key = self.row_keys.get(row_id_str)
        # Prefer the untruncated value: the DataTable cell may hold a
        # display-only ".."-truncated string (see view_settings.truncate_rows).
        current_value = self.row_values.get(row_id_str, {}).get(column_name, self.focused.get_cell_at(coord))

        if row_key is None or row_key.value is None:
            self.notify("Cannot edit tables without identifiable rows (yet)", severity="error")
            return

        if self.provider.capabilities.whole_row_edit and isinstance(current_value, (dict, list)):
            self.action_edit_document()
            return

        col = self.columns_by_name.get(column_name)
        if col is not None and col.read_only:
            # Fail here rather than round-tripping to the provider just to
            # get its ValueError back: the column's own schema already says
            # this is a computed/system field (Notion formula, rollup,
            # relation, unique_id, ...).
            self.notify(f"Column '{column_name}' is read-only", severity="error")
            return

        if col is not None and col.options:
            def perform_choice_update(new_value):
                # No str->int/float coercion here, unlike perform_update
                # below: an option is a name, even one that looks numeric.
                if new_value is None:
                    return
                try:
                    self.provider.update_cell(
                        self.current_item, self.current_type, row_key, column_name, new_value
                    )
                    self.notify("Updated")
                    self.load_item(self.current_item, self.current_type)
                except Exception as e:
                    self.notify(f"Update failed: {e}", severity="error")

            if col.multi_value:
                current = (
                    list(current_value) if isinstance(current_value, (list, tuple))
                    else [p.strip() for p in str(current_value or "").split(",") if p.strip()]
                )
            else:
                current = [current_value] if current_value not in (None, "") else []
            self.push_screen(
                VirtualTableScreen(
                    OptionPickerTable(f"Set {column_name}", col.options, current, multi=col.multi_value)
                ),
                perform_choice_update,
            )
            return

        lookup_conf = self.lookup_plugin.get_lookup_config(self.current_item, column_name) if self.lookup_plugin else None
        if lookup_conf:
            rel_table, rel_key, display_col = lookup_conf
            options = self.provider.get_lookup_options(rel_table, rel_key, display_col)

            def perform_lookup_update(new_val):
                if new_val is not None:
                    try:
                        self.provider.update_cell(self.current_item, self.current_type, row_key, column_name, new_val)
                        self.notify("Updated")
                        self.load_item(self.current_item, self.current_type)
                    except Exception as e:
                        self.notify(f"Update failed: {e}", severity="error")
            self.push_screen(LookupSelectScreen(f"Select {column_name}", options, current_value), perform_lookup_update)
            return

        def perform_update(new_value):
            if new_value is not None:
                typed_value = new_value
                if new_value.strip() == "":
                    typed_value = None
                else:
                    try:
                        if "." in new_value: typed_value = float(new_value)
                        else: typed_value = int(new_value)
                    except ValueError: pass
                try:
                    self.provider.update_cell(self.current_item, self.current_type, row_key, column_name, typed_value)
                    self.notify("Updated")
                    self.load_item(self.current_item, self.current_type)
                except Exception as e:
                    self.notify(f"Update failed: {e}", severity="error")
        self.push_screen(EditCellScreen(current_value), perform_update)

    def action_edit_document(self):
        if self.mode != "view" or not self.provider.capabilities.whole_row_edit:
            self.notify("Whole-document editing is not available for this provider", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return

        coord = self.focused.cursor_coordinate
        row_id_str = list(self.focused.rows.values())[coord.row].key.value
        row_key = self.row_keys.get(row_id_str)
        raw_doc = self.raw_docs.get(row_id_str)
        if row_key is None or raw_doc is None:
            self.notify("Cannot edit this row", severity="error")
            return
        self._open_document_editor(row_key, raw_doc)

    def action_open_in_browser(self):
        if self.mode != "view" or not self.provider.capabilities.open_in_browser:
            self.notify("Opening in browser is not available for this provider", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return

        coord = self.focused.cursor_coordinate
        row_id_str = list(self.focused.rows.values())[coord.row].key.value
        row_key = self.row_keys.get(row_id_str)
        if row_key is None or row_key.value is None:
            self.notify("Cannot open this row", severity="error")
            return
        try:
            url = self.provider.get_row_url(self.current_item, self.current_type, row_key)
            webbrowser.open(url)
            self.notify("Opened in browser")
        except Exception as e:
            self.notify(f"Could not open in browser: {e}", severity="error")

    def action_open_row(self):
        """space in row select mode: open the selected row where its
        provider says it leads (Provider.open_row) - a url in the browser,
        or, for a dbman.sqlite `connections` row, that connection."""
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        coord = self.focused.cursor_coordinate
        row_id_str = list(self.focused.rows.values())[coord.row].key.value
        row_key = self.row_keys.get(row_id_str)
        if row_key is None or row_key.value is None:
            self.notify("Cannot open this row", severity="error")
            return
        try:
            target = self.provider.open_row(self.current_item, self.current_type, row_key)
        except Exception as e:
            self.notify(f"Cannot open this row: {e}", severity="error")
            return
        if target.kind == "url":
            webbrowser.open(target.value)
            self.notify("Opened in browser")
        elif target.kind == "connection":
            if self.workspace is None:
                self.notify("No saved connections in this session", severity="error")
            elif target.value == self.workspace_name:
                self.notify(f"Already connected to '{target.value}'")
            else:
                # The same path as picking it from 'c', including leaving
                # the app untouched if the connect fails.
                self._on_switch_connection_selected(target.value)

    def _open_document_editor(self, row_key, raw_doc):
        """Shared by action_edit_document (existing row) and _add_row
        (freshly-created row, opened immediately so a freeform document is
        filled in right away)."""
        def save_document(new_json_text):
            if new_json_text:
                try:
                    self.provider.update_row_json(self.current_item, self.current_type, row_key, new_json_text)
                    self.notify("Document updated")
                    self.load_item(self.current_item, self.current_type)
                except Exception as e:
                    self.notify(f"Update failed: {e}", severity="error")

        doc_id = row_key.value.get("_id") if isinstance(row_key.value, dict) else row_key.value
        self.push_screen(
            EditTextScreen(f"Edit Document: {doc_id}", json.dumps(raw_doc, indent=2), language="json"),
            save_document,
        )

    def action_add(self):
        """'a': context-sensitive "create new thing". On a Table, add a new
        row/document and put the cursor on it (CouchDB also opens the
        whole-document editor) — only where a provider can create a blank
        row without a schema-aware form (capabilities.add_row; a SQLite
        table with a NOT NULL column lacking a default refuses, deferred to
        a future dynamic-forms layer). On a View, create a new view (formerly the
        standalone 'v' key — folded in here since it's the same "add a new
        thing" gesture, just for a different item type). See issue #10.

        On a section header (TABLES/VIEWS), creates a new Table/View for
        that section via a raw-SQL text prompt — the same mechanism as
        editing SQL on an existing View, just targeting a new object. This
        is the only way to add a Table/View to an empty section, since
        there's no existing item there to select first."""
        header = _highlighted_sidebar_header(self)
        if header is not None:
            if header.section_type == "table":
                self._add_table()
            elif header.section_type == "view":
                self._add_view()
            else:
                self.notify("Add only allowed on a Table or View", severity="error")
            return
        if self.mode != "view" or not self.current_item:
            self.notify("Add only allowed in View mode, on a Table or View", severity="error")
            return
        if self.current_type == "table":
            self._add_row()
        elif self.current_type == "view":
            self._add_view()
        else:
            self.notify("Add only allowed on a Table or View", severity="error")

    def _add_table(self):
        if not self.provider.capabilities.create_table:
            self.notify("Creating tables is not available for this provider", severity="error")
            return
        default_sql = self.provider.default_table_template()
        def execute_create(new_sql):
            if new_sql:
                try:
                    self.provider.create_table_definition(new_sql)
                    self.notify("Table Created")
                    self.refresh_sidebar()
                except Exception as e:
                    self.notify(f"Creation failed: {e}", severity="error")
        language = self.provider.definition_language("table")
        self.push_screen(EditTextScreen("Create New Table", default_sql, language=language), execute_create)

    def _add_row(self):
        if not self.provider.capabilities.add_row:
            self.notify("Adding rows is not available for this provider yet", severity="error")
            return
        try:
            row_key = self.provider.add_row(self.current_item, self.current_type)
        except Exception as e:
            self.notify(f"Add failed: {e}", severity="error")
            return
        self.notify("Row added")
        self.load_item(self.current_item, self.current_type)
        if isinstance(row_key.value, dict):
            key_str = json.dumps(row_key.value, sort_keys=True)
        else:
            key_str = str(row_key.value)
        # A blank row is only useful once filled in, so land on it - it may
        # sort anywhere, or not be on this page at all, in which case the
        # cursor stays put.
        if key_str in self.row_order:
            table_widget = self.query_one("#data-table", DataTable)
            table_widget.move_cursor(row=self.row_order.index(key_str))
        raw_doc = self.raw_docs.get(key_str)
        if raw_doc is not None:
            self._open_document_editor(row_key, raw_doc)

    def _add_view(self):
        if not self.provider.capabilities.create_definition:
            self.notify("Creating views is not available for this provider", severity="error")
            return
        default_sql = self.provider.default_definition_template()
        def execute_create(new_sql):
            if new_sql:
                try:
                    self.provider.create_view(new_sql)
                    self.notify("View Created")
                    self.refresh_sidebar()
                except Exception as e:
                    self.notify(f"Creation failed: {e}", severity="error")
        language = self.provider.definition_language("view")
        self.push_screen(EditTextScreen("Create New View", default_sql, language=language), execute_create)

    def action_edit_sql(self):
        if self.current_type != "view":
            self.notify("Can only edit SQL of Views", severity="error")
            return
        if not self.provider.capabilities.create_definition:
            self.notify("Editing definitions is not available for this provider", severity="error")
            return

        current_sql = self.provider.get_definition(self.current_item, self.current_type)

        def execute_sql(new_sql):
            if new_sql:
                try:
                    self.provider.update_view_definition(self.current_item, new_sql)
                    self.notify("SQL Executed Successfully")
                    self.refresh_sidebar()
                    self.load_item(self.current_item, self.current_type)
                except Exception as e:
                    self.notify(f"SQL Error: {e}", severity="error")
        
        language = self.provider.definition_language(self.current_type)
        self.push_screen(EditTextScreen(f"Edit View: {self.current_item}", current_sql, language=language), execute_sql)

    def action_export_csv(self):
        if not self.current_item:
            return
        
        default_filename = f"{self.current_item}.csv"
        
        def do_export(filename):
            if filename:
                try:
                    if self.current_type == "plugin":
                        columns, rows = self.get_plugin_data(self.current_item)
                    else:
                        page = self.provider.get_page(
                            self.current_item, self.current_type, self.filters, cursor=None,
                            page_size=None, sort=self.sort[0] if self.sort else None,
                        )
                        columns = [c.name for c in page.columns]
                        rows = page.rows
                    with open(filename, 'w', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow(columns)
                        writer.writerows(rows)
                    self.notify(f"Exported to {filename}")
                except Exception as e:
                    self.notify(f"Export failed: {e}", severity="error")
                    
        self.push_screen(ExportCsvScreen(default_filename), do_export)

    def action_truncate_column(self):
        if self.mode != "view":
            self.notify("Truncate only allowed in View mode", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        if self.current_type == "view":
            self.notify("Cannot truncate Views directly", severity="error")
            return
        if not self.provider.capabilities.truncate_column:
            self.notify("Truncate is not available for this provider", severity="error")
            return
        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value

        try:
            max_len = self.provider.get_max_length(self.current_item, column_name)
            suggested = 50 if max_len > 50 else max_len
        except Exception as e:
            self.notify(f"Error checking column: {e}", severity="error")
            return

        def perform_truncate(target_len_str):
            if target_len_str is not None:
                try:
                    target_len = int(target_len_str)
                except ValueError:
                    self.notify("Invalid length", severity="error")
                    return
                def do_it(confirm):
                    if confirm:
                        try:
                            self.provider.truncate_column(self.current_item, column_name, target_len)
                            self.notify(f"Truncated column to {target_len} chars")
                            self.load_item(self.current_item, self.current_type)
                        except Exception as e:
                            self.notify(f"Truncate failed: {e}", severity="error")
                self.push_screen(ConfirmScreen(f"Truncate ALL values in '{column_name}' to {target_len}?", "Apply"), do_it)
        self.push_screen(TruncateColumnScreen(self.current_item, column_name, max_len, suggested, self.provider), perform_truncate)

def _build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="dbman",
        description="A vim-like terminal database browser.",
    )
    parser.add_argument(
        "connection", nargs="?", default=None,
        help="A sqlite path/URL, postgresql://, mysql://, couchdb://, or notion:// "
             "connection string, or the name of a previously-saved connection. "
             "Omit to reconnect to the last-used connection.",
    )
    parser.add_argument(
        "-n", "--name",
        help="Friendly name to save this connection under, e.g. 'couch' or 'notion' "
             "(so it can later be reopened with 'dbman <name>'). Only applies when "
             "CONNECTION is a fresh url/path, not when reconnecting by an existing name.",
    )
    parser.add_argument(
        "--version", action="version", version=f"dbman {__version__}",
    )
    return parser


if __name__ == "__main__":
    parser = _build_arg_parser()
    args = parser.parse_args()
    if args.name and args.connection is None:
        parser.error("--name requires CONNECTION (nothing to name for a bare reconnect)")

    workspace = WorkspaceStore()
    resolved = workspace.resolve(args.connection, name_override=args.name)
    if resolved is None:
        parser.print_usage()
        print("(bare 'dbman' works once a connection has been saved to ./dbman.sqlite)")
        sys.exit(1)
    url, name = resolved
    app = DbMan(url, workspace=workspace, workspace_name=name)
    app.run()
