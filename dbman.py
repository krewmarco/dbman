#!/usr/bin/env python3
import sys
import traceback
import math
import random
import csv
import json
from sqlalchemy import text
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, ListView, ListItem, Label, Static, Button, Input, ContentSwitcher, TextArea, Select
from textual.containers import Horizontal, Vertical, Center, VerticalScroll
from textual.binding import Binding
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
    ViewSettingsStore, derive_db_name, apply_view_settings,
    compute_column_widths, truncate_rows,
)
from workspace import WorkspaceStore, ConnectionSession

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
                " ctrl+d: Switch to Diagram mode\n"
                " ?: Toggle this Shortcuts panel\n"
                " ctrl+p: Toggle this Shortcuts panel\n\n"
                " [bold]Navigation[/]\n"
                " j / k: Move down / up\n"
                " h / l: Move left / right (Table only)\n"
                " pgup / pgdn: Page Up / Down (Mac: fn + up / fn + down)\n"
                " g / G: Home / End\n"
                " \\] / \\[: Next / Previous page of rows (View mode)\n\n"
                " [bold]Editing & Filtering[/]\n"
                " e: Edit selected cell/row (View mode) or SQL (SQL mode)\n"
                " E: Edit whole document as JSON (document DB providers)\n"
                " a: Add a new row (table) or create a new View (where supported)\n"
                " d: Delete selected table/view\n"
                " x: Export current Table/View to CSV\n"
                " f: Filter selected column (column select mode)\n"
                " F: Clear all filters for current table (column select mode)\n"
                " t: Truncate/Shorten Column data (View mode only)\n"
                " w: Set/clear column display width (column select mode)\n\n"
                " [bold]Select Mode (View mode only)[/]\n"
                " s: Rotate select mode: field -> row -> column -> field\n"
                " H / L: Move selected column left / right (column mode; option+left/right also works on some terminals)\n"
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
    
    def key_ctrl_p(self) -> None:
        self.app.pop_screen()

class FilterColumnScreen(ModalScreen):
    """A modal screen for filtering a column."""
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
    def __init__(self, column, current_filter=""):
        super().__init__()
        self.column = column
        self.current_filter = current_filter

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-dialog"):
            yield Label(f"Filter Column: {self.column}")
            yield Static("Enter search term (use 'null' for NULL, 'empty' for empty string):", id="small-label")
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
            self.dismiss("")
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

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

