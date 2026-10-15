from __future__ import annotations

import pytest

from prospect.eval.label import label_session, parse_ids
from prospect.eval.metrics import (
    DecisionCtx,
    format_report,
    norm_value,
    run_eval_extract,
    score_field,
)
from tests.conftest import add_posting, extraction_input


def test_field_metrics_definitions():
    pairs = [
        ("montreal", "montreal"),
        ("toronto", None),
        (None, "ottawa"),
        (None, None),
        ("toronto", "montreal"),
    ]
    fs = score_field("city", pairs)
    assert (fs.made, fs.present, fs.tp) == (3, 3, 1)
    assert fs.precision == pytest.approx(1 / 3) and fs.recall == pytest.approx(1 / 3)
    assert fs.accuracy == pytest.approx(2 / 5)  # includes null == null
    assert score_field("x", []).f1 == 0.0


CTX = DecisionCtx(home_city="Montreal", years_cap=2)


def _label(conn, pid, **gold):
    base = {"city": "Montreal", "posting_language": "en", "min_years": "0", "seniority": "newgrad"}
    for field, value in {**base, **gold}.items():
        conn.execute(
            "INSERT INTO labels (posting_id, field, gold_value) VALUES (?,?,?)", (pid, field, value)
        )
    conn.commit()


def test_eval_extract_reports_per_field_and_lists_disagreements(settings, conn, fake_llm):
    a = add_posting(conn, title="A")
    b = add_posting(conn, title="B", company="Other")
    _label(conn, a)
    _label(conn, b, seniority="intern")

    def responder(p):
        return extraction_input()

    client, _ = fake_llm(responder)
    report = run_eval_extract(settings, conn, "sonnet", client=client, ctx=CTX)
    assert "claude-sonnet-5" in report
    for view in ("location", "posting_language", "min_years", "internship"):
        assert f"  {view}" in report
    assert "work_arrangement" not in report  # not a labeled field
    assert "$/posting" in report and "p50 latency" in report
    assert "disagreements (1):" in report
    assert f"{b:>6} internship        gold intern" in report
    assert "Other — B" in report
    # predictions never touch posting_facts; every call is in runs
    assert conn.execute("SELECT COUNT(*) FROM posting_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE stage='eval_extract'").fetchone()[0] == 2
    assert format_report(conn, ctx=CTX) == report


def test_eval_needs_labels(settings, conn, fake_llm):
    client, _ = fake_llm(lambda p: pytest.fail("no call"))
    assert "no labeled postings" in run_eval_extract(
        settings, conn, "haiku", client=client, ctx=CTX
    )


def test_label_session_stores_gold_values(conn):
    pid = add_posting(
        conn,
        status="candidate",
        description="<p>Bilingual role. " + "Work on things. " * 200 + "</p>",
    )
    answers = iter(["Montreal", "x", "e", "1", "g"])  # 'x' is not a language → re-ask
    shown = []
    out = label_session(conn, limit=5, ask=lambda q: next(answers), echo=shown.append)
    assert "labeled 1 posting" in out
    gold = dict(conn.execute("SELECT field, gold_value FROM labels WHERE posting_id=?", (pid,)))
    assert gold == {
        "city": "Montreal",
        "posting_language": "en",
        "min_years": "1",
        "seniority": "newgrad",
    }
    assert any("expected one of" in s for s in shown)


def test_label_session_quit_and_skip(conn):
    add_posting(conn, status="candidate", description="<p>" + "x " * 900 + "</p>")
    assert "labeled 0" in label_session(conn, ask=lambda q: "q", echo=lambda s: None)
    assert "labeled 0" in label_session(conn, ask=lambda q: "s", echo=lambda s: None)


def test_diverse_sample_one_per_company_and_french_first(conn):
    from prospect.eval.sample import diverse_sample

    body = "<p>" + "Build reliable services in Python. " * 20 + "</p>"
    specs = [
        ("A", "Software Engineer Intern", "Montreal, QC", False),
        ("A", "Software Engineer Intern II", "Montreal, QC", False),  # same company: skipped
        ("B", "Machine Learning Engineer, New Grad", "Toronto, ON", False),
        ("C", "Backend Developer", "Remote - Canada", True),
        ("D", "Quant Developer", "Toronto, ON", False),
        ("E", "Frontend Developer", "Ottawa, ON", True),
    ]
    for company, title, loc, french in specs:
        pid = add_posting(
            conn, title=title, company=company, location=loc, status="candidate", description=body
        )
        if french:
            conn.execute("UPDATE postings SET flags='[\"french_regex\"]' WHERE id=?", (pid,))
    conn.commit()
    picks = diverse_sample(conn, n=4, french_share=0.5)
    assert len({p.company for p in picks}) == 4
    assert [p.french for p in picks[:2]] == [True, True]  # French quota filled first
    assert {p.company for p in picks} <= {"A", "B", "C", "D", "E"}


