from __future__ import annotations

import dataclasses
import json

import pytest

from prospect.factbank.alias_index import AliasIndex
from prospect.models.posting import Requirement, TierLabel
from prospect.pipeline.score import (
    deterministic_tier,
    enforce_tier_rules,
    run_score,
    score_posting,
    score_prompt,
    tier_context,
    tier_key,
)
from tests.conftest import add_posting


def req(skill: str, must: bool = True, years: int | None = None, text: str | None = None):
    return Requirement(text=text or skill, skill_norm=skill, must_have=must, years=years)


@pytest.mark.parametrize(
    ("skill", "years", "tier", "extra"),
    [
        ("Python", None, 0, {"substate": "strong", "matched_node": "python"}),
        ("Go", None, 0, {"substate": "weak", "matched_node": "go"}),  # familiar
        ("OOP", None, 1, {"matched_node": "java"}),
        ("Python", 3, 3, {}),  # years beyond the fact bank
        ("AWS Certified Developer", None, 3, {}),
        ("Master's degree in CS", None, 3, {}),
        ("Bachelor's degree in Computer Science or related", None, 0, {"matched_node": "uni"}),
        ("Security clearance", None, 3, {}),
        ("English", None, 0, {}),
        ("Spanish", None, 3, {}),
        ("Python | Rust", None, 0, {"matched_node": "python"}),  # any one suffices
    ],
)
def test_deterministic_tiers(fb, skill, years, tier, extra):
    t = deterministic_tier(req(skill, years=years), fb, AliasIndex.build(fb), "none")
    assert t.tier == tier
    for k, v in extra.items():
        assert getattr(t, k) == v


def test_alias_tier_one_carries_an_evidence_chain(fb):
    t = deterministic_tier(req("object-oriented programming"), fb, AliasIndex.build(fb), None)
    assert t.tier == 1 and t.evidence == ["java", "acme-api"]


def test_french_requirement_only_fails_a_french_posting(fb):
    """Policy 2026-10: French in an English CS posting is boilerplate, not a gap."""
    idx = AliasIndex.build(fb)
    assert deterministic_tier(req("French"), fb, idx, "required_working").tier == 0
    assert deterministic_tier(req("French"), fb, idx, "posting_in_french").tier == 3


def test_years_at_or_under_the_cap_are_not_a_hard_gap(fb):
    idx = AliasIndex.build(fb)
    assert deterministic_tier(req("Python", years=2), fb, idx, None, years_cap=2).tier == 0
    assert deterministic_tier(req("Python", years=3), fb, idx, None, years_cap=2).tier == 3


def test_adjacent_and_unknown_go_to_the_model(fb):
    idx = AliasIndex.build(fb)
    kafka = deterministic_tier(req("Kafka"), fb, idx, None)
    assert kafka.tier is None and kafka.adjacent_of == "websockets"
    assert deterministic_tier(req("Rust"), fb, idx, None).tier is None


def label(**kw):
    base = {
        "index": 0,
        "soft": False,
        "tier": 2,
        "t3_reason": None,
        "matched_node": None,
        "evidence": [],
        "est_hours": 4.0,
        "nearest_node": "python",
        "rationale": "r",
    }
    return TierLabel(**{**base, **kw})


def test_tier_one_without_citation_becomes_tier_two(fb):
    t = enforce_tier_rules(label(tier=1, matched_node="java", evidence=[]), fb, 8, None)
    assert t.tier == 2


def test_tier_one_with_invented_ids_becomes_tier_two(fb):
    t = enforce_tier_rules(label(tier=1, evidence=["made-up-node"]), fb, 8, None)
    assert t.tier == 2


def test_tier_one_with_real_evidence_stays(fb):
    t = enforce_tier_rules(
        label(tier=1, matched_node="python", evidence=["acme-tool"]), fb, 8, None
    )
    assert t.tier == 1 and t.evidence == ["acme-tool"]


def test_tier_two_over_the_hour_cap_becomes_tier_three(fb):
    assert enforce_tier_rules(label(tier=2, est_hours=20), fb, 8, None).tier == 3
    assert enforce_tier_rules(label(tier=2, est_hours=None), fb, 8, None).tier == 3
    assert enforce_tier_rules(label(tier=2, est_hours=8), fb, 8, None).tier == 2


