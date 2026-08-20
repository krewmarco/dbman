from sqlalchemy import (
    create_engine, inspect, text, MetaData, Table, select, update, func,
)
from sqlalchemy.exc import SQLAlchemyError

from .base import (
    Provider, Column, RowKey, RowPage, Capabilities,
    DiagramModel, DiagramNode, DiagramEdge,
)


class SqlAlchemyProvider(Provider):
    """Relational-database provider (SQLite primarily; Postgres/MySQL best-effort)
    backed by SQLAlchemy Core. No ORM, no caching — everything is reflected live."""

    def __init__(self, db_url: str):
        self.db_url = db_url
        self.engine = create_engine(db_url)
        self.capabilities = Capabilities(
            definition_pane=True,
            create_definition=True,
            create_table=True,
            diagram=True,
            lookup_plugin=True,
            truncate_column=True,
            whole_row_edit=False,
            delete_item=True,
        )

    def sqlalchemy_engine(self):
        return self.engine

    @property
    def inspector(self):
        # A fresh Inspector every access, not stored on self: SQLAlchemy's
        # Inspector caches reflection results internally, which would mask
        # tables/views created or dropped through the app (e.g. right after
        # `a`-on-a-header creates one) until dbman restarts - contrary to
        # the "reflected live" behavior the rest of this provider relies on.
        return inspect(self.engine)

    def list_tables(self) -> list[str]:
        tables = self.inspector.get_table_names()
        return [t for t in tables if not t.startswith("sqlite_") and not t.startswith("_dbman_")]

    def list_views(self) -> list[str]:
        return self.inspector.get_view_names()

    def get_schema(self, name, item_type) -> list[Column]:
        columns_info = self.inspector.get_columns(name)
        result = []
        for col in columns_info:
            default = col.get("default")
            result.append(Column(
                name=col.get("name"),
                type_name=str(col.get("type")),
                nullable=col.get("nullable", True),
                default=str(default) if default is not None else None,
                primary_key=bool(col.get("primary_key")),
            ))
        return result

    def _build_filter_clause(self, table_obj, filters):
        clauses = []
        for col_name, val in filters.items():
            col = table_obj.c[col_name]
            if val.lower() == "null":
                clauses.append(col.is_(None))
            elif val.lower() == "empty":
                clauses.append((col.is_(None)) | (col == ""))
            else:
                clauses.append(col.like(f"%{val}%"))
        return clauses

    def get_page(self, name, item_type, filters, cursor, page_size) -> RowPage:
        metadata = MetaData()
        table = Table(name, metadata, autoload_with=self.engine)
        col_objs = [Column(name=c.name, type_name=str(c.type)) for c in table.columns]
        filter_clauses = self._build_filter_clause(table, filters)
        offset = int(cursor) if cursor else 0
        # Fetch one extra row so we can tell whether another page follows,
        # without a separate COUNT(*) query.
        fetch_limit = page_size + 1 if page_size else None

        with self.engine.connect() as conn:
            if item_type == "table" and self.engine.dialect.name == "sqlite":
                try:
                    rowid_stmt = select(text("rowid"), table)
                    if filter_clauses:
                        rowid_stmt = rowid_stmt.where(*filter_clauses)
                    if fetch_limit:
                        rowid_stmt = rowid_stmt.limit(fetch_limit)
                    if offset:
                        rowid_stmt = rowid_stmt.offset(offset)
                    result = conn.execute(rowid_stmt)
                    raw = [list(row) for row in result]
                    has_more = bool(page_size) and len(raw) > page_size
                    if has_more:
                        raw = raw[:page_size]
                    rows = [r[1:] for r in raw]
                    row_keys = [RowKey(r[0]) for r in raw]
                    next_cursor = str(offset + page_size) if has_more else None
                    return RowPage(col_objs, rows, row_keys, next_cursor=next_cursor, has_more=has_more)
                except SQLAlchemyError:
                    pass  # fall through: this table has no usable rowid

            stmt = select(table)
            if filter_clauses:
                stmt = stmt.where(*filter_clauses)
            if fetch_limit:
                stmt = stmt.limit(fetch_limit)
            if offset:
                stmt = stmt.offset(offset)
            result = conn.execute(stmt)
            rows = [list(row) for row in result]
            has_more = bool(page_size) and len(rows) > page_size
            if has_more:
                rows = rows[:page_size]
            row_keys = [RowKey(None) for _ in rows]
            next_cursor = str(offset + page_size) if has_more else None
            return RowPage(col_objs, rows, row_keys, next_cursor=next_cursor, has_more=has_more)

    def get_definition(self, name, item_type) -> str:
        try:
            if self.engine.dialect.name == "sqlite":
                with self.engine.connect() as conn:
                    res = conn.execute(
                        text("SELECT sql FROM sqlite_master WHERE name = :name"), {"name": name}
                    ).fetchone()
                    return res[0] if res else "Not found"
            elif self.engine.dialect.name == "postgresql":
                return f"-- SQL View/Table definition for {name} (Postgres support limited)"
            return f"-- SQL View/Table definition for {name}"
        except Exception as e:
            return f"Error fetching SQL: {e}"

    def _execute_ddl(self, definition_text) -> None:
        with self.engine.connect() as conn:
            conn.execute(text(definition_text))
            conn.commit()

    def create_view(self, definition_text) -> None:
        self._execute_ddl(definition_text)

    def create_table_definition(self, definition_text) -> None:
        self._execute_ddl(definition_text)

    def update_view_definition(self, name, definition_text) -> None:
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
            conn.execute(text(definition_text))
            conn.commit()

    def get_lookup_options(self, table, key_column, display_column):
        metadata = MetaData()
        rt = Table(table, metadata, autoload_with=self.engine)
        with self.engine.connect() as conn:
            res = conn.execute(select(rt.c[key_column], rt.c[display_column]))
            return [(str(r[1]), r[0]) for r in res]

    def update_cell(self, name, item_type, row_key: RowKey, column, value) -> RowKey:
        metadata = MetaData()
        t = Table(name, metadata, autoload_with=self.engine)
        if self.engine.dialect.name == "sqlite":
            with self.engine.connect() as conn:
                stmt = update(t).where(text("rowid = :rid")).values({column: value})
                conn.execute(stmt, {"rid": row_key.value})
                conn.commit()
        else:
            pass  # PK logic needed for non-sqlite
        return row_key

    def delete_item(self, name, item_type) -> None:
        metadata = MetaData()
        table = Table(name, metadata, autoload_with=self.engine)
        table.drop(self.engine)

    def get_max_length(self, name, column) -> int:
        with self.engine.connect() as conn:
            metadata = MetaData()
            t = Table(name, metadata, autoload_with=self.engine)
            col = t.c[column]
            return conn.execute(select(func.max(func.length(col)))).scalar() or 0

    def count_over_length(self, name, column, target_len) -> int:
        with self.engine.connect() as conn:
            metadata = MetaData()
            t = Table(name, metadata, autoload_with=self.engine)
            col = t.c[column]
            stmt = select(func.count()).where(func.length(col) > target_len)
            return conn.execute(stmt).scalar()

    def truncate_column(self, name, column, target_len) -> int:
        with self.engine.connect() as conn:
            metadata = MetaData()
            t = Table(name, metadata, autoload_with=self.engine)
            col = t.c[column]
            stmt = update(t).values({column: func.substr(col, 1, target_len)})
            result = conn.execute(stmt)
            conn.commit()
            return result.rowcount

    def get_diagram_model(self) -> DiagramModel:
        model = DiagramModel()
        for table_name in sorted(self.list_tables()):
            columns_info = self.inspector.get_columns(table_name)
            pk_constraint = self.inspector.get_pk_constraint(table_name)
            pks = pk_constraint.get("constrained_columns", [])
            fks = self.inspector.get_foreign_keys(table_name)

            columns = [
                Column(name=c["name"], type_name=str(c["type"]), primary_key=c["name"] in pks)
                for c in columns_info
            ]
            model.nodes.append(DiagramNode(name=table_name, columns=columns, primary_keys=pks))

            for fk in fks:
                ref_table = fk["referred_table"]
                ref_cols = fk["referred_columns"]
                cons_cols = fk["constrained_columns"]
                model.edges.append(DiagramEdge(
                    from_table=table_name, from_column=cons_cols[0],
                    to_table=ref_table, to_column=ref_cols[0],
                ))
        return model
