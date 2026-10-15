"""Labeling in an editor: export a Markdown file, fill it over several sittings, import."""

from __future__ import annotations

from prospect.eval.labelfile import export_label_file, import_label_file, label_file_ids
from tests.conftest import add_posting


def _gold(conn, pid):
    return dict(
        conn.execute("SELECT field, gold_value FROM labels WHERE posting_id=?", (pid,)).fetchall()
    )


def _fill(path, pid, block):
    text = path.read_text(encoding="utf-8")
    empty = f"```label {pid}\ncity: \nposting_language: \nmin_years: \nseniority: \n```"
    assert empty in text
    path.write_text(text.replace(empty, f"```label {pid}\n{block}\n```"), encoding="utf-8")


def test_export_writes_the_full_description_and_blank_blocks(conn, tmp_path):
    long_jd = "<p>" + "We build things in Python. " * 300 + "Requires 2+ years.</p>"
    pid = add_posting(conn, description=long_jd)
    path = tmp_path / "labels" / "heldout.md"
    assert export_label_file(conn, [pid], path) == 1
    text = path.read_text(encoding="utf-8")
    assert f"## {pid} · Acme — Software Engineer, New Grad" in text
    assert "Requires 2+ years." in text  # never truncated
    assert "ATS location: Montreal, QC" in text
    assert label_file_ids(path) == [pid]


def test_import_saves_filled_skips_blank_and_reports_the_rest(conn, tmp_path):
    done = add_posting(conn, title="A")
    later = add_posting(conn, title="B", company="Other")
    half = add_posting(conn, title="C", company="Third")
    bad = add_posting(conn, title="D", company="Fourth")
    path = tmp_path / "h.md"
    export_label_file(conn, [done, later, half, bad], path)
    _fill(path, done, "city: Toronto\nposting_language: e\nmin_years: -\nseniority: newgrad")
    _fill(path, half, "city: Remote\nposting_language: en\nmin_years: \nseniority: j")
    _fill(path, bad, "city: -\nposting_language: english\nmin_years: 2\nseniority: g")

    res = import_label_file(conn, path)
    assert res.saved == [done] and res.blank == [later]
    assert res.incomplete == {half: ["min_years"]}
    assert len(res.errors) == 1 and f"{bad} posting_language" in res.errors[0]
    assert _gold(conn, done) == {
        "city": "Toronto",
        "posting_language": "en",
        "min_years": None,
        "seniority": "newgrad",
    }
    assert _gold(conn, later) == {} and _gold(conn, half) == {}

    # re-importing after more work replaces, never duplicates
    text = path.read_text(encoding="utf-8").replace("city: Toronto", "city: Montreal  # fixed")
    path.write_text(text, encoding="utf-8")
    assert import_label_file(conn, path).saved == [done]
    assert _gold(conn, done)["city"] == "Montreal"
    assert (
        conn.execute("SELECT COUNT(*) FROM labels WHERE posting_id=?", (done,)).fetchone()[0] == 4
    )


def test_export_never_overwrites_and_writes_only_markdown(conn, tmp_path):
    import pytest

    from prospect.eval.labelfile import LabelFileError

    pid = add_posting(conn)
    with pytest.raises(LabelFileError, match=r"must be a .md file"):
        export_label_file(conn, [pid], tmp_path / "factbank.yaml")
    path = tmp_path / "h.md"
    export_label_file(conn, [pid], path)
    with pytest.raises(LabelFileError, match="exists"):
        export_label_file(conn, [pid], path)


def test_import_survives_a_markdown_formatter(conn, tmp_path):
    """First real use (2026-10-06): VS Code's formatter turned "```label 41" into "```",
    "-" bullets into "*", and added blank lines. The section heading still names the id."""
    a = add_posting(conn, title="A")
    b = add_posting(conn, title="B", company="Other")
    path = tmp_path / "h.md"
    path.write_text(
        f"# Held-out\n\n* `city`: ...\n\n## {a} · Acme — A\n\n* ATS location: X\n\n> text\n\n"
        "```\ncity: Montreal\nposting_language: bilingual\nmin_years: 3\nseniority: mid\n\n```\n\n"
        f"## {b} · Other — B\n\n> text\n\n```\ncity: \nposting_language: \nmin_years: \n"
        "seniority: \n```\n",
        encoding="utf-8",
    )
    res = import_label_file(conn, path)
    assert res.saved == [a] and res.blank == [b]
    assert _gold(conn, a)["posting_language"] == "bilingual"
    assert label_file_ids(path) == [a, b]
