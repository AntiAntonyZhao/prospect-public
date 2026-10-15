"""`prospect demo` end to end: no API key, no network, the recorded responses replayed."""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from prospect import demo
from prospect.demo import DemoError, DemoHttp, TfidfEmbedder, request_key, run_demo

REPO = Path(__file__).resolve().parents[1]
RESPONSES = REPO / "demo" / "responses"


@pytest.fixture
def offline(monkeypatch):
    """Any socket connection fails, and bge can't load: the demo must still run."""

    def refuse(*a, **k):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(demo.SentenceTransformerEmbedder, "encode", refuse)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_demo_runs_offline_end_to_end(tmp_path, offline):
    before = {p.name: _digest(p) for p in RESPONSES.glob("*.json")}
    lines: list[str] = []
    fake = run_demo(REPO, db_path=tmp_path / "demo.db", echo=lines.append)
    out = "\n".join(lines)

    assert not fake.misses
    assert fake.served == Counter(extract=17, score=11)
    assert "WARNING: could not load" in out and "TF-IDF" in out  # the demo-only fallback

    conn = sqlite3.connect(tmp_path / "demo.db")
    conn.row_factory = sqlite3.Row
    rejected = Counter(
        r["reject_reason"] for r in conn.execute("SELECT reject_reason FROM postings")
    )
    # Every filter rule fires at least once on the 20 synthetic postings.
    assert rejected == Counter(
        {
            None: 11,
            "senior_title": 2,
            "location_metadata": 1,
            "location:country": 1,
            "clearance": 1,
            "years": 1,
            "years:requirement": 1,
            "french:posting_in_french": 1,
            "peng": 1,
        }
    )
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 11

    def breakdown(title: str) -> dict:
        row = conn.execute(
            """SELECT s.breakdown_json FROM scores s JOIN postings p ON p.id = s.posting_id
               WHERE p.title = ?""",
            (title,),
        ).fetchone()
        return json.loads(row[0])

    assert breakdown("Software Developer Intern - Winter 2027")["short_term"] == -20
    part_time = breakdown("Junior Software Developer (Part-Time)")
    assert part_time["part_time"] < 0 and part_time["short_term"] == -15
    assert breakdown("Machine Learning Engineer")["location"] == "remote"
    assert breakdown("Full-Stack Developer")["location"] == "elsewhere"

    # Every model call went through LLMClient and was costed (Hard rule 3).
    runs = conn.execute(
        "SELECT stage, COUNT(*), SUM(cost_usd > 0), COUNT(batch_id) FROM runs "
        "WHERE stage IN ('extract', 'score') GROUP BY stage ORDER BY stage"
    ).fetchall()
    assert [tuple(r) for r in runs] == [("extract", 17, 17, 17), ("score", 11, 11, 0)]
    assert "[term 4mo -20]" in out and "Spend all time" in out

    # Replaying never rewrites the recordings.
    assert {p.name: _digest(p) for p in RESPONSES.glob("*.json")} == before


def test_a_changed_request_is_a_loud_replay_miss(tmp_path, offline, monkeypatch):
    monkeypatch.setattr(demo.ResponseStore, "get", lambda self, params: None)
    with pytest.raises(DemoError, match="re-record"):
        run_demo(REPO, db_path=tmp_path / "demo.db", echo=lambda s: None)


def test_request_key_covers_the_whole_request():
    params = {"model": "m", "messages": [{"role": "user", "content": "a"}], "tools": []}
    changed = {**params, "messages": [{"role": "user", "content": "b"}]}
    assert request_key(params) == request_key(json.loads(json.dumps(params)))
    assert request_key(params) != request_key(changed)


def test_demo_http_serves_only_demo_boards(tmp_path):
    (tmp_path / "lever").mkdir()
    (tmp_path / "lever" / "acme.json").write_text("[]", encoding="utf-8")
    http = DemoHttp(tmp_path)
    assert http.get_json("https://api.lever.co/v0/postings/acme", params={"mode": "json"}) == []
    with pytest.raises(DemoError, match="no board file"):
        http.get_json("https://api.ashbyhq.com/posting-api/job-board/other")
    with pytest.raises(DemoError, match="no route"):
        http.get_json("https://example.com/jobs.json")


def test_tfidf_fallback_ranks_shared_words_higher():
    emb = TfidfEmbedder(["python fastapi backend", "react typescript frontend", "python data"])
    q, a, b = emb.encode(["python backend", "python fastapi backend", "react frontend"])
    assert float(q @ a) > float(q @ b)
    assert abs(float(a @ a) - 1) < 1e-6
