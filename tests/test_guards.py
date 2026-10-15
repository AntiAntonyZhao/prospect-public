"""The four extraction fixes, pinned with the real cases from the first live run."""

from __future__ import annotations

import json

import pytest

from prospect.models.posting import ExtractedRequirement, ExtractionOutput
from prospect.pipeline.digest import format_digest
from prospect.pipeline.extract import store_extraction, to_facts, years_stated_verbatim
from prospect.pipeline.guards import (
    clean_requirements,
    evidence_supports,
    expresses_interest,
    is_experience_cap,
)
from prospect.pipeline.score import run_score
from tests.conftest import add_posting, extraction_input


def req(
    skill: str,
    text: str | None = None,
    years: int | None = None,
    kind: str = "skill",
    must: bool = True,
):
    return ExtractedRequirement(
        text=text or skill, skill_norm=skill, must_have=must, years=years, context=None, kind=kind
    )


# 1. upper-bound years are not minimums ---------------------------------------------------


def test_experience_cap_becomes_eligibility_without_years():
    capped = req("Less than 2 years full-time work experience", years=2)
    [r] = clean_requirements([capped]).kept
    assert (r.years, r.kind) == (None, "eligibility")


def test_upper_bound_on_a_skill_clears_years_but_stays_a_skill():
    [r] = clean_requirements([req("Python", "Up to 3 years of Python", years=3)]).kept
    assert (r.years, r.kind) == (None, "skill")


def test_real_minimums_are_untouched():
    [r] = clean_requirements([req("Python", "2+ years of Python", years=2)]).kept
    assert (r.years, r.kind) == (2, "skill")
    assert not is_experience_cap("3+ years of professional experience")


def test_a_cap_with_a_condition_is_eligibility_not_a_degree():
    """Live run 2026-10-06: "master's" in the condition was tiered a degree."""
    text = (
        "For any candidates who are master's students who worked between their bachelor's "
        "and master's programs: candidates should also have less than 2 years of full-time "
        "work experience"
    )
    capped = req("Less than 2 years full-time work experience (master's candidates only)", text)
    [r] = clean_requirements([capped]).kept
    assert r.kind == "eligibility"


def test_qualifications_are_always_skills():
    """Live run: Haiku marked a posting's bachelor's degree as eligibility; it must be tiered."""
    degree = req("Bachelor's degree in Computer Science or related", kind="eligibility")
    enrolled = req("Current enrollment", kind="eligibility")
    grad = req("Graduating in 2026", kind="eligibility")
    kinds = [r.kind for r in clean_requirements([degree, enrolled, grad]).kept]
    assert kinds == ["skill", "eligibility", "eligibility"]


def test_min_years_upper_bound_quote_is_nulled():
    out = ExtractionOutput.model_validate(
        extraction_input(
            facts={
                "min_years": 2,
                "evidence": [{"field": "min_years", "quote": "less than 2 years of experience"}],
            }
        )
    )
    facts, _ = to_facts(out, "x", "Candidates have less than 2 years of experience.")
    assert facts.min_years is None


# 2. optional wording makes a requirement nice-to-have --------------------------------------


@pytest.mark.parametrize(
    ("skill", "text", "must"),
    [
        ("Master's or PhD in ML", "Master's or PhD in ML/DL/NLP (preferred)", False),
        ("Rust", "Rust is a plus", False),
        ("Go", "Experience with Go would be an asset", False),
        ("Kotlin", "Preferably Kotlin", False),
        ("Python", "Strong Python skills", True),
        ("Kubernetes", "Kubernetes (required)", True),
    ],
)
def test_preferred_wording_is_not_a_must_have(skill, text, must):
    [r] = clean_requirements([req(skill, text)]).kept
    assert r.must_have is must


@pytest.mark.parametrize(
    ("text", "years", "ok"),
    [
        ("4 or more years' experience with Python, C++, or KDB+/q", 4, True),
        ("4+ years of experience coding to a high standard in Python", 4, True),
        ("Three to five years of Java", 3, True),
        ("4+ years of experience coding in Python", 5, False),  # not the stated number
        ("Python, a few years", 3, False),
        ("less than 2 years of experience", 2, False),
        ("5+ years of Go", 5, False),  # not in the posting
    ],
)
def test_requirement_years_must_be_stated_verbatim(text, years, ok):
    posting = (
        "4 or more years’ experience with Python, C++, or KDB+/q. 4+ years of experience coding "
        "to a high standard in Python. Three to five years of Java. Python, a few years. "
        "less than 2 years of experience."
    )
    assert years_stated_verbatim(text, years, posting) is ok


