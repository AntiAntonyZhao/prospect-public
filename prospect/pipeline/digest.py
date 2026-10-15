"""digest: today's ranked shortlist with gap reports (reads v_shortlist + requirements)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from prospect.config import Settings


def _age_days(first_seen: str | None, posted_at: str | None) -> str:
    stamp = posted_at or first_seen
    if not stamp:
        return "?"
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return "?"
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return f"{max(0, (datetime.now(UTC) - t).days)}d"


def _gaps(conn: sqlite3.Connection, posting_id: int) -> str:
    rows = conn.execute(
        """SELECT skill_norm, tier, est_hours, must_have FROM requirements
           WHERE posting_id = ? AND tier >= 2 ORDER BY tier DESC, must_have DESC""",
        (posting_id,),
    ).fetchall()
    parts = []
    for r in rows:
        hours = f" ({r['est_hours']:g}h)" if r["est_hours"] is not None and r["tier"] == 2 else ""
        star = "" if r["must_have"] else "?"
        parts.append(f"T{r['tier']} {r['skill_norm']}{hours}{star}")
    elig = conn.execute(
        "SELECT skill_norm FROM requirements WHERE posting_id = ? AND kind = 'eligibility'",
        (posting_id,),
    ).fetchall()
    if elig:  # not gaps: conditions on the candidate's situation, shown so you can judge
        parts.append("elig: " + ", ".join(r["skill_norm"] for r in elig))
    return "; ".join(parts) or "—"


def _terms(conn: sqlite3.Connection, posting_id: int) -> str:
    """'[part-time 16h/wk −17, term 4mo −20] ' — employment-term costs from the breakdown."""
    row = conn.execute(
        "SELECT breakdown_json FROM scores WHERE posting_id = ?", (posting_id,)
    ).fetchone()
    try:
        bd = json.loads(row["breakdown_json"]) if row else {}
    except ValueError:
        return ""
    parts = []
    if bd.get("part_time"):
        parts.append(f"part-time {bd.get('hours_per_week', 0):g}h/wk {bd['part_time']:g}")
    if bd.get("short_term"):
        parts.append(f"term {bd.get('term_months', 0):g}mo {bd['short_term']:g}")
    return f"[{', '.join(parts)}] " if parts else ""


_WARN_PREFIXES = ("low_confidence:", "weak_evidence:", "optional:", "ghost")


def _warnings(flags_json: str | None) -> list[str]:
    """Flags worth a ⚠ on a scored row: a reject rule fired but was not trusted, or ghost."""
    try:
        flags = json.loads(flags_json or "[]")
    except ValueError:
        return []
    return [f for f in flags if f.startswith(_WARN_PREFIXES)]


def _short(s: str | None, n: int) -> str:
    s = s or "—"
    return s if len(s) <= n else s[: n - 1] + "…"


FR_SHORT = {"none": "—", "asset": "asset", "required_pro_forma": "pro_forma"}


def format_digest(
    settings: Settings, conn: sqlite3.Connection, limit: int = 25, min_score: float = 0
) -> str:
    del settings
    rows = conn.execute(
        """SELECT v.*, p.posted_at, p.flags FROM v_shortlist v JOIN postings p ON p.id = v.id
           WHERE v.score >= ? LIMIT ?""",
        (min_score, limit),
    ).fetchall()
    out = []
    head = (
        f"{'id':>5} {'score':>5}  {'company':<16} {'title':<32} {'loc':<10} {'arr':<7} "
        f"{'fr':<9} {'age':>4}  gaps"
    )
    if rows:
        out.append(head)
        for r in rows:
            warn = _warnings(r["flags"])
            out.append(
                f"{r['id']:>5} {r['score']:>5.0f}  {_short(r['company'], 16):<16} "
                f"{_short(r['title'], 32):<32} {_short(r['city'], 10):<10} "
                f"{_short(r['work_arrangement'], 7):<7} "
                f"{FR_SHORT.get(r['french_requirement'] or 'none', r['french_requirement']):<9} "
                f"{_age_days(r['first_seen'], r['posted_at']):>4}  "
                + (f"⚠ {', '.join(warn)} — " if warn else "")
                + _terms(conn, r["id"])
                + _gaps(conn, r["id"])
            )
    else:
        out.append("No scored candidates yet (run `prospect extract` and `prospect score`).")

    ghosts = conn.execute(
        """SELECT p.repost_count FROM postings p WHERE p.status = 'candidate'
           AND p.flags LIKE '%"ghost"%'"""
    ).fetchall()
    if ghosts:
        worst = max(g["repost_count"] for g in ghosts)
        out.append(
            f"⚠ {len(ghosts)} posting(s) flagged ghost (reposted up to ×{worst}, no reply to a "
            "prior application for the same role)"
        )
    low = conn.execute(
        """SELECT COUNT(*) AS n FROM postings WHERE status='candidate'
           AND (flags LIKE '%low_confidence:%' OR flags LIKE '%weak_evidence:%')"""
    ).fetchone()["n"]
    if low:
        out.append(
            f"⚠ {low} candidate(s) kept although a reject rule fired, on weak evidence or low "
            "extraction confidence (marked ⚠ above when scored) — check before applying"
        )

    unscored = conn.execute(
        """SELECT p.id, c.name AS company, p.title, p.location_raw, e.cosine, p.flags
           FROM postings p JOIN companies c ON c.id = p.company_id
           LEFT JOIN embeddings e ON e.posting_id = p.id
           LEFT JOIN scores s ON s.posting_id = p.id
           WHERE p.status = 'candidate' AND s.posting_id IS NULL
           ORDER BY e.cosine IS NULL, e.cosine DESC, p.first_seen DESC"""
    ).fetchall()
    if unscored:
        out.append("")
        out.append(
            f"Unscored candidates: {len(unscored)} (top {min(limit, len(unscored))} by cosine)"
        )
        for r in unscored[:limit]:
            cos = f"{r['cosine']:.3f}" if r["cosine"] is not None else "  -  "
            flags = [f for f in json.loads(r["flags"] or "[]") if f != "french_regex"]
            fr = " FR?" if "french_regex" in (r["flags"] or "") else ""
            extra = f"  [{', '.join(flags)}]" if flags else ""
            out.append(
                f"{r['id']:>5} {cos}  {_short(r['company'], 16):<16} {_short(r['title'], 40):<40} "
                f"{_short(r['location_raw'], 28)}{fr}{extra}"
            )
    return "\n".join(out)
