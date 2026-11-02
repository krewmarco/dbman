"""Computed columns: read-only columns whose values come from a Python
function rather than the backend.

A binding is a row in dbman.sqlite's `computed_columns` table: (connection,
item, column_name, expression). The expression names a function in a module
in `<cwd>/dbman_plugins/` - the project directory, next to dbman.sqlite, so a
project's functions travel with its config rather than with dbman - and says
what to pass it:

    bst.bst_url(row.ref, row.translation)   columns as arguments
    bst.bst_url(row["Verse Ref"], "ESV")    a column name that isn't an
                                            identifier; literals
    bst.bst_url                             no call: the whole row as a dict

It's Python syntax, parsed with `ast` and never evaluated: exactly one call
of `module.func`, whose arguments are `row.<col>`, `row["col"]`, or a
literal. Anything cleverer belongs in the plugin, not in a cell. The older
"module:func" form still reads as `module.func`.

Applied by table_model.source_table, right after `get_page` and before
view settings, so a computed column hides, reorders, sizes, searches and
exports like any other. The provider never sees it, which is why filtering
and sorting on it are refused. A column with no expression yet (fresh from
`a`/`A` in column mode) shows empty; a broken one shows ERROR_VALUE in every
cell rather than disappearing, so it can still be selected and fixed.
"""
import ast
import importlib
import inspect
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from meta_db import MetaDb
from providers.base import Column

PLUGIN_DIR = "dbman_plugins"
ERROR_VALUE = "#ERR"
# Column.source for a column dbman computes (see providers/base.py).
SOURCE = "computed"


@dataclass(frozen=True)
class ComputedColumn:
    name: str
    expression: Optional[str]


@dataclass(frozen=True)
class Arg:
    """One argument: a column's value, or a literal."""
    column: Optional[str] = None
    value: Any = None

    def __str__(self):
        if self.column is None:
            return repr(self.value)
        return f"row.{self.column}" if self.column.isidentifier() else f"row[{self.column!r}]"


@dataclass(frozen=True)
class Expression:
    module: str
    func: str
    # None: no call, the function gets the whole row.
    args: Optional[tuple] = None
    kwargs: tuple = field(default=())  # ((name, Arg), ...)

    @property
    def columns(self) -> list[str]:
        if self.args is None:
            return []
        args = list(self.args) + [a for _, a in self.kwargs]
        return [a.column for a in args if a.column is not None]

    def __str__(self):
        name = f"{self.module}.{self.func}"
        if self.args is None:
            return name
        parts = [str(a) for a in self.args] + [f"{k}={a}" for k, a in self.kwargs]
        return f"{name}({', '.join(parts)})"


_SHAPE = 'expected module.func(row.column, ...), e.g. bst.bst_url(row.ref, row.translation)'


def _parse_arg(node) -> Arg:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return Arg(value=node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, (int, float)):
        return Arg(value=-node.operand.value)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "row":
        return Arg(column=node.attr)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "row" \
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return Arg(column=node.slice.value)
    raise ValueError(f"argument {ast.unparse(node)!r} isn't row.<column>, row[\"<column>\"] or a literal")


def parse_expression(text: str) -> Expression:
    """Parse, never evaluate. Raises ValueError with a readable message."""
    text = str(text or "").strip()
    if not text:
        raise ValueError(f"empty expression; {_SHAPE}")
    # Legacy "module:func" (schema v5) - same thing, older spelling.
    if ":" in text and "(" not in text and "." not in text:
        text = text.replace(":", ".", 1)
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        raise ValueError(f"can't parse {text!r}; {_SHAPE}") from None
    call = node if isinstance(node, ast.Call) else None
    target = call.func if call else node
    if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
        raise ValueError(f"{text!r} doesn't name module.func; {_SHAPE}")
    module, func = target.value.id, target.attr
    if call is None:
        return Expression(module, func)
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        raise ValueError("*args/**kwargs aren't supported")
    args = tuple(_parse_arg(a) for a in call.args)
    kwargs = tuple((k.arg, _parse_arg(k.value)) for k in call.keywords)
    return Expression(module, func, args, kwargs)


def _import(module_name: str, base_dir: Optional[Path] = None):
    plugin_dir = str((base_dir or Path.cwd()) / PLUGIN_DIR)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    try:
        return importlib.import_module(module_name)
    except Exception as e:
        raise ValueError(f"can't import {module_name} from {plugin_dir}: {e}") from e


