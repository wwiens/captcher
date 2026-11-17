#!/usr/bin/env python3
"""SQLite access — the one owner of the database connection.

Captcher keeps its structured data (walkthroughs, collections, brand profiles,
capture tokens, logs, AI usage) in one SQLite file, `config.database_path()`.
Captured screens are files and live under `config.captures_dir()`; see
`blobs.py`.

THE API IS THE ONE THE POSTGRES VERSION HAD, so the stores barely changed:

    query(sql, params)   every row, as a list of dicts
    one(sql, params)     the first row, or None
    value(sql, params)   the first column of the first row, or a default
    execute(sql, params) the number of rows a statement changed
    connection()         a transaction: `with db.connection() as conn,
                         conn.cursor() as cur: cur.execute(...)`

and three conventions carry the differences:

  - **Placeholders stay `%s`.** They are rewritten to SQLite's `?` here, so SQL
    reads the same in every module. Nothing in this codebase writes a literal
    `%s`; a `%` inside a strftime format is unaffected.
  - **Types come back as Python types, by column name.** SQLite stores JSON as
    text, timestamps as text and booleans as 0/1. The row factory decodes the
    JSON_COLUMNS, every `*_at` column (to an aware UTC datetime) and the
    BOOL_COLUMNS, so callers still get dicts, datetimes and bools.
  - **`now()` works in SQL.** It is registered on every connection and writes
    the same fixed-width UTC text as the schema's defaults, so timestamps from
    either source compare correctly as text.

CONNECTIONS. One per thread (the server is a ThreadingHTTPServer), in WAL mode
so readers never block the writer, with foreign keys on and a busy timeout so
two writers queue rather than fail. A transaction is `BEGIN IMMEDIATE`, which
takes the write lock up front: that is what `select ... for update` did on
Postgres, and it is what stops two saves racing a read-then-write.
"""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

Error = sqlite3.Error
IntegrityError = sqlite3.IntegrityError

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

# The version schema.sql creates. Upgrades for an existing file go in
# UPGRADES, keyed by the version they produce; each runs once, in order.
SCHEMA_VERSION = 1
UPGRADES = {}

# Seconds a writer waits for another writer before giving up. Authoring
# passes write in short bursts, so a long wait means something is wrong.
BUSY_TIMEOUT = 30

# Columns stored as JSON text and handed back decoded.
JSON_COLUMNS = {"definition", "draft", "render_check", "verify_check", "embed",
                "colors", "fonts", "entry"}
# Columns (or expressions aliased so) that are booleans, stored as 0/1.
BOOL_COLUMNS = {"show_attribution", "has_pages", "live"}


class Json:
    """Marks a value for a JSON column: `db.Json(definition)`."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def to_db_time(dt):
    """A datetime as the stored text: UTC, millisecond precision, Z suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def now_text():
    """What `now()` returns in SQL."""
    return to_db_time(datetime.now(timezone.utc))


def parse_time(text):
    """Stored timestamp text back to an aware datetime."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


sqlite3.register_adapter(Json, lambda j: json.dumps(j.value))
sqlite3.register_adapter(datetime, to_db_time)
sqlite3.register_adapter(uuid.UUID, str)
sqlite3.register_adapter(bool, int)


def _row_factory(cursor, row):
    out = {}
    for (name, *_), v in zip(cursor.description, row):
        if v is not None:
            if name in JSON_COLUMNS and isinstance(v, str):
                v = json.loads(v)
            elif name.endswith("_at") and isinstance(v, str):
                v = parse_time(v)
            elif name in BOOL_COLUMNS:
                v = bool(v)
        out[name] = v
    return out


def _sql(text):
    return text.replace("%s", "?")


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

_local = threading.local()
_lock = threading.Lock()
_generation = 0          # bumped by reset(); a thread's stale connection reconnects
_open = []               # every connection opened, so reset() can close them
_migrated = set()        # database paths already brought up to SCHEMA_VERSION


def _migrate(conn, path):
    if path in _migrated:
        return
    with _lock:
        if path in _migrated:
            return
        version = conn.execute("pragma user_version").fetchone()["user_version"]
        if version == 0:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            version = SCHEMA_VERSION
            conn.execute(f"pragma user_version = {version}")
        for target in sorted(v for v in UPGRADES if v > version):
            conn.executescript(UPGRADES[target])
            conn.execute(f"pragma user_version = {target}")
        _migrated.add(path)


def _connect():
    path = config.database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = _row_factory
    conn.create_function("now", 0, now_text)
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    conn.execute(f"pragma busy_timeout = {BUSY_TIMEOUT * 1000}")
    conn.execute("pragma synchronous = normal")
    _migrate(conn, str(path))
    with _lock:
        _open.append(conn)
    return conn


def _conn():
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "generation", None) != _generation:
        conn = _connect()
        _local.conn, _local.generation = conn, _generation
    return conn


def configured():
    """Always true: SQLite needs no configuration. Kept for callers that ask."""
    return True


def reset():
    """Close every connection. Tests call this after repointing CAPTCHER_HOME
    or CAPTCHER_DB; the next query on any thread reconnects."""
    global _generation
    with _lock:
        _generation += 1
        while _open:
            try:
                _open.pop().close()
            except Exception:
                pass
        _migrated.clear()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def query(sql, params=None):
    """Every row, as a list of dicts."""
    return _conn().execute(_sql(sql), params or ()).fetchall()


def one(sql, params=None):
    """The first row as a dict, or None.

    Fetches every row rather than one: a statement with RETURNING is not
    finished — and its write not committed — until it has been stepped to the
    end, and several callers deliberately take the first of an ordered set.
    """
    rows = query(sql, params)
    return rows[0] if rows else None


def value(sql, params=None, default=None):
    """The first column of the first row, or `default` when there is no row."""
    row = one(sql, params)
    if row is None:
        return default
    return next(iter(row.values()))


def execute(sql, params=None):
    """Run a statement, returning the number of rows it changed.

    The rowcount is the useful half: `execute(...) == 0` after an UPDATE with an
    owner_id predicate is how the stores tell "no such row" from "not yours"
    without a second SELECT — and both answers are the same 404 to the caller.
    """
    return _conn().execute(_sql(sql), params or ()).rowcount


def placeholders(values):
    """`%s, %s, ...` for an `in (...)` list. SQLite accepts an empty list."""
    return ", ".join("%s" for _ in values)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._cur = None
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._cur = self._conn.execute(_sql(sql), params or ())
        self.rowcount = self._cur.rowcount
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _Transaction:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _Cursor(self._conn)


@contextmanager
def connection():
    """A transaction on this thread's connection: committed on a clean exit,
    rolled back on an exception. BEGIN IMMEDIATE takes the write lock first."""
    conn = _conn()
    conn.execute("begin immediate")
    try:
        yield _Transaction(conn)
    except BaseException:
        conn.execute("rollback")
        raise
    else:
        conn.execute("commit")


def health():
    """(ok, detail) for the startup banner. Never raises."""
    try:
        value("select 1")
        return True, str(config.database_path())
    except Exception as exc:
        return False, f"{config.database_path()}: {exc}"


if __name__ == "__main__":
    ok, detail = health()
    print(("ok   " if ok else "FAIL ") + detail)
    raise SystemExit(0 if ok else 1)
