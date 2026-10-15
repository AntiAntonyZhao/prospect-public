from __future__ import annotations

import os

import numpy as np
from typer.testing import CliRunner

from prospect.cli import app
from prospect.pipeline.digest import format_digest
from prospect.pipeline.rank import profile_text, run_rank
from tests.conftest import add_posting


class KeywordEmbedder:
    """Deterministic stand-in for bge: counts a few keywords, normalized."""

    name = "keyword"
    words = ("python", "java", "react", "sql", "sales", "marketing")

    def encode(self, texts):
        rows = []
        for t in texts:
            v = np.array([t.lower().count(w) for w in self.words], dtype=np.float32) + 1e-3
            rows.append(v / np.linalg.norm(v))
        return np.stack(rows)


def test_rank_orders_by_similarity_and_caches(settings, conn, fb):
    dev = add_posting(
        conn,
        title="Backend Developer",
        status="candidate",
        description="<p>Python Java SQL</p>" * 5,
    )
    sales = add_posting(
        conn,
        title="Account Executive",
        company="B",
        status="candidate",
        description="<p>sales marketing sales</p>" * 5,
    )
    res = run_rank(settings, conn, fb, embedder=KeywordEmbedder())
    assert res.embedded == 2 and [pid for pid, _ in res.top] == [dev, sales]
    again = run_rank(settings, conn, fb, embedder=KeywordEmbedder())
    assert again.embedded == 0  # vectors are cached


def test_profile_text_uses_only_real_skills(settings, fb):
    text = profile_text(settings, fb)
    assert "Java" in text and "Montreal" in text
    assert "Go," not in text  # familiar skills stay out of the profile


def test_stale_candidates_are_parked(settings, conn, fb):
    pid = add_posting(conn, status="candidate")
    conn.execute("UPDATE postings SET last_seen = datetime('now', '-10 days') WHERE id=?", (pid,))
    conn.commit()
    res = run_rank(settings, conn, fb, embedder=KeywordEmbedder())
    assert res.closed == 1
    row = conn.execute("SELECT status, reject_reason FROM postings").fetchone()
    assert tuple(row) == ("ignored", "closed")


def test_digest_shows_score_and_gaps(settings, conn):
    pid = add_posting(conn, title="Backend Developer", company="Acme Commerce", status="candidate")
    conn.execute(
        "INSERT INTO posting_facts (posting_id, city, work_arrangement, "
        "french_requirement, model, prompt_version) "
        "VALUES (?, 'Montreal', 'hybrid', 'required_pro_forma', 'm', 'p')",
        (pid,),
    )
    conn.execute(
        "INSERT INTO scores (posting_id, score, breakdown_json, weights_version) "
        "VALUES (?, 84, '{}', 'w')",
        (pid,),
    )
    conn.execute(
        "INSERT INTO requirements (posting_id, text, skill_norm, must_have, tier, "
        "est_hours) VALUES (?, 'Kafka', 'Kafka', 1, 2, 4)",
        (pid,),
    )
    conn.commit()
    out = format_digest(settings, conn)
    assert "Acme Commerce" in out and "84" in out and "pro_forma" in out and "T2 Kafka (4h)" in out
    assert "⚠" not in out


def test_digest_marks_flagged_rows(settings, conn):
    """Live run 2026-10-06: 8 postings were kept on low confidence, but which ones?"""
    pid = add_posting(conn, title="Data Pipelines", company="Acme Quant", status="candidate")
    conn.execute(
        'UPDATE postings SET flags=\'["french_regex", "low_confidence:years"]\' WHERE id=?',
        (pid,),
    )
    conn.execute(
        "INSERT INTO posting_facts (posting_id, city, model, prompt_version) "
        "VALUES (?, 'Montreal', 'm', 'p')",
        (pid,),
    )
    conn.execute(
        "INSERT INTO scores (posting_id, score, breakdown_json, weights_version) "
        "VALUES (?, 65, '{}', 'w')",
        (pid,),
    )
    conn.commit()
    row = next(ln for ln in format_digest(settings, conn).splitlines() if "Acme Quant" in ln)
    assert "⚠ low_confidence:years" in row and "french_regex" not in row


# --- CLI ------------------------------------------------------------------------------------


def _cli(settings, *args):
    env = {**os.environ, "PROSPECT_HOME": str(settings.root)}
    (settings.root / "prospect.toml").write_text(
        f'[paths]\nfactbank = "factbank.yaml"\nschema_sql = "{settings.schema_path.as_posix()}"\n',
        encoding="utf-8",
    )
    return CliRunner().invoke(app, list(args), env=env)


def test_cli_w1_commands(settings):
    r = _cli(settings, "db", "init")
    assert r.exit_code == 0 and "change(s)" in r.output
    assert "up to date" in _cli(settings, "db", "init").output
    r = _cli(settings, "factbank", "lint")
    assert r.exit_code == 0 and "0 errors" in r.output
    r = _cli(settings, "log", "--url", "https://x/1", "--company", "Acme", "--title", "Dev")
    assert r.exit_code == 0 and "logged application 1" in r.output
    r = _cli(settings, "log", "1")
    assert r.exit_code == 1 and "already has submitted" in r.output
    r = _cli(settings, "cost")
    assert r.exit_code == 0 and "Spend all time: $0.0000" in r.output


def test_cli_lint_failure_exit_code(settings):
    text = settings.factbank_path.read_text(encoding="utf-8")
    settings.factbank_path.write_text(
        text.replace("level: working", "level: ???", 1), encoding="utf-8"
    )
    r = _cli(settings, "factbank", "lint")
    assert r.exit_code == 1 and "is ???" in r.output


def test_cli_stage_refuses_when_lint_fails(settings):
    _cli(settings, "db", "init")
    text = settings.factbank_path.read_text(encoding="utf-8")
    settings.factbank_path.write_text(
        text.replace("level: working", "level: ???", 1), encoding="utf-8"
    )
    r = _cli(settings, "filter")
    assert r.exit_code == 1 and "no pipeline stage runs" in r.output


def test_cli_extract_without_key_is_a_clear_error(settings):
    _cli(settings, "db", "init")
    r = _cli(settings, "extract", "--sync", "--posting", "999")
    assert r.exit_code == 0 and "nothing to do" in r.output  # no work → no key needed


def test_digest_shows_employment_term_costs(settings, conn):
    pid = add_posting(conn, title="Data Annotation", company="Acme AI", status="candidate")
    conn.execute(
        "INSERT INTO posting_facts (posting_id, city, model, prompt_version) "
        "VALUES (?, 'Toronto', 'm', 'p')",
        (pid,),
    )
    conn.execute(
        "INSERT INTO scores (posting_id, score, breakdown_json, weights_version) VALUES "
        '(?, 58, \'{"part_time": -17.2, "hours_per_week": 16, "short_term": -15, '
        "\"term_months\": 6}', 'w')",
        (pid,),
    )
    conn.commit()
    row = next(ln for ln in format_digest(settings, conn).splitlines() if "Acme AI" in ln)
    assert "[part-time 16h/wk -17.2, term 6mo -15]" in row
