#!/usr/bin/env python3
import sys
import sqlite3
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, ListView, ListItem, Label, Static, Button, Input, ContentSwitcher
from textual.containers import Horizontal, Vertical, Center
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.reactive import reactive

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
                " tab: Cycle focus (Sidebar Views -> Sidebar Tables -> Main Area)\n"
                " m: Toggle View/Schema/SQL mode\n"
                " ?: Toggle this Shortcuts panel\n"
                " ctrl+p: Toggle this Shortcuts panel\n\n"
                " [bold]Navigation[/]\n"
                " j / k: Move down / up\n"
                " h / l: Move left / right (Table only)\n"
                " pgup / pgdn: Page Up / Down (Mac: fn + up / fn + down)\n"
                " g / G: Home / End\n\n"
                " [bold]Editing & Actions[/]\n"
                " e: Edit selected cell (View mode only)\n"
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

    def __init__(self, table, column, current_max, suggested_len, conn):
        super().__init__()
        self.table = table
        self.column = column
        self.current_max = current_max
        self.suggested_len = suggested_len
        self.conn = conn

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
            cursor = self.conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM {self.table} WHERE LENGTH({self.column}) > ?", (target_len,))
            count = cursor.fetchone()[0]
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
        self.item_type = item_type # "table" or "view"