def test_adjacent_is_tier_two_even_when_the_model_says_three(fb):
    t = enforce_tier_rules(label(tier=3, est_hours=12), fb, 8, adjacent_of="websockets")
    assert t.tier == 2 and t.nearest_node


def test_tier_zero_needs_a_real_skill_id(fb):
    assert enforce_tier_rules(label(tier=0, matched_node="rustlang"), fb, 8, None).tier == 2


def _tiered(tier, must=True, substate=None):
    return Requirement(text="x", skill_norm="x", must_have=must, tier=tier, tier_substate=substate)


def test_score_formula(settings, fb):
    facts = {
        "city": "Montreal",
        "province": "QC",
        "work_arrangement": "hybrid",
        "remote_scope": "city",
        "french_requirement": "required_pro_forma",
        "seniority": "newgrad",
    }
    reqs = [_tiered(0), _tiered(3), _tiered(2), _tiered(2, must=False), _tiered(0, substate="weak")]
    score, bd = score_posting(
        reqs,
        facts,
        repost_count=1,
        prior_no_response=False,
        settings=settings,
        home_city="Montreal",
    )
    assert score == 100 - 25 - 10 - 3 - 2 - 4  # no French penalty any more
    assert bd["must_t3"] == 1 and bd["weak_must"] == 1 and bd["ghost"] == -4
    assert bd["fit_location"] == 0 and bd["short_term"] == 0 and bd["part_time"] == 0


def test_score_clamps_and_fits(settings, fb):
    facts = {
        "city": "Vancouver",
        "province": "BC",
        "work_arrangement": "onsite",
        "remote_scope": "unknown",
        "french_requirement": "none",
        "seniority": "senior",
    }
    score, bd = score_posting([_tiered(3)] * 5, facts, 9, True, settings, "Montreal")
    assert score == 0
    assert bd["fit_location"] == -10  # a move, anywhere in Canada
    assert bd["ghost"] == -20  # capped at ghost_max


def test_score_is_pure(settings, fb):
    facts = {
        "city": "Toronto",
        "province": "ON",
        "work_arrangement": "remote",
        "remote_scope": "canada",
        "french_requirement": "none",
        "seniority": "junior",
    }
    a = score_posting([_tiered(0)], facts, 0, False, settings, "Montreal")
    b = score_posting([_tiered(0)], facts, 0, False, settings, "Montreal")
    assert a == b and a[0] == 100  # remote: no move needed


@pytest.mark.parametrize(
    ("city", "arrangement", "seniority", "expected"),
    [
        ("Montréal", "onsite", "newgrad", 100),  # home city (accents ignored)
        (None, "remote", "newgrad", 100),  # remote: no move
        ("Toronto", "hybrid", "newgrad", 90),  # a move
        (None, "unknown", "newgrad", 95),  # don't know yet
        ("Montreal", "hybrid", "intern", 80),  # internship
        ("Toronto", "hybrid", "intern", 70),
    ],
)
def test_home_city_and_intern_terms(settings, city, arrangement, seniority, expected):
    facts = {
        "city": city,
        "province": None,
        "work_arrangement": arrangement,
        "remote_scope": "canada",
        "french_requirement": "none",
        "seniority": seniority,
    }
    assert score_posting([_tiered(0)], facts, 0, False, settings, "Montreal")[0] == expected


def _extracted(conn, pid, reqs, **facts):
    f = {
        "city": "Montreal",
        "province": "QC",
        "work_arrangement": "hybrid",
        "remote_scope": "city",
        "posting_language": "en",
        "french_requirement": "none",
        "seniority": "newgrad",
        **facts,
    }
    conn.execute(
        """INSERT INTO posting_facts (posting_id, city, province, work_arrangement, remote_scope,
               posting_language, french_requirement, seniority, confidence, model, prompt_version)
           VALUES (?,?,?,?,?,?,?,?,0.9,'m','p')""",
        (
            pid,
            f["city"],
            f["province"],
            f["work_arrangement"],
            f["remote_scope"],
            f["posting_language"],
            f["french_requirement"],
            f["seniority"],
        ),
    )
    for skill, must in reqs:
        conn.execute(
            "INSERT INTO requirements (posting_id, text, skill_norm, must_have) VALUES (?,?,?,?)",
            (pid, skill, skill, int(must)),
        )
    conn.execute("UPDATE postings SET status='candidate' WHERE id=?", (pid,))
    conn.commit()


