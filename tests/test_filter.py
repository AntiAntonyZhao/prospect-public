from __future__ import annotations

import json

import pytest

from prospect.factbank.loader import effective_targets
from prospect.pipeline.filter import (
    FilterError,
    classify_location_raw,
    full_pass,
    full_pass_reason,
    prepass,
)
from tests.conftest import add_posting


@pytest.mark.parametrize(
    ("loc", "want"),
    [
        ("Toronto, ON", "canada"),
        ("Remote - Canada", "canada"),
        ("London, ON", "canada"),
        ("Oakville; Chicago, IL", "canada"),
        ("San Francisco, CA", "non_canada"),
        ("Remote - US", "non_canada"),
        ("London, UK", "non_canada"),
        ("Gurugram", "non_canada"),
        ("United States; Remote - California", "non_canada"),
        ("N/A; US", "non_canada"),
        ("Remote", "unknown"),
        ("Remote - US; Remote - CA", "unknown"),
        ("New York, NY; North America", "unknown"),
        ("Home based - Worldwide", "unknown"),
        (None, "unknown"),
    ],
)
def test_classify_location_raw(loc, want):
    assert classify_location_raw(loc) == want


def _status(conn, pid):
    r = conn.execute(
        "SELECT status, reject_reason, flags FROM postings WHERE id=?", (pid,)
    ).fetchone()
    return r["status"], r["reject_reason"], set(json.loads(r["flags"]))


def test_prepass_flags_french_but_never_rejects_on_it(settings, conn, fb):
    fr = add_posting(
        conn, title="Developer", description="<p>Bilingual English/French required.</p>" * 20
    )
    res = prepass(settings, conn, fb)
    status, reason, flags = _status(conn, fr)
    assert status == "candidate" and reason is None and "french_regex" in flags
    assert res.flagged["french_regex"] == 1


def test_prepass_rejects_unambiguous_metadata(settings, conn, fb):
    senior = add_posting(conn, title="Senior Software Engineer")
    us = add_posting(conn, title="Software Engineer", company="B", location="Seattle, WA")
    ok = add_posting(conn, title="Software Engineer", company="C", location="Remote")
    prepass(settings, conn, fb)
    assert _status(conn, senior)[:2] == ("rejected", "senior_title")
    assert _status(conn, us)[:2] == ("rejected", "location_metadata")
    assert _status(conn, ok)[0] == "candidate"


def test_prepass_rejects_duplicate_application(settings, conn, fb):
    first = add_posting(conn, title="Backend Developer", ext_id="1")
    conn.execute(
        "INSERT INTO applications (posting_id, factbank_version) VALUES (?, 'v')", (first,)
    )
    conn.commit()
    again = add_posting(
        conn,
        title="Backend Developer (Toronto)",
        ext_id="2",
        description="<p>different words</p>" * 30,
    )
    prepass(settings, conn, fb)
    assert _status(conn, again)[:2] == ("rejected", "duplicate_application")


FACTS = {
    "city": "Montreal",
    "province": "QC",
    "country": "CA",
    "work_arrangement": "hybrid",
    "remote_scope": "city",
    "posting_language": "en",
    "french_requirement": "none",
    "min_years": 1,
    "clearance": 0,
    "peng": 0,
    "work_auth": "not_stated",
}


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({}, None),
        ({"french_requirement": "required_pro_forma"}, None),
        ({"french_requirement": "asset"}, None),
        # policy 2026-10: an English posting's French requirement never rejects
        ({"french_requirement": "required_working"}, None),
        ({"french_requirement": "posting_in_french"}, "french:posting_in_french"),
        ({"posting_language": "fr"}, "french:posting_in_french"),
        # anywhere in Canada is fine, onsite or not
        ({"province": "BC", "city": "Vancouver"}, None),
        (
            {"province": "NY", "city": "New York", "country": None, "remote_scope": "city"},
            "location:province",
        ),
        (
            {
                "province": "BC",
                "city": None,
                "work_arrangement": "remote",
                "remote_scope": "canada",
            },
            None,
        ),
        (
            {"province": None, "city": None, "country": "US", "remote_scope": "us"},
            "location:country",
        ),
        ({"city": "Ottawa", "province": "ON", "work_arrangement": "onsite"}, None),
        ({"province": None, "city": None, "country": "US", "remote_scope": "global"}, None),
        ({"city": "Montréal", "work_arrangement": "onsite"}, None),
        ({"min_years": 3}, "years"),
        ({"min_years": 2}, None),  # at the cap (2 here; the user's prospect.toml says 4)
        ({"clearance": 1}, "clearance"),
        ({"peng": 1}, "peng"),
        ({"work_auth": "citizen_only"}, "work_auth"),
        ({"work_auth": "no_sponsorship"}, None),
    ],
)
def test_full_pass_rules(settings, fb, over, reason):
    targets = effective_targets(settings, fb)
    assert full_pass_reason({**FACTS, **over}, targets, "pr") == reason


def test_work_auth_matrix(settings, fb):
    targets = effective_targets(settings, fb)
    row = {**FACTS, "work_auth": "no_sponsorship"}
    assert full_pass_reason(row, targets, "pgwp") is None
    assert full_pass_reason(row, targets, "needs_sponsorship") == "work_auth"
    assert full_pass_reason({**FACTS, "work_auth": "citizen_or_pr"}, targets, "pgwp") == "work_auth"


