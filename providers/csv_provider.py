"""CSV files as tables: every `.csv` directly inside a list of directories.

A `csv://` connection names no file of its own. Its directory list is
dbman's config, not data, so it lives in dbman.sqlite's `csv_directories`
table, keyed by the saved connection name, and is edited by browsing that
file (DbmanMetaProvider checks each path). Changes show up on the next
connect. `csv:///some/dir` seeds the list with one directory the first time
the connection has none; after that the url's directory is ignored, so
removing it from the list sticks.

Each file is loaded into one shared in-memory SQLite database, so this is a
SqlAlchemyProvider: paging, filters, sort, search and SQL mode are all
inherited. Writes (cell edits, row add/delete) go to the in-memory table
first and are then written back to the file the table came from - all of
it, atomically (temp file + rename). The file's delimiter, encoding,
byte-order mark, line endings and original header are kept; quoting is
normalized to minimal.

The SQLite csv virtual table (ext/misc/csv.c) was considered and rejected:
it's read-only, isn't compiled into Python's sqlite3, and a CREATE VIRTUAL
TABLE persists in the file it's created in, breaking that file for any
reader without the extension.
"""
import csv
import io
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.pool import StaticPool

from meta_db import MetaDb

from .base import RowKey
from .sqlalchemy_provider import SqlAlchemyProvider

# Names the sidebar filters out (sqlite_*, _dbman_), or that SQLite gives a
# meaning of its own: a column called rowid would shadow the real rowid every
# edit is addressed by.
_HIDDEN_TABLE_PREFIXES = ("sqlite_", "_dbman_")
_ROWID_ALIASES = {"rowid", "oid", "_rowid_"}

_INT = re.compile(r"-?(0|[1-9][0-9]*)")
_FLOAT = re.compile(r"-?(0|[1-9][0-9]*)\.[0-9]+")


@dataclass
class CsvFile:
    """What write-back needs to reproduce the file's own conventions."""
    path: Path
    header: list[str]
    delimiter: str
    newline: str
    encoding: str
    bom: bool
    mtime_ns: int
    size: int
    # A row wider than the header has fields with no column name to write
    # back under, so the table loads but refuses writes.
    ragged: bool


def _guess_type(values: list[str]) -> str:
    """INTEGER or NUMERIC only when every value would be written back exactly
    as it was read - so `007`, `1.50` or `1e3` keep the whole column TEXT,
    and a save never quietly drops a leading or trailing zero. NUMERIC (not
    REAL) for a mix of whole and decimal numbers, because REAL affinity
    turns `1` into `1.0`."""
    present = [v for v in values if v != ""]
    if not present:
        return "TEXT"
    if all(_INT.fullmatch(v) for v in present):
        return "INTEGER"
    if all(_INT.fullmatch(v) or (_FLOAT.fullmatch(v) and repr(float(v)) == v) for v in present):
        return "NUMERIC"
    return "TEXT"


def _convert(value: str, sql_type: str):
    if sql_type == "TEXT":
        return value
    if value == "":
        return None
    return int(value) if _INT.fullmatch(value) else float(value)


