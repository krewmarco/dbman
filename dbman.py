#!/usr/bin/env python3
import sys
import sqlite3
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, ListView, ListItem, Label, Static, Button, Input
from textual.containers import Horizontal, Vertical, Center
from textual.binding import Binding
from textual.screen import ModalScreen

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
                " tab: Switch focus between Sidebar and Table\n"
                " m: Toggle View/Schema mode\n"
                " ?: Toggle this Shortcuts panel\n"
                " ctrl+p: Toggle this Shortcuts panel\n\n"
                " [bold]Navigation[/]\n"
                " j / k: Move down / up\n"
                " h / l: Move left / right (Table only)\n"
                " pgup / pgdn: Page Up / Down (Mac: fn + up / fn + down)\n"
                " g / G: Home / End\n\n"
                " [bold]Editing & Actions[/]\n"
                " e: Edit selected cell (View mode only)\n"
                " ctrl+x: Delete selected table\n",
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
    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.message)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel-confirm")
                yield Button("Delete", variant="error", id="ok-confirm")

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

class TableListItem(ListItem):
    def __init__(self, table_name: str) -> None:
        super().__init__(Label(f" {table_name} "))
        self.table_name = table_name

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
    #sidebar-title {
        padding: 1;
        background: $primary;
        color: $text;
        text-style: bold;
        text-align: center;
    }
    DataTable {
        height: 1fr;
    }
    DataTable:focus {
        border: double $accent;
    }
    ListItem {
        padding: 0 1;
    }
    ListItem.--highlight {
        background: $accent;
        color: $text;
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
        Binding("m", "toggle_mode", "View/Schema Mode"),
        Binding("ctrl+x", "delete_table", "Delete Table"),
        Binding("?", "toggle_shortcuts", "Shortcuts", show=False),
        Binding("ctrl+p", "toggle_shortcuts", "Shortcuts"),
        Binding("e", "edit_cell", "Edit Cell"),
    ]

    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path
        self.current_table = None
        self.has_rowid = False
        self.mode = "view" # or "schema"
        try:
            self.conn = sqlite3.connect(db_path)
            self.cursor = self.conn.cursor()
        except Exception as e:
            print(f"Error connecting to database: {e}")
            sys.exit(1)

    def get_tables(self):
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        return [row[0] for row in self.cursor.fetchall()]

    def get_table_data(self, table_name):
        self.cursor.execute(f"PRAGMA table_info({table_name});")
        info = self.cursor.fetchall()
        columns = [i[1] for i in info]
        
        try:
            # Try to get rowid for unique identification during updates
            self.cursor.execute(f"SELECT rowid, * FROM {table_name} LIMIT 2000;")
            rows = self.cursor.fetchall()
            return columns, rows, True
        except:
            # Fallback if table doesn't have rowid (WITHOUT ROWID)
            self.cursor.execute(f"SELECT * FROM {table_name} LIMIT 2000;")
            rows = self.cursor.fetchall()
            return columns, rows, False

    def get_schema_data(self, table_name):
        self.cursor.execute(f"PRAGMA table_info({table_name});")
        rows = self.cursor.fetchall()
        # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
        columns = ["cid", "name", "type", "notnull", "dflt_value", "pk"]
        return columns, rows

    def compose(self) -> ComposeResult:
        yield Header()
        tables = self.get_tables()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static(" VIEW ", id="sidebar-title")
                yield ListView(*[TableListItem(t) for t in tables], id="table-list")
            with Vertical():
                yield DataTable(id="data-table")
        yield Footer()

    def on_mount(self):
        self.query_one("#table-list").focus()

    def on_list_view_selected(self, event: ListView.Selected):
        self.load_table(event.item.table_name, should_focus=True)

    def on_list_view_highlighted(self, event: ListView.Highlighted):
        if event.item:
            self.load_table(event.item.table_name, should_focus=False)

    def load_table(self, table_name, should_focus=False):
        # Save current cursor position if we are reloading the same table
        saved_coord = None
        if self.current_table == table_name:
            try:
                saved_coord = self.query_one("#data-table").cursor_coordinate
            except:
                pass

        self.current_table = table_name
        table_widget = self.query_one("#data-table", DataTable)
        table_widget.clear(columns=True)
        
        if self.mode == "view":
            columns, rows, has_rowid = self.get_table_data(table_name)
            self.has_rowid = has_rowid
            
            for i, col in enumerate(columns):
                color = COLORS[i % len(COLORS)]
                table_widget.add_column(f"[{color}]{col}[/]", key=col)
            
            for row in rows:
                if has_rowid:
                    # Use rowid as the row key (row[0])
                    table_widget.add_row(*row[1:], key=str(row[0]))
                else:
                    table_widget.add_row(*row)
        else:
            # Schema mode
            columns, rows = self.get_schema_data(table_name)
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
                
        self.title = f"dbman - {table_name} ({self.mode})"

    def action_switch_focus(self):
        if self.focused.id == "table-list":
            self.query_one("#data-table").focus()
        else:
            self.query_one("#table-list").focus()

    def action_toggle_mode(self):
        self.mode = "schema" if self.mode == "view" else "view"
        self.query_one("#sidebar-title").update(f" {self.mode.upper()} ")
        if self.current_table:
            self.load_table(self.current_table)

    def action_delete_table(self):
        # Determine table to delete (from sidebar or currently viewed)
        table_list = self.query_one("#table-list", ListView)
        if table_list.highlighted_child:
            table_name = table_list.highlighted_child.table_name
        elif self.current_table:
            table_name = self.current_table
        else:
            return

        def on_confirm(do_delete):
            if do_delete:
                try:
                    self.cursor.execute(f"DROP TABLE {table_name}")
                    self.conn.commit()
                    self.notify(f"Table '{table_name}' deleted")
                    # Refresh table list
                    new_tables = self.get_tables()
                    table_list.clear()
                    for t in new_tables:
                        table_list.append(TableListItem(t))
                    if new_tables:
                        self.load_table(new_tables[0])
                    else:
                        self.query_one("#data-table").clear(columns=True)
                        self.current_table = None
                except Exception as e:
                    self.notify(f"Delete failed: {e}", severity="error")

        self.push_screen(ConfirmScreen(f"Delete table '{table_name}'?"), on_confirm)

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
            self.notify("Editing only allowed in View mode", severity="error")
            return
            
        if not isinstance(self.focused, DataTable) or not self.current_table:
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
                    query = f"UPDATE {self.current_table} SET {column_name} = ? WHERE rowid = ?"
                    self.cursor.execute(query, (typed_value, row_id))
                    self.conn.commit()
                    self.notify("Updated")
                    # Reload table but preserve cursor
                    self.load_table(self.current_table)
                except Exception as e:
                    self.notify(f"Update failed: {e}", severity="error")

        self.push_screen(EditCellScreen(current_value), perform_update)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dbman <database_file>")
        sys.exit(1)
    
    app = DbMan(sys.argv[1])
    app.run()
