"""Provider abstraction: lets DbMan's UI drive different backend types
(relational DBs via SQLAlchemy, document DBs like CouchDB, ...) through one
interface. See CLAUDE.md for the architecture writeup."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


# Sentinels for the "is this cell empty?" filter conditions in a *list*
# filter value (see get_page's `filters` contract). The string filter path
# keeps its human-typed "empty"/"not empty" keywords, but a picker-built
# list holds real option names, and an enum column could legitimately have
# an option literally named "empty" - hence a value no option name can be.
FILTER_EMPTY = "\x00empty"
FILTER_NOT_EMPTY = "\x00not-empty"


@dataclass(frozen=True)
class ColumnOption:
    """One choice in an enum-like column (a Notion select/status/multi_select
    option). `color` is the provider's own palette name (Notion's
    "red"/"brown"/"default"/...), left untranslated here - mapping it to a
    terminal style is the UI's job, see cell_render.py."""
    name: str
    color: Optional[str] = None


@dataclass(frozen=True)
class Column:
    name: str
    type_name: str
    nullable: bool = True
    default: Optional[str] = None
    primary_key: bool = False
    inferred: bool = False  # True when sampled from data rather than a declared schema
    read_only: bool = False  # True for computed/system columns (e.g. Notion formula/rollup)
    # A non-empty `options` means this column is enum-like: the UI offers a
    # picker (for editing *and* filtering) instead of a free-text box, which
    # is both the expected gesture and the only safe one - writing an
    # arbitrary string to a Notion select silently creates a new option on
    # the user's real database. A tuple, not a list, because Column is
    # frozen and therefore hashable.
    options: tuple = ()
    multi_value: bool = False  # a cell holds several options at once (Notion multi_select)


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
    create_table: bool = False
    diagram: bool = True
    lookup_plugin: bool = False
    truncate_column: bool = True
    whole_row_edit: bool = False
    open_in_browser: bool = False  # row edit hands off to an external app/page instead of an in-app modal
    delete_item: bool = True
    add_row: bool = False
    delete_row: bool = False
    reorder_row: bool = False  # persistent manual row ordering (shift+j/k) - see Provider.move_row
    sort_column: bool = False  # single-column ORDER BY pushed into get_page - see Provider.is_sortable


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

    def is_sortable(self, item_type: str) -> bool:
        """Same scope as is_filterable - sort and filter are both
        query-modifying params pushed into get_page, not something that
        makes sense on a schema pane or a synthetic plugin table."""
        return item_type == "table"

    @abstractmethod
    def get_schema(self, name: str, item_type: str) -> list[Column]: ...

    @abstractmethod
    def get_page(
        self,
        name: str,
        item_type: str,
        filters: dict[str, "str | list[str]"],
        cursor: Optional[str],
        page_size: Optional[int],
        sort: Optional[tuple[str, str]] = None,
    ) -> RowPage:
        """`filters` maps a column name to one of two shapes:

          * a `str` - the free-text path: the "null"/"not null"/"empty"/
            "not empty" keywords, else a substring match (LIKE/$regex/
            Notion's `contains`).
          * a `list[str]` - the picker path, used for enum-like columns
            (Column.options): an OR of exact matches, where an entry may be
            the FILTER_EMPTY / FILTER_NOT_EMPTY sentinel. An empty list
            never reaches a provider; the UI drops the key instead.

        A union rather than a richer object on purpose - every provider's
        existing string handling stays untouched and each adds exactly one
        `isinstance(val, list)` branch.

        `sort`, when given, is (column_name, "asc" | "desc") - a single
        column, pushed into the underlying query/re-fetch rather than
        applied client-side, so it composes correctly with paging. Only
        meaningful for providers with capabilities.sort_column = True;
        callers should not pass it otherwise (dbman.py gates the 'o'
        keybinding on the capability, mirroring is_sortable/is_filterable)."""
        ...

    @abstractmethod
    def get_definition(self, name: str, item_type: str) -> str: ...

    def definition_language(self, item_type: str) -> str:
        return "sql"

    def default_definition_template(self) -> str:
        return "CREATE VIEW new_view AS\nSELECT * FROM table_name"

    def create_view(self, definition_text: str) -> None:
        raise NotImplementedError

    def default_table_template(self) -> str:
        return "CREATE TABLE new_table (\n    id INTEGER PRIMARY KEY,\n    name TEXT\n)"

    def create_table_definition(self, definition_text: str) -> None:
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
        providers with capabilities.add_row = True. A provider that can't
        create a blank row for a particular table (SQLite: a NOT NULL column
        with no default, which needs a schema-aware form) raises with a
        readable reason rather than guessing values."""
        raise NotImplementedError

    @abstractmethod
    def delete_item(self, name: str, item_type: str) -> None: ...

    def delete_row(self, name: str, item_type: str, row_key: RowKey) -> None:
        """Delete/archive a single row, distinct from delete_item's
        table/view-level granularity. Only relevant/implemented for
        providers with capabilities.delete_row = True."""
        raise NotImplementedError

    def reorder_rows(self, name: str, item_type: str, ordered_row_keys: list) -> None:
        """Persist a new relative order for a contiguous run of rows that
        moved locally (shift+j/k in the UI), then survive a reload/
        reconnect. `ordered_row_keys` is the new desired sequence for
        exactly the rows whose position changed - dbman.py's
        action_move_row moves the row locally and instantly with no network
        call, debounces, and calls this once per settled burst rather than
        once per keystroke (see _sync_row_order).

        A correct implementation only needs to reassign the ordinals these
        rows already have among themselves (a pure permutation) rather than
        renumbering - that keeps it safe regardless of how the rest of the
        table's rows are paginated/ordered, and avoids re-deriving each
        row's position from a live query mid-batch, which can race against
        a backend whose query/index layer is only eventually consistent
        with recent writes (a real issue hit against Notion's query
        endpoint when this was implemented and tested against a live page -
        seconds-old writes could still read stale in a tight loop).

        Only relevant/implemented for providers with
        capabilities.reorder_row = True. No backend used by dbman has a
        native "manual row order" concept, so each implementation
        necessarily owns an explicit ordinal of its own (a sidecar table, a
        real property, ...) - the specifics vary enough per backend that
        there's no shared implementation here beyond this interface."""
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
