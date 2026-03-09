#!/usr/bin/env python3
import sys
import os
import traceback
from sqlalchemy import create_engine, inspect, text, MetaData, Table, Column, String, Integer, select, update, delete, func
from sqlalchemy.exc import SQLAlchemyError
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, ListView, ListItem, Label, Static, Button, Input, ContentSwitcher
from textual.containers import Horizontal, Vertical, Center, VerticalScroll
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.reactive import reactive
from rich.panel import Panel
from rich.table import Table as RichTable

# Import LookupPlugin from plugins folder
from plugins.lookup import LookupPlugin, LookupSelectScreen, LookupConfigScreen

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
            name = col["name"]
            col_type = str(col["type"])
            pk_marker = "[bold yellow]*[/]" if name in self.pks else " "
            table.add_row(f"{pk_marker} {name}", f"[dim]{col_type}[/]")
            
        if self.fks:
            table.add_section()
            table.add_row("[italic yellow]Relationships[/]", "")
            for fk in self.fks:
                ref_table = fk["referred_table"]
                ref_cols = fk["referred_columns"]
                cons_cols = fk["constrained_columns"]
                rel_str = f"{cons_cols[0]} -> {ref_table}({ref_cols[0]})"
                table.add_row(f"  [dim]↳[/] {rel_str}", "")
                
        return Panel(
            table, 
            title=f"[bold cyan] {self.table_name} [/]", 
            border_style="accent" if self.has_focus else "blue",
            expand=False,
            width=50
        )

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
                " g / G: Home / End\n\n"
                " [bold]Editing & Filtering[/]\n"
                " e: Edit selected cell (View mode only)\n"
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

    def __init__(self, table_name, column, current_max, suggested_len, engine):
        super().__init__()
        self.table_name = table_name
        self.column = column
        self.current_max = current_max
        self.suggested_len = suggested_len
        self.engine = engine

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
            with self.engine.connect() as conn:
                metadata = MetaData()
                table = Table(self.table_name, metadata, autoload_with=self.engine)
                col = table.c[self.column]
                stmt = select(func.count()).where(func.length(col) > target_len)
                count = conn.execute(stmt).scalar()
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
    """A container for displaying table diagrams."""
    
    def __init__(self, engine, **kwargs):
        super().__init__(**kwargs)
        self.engine = engine
        self._inspector = None

    @property
    def inspector(self):
        if self._inspector is None:
            self._inspector = inspect(self.engine)
        return self._inspector

    def refresh_diagram(self):
        try:
            self._inspector = inspect(self.engine)
            tables = self.inspector.get_table_names()
            
            # Remove existing diagrams
            self.query(TableDiagram).remove()
            
            table_widgets = []
            for table_name in sorted(tables):
                if table_name.startswith("sqlite_") or table_name.startswith("_dbman_"):
                    continue
                
                columns = self.inspector.get_columns(table_name)
                pk_constraint = self.inspector.get_pk_constraint(table_name)
                pks = pk_constraint.get("constrained_columns", [])
                fks = self.inspector.get_foreign_keys(table_name)
                
                table_widgets.append(TableDiagram(table_name, columns, pks, fks))
            
            if not table_widgets:
                self.mount(Static("No tables found."))
            else:
                self.mount(*table_widgets)
                
        except Exception as e:
            self.mount(Static(f"Error generating diagram: {e}"))

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
        margin: 1 2;
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
        Binding("f", "filter_column", "Filter Column"),
        Binding("F", "clear_filters", "Clear Filters"),
        Binding("t", "truncate_column", "Shorten Column"),
    ]

    def __init__(self, db_url):
        super().__init__()
        # If db_url is a file path, assume sqlite
        if not (db_url.startswith("sqlite://") or db_url.startswith("postgresql://") or db_url.startswith("mysql://")):
             db_url = f"sqlite:///{os.path.abspath(db_url)}"
        
        self.db_url = db_url
        self.current_item = None
        self.current_type = None 
        self.has_rowid = False
        self.mode = "view" 
        self.filters = {}
        try:
            self.engine = create_engine(db_url)
            self.inspector = inspect(self.engine)
            self.lookup_plugin = LookupPlugin(self.engine)
        except Exception as e:
            print(f"Error connecting to database: {e}")
            sys.exit(1)

    def get_tables(self):
        tables = self.inspector.get_table_names()
        return [t for t in tables if not t.startswith("sqlite_") and not t.startswith("_dbman_")]

    def get_views(self):
        return self.inspector.get_view_names()

    def build_filter_clause(self, table_obj):
        clauses = []
        for col_name, val in self.filters.items():
            col = table_obj.c[col_name]
            if val.lower() == "null":
                clauses.append(col.is_(None))
            elif val.lower() == "empty":
                clauses.append((col.is_(None)) | (col == ""))
            else:
                clauses.append(col.like(f"%{val}%"))
        return clauses

    def get_item_data(self, name, item_type):
        if item_type == "plugin":
            if name == "lookup":
                columns = ["Table", "ForeignKeyField", "RelatedTable", "RelatedKey", "LookupField"]
                rows = self.lookup_plugin.get_config_data()
                return columns, rows, False
            return [], [], False

        metadata = MetaData()
        table = Table(name, metadata, autoload_with=self.engine)
        columns = [c.name for c in table.columns]
        
        has_rowid = False
        if self.engine.dialect.name == "sqlite":
             # Rowid check is tricky with SQLAlchemy Core for reflected tables
             # We'll just try to select it if it's a table
             if item_type == "table":
                 has_rowid = True # Default to true for sqlite tables, handled in try/except later

        with self.engine.connect() as conn:
            stmt = select(table)
            filter_clauses = self.build_filter_clause(table)
            if filter_clauses:
                stmt = stmt.where(*filter_clauses)
            
            stmt = stmt.limit(2000)
            
            if item_type == "table" and self.engine.dialect.name == "sqlite":
                try:
                    # Try to select rowid explicitly for sqlite
                    rowid_stmt = select(text("rowid"), table).limit(2000)
                    if filter_clauses:
                         rowid_stmt = select(text("rowid"), table).where(*filter_clauses).limit(2000)
                    
                    result = conn.execute(rowid_stmt)
                    rows = [list(row) for row in result]
                    return columns, rows, True
                except:
                    has_rowid = False

            result = conn.execute(stmt)
            rows = [list(row) for row in result]
            return columns, rows, has_rowid

    def get_schema_data(self, name):
        columns_info = self.inspector.get_columns(name)
        rows = []
        for col in columns_info:
            rows.append((
                col.get("name"),
                str(col.get("type")),
                "NOT NULL" if not col.get("nullable") else "NULL",
                str(col.get("default")) if col.get("default") is not None else "",
                "PK" if col.get("primary_key") else ""
            ))
        columns = ["name", "type", "nullable", "default", "pk"]
        return columns, rows

    def get_sql_data(self, name):
        # SQLAlchemy doesn't provide a cross-db "get CREATE SQL" easily
        # For SQLite/Postgres/MySQL we might have to use dialect specific queries
        try:
            if self.engine.dialect.name == "sqlite":
                with self.engine.connect() as conn:
                    res = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = :name"), {"name": name}).fetchone()
                    return res[0] if res else "Not found"
            elif self.engine.dialect.name == "postgresql":
                # Very basic view/table definition fetch
                return f"-- SQL View/Table definition for {name} (Postgres support limited)"
            return f"-- SQL View/Table definition for {name}"
        except Exception as e:
            return f"Error fetching SQL: {e}"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield ListView(id="sidebar-list")
            with ContentSwitcher(initial="data-table"):
                yield DataTable(id="data-table")
                yield Static("", id="sql-view")
                yield DiagramView(self.engine, id="diagram-view")
        yield Footer()

    def on_mount(self):
        self.refresh_sidebar()
        self.query_one("#sidebar-list").focus()

    def refresh_sidebar(self):
        sidebar_list = self.query_one("#sidebar-list", ListView)
        sidebar_list.clear()
        
        views = self.get_views()
        tables = self.get_tables()
        plugins = ["lookup"]
        
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

    def on_list_view_selected(self, event: ListView.Selected):
        item = event.item
        if isinstance(item, DbItem):
            self.filters = {}
            self.load_item(item.item_name, item.item_type, should_focus=True)

    def on_list_view_highlighted(self, event: ListView.Highlighted):
        item = event.item
        if isinstance(item, DbItem):
            self.filters = {}
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
            
            if self.mode == "view" or item_type == "plugin":
                columns, rows, has_rowid = self.get_item_data(name, item_type)
                self.has_rowid = has_rowid
                
                for i, col in enumerate(columns):
                    color = COLORS[i % len(COLORS)]
                    label = f"[{color}]{col}[/]"
                    if col in self.filters:
                        label = f"[reverse]{label} (F)[/]"
                    table_widget.add_column(label, key=col)
                
                for i, row in enumerate(rows):
                    if has_rowid:
                        table_widget.add_row(*row[1:], key=str(row[0]))
                    else:
                        table_widget.add_row(*row, key=str(i))
            else:
                # Schema mode
                columns, rows = self.get_schema_data(name)
                for i, col in enumerate(columns):
                    color = COLORS[i % len(COLORS)]
                    table_widget.add_column(f"[{color}]{col}[/]", key=col)
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
            sql_text = self.get_sql_data(name)
            sql_widget.update(sql_text)
            if should_focus:
                sql_widget.focus()
                
        self.title = f"dbman - {name} ({self.mode.upper()})"

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
        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value
        current_filter = self.filters.get(column_name, "")
        def apply_filter(val):
            if val is not None:
                if val == "":
                    self.filters.pop(column_name, None)
                else:
                    self.filters[column_name] = val
                self.load_item(self.current_item, self.current_type)
        self.push_screen(FilterColumnScreen(column_name, current_filter), apply_filter)

    def action_clear_filters(self):
        self.filters = {}
        if self.current_item:
            self.load_item(self.current_item, self.current_type)
        self.notify("All filters cleared")

    def action_delete_item(self):
        if self.current_type == "plugin":
            self.notify("Cannot delete Plugins", severity="error")
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
                    metadata = MetaData()
                    table = Table(name, metadata, autoload_with=self.engine)
                    table.drop(self.engine)
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
        if self.current_type == "plugin" and self.current_item == "lookup":
            coord = self.focused.cursor_coordinate
            row_vals = self.focused.get_row_at(coord.row)
            table, fk_col, rel_table, rel_key, current_lookup = row_vals
            
            cols = [c["name"] for c in self.inspector.get_columns(rel_table)]
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
            self.notify("Cannot edit Views directly", severity="error")
            return
        if not self.has_rowid:
            self.notify("Cannot edit tables without identifiable rows (yet)", severity="error")
            return

        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value
        row_id = list(self.focused.rows.values())[coord.row].key.value
        current_value = self.focused.get_cell_at(coord)

        lookup_conf = self.lookup_plugin.get_lookup_config(self.current_item, column_name)
        if lookup_conf:
            rel_table, rel_key, display_col = lookup_conf
            
            with self.engine.connect() as conn:
                metadata = MetaData()
                rt = Table(rel_table, metadata, autoload_with=self.engine)
                res = conn.execute(select(rt.c[rel_key], rt.c[display_col]))
                options = [(str(r[1]), r[0]) for r in res]
            
            def perform_lookup_update(new_val):
                if new_val is not None:
                    try:
                        with self.engine.connect() as conn:
                             metadata = MetaData()
                             t = Table(self.current_item, metadata, autoload_with=self.engine)
                             if self.engine.dialect.name == "sqlite":
                                 stmt = update(t).where(text("rowid = :rid")).values({column_name: new_val})
                                 conn.execute(stmt, {"rid": row_id})
                             else:
                                 # Fallback for non-sqlite would need PK logic
                                 pass
                             conn.commit()
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
                    with self.engine.connect() as conn:
                         metadata = MetaData()
                         t = Table(self.current_item, metadata, autoload_with=self.engine)
                         if self.engine.dialect.name == "sqlite":
                             stmt = update(t).where(text("rowid = :rid")).values({column_name: typed_value})
                             conn.execute(stmt, {"rid": row_id})
                         else:
                             # PK logic needed for non-sqlite
                             pass
                         conn.commit()
                    self.notify("Updated")
                    self.load_item(self.current_item, self.current_type)
                except Exception as e:
                    self.notify(f"Update failed: {e}", severity="error")
        self.push_screen(EditCellScreen(current_value), perform_update)

    def action_truncate_column(self):
        if self.mode != "view":
            self.notify("Truncate only allowed in View mode", severity="error")
            return
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
        if self.current_type == "view":
            self.notify("Cannot truncate Views directly", severity="error")
            return
        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value
        
        try:
            with self.engine.connect() as conn:
                metadata = MetaData()
                t = Table(self.current_item, metadata, autoload_with=self.engine)
                col = t.c[column_name]
                max_len = conn.execute(select(func.max(func.length(col)))).scalar() or 0
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
                            with self.engine.connect() as conn:
                                metadata = MetaData()
                                t = Table(self.current_item, metadata, autoload_with=self.engine)
                                col = t.c[column_name]
                                # SQLite specific substr for now, but SQLAlchemy can abstract it
                                stmt = update(t).values({column_name: func.substr(col, 1, target_len)})
                                conn.execute(stmt)
                                conn.commit()
                            self.notify(f"Truncated column to {target_len} chars")
                            self.load_item(self.current_item, self.current_type)
                        except Exception as e:
                            self.notify(f"Truncate failed: {e}", severity="error")
                self.push_screen(ConfirmScreen(f"Truncate ALL values in '{column_name}' to {target_len}?", "Apply"), do_it)
        self.push_screen(TruncateColumnScreen(self.current_item, column_name, max_len, suggested, self.engine), perform_truncate)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dbman <database_url_or_file>")
        sys.exit(1)
    app = DbMan(sys.argv[1])
    app.run()
