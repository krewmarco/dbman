"""Turning a provider's page into the table dbman shows.

Two steps, kept apart because export wants only the first:

1. source_table: what the table *is* - the provider's columns, each stamped
   with where it came from (Column.source), plus dbman's computed columns.
   This is dbman's table schema, as opposed to the provider schema Schema
   mode (`m`) shows: the provider's columns only, as the backend declares
   them. Per-column facts about the dbman table (source, key, computed
   function) surface in ColumnMetadataTable (`e` in column mode).
2. arrange: how it's laid out - hidden/order from ViewSettings, then key
   columns pinned to the left.

Everything here is UI-side; no provider knows about computed columns or
sources.
"""
from dataclasses import replace
from typing import Optional

import computed
from view_settings import ViewSettings, apply_view_settings


def source_table(columns, rows, source: str, bindings) -> tuple[list, list, list[str]]:
    """(columns, rows, errors): the provider's columns stamped with
    `source` (the connection's name), then one column per computed binding.
    See computed.apply_computed for how a broken binding degrades."""
    columns = [c if c.source else replace(c, source=source) for c in columns]
    if not bindings:
        return columns, rows, []
    return computed.apply_computed(columns, rows, bindings)


def pinned_columns(columns) -> list:
    """The key columns, which stay at the left edge."""
    return [c for c in columns if c.primary_key and c.source != computed.SOURCE]


def arrange(columns, rows, settings: Optional[ViewSettings]) -> tuple[list, list, int]:
    """(display_columns, display_rows, pinned_count). Applies hidden/order,
    then moves visible key columns to the front, keeping their relative
    order. The first `pinned_count` columns are the pinned ones: the caller
    hands that to DataTable.fixed_columns, which keeps them on screen when
    scrolling sideways and paints them with the themed fixed-column style.

    A key column's saved order position is ignored while it's pinned. Hiding
    one (`z`) unpins it with it."""
    columns, rows = apply_view_settings(columns, rows, settings or ViewSettings())
    pinned = pinned_columns(columns)
    if not pinned:
        return columns, rows, 0
    rest = [c for c in columns if c not in pinned]
    ordered = pinned + rest
    indices = [columns.index(c) for c in ordered]
    rows = [[row[i] for i in indices] for row in rows]
    return ordered, rows, len(pinned)
