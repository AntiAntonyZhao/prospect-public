"""SQLite access. DDL lives in db/schema.sql; `init_db` applies it and migrates.

Migration is additive and derived from the schema file itself: the schema is
applied to an in-memory database, and any column present there but missing
from the real database is added with ALTER TABLE. Views whose SQL changed are
recreated. Nothing is ever dropped from a table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    """UTC timestamp in the same shape as SQLite's datetime('now')."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on any exception."""
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def init_db(conn: sqlite3.Connection, schema_sql: str) -> list[str]:
    """Apply schema.sql (idempotent) and add missing columns / refresh views.

    Returns a human-readable list of the changes made (empty when up to date).
    """
    changes: list[str] = []
    existing_tables = _tables(conn)
    conn.executescript(schema_sql)
    for name in sorted(_tables(conn) - existing_tables):
        changes.append(f"created table {name}")

    ref = sqlite3.connect(":memory:")
    ref.executescript(schema_sql)
    for table in sorted(_tables(ref)):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for cid, name, ctype, notnull, default, _pk in ref.execute(f"PRAGMA table_info({table})"):
            del cid
            if name in have:
                continue
            clause = f"ALTER TABLE {table} ADD COLUMN {name} {ctype}"
            if default is not None:
                clause += f" DEFAULT {default}"
            if notnull and default is not None:
                clause += " NOT NULL"
            conn.execute(clause)
            changes.append(f"added column {table}.{name}")

    ref_views = dict(
        ref.execute("SELECT name, sql FROM sqlite_master WHERE type='view'").fetchall()
    )
    cur_views = dict(
        conn.execute("SELECT name, sql FROM sqlite_master WHERE type='view'").fetchall()
    )
    for name, sql in ref_views.items():
        if cur_views.get(name) != sql:
            conn.execute(f"DROP VIEW IF EXISTS {name}")
            conn.execute(sql)
            changes.append(f"recreated view {name}")
    ref.close()
    conn.commit()
    return changes


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def require_schema(conn: sqlite3.Connection) -> None:
    if "postings" not in _tables(conn):
        raise RuntimeError("database not initialized; run `prospect db init` first")