def load_function(module_name: str, func_name: str, base_dir: Optional[Path] = None) -> Callable:
    """`func_name` from `<base_dir>/dbman_plugins/<module_name>.py`. Raises
    ValueError on any failure. Modules are cached by Python's import system,
    so a module's own state (an http session, a parsed cache) lives for the
    whole dbman session; edits to the module need a restart."""
    module = _import(module_name, base_dir)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise ValueError(f"{module_name} has no function {func_name}")
    return func


def clear_caches(connection: str, item: str, base_dir: Optional[Path] = None) -> int:
    """Clear the memo of every function item's computed columns call, where
    it has one (`functools.cache`/`lru_cache` give `cache_clear`). Called by
    'r': dbman doesn't cache calls itself, since it can't know a function is
    deterministic - the recommended pattern reads a cache file, a hidden
    input - so caching is the plugin author's call, and this is how 'r'
    still means "fresh". Returns how many were cleared; a binding that
    doesn't resolve is skipped (the load reports it)."""
    cleared = set()
    for binding in read_bindings(connection, item, base_dir):
        try:
            expr = parse_expression(binding.expression or "")
            func = load_function(expr.module, expr.func, base_dir)
        except ValueError:
            continue
        if callable(getattr(func, "cache_clear", None)) and func not in cleared:
            func.cache_clear()
            cleared.add(func)
    return len(cleared)


def compile_expression(text: str, base_dir: Optional[Path] = None,
                       columns: Optional[list[str]] = None) -> Callable[[dict], Any]:
    """A `row dict -> value` callable for an expression, checked as far as
    can be without running it: the function exists, the arguments fit its
    signature, and (given `columns`) every referenced column exists.
    Raises ValueError naming what's wrong."""
    expr = parse_expression(text)
    func = load_function(expr.module, expr.func, base_dir)
    if expr.args is None:
        return func
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        signature = None  # a builtin without one: nothing to check against
    if signature is not None:
        try:
            signature.bind(*expr.args, **dict(expr.kwargs))
        except TypeError as e:
            raise ValueError(f"{expr.module}.{expr.func}{_params(signature)}: {e}") from None
    if columns is not None:
        missing = [c for c in expr.columns if c not in columns]
        if missing:
            raise ValueError(
                f"no column {', '.join(repr(c) for c in missing)}; columns are {', '.join(columns)}"
            )
    args, kwargs = expr.args, expr.kwargs

    def value(a: Arg, row: dict):
        return a.value if a.column is None else row[a.column]

    def call(row: dict):
        return func(*[value(a, row) for a in args], **{k: value(a, row) for k, a in kwargs})
    return call


def _params(signature: inspect.Signature) -> str:
    """'(ref, translation='')' - the parameters without annotations."""
    return "(" + ", ".join(str(p.replace(annotation=inspect.Parameter.empty)) for p in signature.parameters.values()) + ")"


# ---- bindings (dbman.sqlite's computed_columns) ----

def read_bindings(connection: str, item: str, base_dir: Optional[Path] = None) -> list[ComputedColumn]:
    """One table/view's bindings, in id order. A row without a column name
    (a fresh `a` row when browsing dbman.sqlite) is skipped; one without an
    expression is a column waiting for one, and shows empty."""
    with MetaDb(base_dir).read() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT column_name, expression FROM computed_columns"
            " WHERE connection = ? AND item = ?"
            " AND column_name IS NOT NULL AND column_name != ''"
            " ORDER BY id",
            (connection, item),
        ).fetchall()
    return [ComputedColumn(name, expression or None) for name, expression in rows]


def add_binding(connection: str, item: str, name: str, expression: Optional[str] = None,
                base_dir: Optional[Path] = None) -> None:
    with MetaDb(base_dir).write() as conn:
        conn.execute("INSERT OR IGNORE INTO connections (name) VALUES (?)", (connection,))
        try:
            conn.execute(
                "INSERT INTO computed_columns (connection, item, column_name, expression) VALUES (?, ?, ?, ?)",
                (connection, item, name, expression),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"a computed column named {name!r} already exists") from None


def set_expression(connection: str, item: str, name: str, expression: Optional[str],
                   base_dir: Optional[Path] = None) -> None:
    with MetaDb(base_dir).write() as conn:
        conn.execute(
            "UPDATE computed_columns SET expression = ? WHERE connection = ? AND item = ? AND column_name = ?",
            (expression, connection, item, name),
        )


