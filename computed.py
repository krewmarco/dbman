"""Computed columns: read-only columns whose values come from a Python
function rather than the backend.

A binding is a row in dbman.sqlite's `computed_columns` table: (connection,
item, column_name, function), where `function` is "module:func" and the
module lives in `<cwd>/dbman_plugins/` - the project directory, next to
dbman.sqlite, so a project's functions travel with its config rather than
with dbman. The function gets one row as a {column: value} dict and returns
the cell's value; None shows as an empty cell.

Applied in `load_item` after `get_page` and before `apply_view_settings`,
so a computed column hides, reorders, sizes, searches and exports like any
other. The provider never sees it, which is why filtering and sorting on it
are refused.
"""
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from meta_db import MetaDb
from providers.base import Column

PLUGIN_DIR = "dbman_plugins"
ERROR_VALUE = "#ERR"


@dataclass(frozen=True)
class ComputedColumn:
    name: str
    function: str


def load_function(spec: str, base_dir: Optional[Path] = None) -> Callable[[dict], Any]:
    """Resolve "module:func" from `<base_dir>/dbman_plugins/`. Raises
    ValueError with a readable message on any failure. Modules are cached by
    Python's import system, so a module's own state (an http session, a
    parsed cache) lives for the whole dbman session; edits to the module need
    a restart."""
    module_name, sep, func_name = str(spec or "").strip().partition(":")
    if not sep or not module_name or not func_name:
        raise ValueError(f'function must look like "module:func", not {spec!r}')
    plugin_dir = str((base_dir or Path.cwd()) / PLUGIN_DIR)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise ValueError(f"can't import {module_name} from {plugin_dir}: {e}") from e
    func = getattr(module, func_name, None)
    if not callable(func):
        raise ValueError(f"{module_name} has no function {func_name}")
    return func


def read_bindings(connection: str, item: str, base_dir: Optional[Path] = None) -> list[ComputedColumn]:
    """The complete bindings for one table/view, in id order. Rows with any
    field blank (a fresh `a` row) are skipped."""
    with MetaDb(base_dir).read() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT column_name, function FROM computed_columns"
            " WHERE connection = ? AND item = ?"
            " AND column_name IS NOT NULL AND column_name != ''"
            " AND function IS NOT NULL AND function != ''"
            " ORDER BY id",
            (connection, item),
        ).fetchall()
    return [ComputedColumn(name, function) for name, function in rows]


def apply_computed(columns: list[Column], rows: list, bindings: list[ComputedColumn],
                   base_dir: Optional[Path] = None) -> tuple[list[Column], list[tuple], list[str]]:
    """Append one read-only column per binding and compute it for every row.
    Returns (columns, rows, errors): a binding whose function won't load is
    skipped, and a function that raises on a row shows ERROR_VALUE in that
    cell; either way one message per binding ends up in `errors`, never an
    exception, so a broken plugin can't take a page load down with it."""
    errors: list[str] = []
    taken = {c.name for c in columns}
    loaded = []
    for b in bindings:
        if b.name in taken:
            errors.append(f"computed column {b.name!r} clashes with a real column; skipped")
            continue
        try:
            loaded.append((b, load_function(b.function, base_dir)))
        except ValueError as e:
            errors.append(f"computed column {b.name!r}: {e}")
            continue
        taken.add(b.name)
    if not loaded:
        return columns, rows, errors

    names = [c.name for c in columns]
    failed: set[str] = set()
    out_rows = []
    for row in rows:
        as_dict = dict(zip(names, row))
        extra = []
        for b, func in loaded:
            try:
                extra.append(func(dict(as_dict)))
            except Exception as e:
                if b.name not in failed:
                    failed.add(b.name)
                    errors.append(f"computed column {b.name!r}: {type(e).__name__}: {e}")
                extra.append(ERROR_VALUE)
        out_rows.append(tuple(row) + tuple(extra))
    new_columns = list(columns) + [
        Column(name=b.name, type_name="TEXT", read_only=True) for b, _ in loaded
    ]
    return new_columns, out_rows, errors
