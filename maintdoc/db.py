"""SQLite access: connection setup, schema creation, transactions and run records."""

from __future__ import annotations

import contextlib
import logging
import platform
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Iterator

from maintdoc.constants import TOOL_VERSION
from maintdoc.utils import dumps, now_iso

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"


def _schema_sql() -> str:
    return resources.files("maintdoc").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(db_path: str | Path, *, readonly: bool = False, timeout: float = 120.0) -> sqlite3.Connection:
    """Open a connection in autocommit mode; use :func:`transaction` for writes."""
    db_path = Path(db_path)
    if readonly:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None, check_same_thread=False)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=timeout, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_schema_sql())
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (now_iso(),))
    elif row["value"] != SCHEMA_VERSION:
        raise RuntimeError(f"Database schema version {row['value']} is not supported (expected {SCHEMA_VERSION})")


def open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    init_db(conn)
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. Nested use joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r is not None else None


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    r = conn.execute(sql, tuple(params)).fetchone()
    return r[0] if r is not None and r[0] is not None else default


def start_run(conn: sqlite3.Connection, run_id: str, command: str, dry_run: bool, config_hash: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs(run_id, command, started_at, status, dry_run, config_hash, tool_version, host) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (run_id, command, now_iso(), "running", int(dry_run), config_hash, TOOL_VERSION, platform.node()),
        )


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, stats: dict | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE run_id=?",
            (now_iso(), status, dumps(stats or {}), run_id),
        )


def backup_to(conn: sqlite3.Connection, target: Path) -> Path:
    """Consistent online snapshot of the database (safe while other readers are active)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    dst = sqlite3.connect(str(tmp))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    tmp.replace(target)
    return target