class DbMan(App):
    """A vim-like SQLite database browser with Sidebar navigation."""

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
    .sidebar-section-title {
        padding: 0 1;
        background: $primary;
        color: $text;
        text-style: bold;
        text-align: center;
        border-bottom: solid $primary;
        border-top: solid $primary;
    }
    #view-title {
        border-top: none;
    }
    ListView {
        height: auto;
        max-height: 50%;
        background: $panel;
    }
    DataTable {
        height: 1fr;
    }
    DataTable:focus {
        border: double $accent;
    }
    #sql-view {
        height: 1fr;
        padding: 1 2;
        background: $surface;
        color: $text;
        overflow-x: scroll;
        overflow-y: scroll;
    }
    #sql-view:focus {
        border: double $accent;
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
        Binding("tab", "switch_focus", "Switch Focus"),
        Binding("m", "toggle_mode", "View/Schema/SQL Mode"),
        Binding("ctrl+x", "delete_item", "Delete"),
        Binding("?", "toggle_shortcuts", "Shortcuts", show=False),
        Binding("ctrl+p", "toggle_shortcuts", "Shortcuts"),
        Binding("e", "edit_cell", "Edit Cell"),
        Binding("t", "truncate_column", "Shorten Column"),
    ]

    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path
        self.current_item = None
        self.current_type = None # "table" or "view"
        self.has_rowid = False
        self.mode = "view" # or "schema" or "sql"
        try:
            self.conn = sqlite3.connect(db_path)
            self.cursor = self.conn.cursor()
        except Exception as e:
            print(f"Error connecting to database: {e}")
            sys.exit(1)

    def get_tables(self):
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        return [row[0] for row in self.cursor.fetchall()]

    def get_views(self):
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='view' AND name NOT LIKE 'sqlite_%';")
        return [row[0] for row in self.cursor.fetchall()]

    def get_item_data(self, name, item_type):
        self.cursor.execute(f"PRAGMA table_info({name});")
        info = self.cursor.fetchall()
        columns = [i[1] for i in info]
        
        if item_type == "table":
            try:
                # Try to get rowid for unique identification during updates
                self.cursor.execute(f"SELECT rowid, * FROM {name} LIMIT 2000;")
                rows = self.cursor.fetchall()
                return columns, rows, True
            except:
                # Fallback if table doesn't have rowid (WITHOUT ROWID)
                self.cursor.execute(f"SELECT * FROM {name} LIMIT 2000;")
                rows = self.cursor.fetchall()
                return columns, rows, False
        else:
            # Views don't have rowids in the same way
            self.cursor.execute(f"SELECT * FROM {name} LIMIT 2000;")
            rows = self.cursor.fetchall()
            return columns, rows, False

    def get_schema_data(self, name):
        self.cursor.execute(f"PRAGMA table_info({name});")
        rows = self.cursor.fetchall()
        # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
        columns = ["cid", "name", "type", "notnull", "dflt_value", "pk"]
        return columns, rows

    def get_sql_data(self, name):
        self.cursor.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,))
        sql = self.cursor.fetchone()
        return sql[0] if sql else "Not found"

    def compose(self) -> ComposeResult:
        yield Header()
        tables = self.get_tables()
        views = self.get_views()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static(" VIEWS ", classes="sidebar-section-title", id="view-title")
                yield ListView(*[DbItem(v, "view") for v in views], id="view-list")
                yield Static(" TABLES ", classes="sidebar-section-title", id="table-title")
                yield ListView(*[DbItem(t, "table") for t in tables], id="table-list")
            with ContentSwitcher(initial="data-table"):
                yield DataTable(id="data-table")
                yield Static("", id="sql-view")
        yield Footer()

    def on_mount(self):
        if self.get_views():
            self.query_one("#view-list").focus()
        else:
            self.query_one("#table-list").focus()

    def on_list_view_selected(self, event: ListView.Selected):
        item = event.item
        if isinstance(item, DbItem):
            self.load_item(item.item_name, item.item_type, should_focus=True)

    def on_list_view_highlighted(self, event: ListView.Highlighted):
        item = event.item
        if isinstance(item, DbItem):
            self.load_item(item.item_name, item.item_type, should_focus=False)

    def load_item(self, name, item_type, should_focus=False):
        # Save current cursor position if we are reloading the same item
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
        switcher = self.query_one(ContentSwitcher)
        
        if self.mode in ["view", "schema"]:
            switcher.current = "data-table"
            table_widget.clear(columns=True)
            
            if self.mode == "view":
                columns, rows, has_rowid = self.get_item_data(name, item_type)
                self.has_rowid = has_rowid
                
                for i, col in enumerate(columns):
                    color = COLORS[i % len(COLORS)]
                    table_widget.add_column(f"[{color}]{col}[/]", key=col)
                
                for row in rows:
                    if has_rowid:
                        table_widget.add_row(*row[1:], key=str(row[0]))
                    else:
                        table_widget.add_row(*row)
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
            switcher.current = "sql-view"
            sql_text = self.get_sql_data(name)
            sql_widget.update(sql_text)
            if should_focus:
                sql_widget.focus()
                
        self.title = f"dbman - {name} ({self.mode.upper()})"

    def action_switch_focus(self):
        if self.focused.id == "view-list":
            self.query_one("#table-list").focus()
        elif self.focused.id == "table-list":
            switcher = self.query_one(ContentSwitcher)
            if switcher.current == "data-table":
                self.query_one("#data-table").focus()
            else:
                self.query_one("#sql-view").focus()
        else:
            self.query_one("#view-list").focus()

    def action_toggle_mode(self):
        modes = ["view", "schema", "sql"]
        idx = modes.index(self.mode)
        self.mode = modes[(idx + 1) % len(modes)]
        
        # Update sidebar titles to show current mode
        self.query_one("#view-title").update(f" VIEWS ({self.mode.upper()}) ")
        self.query_one("#table-title").update(f" TABLES ({self.mode.upper()}) ")
        
        if self.current_item:
            self.load_item(self.current_item, self.current_type)

    def action_delete_item(self):
        # Determine item to delete
        focused_list = None
        if self.focused and self.focused.id in ["view-list", "table-list"]:
            focused_list = self.focused
            
        if focused_list and focused_list.highlighted_child:
            item = focused_list.highlighted_child
            name = item.item_name
            item_type = item.item_type
        elif self.current_item:
            name = self.current_item
            item_type = self.current_type
        else:
            return

        def on_confirm(do_delete):
            if do_delete:
                try:
                    sql_type = "TABLE" if item_type == "table" else "VIEW"
                    self.cursor.execute(f"DROP {sql_type} {name}")
                    self.conn.commit()
                    self.notify(f"{sql_type.capitalize()} '{name}' deleted")
                    
                    # Refresh lists
                    view_list = self.query_one("#view-list", ListView)
                    table_list = self.query_one("#table-list", ListView)
                    
                    view_list.clear()
                    for v in self.get_views():
                        view_list.append(DbItem(v, "view"))
                        
                    table_list.clear()
                    for t in self.get_tables():
                        table_list.append(DbItem(t, "table"))
                        
                    # Load something else if available
                    views = self.get_views()
                    tables = self.get_tables()
                    if views:
                        self.load_item(views[0], "view")
                    elif tables:
                        self.load_item(tables[0], "table")
                    else:
                        self.query_one("#data-table").clear(columns=True)
                        self.current_item = None
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
        if self.mode != "view":
            self.notify(f"Editing only allowed in View mode (current: {self.mode})", severity="error")
            return
            
        if not isinstance(self.focused, DataTable) or not self.current_item:
            return
            
        if self.current_type == "view":
            self.notify("Cannot edit Views directly", severity="error")
            return

        if not self.has_rowid:
            self.notify("Cannot edit tables without rowid (yet)", severity="error")
            return

        coord = self.focused.cursor_coordinate
        column_name = self.focused.ordered_columns[coord.column].key.value
        row_id = list(self.focused.rows.values())[coord.row].key.value
        current_value = self.focused.get_cell_at(coord)

        def perform_update(new_value):
            if new_value is not None:
                # Type inference
                typed_value = new_value
                if new_value.strip() == "":
                    typed_value = None
                else:
                    try:
                        if "." in new_value:
                            typed_value = float(new_value)
                        else:
                            typed_value = int(new_value)
                    except ValueError:
                        pass # Keep as string

                try:
                    query = f"UPDATE {self.current_item} SET {column_name} = ? WHERE rowid = ?"
                    self.cursor.execute(query, (typed_value, row_id))
                    self.conn.commit()
                    self.notify("Updated")
                    # Reload item but preserve cursor
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
        
        # Calculate max length of current column values
        try:
            self.cursor.execute(f"SELECT MAX(LENGTH({column_name})) FROM {self.current_item}")
            max_len = self.cursor.fetchone()[0] or 0
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
                            query = f"UPDATE {self.current_item} SET {column_name} = SUBSTR({column_name}, 1, ?)"
                            self.cursor.execute(query, (target_len,))
                            self.conn.commit()
                            self.notify(f"Truncated column to {target_len} chars")
                            self.load_item(self.current_item, self.current_type)
                        except Exception as e:
                            self.notify(f"Truncate failed: {e}", severity="error")
                
                self.push_screen(ConfirmScreen(f"Truncate ALL values in '{column_name}' to {target_len}?", "Apply"), do_it)

        self.push_screen(TruncateColumnScreen(self.current_item, column_name, max_len, suggested, self.conn), perform_truncate)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dbman <database_file>")
        sys.exit(1)
    
    app = DbMan(sys.argv[1])
    app.run()