class ColumnWidthScreen(ModalScreen):
    """A modal screen for pinning (or clearing) a column's display width."""
    CSS = """
    ColumnWidthScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #width-dialog {
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
    #width-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #width-buttons {
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """

    def __init__(self, column: str, current_width, auto_width: int):
        super().__init__()
        self.column = column
        self.current_width = current_width
        self.auto_width = auto_width

    def compose(self) -> ComposeResult:
        with Vertical(id="width-dialog"):
            yield Label(f"Width for '{self.column}'")
            yield Input(
                value=str(self.current_width) if self.current_width is not None else "",
                placeholder=str(self.auto_width),
                id="width-input",
            )
            yield Static(
                f"Auto width would be {self.auto_width}. Clear the field to remove the override.",
                id="width-hint",
            )
            with Horizontal(id="width-buttons"):
                yield Button("Cancel", id="cancel-width")
                yield Button("Apply", variant="success", id="apply-width")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply-width":
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
    def __init__(self, title: str) -> None:
        super().__init__(Label(f" {title} "))
        self.title = title
        self.disabled = True 

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
    select_mode (field: the cell, row: the whole row/document, column: n/a).
    Too many cross-cutting branches to express as a plain _ctx() AND."""
    if app.mode == "sql":
        return app.current_type == "view" and app.provider.capabilities.create_definition
    if app.current_type == "plugin":
        return app.current_item == "lookup" and app.lookup_plugin is not None
    if app.mode != "view":
        return False
    if app.select_mode == "row":
        return app.provider.capabilities.whole_row_edit
    if app.select_mode == "column":
        return False
    return app.current_type == "table" and app.rows_editable


def _filter_column_ctx(app):
    return (
        app.mode == "view"
        and app.select_mode == "column"
        and bool(app.current_item)
        and app.provider.is_filterable(app.current_type)
    )


def _truncate_column_ctx(app):
    return (
        app.mode == "view"
        and app.current_type == "table"
        and bool(app.current_item)
        and app.provider.capabilities.truncate_column
    )


def _delete_item_ctx(app):
    return bool(app.current_type) and app.current_type != "plugin" and app.provider.capabilities.delete_item


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


def _add_ctx(app):
    """'a' is polymorphic like 'e': add a row on a Table, create a new View
    on a View. OR-of-branches, not a plain _ctx() AND."""
    if app.mode != "view" or not app.current_item:
        return False
    if app.current_type == "table":
        return app.provider.capabilities.add_row
    if app.current_type == "view":
        return app.provider.capabilities.create_definition
    return False


def _clear_filters_ctx(app):
    """Column-select-mode-scoped, paired with 'f' like unhide is paired with
    hide. Greys out (rather than hides) 'F' when there's nothing to clear."""
    if app.mode != "view" or app.select_mode != "column" or not app.current_item:
        return False
    return True if app.filters else None


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
        background: $primary;
        color: $text;
        text-style: bold;
        padding: 0 1;
        border-bottom: solid $primary;
        border-top: solid $primary;
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
    ListItem.--highlight {
        background: $accent;
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
        Binding("g", "scroll_home", "Home", show=False),
        Binding("G", "scroll_end", "End", show=False),
        Binding("tab", "switch_focus", "Sidebar/Main"),
        Binding("shift+tab", "jump_section", "Jump Section"),
        Binding("m", "toggle_mode", "View/Schema/SQL/Diag Mode"),
        Binding("ctrl+d", "change_mode_diagram", "Diagram Mode"),
        Binding("command+d", "change_mode_diagram", "Diagram Mode", show=False),
        Binding("d", "delete_item", "Delete"),
        Binding("?", "toggle_shortcuts", "Shortcuts", show=False),
        Binding("ctrl+p", "toggle_shortcuts", "Shortcuts"),
        Binding("e", "edit_cell", "Edit"),
        Binding("E", "edit_document", "Edit Document", show=False),
        Binding("a", "add", "Add"),
        Binding("x", "export_csv", "Export CSV", show=False),
        Binding("f", "filter_column", "Filter Column"),
        Binding("F", "clear_filters", "Clear Filters"),
        Binding("t", "truncate_column", "Shorten Column"),
        Binding("w", "set_column_width", "Column Width"),
        Binding("s", "rotate_select_mode", "Select Mode"),
        Binding("z", "hide_column", "Hide Column"),
        Binding("Z", "unhide_column", "Unhide Column"),
        Binding("alt+left", "reorder_column(-1)", "Move Column Left", show=False),
        Binding("alt+right", "reorder_column(1)", "Move Column Right", show=False),
        Binding("H", "reorder_column(-1)", "Move Column Left"),
        Binding("L", "reorder_column(1)", "Move Column Right"),
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
        "edit_document": _ctx(modes={"view"}, capability="whole_row_edit"),
        "filter_column": _filter_column_ctx,
        "truncate_column": _truncate_column_ctx,
        "set_column_width": _ctx(modes={"view"}, select_modes={"column"}),
        "rotate_select_mode": _ctx(modes={"view"}),
        "reorder_column": _ctx(modes={"view"}, select_modes={"column"}),
        "hide_column": _ctx(modes={"view"}, select_modes={"column"}),
        "unhide_column": _unhide_column_ctx,
        "delete_item": _delete_item_ctx,
        "toggle_mode": _toggle_mode_ctx,
        "change_mode_diagram": _ctx(capability="diagram"),
        "export_csv": _export_csv_ctx,
        "add": _add_ctx,
        "clear_filters": _clear_filters_ctx,
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
        self.mode = "view"
        self.select_mode = "field"
        self.filters = {}
        self.page_size = 500
        self.page_cursor = None
        self.page_history = []
        self.page_has_more = False
        self._next_cursor = None
        try:
            self.provider = create_provider(db_url)
            self.lookup_plugin = (
                LookupPlugin(self.provider.sqlalchemy_engine())
                if self.provider.capabilities.lookup_plugin else None
            )
            self.view_settings = ViewSettingsStore(derive_db_name(db_url))
            if self.workspace is not None:
                self.workspace.upsert_connection(self.workspace_name, db_url)
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

    def refresh_sidebar(self):
        sidebar_list = self.query_one("#sidebar-list", ListView)
        sidebar_list.clear()
        
        views = self.get_views()
        tables = self.get_tables()
        plugins = ["lookup"] if self.lookup_plugin else []
        
        mode_suffix = f" ({self.mode.upper()})"
        
        sidebar_list.append(SidebarHeader(f"VIEWS{mode_suffix}"))
        for v in views:
            sidebar_list.append(DbItem(v, "view"))
            
        sidebar_list.append(SidebarHeader(f"TABLES{mode_suffix}"))
        for t in tables:
            sidebar_list.append(DbItem(t, "table"))

        sidebar_list.append(SidebarHeader(f"PLUGINS"))
        for p in plugins:
            sidebar_list.append(DbItem(p, "plugin"))
            
        if not self.current_item:
            restored = self._restore_workspace_session(views, tables)
            if not restored:
                if views:
                    self.load_item(views[0], "view")
                elif tables:
                    self.load_item(tables[0], "table")

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

        sidebar_list = self.query_one("#sidebar-list", ListView)
        for i, child in enumerate(sidebar_list.children):
            if isinstance(child, DbItem) and child.item_name == session.item_name and child.item_type == session.item_type:
                sidebar_list.index = i
                break

        if session.cursor_row is not None and session.cursor_column is not None:
            try:
                self.query_one("#data-table", DataTable).move_cursor(
                    row=session.cursor_row, column=session.cursor_column
                )
            except Exception:
                pass
        return True

    async def action_quit(self) -> None:
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
            self.reset_paging()
            self.load_item(item.item_name, item.item_type, should_focus=True)

    def on_list_view_highlighted(self, event: ListView.Highlighted):
        item = event.item
        if isinstance(item, DbItem):
            self.filters = {}
            self.reset_paging()
            self.load_item(item.item_name, item.item_type, should_focus=False)

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
            table_widget.clear(columns=True)
            
            if item_type == "plugin":
                self.row_keys = {}
                self.raw_docs = {}
                self.row_values = {}
                self.rows_editable = False
                self.page_has_more = False
                columns, rows = self.get_plugin_data(name)
                for i, col in enumerate(columns):
                    color = COLORS[i % len(COLORS)]
                    table_widget.add_column(f"[{color}]{col}[/]", key=col)
                for i, row in enumerate(rows):
                    table_widget.add_row(*row, key=str(i))
            elif self.mode == "view":
                page = self.provider.get_page(name, item_type, self.filters, cursor=self.page_cursor, page_size=self.page_size)
                self.page_has_more = page.has_more
                self._next_cursor = page.next_cursor
                self.row_keys = {}
                self.raw_docs = {}
                self.row_values = {}
                self.rows_editable = bool(page.row_keys) and page.row_keys[0].value is not None

                view_settings = self.view_settings.get(name)
                display_columns, display_rows = apply_view_settings(page.columns, page.rows, view_settings)
                column_widths = compute_column_widths(display_columns, display_rows, view_settings)
                self.column_widths = column_widths
                rendered_rows = truncate_rows(display_columns, display_rows, column_widths)

                for i, col in enumerate(display_columns):
                    color = COLORS[i % len(COLORS)]
                    label = f"[{color}]{col.name}[/]"
                    if col.name in self.filters:
                        label = f"[reverse]{label} (F)[/]"
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
                    table_widget.add_row(*rendered_row, key=key_str)
            else:
                # Schema mode
                self.row_keys = {}
                self.raw_docs = {}
                self.row_values = {}
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
        self.title = f"dbman - {self.current_item} ({self.mode.upper()}){page_indicator}{select_indicator}"

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
        current_idx = sidebar_list.index
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
        sidebar_list = self.query_one("#sidebar-list", ListView)
        for i, child in enumerate(sidebar_list.children):
            if isinstance(child, DbItem) and child.item_name == self.current_item:
                sidebar_list.index = i
                break
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
        self.select_mode = self.SELECT_MODES[(idx + 1) % len(self.SELECT_MODES)]
        self.focused.cursor_type = self._cursor_type_for_select_mode()
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

    def action_change_mode_diagram(self):
        if not self.provider.capabilities.diagram:
            self.notify("Diagram not available for this provider", severity="error")
            return
        self.mode = "diagram"
        self.refresh_bindings()
        self.refresh_sidebar()
        if self.current_item:
            self.load_item(self.current_item, self.current_type, should_focus=True)
        else:
            self.query_one("#sidebar").display = False
            switcher = self.query_one(ContentSwitcher)
            switcher.current = "diagram-view"
            diag_widget = self.query_one("#diagram-view", DiagramView)
            diag_widget.refresh_diagram()
            diag_widget.focus()

    def action_filter_column(self):
        if self.mode != "view":
            self.notify("Filtering only allowed in View mode", severity="error")
            return
        if self.select_mode != "column":
            self.notify("Filtering only allowed in column select mode (press 's' to rotate)", severity="error")
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
            if val is not None:
                if val == "":
                    self.filters.pop(column_name, None)
                else:
                    self.filters[column_name] = val
                self.reset_paging()
                self.load_item(self.current_item, self.current_type)
        self.push_screen(FilterColumnScreen(column_name, current_filter), apply_filter)

    def action_clear_filters(self):
        if self.mode != "view":
            self.notify("Filtering only allowed in View mode", severity="error")
            return
        if self.select_mode != "column":
            self.notify("Filtering only allowed in column select mode (press 's' to rotate)", severity="error")
            return
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
            else:
                self.notify("Row edit not supported for this provider — switch to field mode", severity="error")
            return
        if self.select_mode == "column":
            self.notify("No edit action in column mode (use 't' to shorten, option+left/right to reorder)", severity="error")
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
        row/document and immediately open it for editing — only implemented
        where a provider can create a sensible blank row without a
        schema-aware form (currently CouchDB's freeform documents,
        capabilities.add_row; SqlAlchemyProvider tables need typed defaults
        for NOT NULL columns, deferred to a future dynamic-forms/
        business-logic layer). On a View, create a new view (formerly the
        standalone 'v' key — folded in here since it's the same "add a new
        thing" gesture, just for a different item type). See issue #10."""
        if self.mode != "view" or not self.current_item:
            self.notify("Add only allowed in View mode, on a Table or View", severity="error")
            return
        if self.current_type == "table":
            self._add_row()
        elif self.current_type == "view":
            self._add_view()
        else:
            self.notify("Add only allowed on a Table or View", severity="error")

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
                        page = self.provider.get_page(self.current_item, self.current_type, self.filters, cursor=None, page_size=None)
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

    def action_set_column_width(self):
        """Pin (or clear) a display-only column width, persisted via
        ViewSettingsStore. Distinct from action_truncate_column, which
        mutates the underlying data rather than just how it's displayed."""
        if self.mode != "view":
            self.notify("Column width only allowed in View mode", severity="error")
            return
        if self.select_mode != "column":
            self.notify("Column width only allowed in column select mode (press 's' to rotate)", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value

        settings = self.view_settings.get(self.current_item)
        current_override = settings.widths.get(column_name)
        auto_width = self.column_widths.get(column_name, current_override or 20)

        def apply_width(value):
            if value is None:
                return
            value = value.strip()
            settings = self.view_settings.get(self.current_item)
            if value == "":
                settings.widths.pop(column_name, None)
            else:
                try:
                    width = int(value)
                    if width < 1:
                        raise ValueError
                except ValueError:
                    self.notify("Width must be a positive integer", severity="error")
                    return
                settings.widths[column_name] = width
            self.view_settings.save(self.current_item, settings)
            self.load_item(self.current_item, self.current_type)

        self.push_screen(ColumnWidthScreen(column_name, current_override, auto_width), apply_width)

if __name__ == "__main__":
    workspace = WorkspaceStore()
    resolved = workspace.resolve(sys.argv[1] if len(sys.argv) > 1 else None)
    if resolved is None:
        print("Usage: dbman <database_url_or_file_or_saved_connection_name>")
        print("(bare 'dbman' works once a connection has been saved to ./dbman.json)")
        sys.exit(1)
    url, name = resolved
    app = DbMan(url, workspace=workspace, workspace_name=name)
    app.run()