def _facts(conn, pid, evidence=None, **over):
    f = {**FACTS, "confidence": 0.9, **over}
    conn.execute(
        """INSERT INTO posting_facts (posting_id, city, province, country, work_arrangement,
               remote_scope, posting_language, french_requirement, min_years, seniority,
               clearance, peng, work_auth, confidence, evidence_json, model, prompt_version)
           VALUES (?,?,?,?,?,?,?,?,?,'newgrad',?,?,?,?,?,'m','p')""",
        (
            pid,
            f["city"],
            f["province"],
            f["country"],
            f["work_arrangement"],
            f["remote_scope"],
            f["posting_language"],
            f["french_requirement"],
            f["min_years"],
            f["clearance"],
            f["peng"],
            f["work_auth"],
            f["confidence"],
            json.dumps(evidence or {}),
        ),
    )
    conn.commit()


def test_full_pass_confidence_gate(settings, conn, fb):
    sure = add_posting(conn, title="Dev A", status="candidate")
    unsure = add_posting(conn, title="Dev B", company="Other", status="candidate")
    quote = {"clearance": "Active Secret clearance required"}
    _facts(conn, sure, quote, clearance=1, confidence=0.9)
    _facts(conn, unsure, quote, clearance=1, confidence=0.4)
    full_pass(settings, conn, fb)
    assert _status(conn, sure)[:2] == ("rejected", "clearance")
    status, _, flags = _status(conn, unsure)
    assert status == "candidate" and "low_confidence:clearance" in flags


def test_a_verbatim_years_quote_rejects_at_any_confidence(settings, conn, fb):
    """Policy 2026-10-06 (a live posting: '4+ years… Python' kept at confidence 0.5)."""
    pid = add_posting(conn, title="Dev A", status="candidate")
    _facts(
        conn, pid, {"min_years": "5+ years of professional experience"}, min_years=5, confidence=0.3
    )
    full_pass(settings, conn, fb)
    assert _status(conn, pid)[:2] == ("rejected", "years")


def test_preferred_years_are_not_a_minimum(settings, conn, fb):
    pid = add_posting(conn, title="Dev A", status="candidate")
    _facts(conn, pid, {"min_years": "5+ years of experience preferred"}, min_years=5)
    full_pass(settings, conn, fb)
    status, _, flags = _status(conn, pid)
    assert status == "candidate" and "optional:years" in flags


def test_a_must_have_requirement_over_the_cap_rejects(settings, conn, fb):
    must = add_posting(conn, title="Dev A", status="candidate")
    nice = add_posting(conn, title="Dev B", company="Other", status="candidate")
    for pid, must_have in ((must, 1), (nice, 0)):
        _facts(conn, pid, min_years=None)
        conn.execute(
            "INSERT INTO requirements (posting_id, text, skill_norm, must_have, years, kind) "
            "VALUES (?, '5+ years of Python', 'Python', ?, 5, 'skill')",
            (pid, must_have),
        )
    conn.commit()
    full_pass(settings, conn, fb)
    assert _status(conn, must)[:2] == ("rejected", "years:requirement")
    assert _status(conn, nice)[0] == "candidate"


def test_a_rule_change_restores_what_the_full_pass_rejected(settings, conn, fb):
    """Raising targets.max_years brings back postings an earlier full pass rejected."""
    pid = add_posting(conn, title="Dev A", status="candidate")
    _facts(conn, pid, {"min_years": "3+ years of experience"}, min_years=3)
    full_pass(settings, conn, fb)
    assert _status(conn, pid)[:2] == ("rejected", "years")
    settings.targets.max_years = 4
    res = full_pass(settings, conn, fb)
    assert _status(conn, pid)[:2] == ("candidate", None)
    assert res.restored == 1 and "1 restored" in res.summary()


def test_rejection_needs_a_supporting_quote(settings, conn, fb):
    """Confident but unquoted (or quoted about something else) → flag, never reject."""
    pid = add_posting(conn, title="Dev C", status="candidate")
    _facts(conn, pid, {"city": "Montreal"}, min_years=5, confidence=0.95)
    full_pass(settings, conn, fb)
    status, _, flags = _status(conn, pid)
    assert status == "candidate" and "weak_evidence:years" in flags


def test_posting_in_french_is_deterministic_even_at_low_confidence(settings, conn, fb):
    pid = add_posting(conn, title="Développeur", status="candidate")
    _facts(conn, pid, posting_language="fr", confidence=0.1)
    full_pass(settings, conn, fb)
    assert _status(conn, pid)[0] == "rejected"


def test_full_pass_refuses_without_work_authorization(settings, conn, fb):
    fb.identity.work_authorization = None
    with pytest.raises(FilterError, match="work_authorization"):
        full_pass(settings, conn, fb)


def test_the_filter_passes_report_progress(settings, conn, fb):
    for i in range(30):
        add_posting(conn, title=f"Developer {i}", company=f"Co {i}")
    seen: list[float] = []
    res = prepass(settings, conn, fb, progress=lambda f, m: seen.append(f))
    assert res.examined == 30
    assert seen == sorted(seen) and seen[0] == 0 and len(seen) >= 10  # about every 4%