def rename_binding(connection: str, item: str, old: str, new: str, base_dir: Optional[Path] = None) -> None:
    with MetaDb(base_dir).write() as conn:
        try:
            conn.execute(
                "UPDATE computed_columns SET column_name = ? WHERE connection = ? AND item = ? AND column_name = ?",
                (new, connection, item, old),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"a computed column named {new!r} already exists") from None


def delete_binding(connection: str, item: str, name: str, base_dir: Optional[Path] = None) -> None:
    with MetaDb(base_dir).write() as conn:
        conn.execute(
            "DELETE FROM computed_columns WHERE connection = ? AND item = ? AND column_name = ?",
            (connection, item, name),
        )


# ---- applying ----

def apply_computed(columns: list[Column], rows: list, bindings: list[ComputedColumn],
                   base_dir: Optional[Path] = None) -> tuple[list[Column], list[tuple], list[str]]:
    """Append one read-only column per binding and compute it for every row.
    Returns (columns, rows, errors). Never raises, so a broken plugin can't
    take a page load down with it: a binding that won't compile (bad
    expression, missing function or column) shows ERROR_VALUE in every
    cell, and a function that raises shows it in that row's cell; either
    way one message per binding ends up in `errors`. Only a name clash with
    a provider column is skipped outright - there'd be two columns with
    one name."""
    errors: list[str] = []
    names = [c.name for c in columns]
    taken = set(names)
    compiled = []  # (binding, callable | None for "no expression" | False for broken)
    for b in bindings:
        if b.name in taken:
            errors.append(f"computed column {b.name!r} clashes with a real column; skipped")
            continue
        taken.add(b.name)
        if not b.expression:
            compiled.append((b, None))
            continue
        try:
            compiled.append((b, compile_expression(b.expression, base_dir, names)))
        except ValueError as e:
            errors.append(f"computed column {b.name!r}: {e}")
            compiled.append((b, False))
    if not compiled:
        return columns, rows, errors

    failed: set[str] = set()
    out_rows = []
    for row in rows:
        as_dict = dict(zip(names, row))
        extra = []
        for b, func in compiled:
            if func is None:
                extra.append(None)
                continue
            if func is False:
                extra.append(ERROR_VALUE)
                continue
            try:
                extra.append(func(dict(as_dict)))
            except Exception as e:
                if b.name not in failed:
                    failed.add(b.name)
                    errors.append(f"computed column {b.name!r}: {type(e).__name__}: {e}")
                extra.append(ERROR_VALUE)
        out_rows.append(tuple(row) + tuple(extra))
    new_columns = list(columns) + [
        Column(name=b.name, type_name="TEXT", read_only=True, source=SOURCE) for b, _ in compiled
    ]
    return new_columns, out_rows, errors


# ---- plugin modules, for the sidebar's reference tables ----

@dataclass(frozen=True)
class PluginFunction:
    name: str
    params: str       # "(ref, translation='')"
    usage: str        # "bst.bst_url(row.ref, row.translation)"
    summary: str      # the docstring's first line
    doc: str          # the whole docstring


def plugin_modules(base_dir: Optional[Path] = None) -> list[str]:
    """Module names in `<base_dir>/dbman_plugins/`, without importing them -
    this runs on every sidebar refresh."""
    directory = (base_dir or Path.cwd()) / PLUGIN_DIR
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.py") if not p.stem.startswith("_"))


def plugin_functions(module_name: str, base_dir: Optional[Path] = None) -> list[PluginFunction]:
    """A module's exported functions: those in its `__all__`, or, without
    one, every public function defined in it (not imported into it).
    Raises ValueError if the module won't import."""
    module = _import(module_name, base_dir)
    names = getattr(module, "__all__", None)
    if names is None:
        names = [
            n for n, obj in vars(module).items()
            if not n.startswith("_") and inspect.isfunction(obj) and obj.__module__ == module.__name__
        ]
    out = []
    for n in names:
        func = getattr(module, n, None)
        if not callable(func):
            continue
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            signature = None
        params = _params(signature) if signature else "(...)"
        # Every positional parameter as a same-named column: a template to
        # copy into Source and adjust, not a claim about the table.
        positional = [
            p.name for p in (signature.parameters.values() if signature else [])
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        if positional == ["row"]:
            usage = f"{module_name}.{n}"  # takes the whole row
        else:
            usage = f"{module_name}.{n}({', '.join(f'row.{p}' for p in positional)})"
        doc = inspect.getdoc(func) or ""
        out.append(PluginFunction(n, params, usage, doc.splitlines()[0] if doc else "", doc))
    return out
