import os
from sqlalchemy import create_engine, inspect, text, MetaData, Table, Column, String, select, insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from textual.app import ComposeResult
from textual.widgets import DataTable, Select, Label, Button, Static
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen

class LookupPlugin:
    def __init__(self, engine):
        self.engine = engine
        self.inspector = inspect(self.engine)
        self.ensure_config_table()

    def ensure_config_table(self):
        metadata = MetaData()
        table = Table(
            '_dbman_lookup_config', metadata,
            Column('table_name', String, primary_key=True),
            Column('column_name', String, primary_key=True),
            Column('related_table', String),
            Column('related_key', String),
            Column('display_column', String)
        )
        metadata.create_all(self.engine)

    def get_foreign_keys(self):
        """Scans the database for all foreign keys."""
        tables = self.inspector.get_table_names()
        fks = []
        for table in tables:
            if table.startswith("sqlite_") or table.startswith("_dbman_"):
                continue
            for fk in self.inspector.get_foreign_keys(table):
                # fk: {'name': ..., 'constrained_columns': [...], 'referred_schema': ..., 'referred_table': ..., 'referred_columns': [...]}
                for i in range(len(fk['constrained_columns'])):
                    fks.append({
                        "table": table,
                        "from": fk['constrained_columns'][i],
                        "to_table": fk['referred_table'],
                        "to_column": fk['referred_columns'][i]
                    })
        return fks

    def get_config_data(self):
        """Returns the configuration data for the UI."""
        fks = self.get_foreign_keys()
        
        metadata = MetaData()
        config_table = Table('_dbman_lookup_config', metadata, autoload_with=self.engine)
        
        with self.engine.connect() as conn:
            stmt = select(config_table)
            res = conn.execute(stmt)
            saved_configs = {(r[0], r[1]): r[4] for r in res}
        
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

    def save_config(self, table_name, column, related_table, related_key, display_column):
        metadata = MetaData()
        config_table = Table('_dbman_lookup_config', metadata, autoload_with=self.engine)
        
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                stmt = sqlite_insert(config_table).values(
                    table_name=table_name,
                    column_name=column,
                    related_table=related_table,
                    related_key=related_key,
                    display_column=display_column
                ).on_conflict_do_update(
                    index_elements=['table_name', 'column_name'],
                    set_=dict(related_table=related_table, related_key=related_key, display_column=display_column)
                )
            else:
                # Generic UPSERT is harder in SQLAlchemy Core across all DBs
                # We'll do a delete then insert for now as a simple fallback
                conn.execute(config_table.delete().where(
                    config_table.c.table_name == table_name,
                    config_table.c.column_name == column
                ))
                stmt = insert(config_table).values(
                    table_name=table_name,
                    column_name=column,
                    related_table=related_table,
                    related_key=related_key,
                    display_column=display_column
                )
            conn.execute(stmt)
            conn.commit()

    def find_foreign_key(self, table, column):
        """Checks if a specific column is a foreign key."""
        try:
            for fk in self.inspector.get_foreign_keys(table):
                if column in fk['constrained_columns']:
                    idx = fk['constrained_columns'].index(column)
                    return {
                        "to_table": fk['referred_table'],
                        "to_column": fk['referred_columns'][idx]
                    }
        except:
            pass
        return None

    def get_lookup_config(self, table_name, column):
        metadata = MetaData()
        config_table = Table('_dbman_lookup_config', metadata, autoload_with=self.engine)
        
        with self.engine.connect() as conn:
            stmt = select(config_table.c.related_table, config_table.c.related_key, config_table.c.display_column).where(
                config_table.c.table_name == table_name,
                config_table.c.column_name == column
            )
            res = conn.execute(stmt).fetchone()
            if res:
                return res
        
        # Auto-detect if it's a FK but not yet configured
        fk = self.find_foreign_key(table_name, column)
        if fk:
            return (fk["to_table"], fk["to_column"], fk["to_column"])
        
        return None

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
        self.options = [(str(opt[0]), str(opt[1])) for opt in options]
        self.current_value = str(current_value) if current_value is not None else None

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