def test_label_ids_follow_the_given_order(conn):
    a = add_posting(conn, title="A", status="candidate")
    b = add_posting(conn, title="B", company="Other", status="candidate")
    seen = []
    label_session(conn, ids=[b, a], ask=lambda q: "q", echo=seen.append)
    assert any(f"#{b} " in s for s in seen)
    assert not any(f"#{a} " in s for s in seen)  # quit before reaching a


def test_report_scores_the_decisions_and_reads_remote_as_no_move(settings, conn, fake_llm):
    a = add_posting(conn, title="A")
    b = add_posting(conn, title="B", company="Other")
    c = add_posting(conn, title="C", company="Third")
    _label(conn, a)
    _label(conn, b, seniority="intern")
    _label(conn, c, city="Remote", min_years="5")

    def responder(p):
        if "Title: C" in p["messages"][0]["content"]:
            return extraction_input(
                facts={"city": None, "work_arrangement": "remote", "min_years": 5}
            )
        return extraction_input()

    client, _ = fake_llm(responder)
    ctx = DecisionCtx(home_city="Montreal", years_cap=2)
    report = run_eval_extract(settings, conn, "haiku", client=client, ctx=ctx)
    assert norm_value("city", "Remote") is None  # not a city
    assert norm_value("min_years", "0") is None  # no minimum == not stated
    assert norm_value("min_years", "2") == "2"
    assert "French posting -> reject               0     0      3/3" in report
    assert "internship (term penalty)              1     0      2/3" in report
    assert "no move (Montreal or remote)           3     3      3/3" in report


def test_fields_are_scored_the_way_the_pipeline_uses_them(settings, conn, fake_llm):
    """Policy 2026-10-06: junior vs mid, or which listed city was picked, costs nothing;
    only intern-or-not and home / remote / elsewhere / unknown do."""
    multi = add_posting(conn, title="A", location="London; Montreal; Singapore")
    level = add_posting(conn, title="B", company="Other", location="Toronto, ON")
    wrong = add_posting(conn, title="C", company="Third", location="Toronto, ON")
    _label(conn, multi, city="-", seniority="newgrad")  # labeled "not stated"
    _label(conn, level, city="Toronto", seniority="mid")
    _label(conn, wrong, city="Toronto")

    def responder(p):
        content = p["messages"][0]["content"]
        if "Title: A" in content:
            return extraction_input(facts={"city": "Montreal"})  # picked the listed home city
        if "Title: B" in content:
            return extraction_input(facts={"city": "Toronto", "seniority": "junior"})
        return extraction_input(facts={"city": "Toronto", "work_arrangement": "remote"})

    client, _ = fake_llm(responder)
    report = run_eval_extract(settings, conn, "haiku", client=client, ctx=CTX)
    assert "disagreements (1):" in report  # only the remote over-call counts
    assert f"{wrong:>6} location          gold elsewhere  pred remote" in report
    # C's remote over-call also drops its city (a remote role has no city): exact 1/3
    assert "exact labels (reference, not scored): city 1/3, seniority 2/3" in report


def test_parse_ids_accepts_full_width_commas():
    assert parse_ids("8396,10098") == [8396, 10098]
    assert parse_ids("8396，10098") == [8396, 10098]
    assert parse_ids(" 8396  10098 ") == [8396, 10098]
    with pytest.raises(ValueError, match="not a posting id"):
        parse_ids("8396,abc")


def test_relabel_shows_old_values_and_replaces_them(conn):
    a = add_posting(conn, title="A", status="candidate")
    b = add_posting(conn, title="B", company="Other", status="candidate")
    _label(conn, a, seniority="intern")
    _label(conn, b)
    answers = iter(["Toronto", "e", "2", "j", "Remote", "e", "-", "g"])
    shown: list[str] = []
    out = label_session(conn, relabel=[a, b], ask=lambda q: next(answers), echo=shown.append)
    assert "labeled 2" in out
    assert any("current labels (to be replaced): city=Montreal" in s for s in shown)
    gold_a = dict(conn.execute("SELECT field, gold_value FROM labels WHERE posting_id=?", (a,)))
    assert gold_a["city"] == "Toronto" and gold_a["seniority"] == "junior"
    gold_b = dict(conn.execute("SELECT field, gold_value FROM labels WHERE posting_id=?", (b,)))
    assert gold_b["city"] == "Remote" and gold_b["min_years"] is None