def test_run_score_only_asks_the_model_what_rules_cannot_settle(settings, conn, fb, fake_llm):
    pid = add_posting(conn)
    _extracted(conn, pid, [("Python", True), ("OOP", True), ("Kafka", True), ("Rust", False)])
    asked = []

    def responder(p):
        lines = [
            json.loads(ln) for ln in p["messages"][0]["content"].splitlines() if ln.startswith("{")
        ]
        asked.extend(item["skill_norm"] for item in lines)
        return {
            "labels": [
                {
                    "index": item["index"],
                    "soft": False,
                    "tier": 2 if item["skill_norm"] == "Kafka" else 3,
                    "t3_reason": None if item["skill_norm"] == "Kafka" else "depth",
                    "matched_node": None,
                    "evidence": [],
                    "est_hours": 4 if item["skill_norm"] == "Kafka" else 40,
                    "nearest_node": "websockets",
                    "rationale": "r",
                }
                for item in lines
            ]
        }

    client, fake = fake_llm(responder)
    out = run_score(settings, conn, fb, client=client)
    assert "1 scored" in out
    assert asked == ["Kafka", "Rust"]
    assert "adjacent" in fake.messages.calls[0]["messages"][0]["content"]
    tiers = dict(conn.execute("SELECT skill_norm, tier FROM requirements").fetchall())
    assert tiers == {"Python": 0, "OOP": 1, "Kafka": 2, "Rust": 3}
    score = conn.execute("SELECT score, breakdown_json FROM scores").fetchone()
    assert score["score"] == 100 - 10 - 3
    assert json.loads(score["breakdown_json"])["must_t2"] == 1


def test_run_score_needs_no_model_when_rules_decide_everything(settings, conn, fb, fake_llm):
    pid = add_posting(conn)
    _extracted(conn, pid, [("Python", True), ("SQL", True)])
    client, fake = fake_llm(lambda p: pytest.fail("model should not be called"))
    run_score(settings, conn, fb, client=client)
    assert fake.messages.calls == []
    assert conn.execute("SELECT score FROM scores").fetchone()[0] == 100


def test_run_score_scores_french_working_but_skips_french_postings(settings, conn, fb, fake_llm):
    """Policy 2026-10: a French requirement in an English posting is boilerplate."""
    working = add_posting(conn)
    _extracted(conn, working, [("Python", True)], french_requirement="required_working")
    in_french = add_posting(conn, title="Développeur", company="Autre")
    _extracted(conn, in_french, [("Python", True)], french_requirement="posting_in_french")
    client, _ = fake_llm(lambda p: pytest.fail("no call"))
    assert "1 scored" in run_score(settings, conn, fb, client=client)
    rows = conn.execute("SELECT posting_id, score FROM scores").fetchall()
    assert [tuple(r) for r in rows] == [(working, 100)]


def test_score_prompt_tells_the_model_years_are_settled(settings, fb):
    p = score_prompt(settings, fb)
    assert p.version == "score_v3"
    assert "Absent from the fact bank is not a hard gap" in p.text
    assert "at most 2 years" in p.text and "{years_cap}" not in p.text
    assert "years of experience beyond" not in p.text


def test_tier_three_with_a_small_estimate_is_tier_two(fb):
    """Live run 2026-10-06: Sass and Jest came back Tier 3 because 'not in the fact bank'."""
    sass = enforce_tier_rules(label(tier=3, t3_reason="depth", est_hours=3), fb, 8, None)
    assert sass.tier == 2
    for reason, hours, tier in ((None, 4, 2), ("depth", 40, 3), ("depth", None, 3)):
        lab = label(tier=3, t3_reason=reason, est_hours=hours)
        assert enforce_tier_rules(lab, fb, 8, None).tier == tier
    # a credential is not about hours
    cert = label(tier=3, t3_reason="credential", est_hours=5)
    assert enforce_tier_rules(cert, fb, 8, None).tier == 3