def _format(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        # A whole float can only come from an edit (loaded whole numbers are
        # ints); write it as typed-looking text rather than "3.0".
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _unique(name: str, taken: set[str]) -> str:
    """`name`, or `name_2`, `name_3`, ... - compared case-insensitively,
    since SQLite identifiers are."""
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate, n = f"{name}_{n}", n + 1
    taken.add(candidate.lower())
    return candidate


def _guess_delimiter(content: str) -> str:
    """Whichever candidate splits the header line into the most fields,
    comma on a tie. Deliberately not csv.Sniffer, which guesses from the
    data rows and can pick a character that merely recurs in the values."""
    first_line = content.split("\n", 1)[0].rstrip("\r")
    counts = {d: len(next(csv.reader([first_line], delimiter=d), [])) for d in ",;\t|"}
    return max(counts, key=lambda d: (counts[d], d == ","))


def read_csv(path: Path):
    """(CsvFile, column names, column types, rows) or None for an empty
    file - there's no header to make a table from."""
    raw = path.read_bytes()
    stat = path.stat()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        content, encoding = raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        # latin-1 maps every byte to one character, so it always decodes and
        # always writes back the same bytes.
        content, encoding = raw.decode("latin-1"), "latin-1"
    if not content.strip():
        return None
    newline = "\r\n" if "\r\n" in content else "\n"
    delimiter = _guess_delimiter(content)
    rows = list(csv.reader(io.StringIO(content, newline=""), delimiter=delimiter))
    header, body = rows[0], rows[1:]
    width = max([len(header), *(len(r) for r in body)])
    body = [r + [""] * (width - len(r)) for r in body]

    taken: set[str] = set()
    names = []
    for i in range(width):
        label = header[i].strip() if i < len(header) else ""
        if not label or label.lower() in _ROWID_ALIASES:
            label = f"{label}_" if label else f"column{i + 1}"
        names.append(_unique(label, taken))
    types = [_guess_type([r[i] for r in body]) for i in range(width)]
    rows_typed = [tuple(_convert(v, t) for v, t in zip(r, types)) for r in body]
    info = CsvFile(
        path=path, header=header, delimiter=delimiter, newline=newline,
        encoding=encoding, bom=bom, mtime_ns=stat.st_mtime_ns, size=stat.st_size,
        ragged=width > len(header),
    )
    return info, names, types, rows_typed


def write_csv(info: CsvFile, rows) -> None:
    """Rewrite the whole file: header as originally read, then `rows`.
    Written to a temp file in the same directory and renamed over the
    original, so a crash leaves either the old file or the new one, never
    half of one."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=info.delimiter, lineterminator=info.newline)
    writer.writerow(info.header)
    writer.writerows([_format(v) for v in row] for row in rows)
    data = buf.getvalue().encode(info.encoding)
    if info.bom:
        data = b"\xef\xbb\xbf" + data
    fd, tmp = tempfile.mkstemp(dir=info.path.parent, prefix=f".{info.path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, info.path.stat().st_mode & 0o7777)
        os.replace(tmp, info.path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def normalize_directory(value) -> str:
    path = str(value or "").strip()
    if not path:
        raise ValueError("give a directory path")
    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        raise ValueError(f"not a directory: {resolved}")
    return resolved


class CsvProvider(SqlAlchemyProvider):

    def __init__(self, db_url: str, name: str):
        # One in-memory database that every connection shares: SQLAlchemy's
        # default pool for sqlite:// gives each thread its own, empty one.
        super().__init__(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
        )
        self.db_url = db_url
        self.connection_name = name
        self.meta = MetaDb()
        self.files: dict[str, CsvFile] = {}
        self.capabilities = replace(
            self.capabilities,
            # Each of these would live only in memory and vanish on restart,
            # or (dropping a table) read as deleting the file without doing so.
            create_definition=False, create_table=False, delete_item=False,
            truncate_column=False, diagram=False, lookup_plugin=False,
        )
        self._seed_directory(db_url[len("csv://"):])
        self._load()

    # ---- the directory list, in dbman.sqlite ----

    def _stored_directories(self) -> list[str]:
        with self.meta.read() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT path FROM csv_directories WHERE connection = ? AND path IS NOT NULL AND path != ''"
                " ORDER BY id",
                (self.connection_name,),
            ).fetchall()
        return [r[0] for r in rows]

    def _seed_directory(self, url_path: str) -> None:
        if not url_path or self._stored_directories():
            return
        path = normalize_directory(url_path)
        with self.meta.write() as conn:
            # The connection row may not exist yet: dbman saves it only
            # after the provider connects. A url-less one satisfies the
            # foreign key and is filled in by that save.
            conn.execute("INSERT OR IGNORE INTO connections (name) VALUES (?)", (self.connection_name,))
            conn.execute(
                "INSERT OR IGNORE INTO csv_directories (connection, path) VALUES (?, ?)",
                (self.connection_name, path),
            )

    # ---- loading ----

    def _execute(self, sql: str, params=None) -> None:
        with self.engine.begin() as conn:
            conn.exec_driver_sql(sql, params) if params is not None else conn.exec_driver_sql(sql)

    def _quote(self, name: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(name)

    def _load(self) -> None:
        """Build the in-memory database from dbman.sqlite's list and the
        files on disk."""
        found = []
        for directory in self._stored_directories():
            try:
                entries = sorted(Path(directory).iterdir())
            except OSError:
                continue  # gone or unreadable: listed, but contributes nothing
            found += [p for p in entries if p.is_file() and p.suffix.lower() == ".csv"]

        # A file's stem is its table name unless two directories share one;
        # then each gets its directory's name in front.
        stems = [p.stem.lower() for p in found]
        taken: set[str] = set()
        for path in found:
            name = path.stem
            if stems.count(name.lower()) > 1 or name.lower().startswith(_HIDDEN_TABLE_PREFIXES):
                name = f"{path.parent.name}_{name}"
            self._load_file(_unique(name, taken), path)

    def _load_file(self, table: str, path: Path) -> None:
        try:
            loaded = read_csv(path)
        except OSError:
            return
        if loaded is None:
            return  # empty file: no header to make columns from
        info, names, types, rows = loaded
        columns = ", ".join(f"{self._quote(n)} {t}" for n, t in zip(names, types))
        self._execute(f"DROP TABLE IF EXISTS {self._quote(table)}")
        self._execute(f"CREATE TABLE {self._quote(table)} ({columns})")
        if rows:
            marks = ", ".join("?" for _ in names)
            self._execute(f"INSERT INTO {self._quote(table)} VALUES ({marks})", rows)
        self.files[table] = info

    # ---- writes ----

    def _before_write(self, table: str) -> None:
        """Refuse a write that would overwrite changes made on disk since
        the table was loaded - reloading the table from the file instead,
        so the next try starts from what's really there."""
        info = self.files.get(table)
        if info is None:
            raise ValueError(f"'{table}' isn't backed by a file")
        if info.ragged:
            raise ValueError(
                f"{info.path.name} has rows wider than its header, so it's read-only here"
            )
        try:
            stat = info.path.stat()
        except OSError:
            raise ValueError(f"{info.path} is gone")
        if (stat.st_mtime_ns, stat.st_size) != (info.mtime_ns, info.size):
            self._load_file(table, info.path)
            raise ValueError(f"{info.path.name} changed on disk - reloaded it, try again")

    def _write_back(self, table: str) -> None:
        info = self.files[table]
        with self.engine.connect() as conn:
            rows = conn.exec_driver_sql(f"SELECT * FROM {self._quote(table)} ORDER BY rowid").fetchall()
        try:
            write_csv(info, rows)
        except OSError:
            # The file didn't change, so put the table back in step with it.
            self._load_file(table, info.path)
            raise
        stat = info.path.stat()
        info.mtime_ns, info.size = stat.st_mtime_ns, stat.st_size

    def update_cell(self, name, item_type, row_key: RowKey, column, value) -> RowKey:
        self._before_write(name)
        super().update_cell(name, item_type, row_key, column, value)
        self._write_back(name)
        return row_key

    def add_row(self, name, item_type) -> RowKey:
        self._before_write(name)
        key = super().add_row(name, item_type)
        self._write_back(name)
        return key

    def delete_row(self, name, item_type, row_key: RowKey) -> None:
        self._before_write(name)
        super().delete_row(name, item_type, row_key)
        self._write_back(name)
