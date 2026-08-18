"""Provider abstraction: lets DbMan's UI drive different backend types
(relational DBs via SQLAlchemy, document DBs like CouchDB, ...) through one
interface. See CLAUDE.md for the architecture writeup."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Column:
    name: str
    type_name: str
    nullable: bool = True
    default: Optional[str] = None
    primary_key: bool = False
    inferred: bool = False  # True when sampled from data rather than a declared schema
    read_only: bool = False  # True for computed/system columns (e.g. Notion formula/rollup)


@dataclass(frozen=True)
class RowKey:
    """Opaque per-row identity used to target an update/delete.
    SQLite: an int rowid. CouchDB: {"_id": ..., "_rev": ...}."""
    value: Any


@dataclass
class RowPage:
    columns: list[Column]
    rows: list[list[Any]]
    row_keys: list[RowKey]
    next_cursor: Optional[str]
    has_more: bool
    raw_rows: Optional[list[Any]] = None


@dataclass(frozen=True)
class Capabilities:
    definition_pane: bool = True
    create_definition: bool = True
    diagram: bool = True
    lookup_plugin: bool = False
    truncate_column: bool = True
    whole_row_edit: bool = False
    open_in_browser: bool = False  # row edit hands off to an external app/page instead of an in-app modal
    delete_item: bool = True
    add_row: bool = False
    delete_row: bool = False


@dataclass
class DiagramNode:
    name: str
    columns: list[Column]
    primary_keys: list[str]


@dataclass
class DiagramEdge:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class DiagramModel:
    nodes: list[DiagramNode] = field(default_factory=list)
    edges: list[DiagramEdge] = field(default_factory=list)


class Provider(ABC):
    """Common interface a dbman backend implements. Add a new provider by
    subclassing this, setting `capabilities` honestly, and adding one
    `elif` branch to `create_provider` in providers/__init__.py."""

    capabilities: Capabilities

    @abstractmethod
    def list_tables(self) -> list[str]: ...

    @abstractmethod
    def list_views(self) -> list[str]: ...

    def is_editable(self, item_type: str) -> bool:
        return item_type == "table"

    def is_filterable(self, item_type: str) -> bool:
        return item_type == "table"

    @abstractmethod
    def get_schema(self, name: str, item_type: str) -> list[Column]: ...

    @abstractmethod
    def get_page(
        self,
        name: str,
        item_type: str,
        filters: dict[str, str],
        cursor: Optional[str],
        page_size: Optional[int],
    ) -> RowPage: ...

    @abstractmethod
    def get_definition(self, name: str, item_type: str) -> str: ...

    def definition_language(self, item_type: str) -> str:
        return "sql"

    def default_definition_template(self) -> str:
        return "CREATE VIEW new_view AS\nSELECT * FROM table_name"

    def create_view(self, definition_text: str) -> None:
        raise NotImplementedError

    def update_view_definition(self, name: str, definition_text: str) -> None:
        raise NotImplementedError

    def update_cell(
        self, name: str, item_type: str, row_key: RowKey, column: str, new_value: str
    ) -> RowKey:
        raise NotImplementedError

    def get_lookup_options(
        self, table: str, key_column: str, display_column: str
    ) -> list[tuple[str, Any]]:
        """(display, key) pairs for a FK dropdown. Only relevant/implemented
        for providers with capabilities.lookup_plugin = True."""
        raise NotImplementedError

    def get_row_url(self, name: str, item_type: str, row_key: RowKey) -> str:
        """A URL that opens this row for full viewing/editing in its native
        app (e.g. a Notion page's own share link). Only relevant/implemented
        for providers with capabilities.open_in_browser = True."""
        raise NotImplementedError

    def update_row_json(
        self, name: str, item_type: str, row_key: RowKey, new_json_text: str
    ) -> RowKey:
        raise NotImplementedError

    def add_row(self, name: str, item_type: str) -> RowKey:
        """Create a new, minimal row/document. Only relevant/implemented for
        providers with capabilities.add_row = True — currently CouchDB's
        freeform documents. SQL tables need a schema-aware form (typed
        defaults for NOT NULL columns), deferred to a future dynamic-forms
        layer rather than implemented here."""
        raise NotImplementedError

    @abstractmethod
    def delete_item(self, name: str, item_type: str) -> None: ...

    def delete_row(self, name: str, item_type: str, row_key: RowKey) -> None:
        """Delete/archive a single row, distinct from delete_item's
        table/view-level granularity. Only relevant/implemented for
        providers with capabilities.delete_row = True."""
        raise NotImplementedError

    def count_over_length(self, name: str, column: str, target_len: int) -> int:
        raise NotImplementedError

    def truncate_column(self, name: str, column: str, target_len: int) -> int:
        raise NotImplementedError

    def get_diagram_model(self) -> DiagramModel:
        raise NotImplementedError

    def sqlalchemy_engine(self):
        """Only SqlAlchemyProvider overrides this; used to feed LookupPlugin,
        which is inherently relational (inspector.get_foreign_keys())."""
        raise NotImplementedError
