from __future__ import annotations

from prospect.db import connect, init_db


def test_init_is_idempotent(settings):
    schema = settings.schema_path.read_text(encoding="utf-8")
    conn = connect(settings.db_path)
    first = init_db(conn, schema)
    assert any(c == "created table postings" for c in first)
    assert init_db(conn, schema) == []
    assert init_db(conn, schema) == []
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert {"v_shortlist", "v_conversion"} <= views


def test_init_migrates_missing_columns(settings):
    """An older database gains columns added to schema.sql later, data intact."""
    conn = connect(settings.db_path)
    conn.execute(
        """CREATE TABLE runs (id INTEGER PRIMARY KEY, stage TEXT NOT NULL,
           started_at TEXT NOT NULL)"""
    )
    conn.execute("INSERT INTO runs (stage, started_at) VALUES ('ingest', '2026-01-01')")
    conn.commit()
    changes = init_db(conn, settings.schema_path.read_text(encoding="utf-8"))
    assert "added column runs.cost_usd" in changes
    assert "added column runs.cache_write_tokens" in changes
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"cost_usd", "model", "posting_id"} <= cols
    assert conn.execute("SELECT stage FROM runs").fetchone()[0] == "ingest"
