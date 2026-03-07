import sqlite3
from textual.app import ComposeResult
from textual.widgets import DataTable, Select, Label, Button, Static
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen

class LookupPlugin:
    def __init__(self, db_conn):
        self.conn = db_conn
        self.ensure_config_table()

    def ensure_config_table(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS _dbman_lookup_config (
                table_name TEXT,
                column_name TEXT,
                related_table TEXT,
                related_key TEXT,
                display_column TEXT,
                PRIMARY KEY (table_name, column_name)
            )
        """)
        self.conn.commit()

    def get_foreign_keys(self):
        """Scans the database for all foreign keys."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_dbman_%'")
        tables = [row[0] for row in cursor.fetchall()]
        
        fks = []
        for table in tables:
            cursor.execute(f"PRAGMA foreign_key_list({table})")
            for row in cursor.fetchall():
                # row: id, seq, table, from, to, on_update, on_delete, match
                fks.append({
                    "table": table,
                    "from": row[3],
                    "to_table": row[2],
                    "to_column": row[4]
                })
        return fks

    def get_config_data(self):
        """Returns the configuration data for the UI."""
        fks = self.get_foreign_keys()
        cursor = self.conn.cursor()
        cursor.execute("SELECT table_name, column_name, display_column FROM _dbman_lookup_config")
        saved_configs = {(r[0], r[1]): r[2] for r in cursor.fetchall()}
        
        rows = []
        for fk in fks:
            display_col = saved_configs.get((fk["table"], fk["from"]), "")
            rows.append((
                fk["table"], 
                fk["from"], 
                fk["to_table"], 
                fk["to_column"],
                display_col
            ))
        return rows

    def save_config(self, table, column, related_table, related_key, display_column):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO _dbman_lookup_config 
            (table_name, column_name, related_table, related_key, display_column)
            VALUES (?, ?, ?, ?, ?)
        """, (table, column, related_table, related_key, display_column))
        self.conn.commit()

    def get_lookup_config(self, table, column):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT related_table, related_key, display_column 
            FROM _dbman_lookup_config 
            WHERE table_name = ? AND column_name = ?
        """, (table, column))
        return cursor.fetchone()

class LookupSelectScreen(ModalScreen):
    """A screen with a dropdown to select a related value."""
    CSS = """
    LookupSelectScreen {
        background: rgba(0, 0, 0, 0.5);
        align: center middle;
    }
    #lookup-dialog {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    Select {
        margin: 1 0;
    }
    """
    def __init__(self, title, options, current_value):
        super().__init__()
        self.dialog_title = title
        self.options = options
        self.current_value = current_value

    def compose(self) -> ComposeResult:
        with Vertical(id="lookup-dialog"):
            yield Label(self.dialog_title)
            yield Select(self.options, value=self.current_value, id="lookup-select")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="success", id="save")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.dismiss(self.query_one(Select).value)
        else:
            self.dismiss(None)

class LookupConfigScreen(ModalScreen):
    """Screen to pick which column from the related table to use for display."""
    def __init__(self, table, fk_col, related_table, related_key, columns):
        super().__init__()
        self.table = table
        self.fk_col = fk_col
        self.related_table = related_table
        self.related_key = related_key
        self.columns = columns

    def compose(self) -> ComposeResult:
        with Vertical(id="lookup-dialog"):
            yield Label(f"Pick display column for {self.related_table}")
            yield Select([(c, c) for c in self.columns], id="col-select")
            yield Button("Save", variant="success")

    def on_select_changed(self, event: Select.Changed):
        self.dismiss(event.value)