def test_soft_requirements_are_kept_but_never_scored(settings, conn, fb, fake_llm):
    """Policy 2026-10-06: the model calls a requirement soft; it never costs points."""
    pid = add_posting(conn)
    _extracted(
        conn,
        pid,
        [("Python", True), ("Passion for transportation", True), ("Thrive in a startup", True)],
    )

    def responder(p):
        lines = [
            json.loads(ln) for ln in p["messages"][0]["content"].splitlines() if ln.startswith("{")
        ]
        return {
            "labels": [
                {
                    "index": item["index"],
                    "soft": True,
                    "tier": 3,  # ignored for a soft label
                    "t3_reason": None,
                    "matched_node": None,
                    "evidence": [],
                    "est_hours": None,
                    "nearest_node": None,
                    "rationale": "a trait",
                }
                for item in lines
            ]
        }

    client, _ = fake_llm(responder)
    run_score(settings, conn, fb, client=client)
    rows = conn.execute("SELECT skill_norm, kind, tier FROM requirements ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("Python", "skill", 0),
        ("Passion for transportation", "soft", None),
        ("Thrive in a startup", "soft", None),
    ]
    assert conn.execute("SELECT score FROM scores").fetchone()[0] == 100


@pytest.mark.parametrize(
    ("facts", "key"),
    [
        ({"city": "Montreal"}, "home"),
        ({"city": None, "location_raw": "London, Montreal, Singapore"}, "home"),
        ({"city": "Toronto", "location_raw": "Toronto, ON; Montréal, QC"}, "home"),
        ({"city": "Toronto", "location_raw": "Toronto; Remote - Canada"}, "remote"),
        ({"city": None, "work_arrangement": "remote"}, "remote"),
        ({"city": None, "location_raw": "Toronto, ON; Vancouver, BC"}, "elsewhere"),
        ({"city": "Toronto", "location_raw": "Toronto"}, "elsewhere"),
        ({"city": None, "location_raw": None}, "unknown"),
    ],
)
def test_location_counts_every_listed_place(facts, key):
    from prospect.pipeline.score import location_key

    assert location_key(facts, "Montreal") == key


def test_montreal_and_remote_together_count_once(settings):
    def fit(location_raw, arrangement="hybrid"):
        facts = {"city": None, "work_arrangement": arrangement, "location_raw": location_raw}
        return score_posting([], facts, 0, False, settings, "Montreal")[0]

    assert fit("Montreal") == fit("Montreal; Toronto") == fit("Montreal; Remote") == 100
    assert fit("Toronto; Remote - Canada") == 100
    assert fit("Toronto; Vancouver") == 90


@pytest.mark.parametrize(
    ("facts", "part", "short"),
    [
        # policy 2026-10-06, the user's examples: fewer hours cost more, 37.5 h is full time
        ({"hours_per_week": 37.5}, 0, 0),
        ({"hours_per_week": 40}, 0, 0),
        ({"hours_per_week": 30}, 6, 0),
        ({"hours_per_week": 10}, 22, 0),
        ({"part_time": True}, 14, 0),  # no hours stated → 20 h
        # a shorter contract costs more; one year costs nothing
        ({"contract": True, "term_months": 12}, 0, 0),
        ({"contract": True, "term_months": 6}, 0, 15),
        ({"contract": True, "term_months": 2}, 0, 25),
        ({"contract": True}, 0, 15),  # no length stated → 6 months
        # an internship is a fixed term too: 12 months free, 4 months (the default) = old −20
        ({"seniority": "intern", "term_months": 12}, 0, 0),
        ({"seniority": "intern", "term_months": 3}, 0, 22.5),
        ({"seniority": "intern"}, 0, 20),
        # part-time and short-term add up (e.g. 16 h/week on a 12-month contract)
        ({"hours_per_week": 16, "contract": True, "term_months": 12}, 17.2, 0),
        ({"seniority": "newgrad", "term_months": 3}, 0, 0),  # a permanent role has no term
    ],
)
def test_employment_terms_cost_points(settings, facts, part, short):
    from prospect.pipeline.score import employment_penalties

    got = employment_penalties(facts, settings.score)
    assert (got["part_time"], got["short_term"]) == (pytest.approx(part), pytest.approx(short))