# 3. eligibility conditions are stored apart and never tiered ---------------------------------


def test_store_keeps_soft_for_the_score_stage_and_eligibility_kind(conn):
    pid = add_posting(conn)
    out = ExtractionOutput.model_validate(
        extraction_input(
            requirements=[
                {
                    "text": "Python",
                    "skill_norm": "Python",
                    "must_have": True,
                    "years": None,
                    "context": None,
                },
                {
                    "text": "Problem-solving",
                    "skill_norm": "Problem-solving",
                    "must_have": True,
                    "years": None,
                    "context": None,
                },
                {
                    "text": "Currently enrolled in a degree",
                    "skill_norm": "Current enrollment",
                    "must_have": True,
                    "years": None,
                    "context": None,
                    "kind": "eligibility",
                },
                {
                    "text": "Less than 2 years full-time work experience",
                    "skill_norm": "Less than 2 years full-time work experience",
                    "must_have": True,
                    "years": 2,
                    "context": None,
                },
            ]
        )
    )
    store_extraction(conn, pid, out, "x", "x", "m", "extract_v2")
    rows = conn.execute("SELECT skill_norm, kind, years FROM requirements ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("Python", "skill", None),
        ("Problem-solving", "skill", None),  # the score stage labels it soft
        ("Current enrollment", "eligibility", None),
        ("Less than 2 years full-time work experience", "eligibility", None),
    ]


def test_eligibility_is_never_tiered_or_scored(settings, conn, fb, fake_llm):
    pid = add_posting(conn, title="SWE Intern", company="Acme", location="Toronto, ON")
    conn.execute(
        """INSERT INTO posting_facts (posting_id, city, province, work_arrangement,
               remote_scope, posting_language, french_requirement, seniority, confidence,
               model, prompt_version)
           VALUES (?, 'Toronto', 'ON', 'hybrid', 'city', 'en', 'none', 'intern', 0.9, 'm', 'p')""",
        (pid,),
    )
    conn.execute(
        "INSERT INTO requirements (posting_id, text, skill_norm, must_have, kind) "
        "VALUES (?, 'Python', 'Python', 1, 'skill')",
        (pid,),
    )
    conn.execute(
        "INSERT INTO requirements (posting_id, text, skill_norm, must_have, kind) "
        "VALUES (?, 'Currently enrolled', 'Current enrollment', 1, 'eligibility')",
        (pid,),
    )
    conn.execute("UPDATE postings SET status='candidate' WHERE id=?", (pid,))
    conn.commit()
    client, fake = fake_llm(lambda p: pytest.fail("rules settle Python; nothing else to tier"))
    run_score(settings, conn, fb, client=client)
    assert fake.messages.calls == []
    tiers = dict(conn.execute("SELECT skill_norm, tier FROM requirements").fetchall())
    assert tiers == {"Python": 0, "Current enrollment": None}
    breakdown = json.loads(conn.execute("SELECT breakdown_json FROM scores").fetchone()[0])
    assert breakdown["must_t3"] == 0  # eligibility never becomes a skill gap
    assert breakdown["score"] == 100 - 10 - 20  # Toronto (a move) + internship
    assert "elig: Current enrollment" in format_digest(settings, conn)


# 4. evidence must visibly state the value -------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "quote", "ok"),
    [
        # the real weak quotes from the first run
        ("min_years", 0, "Currently pursuing a Bachelor's, Master's, or PhD degree", False),
        (
            "remote_scope",
            "global",
            "we also have offices in Toronto, London, New York City, San Francisco, Montreal",
            False,
        ),
        # and what supporting quotes look like
        ("min_years", 0, "New grads welcome", True),
        ("min_years", 2, "2+ years of professional experience", True),
        ("min_years", 2, "less than 2 years of experience", False),
        ("min_years", 4, "4 or more years' experience with Python", True),
        ("min_years", 3, "3–5 years building real-time systems", True),
        ("remote_scope", "global", "You can work from anywhere", True),
        ("city", "Toronto", "Hybrid role based in Toronto, ON, or Sunnyvale CA", True),
        ("province", "ON", "Hybrid role based in Toronto, ON, or Sunnyvale CA", True),
        ("country", "CA", "Hybrid role based in Toronto, ON, or Sunnyvale CA", True),
        ("country", "US", "Hybrid role based in Toronto, ON", False),
        ("work_arrangement", "hybrid", "This role will be in-office on a hybrid schedule", True),
        ("work_arrangement", "remote", "This role will be in-office on a hybrid schedule", False),
        ("onsite_days", 3, "in the office at least 3 days per week", True),
        ("seniority", "intern", "Software Engineer Intern, Machine Learning", True),
        ("french_requirement", "required_working", "Clients francophones au quotidien", True),
        ("french_requirement", "required_working", "Our headquarters are in Quebec City", True),
        ("french_requirement", "asset", "Mandarin is an asset", False),
    ],
)
def test_evidence_supports(field, value, quote, ok):
    assert evidence_supports(field, value, quote) is ok


