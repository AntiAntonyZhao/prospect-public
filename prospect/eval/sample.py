"""`prospect eval sample`: pick unlabeled candidates that differ as much as possible.

Greedy and deterministic: one posting per company, each pick chosen to add the
most not-yet-covered values across seniority, role family, location and the
French flag, with the French-flagged share topped up first (the pro_forma /
working boundary is the field that matters most). Ties go to higher cosine.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from functools import partial

from prospect.models.posting import RawPosting
from prospect.pipeline.filter import CANADA_RE
from prospect.text import html_to_text

SENIORITY = [
    ("intern", r"\bintern(?:ship)?s?\b|co-?op|\bstage\b"),
    ("newgrad", r"new grad|graduate|entry|early career|university|campus"),
    ("junior", r"junior|\bjr\b|associate|\bi\b"),
    ("mid", r"\bii\b|\biii\b|intermediate"),
]
FAMILY = [
    ("quant", r"quant|trading|trader|low[- ]latency"),
    ("ml", r"machine learning|\bml\b|\bai\b|data scien|\bllm|nlp|research"),
    ("data", r"\bdata\b|analytics"),
    ("mobile", r"ios|android|mobile"),
    ("fullstack", r"full[- ]?stack"),
    ("frontend", r"front[- ]?end|\bui\b|\bweb\b"),
    ("infra", r"infra|\bsre\b|devops|cloud|reliability|platform"),
    ("security", r"security"),
    ("qa", r"\bqa\b|\btest|quality"),
    ("systems", r"embedded|firmware|hardware|systems|kernel"),
    ("backend", r"back[- ]?end|\bapi\b|server"),
]


@dataclass
class Pick:
    id: int
    company: str
    title: str
    location: str
    seniority: str
    family: str
    place: str
    french: bool
    cosine: float


def _bucket(title: str, table: list[tuple[str, str]], default: str) -> str:
    t = title.lower()
    return next((name for name, pat in table if re.search(pat, t)), default)


def _place(location: str | None) -> str:
    loc = (location or "").lower()
    if "montr" in loc:
        return "montreal"
    if "toronto" in loc:
        return "toronto"
    if "remote" in loc:
        return "remote"
    return "other-canada" if CANADA_RE.search(loc) else "unclear"


def candidates(conn: sqlite3.Connection) -> list[Pick]:
    rows = conn.execute(
        """SELECT p.id, p.title, p.location_raw, p.flags, p.raw_json, c.name AS company,
                  COALESCE(e.cosine, 0) AS cosine
           FROM postings p JOIN companies c ON c.id = p.company_id
           LEFT JOIN embeddings e ON e.posting_id = p.id
           WHERE p.status = 'candidate'
             AND p.id NOT IN (SELECT posting_id FROM labels)"""
    ).fetchall()
    out = []
    for r in rows:
        body = html_to_text(RawPosting.model_validate_json(r["raw_json"]).description_html)
        if len(body) < 200:
            continue  # nothing to label against
        out.append(
            Pick(
                id=r["id"],
                company=r["company"],
                title=r["title"],
                location=r["location_raw"] or "",
                cosine=r["cosine"],
                seniority=_bucket(r["title"], SENIORITY, "unspecified"),
                family=_bucket(r["title"], FAMILY, "general"),
                place=_place(r["location_raw"]),
                french="french_regex" in (r["flags"] or ""),
            )
        )
    return out


def _gain(p: Pick, seen: frozenset[tuple[str, str]], need_french: bool) -> tuple[int, int]:
    """(fills the French quota, number of not-yet-covered bucket values)."""
    new = {("s", p.seniority), ("f", p.family), ("p", p.place)} - seen
    return (int(need_french and p.french), len(new))


def diverse_sample(
    conn: sqlite3.Connection,
    n: int = 25,
    french_share: float = 0.3,
    exclude: frozenset[int] = frozenset(),
) -> list[Pick]:
    """`exclude`: postings already in a set waiting to be labeled (the UI's label sets)."""
    pool = sorted(
        (p for p in candidates(conn) if p.id not in exclude), key=lambda p: (-p.cosine, p.id)
    )
    chosen: list[Pick] = []
    seen: set[tuple[str, str]] = set()
    companies: set[str] = set()
    french_target = round(n * french_share)
    while len(chosen) < n:
        open_ = [p for p in pool if p.company not in companies]
        if not open_:
            break
        need_french = sum(p.french for p in chosen) < french_target
        # pool is cosine-sorted and max() keeps the first maximum, so ties keep higher cosine
        best = max(open_, key=partial(_gain, seen=frozenset(seen), need_french=need_french))
        chosen.append(best)
        companies.add(best.company)
        seen |= {("s", best.seniority), ("f", best.family), ("p", best.place)}
    return chosen


def format_sample(picks: list[Pick]) -> str:
    lines = [
        f"{'id':>6}  {'company':<20} {'title':<44} {'seniority':<11} {'family':<10} "
        f"{'place':<12} fr"
    ]
    for p in picks:
        lines.append(
            f"{p.id:>6}  {p.company[:20]:<20} {p.title[:44]:<44} {p.seniority:<11} "
            f"{p.family:<10} {p.place:<12} {'FR' if p.french else ''}"
        )
    cover = lambda attr: len({getattr(p, attr) for p in picks})  # noqa: E731
    lines.append(
        f"{len(picks)} postings, {len({p.company for p in picks})} companies; "
        f"{cover('seniority')} seniority buckets, {cover('family')} role families, "
        f"{cover('place')} places, {sum(p.french for p in picks)} French-flagged"
    )
    lines.append(
        "label them:  uv run prospect eval label --ids " + ",".join(str(p.id) for p in picks)
    )
    return "\n".join(lines)