def test_employment_terms_enter_the_score(settings):
    facts = {"city": "Montreal", "hours_per_week": 16, "contract": True, "term_months": 6}
    score, bd = score_posting([], facts, 0, False, settings, "Montreal")
    assert score == pytest.approx(100 - 17.2 - 15)
    assert bd["part_time"] == -17.2 and bd["short_term"] == -15 and bd["term_months"] == 6


# --- tier cache (live run 2026-10-06: re-tiering moved scores by up to 25 points) ----------


def _labeling(tiers: dict[str, int], asked: list[str]):
    """Labels each asked skill with `tiers[tier_key(skill)]` and records what was asked."""

    def responder(p):
        lines = [
            json.loads(ln) for ln in p["messages"][0]["content"].splitlines() if ln.startswith("{")
        ]
        asked.extend(item["skill_norm"] for item in lines)
        out = []
        for item in lines:
            tier = tiers[tier_key(item["skill_norm"])]
            out.append(
                label(
                    index=item["index"],
                    tier=tier,
                    t3_reason="depth" if tier == 3 else None,
                    est_hours=4 if tier == 2 else 40,
                ).model_dump()
            )
        return {"labels": out}

    return responder


def _scores(conn):
    return dict(conn.execute("SELECT posting_id, score FROM scores").fetchall())


def test_tier_key_ignores_case_accents_spacing_and_alternative_order():
    assert tier_key("Rust ") == tier_key("rust") == "rust"
    assert tier_key("Java | Kotlin") == tier_key("kotlin|JAVA") == "java | kotlin"
    assert tier_key("Pandas.") == "pandas"
    assert tier_key("Québec  tooling") == "quebec tooling"


def test_a_skill_is_tiered_once_and_reused_across_postings_and_runs(settings, conn, fb, fake_llm):
    a = add_posting(conn)
    _extracted(conn, a, [("Python", True), ("Rust", True)])
    b = add_posting(conn, title="Other role", company="Other")
    _extracted(conn, b, [("rust ", True), ("Kafka", False)])
    asked: list[str] = []
    client, _ = fake_llm(_labeling({"rust": 3, "kafka": 2}, asked))
    out = run_score(settings, conn, fb, client=client)
    assert "2 scored" in out and "1 skill tiers reused from the cache" in out
    assert sorted(tier_key(s) for s in asked) == ["kafka", "rust"]  # Rust asked once
    first = _scores(conn)
    assert first == {a: 75, b: 72}
    # a re-run asks nothing and scores the same, whatever the model would say today
    client, _ = fake_llm(lambda p: pytest.fail("a cached skill is not asked again"))
    run_score(settings, conn, fb, client=client, force=True)
    assert _scores(conn) == first
    rationale = conn.execute("SELECT rationale FROM requirements WHERE skill_norm='Rust'")
    assert rationale.fetchone()[0].endswith("[cached]")


def test_a_skill_listed_twice_in_one_posting_is_asked_once(settings, conn, fb, fake_llm):
    pid = add_posting(conn)
    _extracted(conn, pid, [("Rust", True), ("RUST", False)])
    asked: list[str] = []
    client, _ = fake_llm(_labeling({"rust": 3}, asked))
    run_score(settings, conn, fb, client=client)
    assert asked == ["Rust"]
    assert [r[0] for r in conn.execute("SELECT tier FROM requirements ORDER BY id")] == [3, 3]


def test_fresh_tiers_ask_again_and_overwrite_the_cache(settings, conn, fb, fake_llm):
    pid = add_posting(conn)
    _extracted(conn, pid, [("Rust", True)])
    client, _ = fake_llm(_labeling({"rust": 3}, []))
    run_score(settings, conn, fb, client=client)
    assert _scores(conn) == {pid: 75}
    asked: list[str] = []
    client, _ = fake_llm(_labeling({"rust": 2}, asked))
    run_score(settings, conn, fb, client=client, force=True, fresh_tiers=True)
    assert asked == ["Rust"] and _scores(conn) == {pid: 90}
    client, _ = fake_llm(lambda p: pytest.fail("the new label is cached"))
    run_score(settings, conn, fb, client=client, force=True)
    assert _scores(conn) == {pid: 90}