def test_weak_quotes_are_dropped_at_store_time():
    out = ExtractionOutput.model_validate(
        extraction_input(
            facts={
                "city": None,
                "province": None,
                "country": None,
                "remote_scope": "global",
                "min_years": 0,
                "evidence": [
                    {"field": "min_years", "quote": "Currently pursuing a degree"},
                    {"field": "remote_scope", "quote": "offices in Toronto and London"},
                    {"field": "work_arrangement", "quote": "This role is hybrid"},
                ],
            }
        )
    )
    seen = "Currently pursuing a degree. offices in Toronto and London. This role is hybrid."
    facts, dropped = to_facts(out, seen, seen)
    assert facts.evidence_span == {"work_arrangement": "This role is hybrid"}
    assert dropped == 2


def test_internal_is_not_intern():
    assert evidence_supports("seniority", "intern", "Software Engineer Intern, ML")
    assert evidence_supports("seniority", "intern", "Summer internships for students")
    assert not evidence_supports("seniority", "intern", "Internal Product Engineer")


# 5. employment terms count only with a quote that shows them ------------------------------


def test_employment_terms_need_evidence():
    from prospect.pipeline.extract import term_months

    assert (term_months(16, "weeks"), term_months(12, "months"), term_months(1, "years")) == (
        3.7,
        12,
        12,
    )
    out = ExtractionOutput.model_validate(
        extraction_input(
            facts={
                "part_time": True,
                "hours_per_week": 16,
                "contract": True,
                "term_length": 12,
                "term_unit": "months",
                "evidence": [
                    {"field": "hours_per_week", "quote": "Availability: 16 hours per week"},
                    {"field": "term_length", "quote": "a 12-month contract"},
                ],
            }
        )
    )
    seen = "Availability: 16 hours per week, on a 12-month contract."
    facts, _ = to_facts(out, seen, seen)
    assert (facts.part_time, facts.hours_per_week, facts.contract, facts.term_months) == (
        True,
        16,
        True,
        12,
    )
    # the same values with no quote that shows them cost nothing
    out = ExtractionOutput.model_validate(
        extraction_input(
            facts={
                "part_time": True,
                "hours_per_week": 16,
                "contract": True,
                "term_length": 12,
                "term_unit": "months",
                "evidence": [{"field": "term_length", "quote": "Join our team"}],
            }
        )
    )
    facts, _ = to_facts(out, "Join our team", "Join our team")
    assert (facts.part_time, facts.hours_per_week, facts.contract, facts.term_months) == (
        False,
        None,
        False,
        None,
    )


@pytest.mark.parametrize(
    ("field", "quote", "ok"),
    [
        ("part_time", "This is a part-time role", True),
        ("part_time", "16 hours per week", True),
        ("part_time", "full-time, permanent", False),
        ("hours_per_week", "10-20 hours per week", True),
        ("hours_per_week", "flexible schedule", False),
        ("contract", "12-month contract", True),
        ("contract", "independent contractor", True),
        ("contract", "permanent full-time position", False),
        ("term_length", "a 16-week co-op term", True),
        ("term_length", "May to August 2027", True),
        ("term_length", "Summer 2027", False),
    ],
)
def test_employment_term_evidence(field, quote, ok):
    assert evidence_supports(field, True, quote) is ok


@pytest.mark.parametrize(
    ("text", "soft"),
    [
        # live rerun 2026-10-08: real requirement texts
        ("Genuine interest in large-scale distributed systems, infrastructure, and ML", True),
        ("demonstrated passion for applied NLP models and products", True),
        ("Desire to work close to hardware/network and learn low-level engineering", True),
        ("Love for performance engineering", True),
        ("a deep curiosity to learn and absorb new technologies quickly", True),
        # the skill is claimed too: still a skill
        ("Experience with or strong interest in Workato or similar platforms", False),
        ("Familiarity with or interest in the Identity and Access Management domain", False),
        ("You are a passionate Python developer", False),
        ("3+ years of Python and a passion for clean code", False),
        # no interest wording at all
        ("Distributed systems design", False),
        ("Strong written communication", False),
    ],
)
def test_interest_wording_is_soft_unless_the_skill_is_claimed(text, soft):
    assert expresses_interest(text) is soft
