from __future__ import annotations

import json

import pytest

from prospect.models.posting import ExtractionOutput
from prospect.pipeline.extract import run_extract, store_extraction, to_facts
from tests.conftest import add_posting, extraction_input


def _prime_shortlist(conn, pid, cosine=0.9):
    conn.execute(
        "INSERT INTO embeddings (posting_id, model, vector, cosine) VALUES (?, 'm', x'00', ?)",
        (pid, cosine),
    )
    conn.execute("UPDATE postings SET status='candidate' WHERE id=?", (pid,))
    conn.commit()


def test_store_is_idempotent_and_invalidates_scores(conn):
    pid = add_posting(conn)
    out = ExtractionOutput.model_validate(extraction_input())
    store_extraction(conn, pid, out, "We build things.", "We build things.", "m", "extract_v1")
    conn.execute(
        "INSERT INTO scores (posting_id, score, breakdown_json, weights_version) "
        "VALUES (?, 50, '{}', 'w')",
        (pid,),
    )
    store_extraction(conn, pid, out, "We build things.", "We build things.", "m", "extract_v1")
    assert conn.execute("SELECT COUNT(*) FROM posting_facts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM requirements").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0


def test_evidence_quotes_must_be_verbatim():
    out = ExtractionOutput.model_validate(
        extraction_input(
            facts={
                "evidence": [
                    {"field": "city", "quote": "based in Montréal"},
                    {"field": "french_requirement", "quote": "French is an asset"},
                ]
            }
        )
    )
    facts, dropped = to_facts(out, "We are based in Montreal.", "Title\nWe are based in Montreal.")
    assert facts.evidence_span == {"city": "based in Montréal"}  # accent-insensitive match
    assert dropped == 1


def test_posting_language_is_deterministic_not_the_models():
    out = ExtractionOutput.model_validate(extraction_input())
    body = (
        "Nous cherchons un développeur pour notre équipe. Vous serez responsable de la "
        "plateforme et des projets avec les clients dans une équipe de travail. "
    ) * 4
    facts, _ = to_facts(out, body, body)
    assert facts.posting_language == "fr"


def test_city_normalization():
    out = ExtractionOutput.model_validate(extraction_input(facts={"city": "Montréal"}))
    assert to_facts(out, "x", "x")[0].city == "Montreal"
    out = ExtractionOutput.model_validate(extraction_input(facts={"city": "Québec"}))
    assert to_facts(out, "x", "x")[0].city == "Quebec City"


def test_run_extract_sync(settings, conn, fake_llm):
    pid = add_posting(conn)
    _prime_shortlist(conn, pid)
    client, fake = fake_llm(lambda p: extraction_input())
    msg = run_extract(settings, conn, mode="sync", client=client)
    assert "1 extracted" in msg
    assert (
        conn.execute("SELECT city FROM posting_facts WHERE posting_id=?", (pid,)).fetchone()[0]
        == "Montreal"
    )
    # second run: nothing to do, no API call
    assert "nothing to do" in run_extract(settings, conn, mode="sync", client=client)
    assert len(fake.messages.calls) == 1


def test_short_descriptions_are_skipped_and_flagged(settings, conn, fake_llm):
    pid = add_posting(conn, description="<p>Apply!</p>")
    _prime_shortlist(conn, pid)
    client, fake = fake_llm(lambda p: extraction_input())
    run_extract(settings, conn, mode="sync", client=client)
    assert fake.messages.calls == []
    flags = json.loads(conn.execute("SELECT flags FROM postings WHERE id=?", (pid,)).fetchone()[0])
    assert "no_description" in flags


def test_batch_submit_then_collect(settings, conn, fake_llm):
    a = add_posting(conn, title="Dev A")
    b = add_posting(conn, title="Dev B", company="Other")
    _prime_shortlist(conn, a)
    _prime_shortlist(conn, b, 0.8)
    client, _fake = fake_llm(lambda p: extraction_input())
    msg = run_extract(settings, conn, mode="batch", submit_only=True, client=client)
    assert "submitted batch msgbatch_1 with 2" in msg
    # pending postings are not resubmitted
    assert "nothing to do" in run_extract(
        settings, conn, mode="batch", submit_only=True, client=client
    )
    msg = run_extract(settings, conn, mode="batch", collect_only=True, client=client)
    assert "collected 2/2" in msg
    runs = conn.execute("SELECT batch_id, cost_usd FROM runs WHERE stage='extract'").fetchall()
    assert len(runs) == 2 and all(r["batch_id"] == "msgbatch_1" for r in runs)
    assert runs[0]["cost_usd"] == (1200 * 1 + 300 * 5) / 1e6 * 0.5  # batch price


@pytest.mark.parametrize(
    ("location_raw", "remote"),
    [
        ("Kitchener, Ontario; Remote - Canada; Toronto, Ontario", True),
        ("Kitchener, Remote Canada", True),
        ("Remote", True),
        ("Toronto; Remote, United States", False),  # remote only outside Canada
        ("Toronto, ON (not remote)", False),
        ("Toronto, ON", False),
    ],
)
def test_a_remote_option_in_the_ats_list_makes_the_role_remote(location_raw, remote):
    """Policy 2026-10: any remote option counts, even over an office policy in the body."""
    out = ExtractionOutput.model_validate(
        extraction_input(
            facts={
                "city": "Kitchener",
                "work_arrangement": "hybrid",
                "onsite_days": 3,
                "remote_scope": "city",
                "evidence": [{"field": "work_arrangement", "quote": "3 days per week in office"}],
            }
        )
    )
    body = "3 days per week in office"
    seen = f"Company: Acme\nTitle: Dev\nATS location metadata: {location_raw}\n\n{body}"
    facts, _ = to_facts(out, body, seen)
    if remote:
        assert facts.work_arrangement == "remote" and facts.city is None
        assert facts.onsite_days is None
        assert facts.evidence_span["work_arrangement"] in location_raw
    else:
        assert facts.work_arrangement == "hybrid" and facts.city == "Kitchener"
    assert (facts.remote_scope == "canada") == ("Canada" in location_raw and remote)


def test_a_remote_role_carries_no_city():
    """Live run 2026-10-06: the model said remote but kept a city from the office list."""
    out = ExtractionOutput.model_validate(
        extraction_input(
            facts={
                "city": "Toronto",
                "province": "ON",
                "work_arrangement": "remote",
                "onsite_days": None,
                "remote_scope": "province",
                "evidence": [{"field": "city", "quote": "our office is in Toronto"}],
            }
        )
    )
    body = "Fully remote within Ontario; our office is in Toronto."
    seen = f"Company: X\nTitle: Dev\nATS location metadata: Toronto, ON\n\n{body}"
    facts, _ = to_facts(out, body, seen)
    assert facts.work_arrangement == "remote" and facts.city is None
    assert facts.province == "ON" and facts.remote_scope == "province"  # scope stays
    assert "city" not in facts.evidence_span