def test_a_skipped_label_is_not_cached(settings, conn, fb, fake_llm):
    pid = add_posting(conn)
    _extracted(conn, pid, [("Rust", True)])
    client, _ = fake_llm(lambda p: {"labels": []})
    run_score(settings, conn, fb, client=client)  # the cautious default: Tier 3
    asked: list[str] = []
    client, _ = fake_llm(_labeling({"rust": 2}, asked))
    run_score(settings, conn, fb, client=client, force=True)
    assert asked == ["Rust"] and _scores(conn) == {pid: 90}


def test_the_cache_starts_fresh_when_model_prompt_or_fact_bank_change(settings, fb):
    prompt = score_prompt(settings, fb)
    base = tier_context("claude-sonnet-5", prompt, fb)
    assert base == tier_context("claude-sonnet-5", prompt, fb)
    assert base != tier_context("claude-sonnet-6", prompt, fb)
    reworded = dataclasses.replace(prompt, text=prompt.text + "\nOne more rule.")
    assert base != tier_context("claude-sonnet-5", reworded, fb)
    learned = fb.model_copy(deep=True)
    learned.skills[0].aliases.append("a new alias")
    assert base != tier_context("claude-sonnet-5", prompt, learned)


def test_a_soft_label_from_before_the_cache_is_judged_again(settings, conn, fb, fake_llm):
    """2026-10-08: 32 rows marked soft before the tier cache existed were never asked
    again, so the same skill could stay soft in one posting and be a gap in the next."""
    pid = add_posting(conn)
    _extracted(conn, pid, [("Technical writing", True)])
    conn.execute("UPDATE requirements SET kind = 'soft'")  # an earlier run's label
    conn.commit()
    asked: list[str] = []
    client, _ = fake_llm(_labeling({"technical writing": 2}, asked))
    run_score(settings, conn, fb, client=client)
    assert asked == ["Technical writing"]
    row = conn.execute("SELECT kind, tier FROM requirements").fetchone()
    assert tuple(row) == ("skill", 2) and _scores(conn) == {pid: 90}


def test_interest_wording_is_soft_per_requirement_not_per_skill(settings, conn, fb, fake_llm):
    """2026-10-08: "Genuine interest in distributed systems" is soft; the same skill asked
    for as experience in another posting is a gap. Neither label leaks through the cache."""
    a = add_posting(conn)
    b = add_posting(conn, title="Other role", company="Other")
    _extracted(conn, a, [("Distributed systems", True)])
    _extracted(conn, b, [("Distributed systems", True)])
    text = "UPDATE requirements SET text = ? WHERE posting_id = ?"
    conn.execute(text, ("Genuine interest in large-scale distributed systems", a))
    conn.execute(text, ("Experience building distributed systems", b))
    conn.commit()
    asked: list[str] = []
    client, _ = fake_llm(_labeling({"distributed systems": 3}, asked))
    run_score(settings, conn, fb, client=client)
    assert asked == ["Distributed systems"]  # b's only: a's wording settles it in code
    kinds = dict(conn.execute("SELECT posting_id, kind FROM requirements").fetchall())
    assert kinds == {a: "soft", b: "skill"}
    assert _scores(conn) == {a: 100, b: 75}


def test_a_soft_label_on_interest_wording_is_not_cached(settings, conn, fb, fake_llm):
    """Mixed wording reaches the model; if it calls it soft for the enthusiasm, that label
    belongs to the sentence, so a later posting that requires the skill is asked again."""
    a = add_posting(conn)
    _extracted(conn, a, [("Rust", True)])
    conn.execute(
        "UPDATE requirements SET text = 'Real enthusiasm for systems and hands-on experience "
        "with Rust'"
    )
    conn.commit()

    def soft(p):
        lines = [json.loads(ln) for ln in p["messages"][0]["content"].splitlines() if ln[:1] == "{"]
        return {"labels": [label(index=x["index"], soft=True).model_dump() for x in lines]}

    client, _ = fake_llm(soft)
    run_score(settings, conn, fb, client=client)
    assert conn.execute("SELECT kind FROM requirements").fetchone()[0] == "soft"
    b = add_posting(conn, title="Other role", company="Other")
    _extracted(conn, b, [("Rust", True)])
    asked: list[str] = []
    client, _ = fake_llm(_labeling({"rust": 3}, asked))
    run_score(settings, conn, fb, client=client, posting_ids=[b])
    assert asked == ["Rust"] and _scores(conn)[b] == 75
