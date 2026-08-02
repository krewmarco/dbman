#!/usr/bin/env python3
import sys
import traceback
import math
import random
import csv
import json
from sqlalchemy import text
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, ListView, ListItem, Label, Static, Button, Input, ContentSwitcher, TextArea
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
                " d / ctrl+d: Switch to Diagram mode\n"
                " ?: Toggle this Shortcuts panel\n"
                " ctrl+p: Toggle this Shortcuts panel\n\n"
                " [bold]Navigation[/]\n"
                " j / k: Move down / up\n"
                " h / l: Move left / right (Table only)\n"
                " pgup / pgdn: Page Up / Down (Mac: fn + up / fn + down)\n"
                " g / G: Home / End\n"
                " ] / [: Next / Previous page of rows (View mode)\n\n"
                " [bold]Editing & Filtering[/]\n"
                " e: Edit selected cell (View mode) or SQL (SQL mode)\n"
                " E: Edit whole document as JSON (document DB providers)\n"
                " v: Create new View\n"
                " x: Export current Table/View to CSV\n"
                " f: Filter selected column (View mode only)\n"
                " F: Clear all filters for current table\n"
                " t: Truncate/Shorten Column data (View mode only)\n"
                " ctrl+x: Delete selected table/view\n",
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
        Binding("d", "change_mode_diagram", "Diagram Mode"),
        Binding("ctrl+d", "change_mode_diagram", "Diagram Mode", show=False),
        Binding("command+d", "change_mode_diagram", "Diagram Mode", show=False),
        Binding("ctrl+x", "delete_item", "Delete"),
        Binding("?", "toggle_shortcuts", "Shortcuts", show=False),
        Binding("ctrl+p", "toggle_shortcuts", "Shortcuts"),
        Binding("e", "edit_cell", "Edit Cell"),
        Binding("E", "edit_document", "Edit Document", show=False),
        Binding("v", "create_view", "Create View"),
        Binding("x", "export_csv", "Export CSV"),
        Binding("f", "filter_column", "Filter Column"),
        Binding("F", "clear_filters", "Clear Filters"),
        Binding("t", "truncate_column", "Shorten Column"),
        Binding("]", "next_page", "Next Page", show=False),
        Binding("[", "prev_page", "Prev Page", show=False),
    ]

    def __init__(self, db_url):
        super().__init__()
        self.db_url = db_url
        self.current_item = None
        self.current_type = None
        self.rows_editable = False
        self.row_keys = {}
        self.raw_docs = {}
        self.mode = "view"
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
            if views:
                self.load_item(views[0], "view")
            elif tables:
                self.load_item(tables[0], "table")

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
                self.rows_editable = bool(page.row_keys) and page.row_keys[0].value is not None

                for i, col in enumerate(page.columns):
                    color = COLORS[i % len(COLORS)]
                    label = f"[{color}]{col.name}[/]"
                    if col.name in self.filters:
                        label = f"[reverse]{label} (F)[/]"
                    table_widget.add_column(label, key=col.name)

                for i, (row, row_key) in enumerate(zip(page.rows, page.row_keys)):
                    if row_key.value is None:
                        key_str = str(i)
                    elif isinstance(row_key.value, dict):
                        key_str = json.dumps(row_key.value, sort_keys=True)
                    else:
                        key_str = str(row_key.value)
                    self.row_keys[key_str] = row_key
                    if page.raw_rows is not None:
                        self.raw_docs[key_str] = page.raw_rows[i]
                    table_widget.add_row(*row, key=key_str)
            else:
                # Schema mode
                self.row_keys = {}
                self.raw_docs = {}
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
                
        page_indicator = ""
        if self.mode == "view":
            page_num = len(self.page_history) + 1
            if page_num > 1 or self.page_has_more:
                page_indicator = f" [page {page_num}{'+' if self.page_has_more else ''}]"
        self.title = f"dbman - {name} ({self.mode.upper()}){page_indicator}"

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
        self.refresh_sidebar()
        sidebar_list = self.query_one("#sidebar-list", ListView)
        for i, child in enumerate(sidebar_list.children):
            if isinstance(child, DbItem) and child.item_name == self.current_item:
                sidebar_list.index = i
                break
        if self.current_item:
            self.load_item(self.current_item, self.current_type)

    def action_change_mode_diagram(self):
        if not self.provider.capabilities.diagram:
            self.notify("Diagram not available for this provider", severity="error")
            return
        self.mode = "diagram"
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
        current_value = self.focused.get_cell_at(coord)

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

        def save_document(new_json_text):
            if new_json_text:
                try:
                    self.provider.update_row_json(self.current_item, self.current_type, row_key, new_json_text)
                    self.notify("Document updated")
                    self.load_item(self.current_item, self.current_type)
                except Exception as e:
                    self.notify(f"Update failed: {e}", severity="error")

        doc_id = row_key.value.get("_id") if isinstance(row_key.value, dict) else row_id_str
        self.push_screen(
            EditTextScreen(f"Edit Document: {doc_id}", json.dumps(raw_doc, indent=2), language="json"),
            save_document,
        )

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

    def action_create_view(self):
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

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dbman <database_url_or_file>")
        sys.exit(1)
    app = DbMan(sys.argv[1])
    app.run()
