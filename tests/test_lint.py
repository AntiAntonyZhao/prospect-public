"""One test per lint rule in docs/FACTBANK.md (plus the metric regex and ??? rule)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prospect.factbank.lint import lint_file, lint_text

REPO = Path(__file__).resolve().parents[1]
FIXTURE = (Path(__file__).parent / "fixtures" / "factbank_fixture.yaml").read_text(encoding="utf-8")


def lint_with(old: str, new: str):
    assert FIXTURE.count(old) >= 1, old
    return lint_text(FIXTURE.replace(old, new, 1))


def messages(report, level: str = "error") -> str:
    return "\n".join(str(i) for i in report.issues if i.level == level)


def test_fixture_is_clean():
    r = lint_text(FIXTURE)
    assert r.ok and not r.warnings, r.render()


def test_demo_factbank_is_clean():
    """The demo profile (the default `[paths] factbank`) lints with no errors or warnings."""
    r = lint_file(REPO / "demo" / "factbank_demo.yaml")
    assert r.ok and not r.warnings, r.render()


def test_duplicate_id():
    r = lint_with("  - id: java\n", "  - id: python\n")
    assert "used 2 times" in messages(r)


def test_id_must_be_slug():
    r = lint_with("  - id: uml\n", "  - id: UML_Tool\n")
    assert "not slug-cased" in messages(r)


def test_evidence_must_resolve():
    r = lint_with("evidence: [acme-db]", "evidence: [acme-db, no-such-bullet]")
    assert "'no-such-bullet' is not a bullet or project id" in messages(r)


def test_evidence_may_be_a_project():
    r = lint_with("evidence: [side-deploy]", "evidence: [side-deploy, side]")
    assert r.ok, r.render()


def test_bullet_skill_must_resolve():
    r = lint_with("skills: [sql]\n", "skills: [sql, mysql]\n")
    assert "'mysql' is not a skill id" in messages(r)


def test_project_stack_must_resolve():
    r = lint_with("stack: [python, docker]", "stack: [python, docker, rust]")
    assert "'rust' is not a skill id" in messages(r)


def test_alias_may_not_be_another_skills_label():
    r = lint_with("aliases: [scripting]", "aliases: [scripting, SQL]")
    assert "Tier 0 match" in messages(r)


def test_alias_may_not_be_another_skills_id_normalized():
    r = lint_with("aliases: [scripting]", "aliases: [scripting, Web Sockets]")
    assert "skill 'websockets'" in messages(r)


def test_adjacent_may_not_be_a_skill():
    r = lint_with("adjacent: [kotlin]", "adjacent: [kotlin, python]")
    assert "declares a skill you do NOT have" in messages(r)


def test_level_must_be_valid():
    r = lint_with(
        "level: working\n    evidence: [acme-docs]", "level: guru\n    evidence: [acme-docs]"
    )
    assert "schema" in messages(r)


def test_level_todo_is_an_error():
    r = lint_with(
        "level: working\n    evidence: [acme-docs]", "level: ???\n    evidence: [acme-docs]"
    )
    assert "is ???" in messages(r)
    assert not r.ok


def test_metrics_must_list_every_numeric_token():
    r = lint_with('metrics: ["78+"]', "metrics: []")
    assert "['78+'] appear in text but not in metrics" in messages(r)


def test_stale_metric_is_a_warning():
    r = lint_with('metrics: ["10K"]', 'metrics: ["10K", "99%"]')
    assert r.ok
    assert "['99%'] listed but not found" in messages(r, "warning")


def test_name_digits_are_not_metrics():
    """GPT-4 / STL10 / S3 are names, not metrics (fact banks rely on this)."""
    r = lint_with(
        "Wrote a minimax game agent in Python",
        "Wrote a minimax game agent in Python against GPT-4 on STL10 and S3",
    )
    assert r.ok, r.render()


def test_no_evidence_no_basis_warns():
    r = lint_with('    basis: "Self-taught"\n', "")
    assert "without support" in messages(r, "warning")


def test_work_authorization_unset_warns():
    r = lint_with("work_authorization: pr", "work_authorization: null")
    assert r.ok
    assert "work_authorization" in messages(r, "warning")


def test_yaml11_boolean_warning():
    r = lint_with('provinces: ["QC", "ON"]', "provinces: [QC, ON]")
    assert r.ok
    assert "unquoted 'ON'" in messages(r, "warning")


@pytest.mark.parametrize("bad", ["version: [", "- just\n- a list\n"])
def test_not_a_factbank(bad):
    assert not lint_text(bad).ok
