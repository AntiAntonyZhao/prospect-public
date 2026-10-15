"""`prospect eval label`: show a posting, enter gold values for the labeled fields.

No model prediction is shown while labeling (no anchoring). Postings the French
regex flagged come first: the pro_forma / working boundary is the field that
matters most and the one the model gets wrong most (CLAUDE.md "Eval").
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

from prospect.db import transaction
from prospect.models.posting import LABELED_FIELDS, RawPosting
from prospect.text import html_to_text

CHOICES: dict[str, dict[str, str]] = {
    "work_arrangement": {"o": "onsite", "h": "hybrid", "r": "remote", "u": "unknown"},
    "posting_language": {"e": "en", "f": "fr", "b": "bilingual"},
    "french_requirement": {
        "n": "none",
        "a": "asset",
        "p": "required_pro_forma",
        "w": "required_working",
        "f": "posting_in_french",
    },
    "seniority": {
        "i": "intern",
        "g": "newgrad",
        "j": "junior",
        "m": "mid",
        "s": "senior",
        "u": "unknown",
    },
}
FREE = {
    "city": "English, no accents (Montreal); 'Remote' if fully remote; '-' = not stated",
    "province": "2-letter (QC, ON, NY); '-' = none",
    "min_years": "integer; '-' = not stated",
}
SHOW_CHARS = 3500
# ASCII or full-width commas (a Chinese keyboard types "，"), semicolons, spaces.
_ID_SEP = re.compile(r"[\s,，、;；]+")


class Quit(Exception):
    pass


def _parse(field: str, raw: str) -> str | None:
    raw = raw.strip()
    if raw in ("-", ""):
        return None
    if field in CHOICES:
        opts = CHOICES[field]
        if raw in opts:
            return opts[raw]
        if raw in opts.values():
            return raw
        raise ValueError(f"one of {', '.join(f'{k}={v}' for k, v in opts.items())}")
    if field == "province":
        if len(raw) != 2:
            raise ValueError("two letters")
        return raw.upper()
    if field == "min_years":
        return str(int(raw))
    return raw


def parse_ids(text: str) -> list[int]:
    """'8396,10098', '8396，10098' or '8396 10098' -> [8396, 10098]."""
    ids = []
    for tok in _ID_SEP.split(text.strip()):
        if not tok:
            continue
        if not tok.isdigit():
            raise ValueError(f"not a posting id: {tok!r}")
        ids.append(int(tok))
    return ids


def save_labels(conn: sqlite3.Connection, posting_id: int, values: dict[str, str | None]) -> None:
    """Upsert gold values: relabeling replaces, it never duplicates."""
    with transaction(conn):
        for field, value in values.items():
            conn.execute(
                """INSERT INTO labels (posting_id, field, gold_value) VALUES (?,?,?)
                   ON CONFLICT(posting_id, field) DO UPDATE SET
                     gold_value=excluded.gold_value, labeled_at=datetime('now')""",
                (posting_id, field, value),
            )


def _prompt_text(field: str) -> str:
    if field in CHOICES:
        return f"{field} [{' '.join(f'{k}={v}' for k, v in CHOICES[field].items())}]"
    return f"{field} ({FREE[field]})"


def _in_order(conn: sqlite3.Connection, ids: list[int], unlabeled_only: bool) -> list:
    rows = conn.execute(
        f"""SELECT p.*, c.name AS company FROM postings p JOIN companies c ON c.id=p.company_id
            WHERE p.id IN ({",".join("?" * len(ids))})"""
        + (" AND p.id NOT IN (SELECT posting_id FROM labels)" if unlabeled_only else ""),
        ids,
    ).fetchall()
    order = {pid: i for i, pid in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r["id"]])


def queue(
    conn: sqlite3.Connection,
    limit: int,
    relabel: list[int] | None = None,
    ids: list[int] | None = None,
) -> list[sqlite3.Row]:
    if relabel:  # these exact postings, labeled or not, in order
        return _in_order(conn, relabel, unlabeled_only=False)
    if ids:  # an explicit list (e.g. from `eval sample`), in order, skipping labeled ones
        return _in_order(conn, ids, unlabeled_only=True)
    return conn.execute(
        """SELECT p.*, c.name AS company FROM postings p JOIN companies c ON c.id = p.company_id
           WHERE p.id NOT IN (SELECT posting_id FROM labels)
             AND p.source != 'manual' AND length(p.raw_json) > 1500
             AND p.status IN ('candidate', 'shortlisted', 'applied', 'rejected')
             AND (p.status != 'rejected' OR p.reject_reason NOT IN ('senior_title',
                  'location_metadata', 'duplicate_application'))
           ORDER BY p.flags LIKE '%french_regex%' DESC, random() LIMIT ?""",
        (limit,),
    ).fetchall()


def label_session(
    conn: sqlite3.Connection,
    limit: int = 20,
    relabel: list[int] | None = None,
    ids: list[int] | None = None,
    ask: Callable[[str], str] | None = None,
    echo: Callable[[str], None] | None = None,
) -> str:
    import typer

    ask = ask or (lambda q: typer.prompt(q, default="", show_default=False))
    echo = echo or typer.echo
    rows = queue(conn, limit, relabel, ids)
    done = 0
    try:
        for row in rows:
            raw = RawPosting.model_validate_json(row["raw_json"])
            text = html_to_text(raw.description_html)
            echo("=" * 78)
            echo(f"#{row['id']}  {row['company']} — {row['title']}")
            echo(f"ATS location: {row['location_raw']}")
            if relabel:
                old = dict(
                    conn.execute(
                        "SELECT field, gold_value FROM labels WHERE posting_id=?", (row["id"],)
                    ).fetchall()
                )
                if old:
                    shown = ", ".join(f"{f}={old.get(f) or '-'}" for f in LABELED_FIELDS)
                    echo(f"current labels (to be replaced): {shown}")
            echo("-" * 78)
            echo(text[:SHOW_CHARS] + ("\n[…truncated]" if len(text) > SHOW_CHARS else ""))
            echo("-" * 78)
            echo(
                "Enter a value per field; '-' or Enter = none/not stated; 's' skips the posting; "
                "'q' quits."
            )
            values: dict[str, str | None] = {}
            skipped = False
            for field in LABELED_FIELDS:
                while True:
                    answer = ask(_prompt_text(field))
                    if answer.strip() == "q":
                        raise Quit
                    if answer.strip() == "s":
                        skipped = True
                        break
                    try:
                        values[field] = _parse(field, answer)
                        break
                    except ValueError as e:
                        echo(f"  expected {e}")
                if skipped:
                    break
            if skipped:
                continue
            save_labels(conn, row["id"], values)
            done += 1
    except Quit:
        pass
    total = conn.execute("SELECT COUNT(DISTINCT posting_id) AS n FROM labels").fetchone()["n"]
    return f"labeled {done} posting(s) this session; {total} labeled in total (target 200)"
