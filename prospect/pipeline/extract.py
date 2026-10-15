"""extract: Haiku → posting_facts + requirements (CLAUDE.md "Pipeline"). Idempotent.

Works on the rank shortlist (top-N candidates by cosine) that has no facts yet.
Batch mode (default) is asynchronous: `--submit` sends, `--collect` stores
whatever finished; a plain `prospect extract` does both, waiting up to
`extract.batch_wait_minutes`.

`posting_language` is deterministic (stopword detection), never the model's.
Evidence quotes must be verbatim in the text the model saw *and* show the value
they are quoted for (pipeline/guards.py); anything else is dropped rather than
stored. Requirements pass the same guards: upper-bound years cleared, experience
caps reclassified as eligibility, "preferred" / "a plus" made optional, and a
years number kept only when the requirement's text states it verbatim.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from prospect.config import Settings
from prospect.db import transaction
from prospect.llm.client import (
    BatchItem,
    BudgetExceeded,
    LLMClient,
    LLMError,
    ToolSpec,
)
from prospect.llm.prompts import Prompt, load_prompt
from prospect.logs import get_logger
from prospect.models.posting import ExtractionOutput, PostingFacts, RawPosting
from prospect.pipeline.filter import CANADA_RE, metadata_remote_entry
from prospect.pipeline.guards import UPPER_BOUND_RE, clean_requirements, evidence_supports
from prospect.pipeline.rank import shortlist_ids
from prospect.progress import Progress, report
from prospect.text import ascii_fold, collapse_ws, detect_language, html_to_text

log = get_logger("extract")

TOOL = ToolSpec(
    name="record_posting",
    description="Record the structured facts and the requirement list of one job posting.",
    model=ExtractionOutput,
)
MIN_DESC_CHARS = 200
_CITY_FIX = {
    "quebec": "Quebec City",
    "ville de quebec": "Quebec City",
    "quebec city": "Quebec City",
}


METADATA_PREFIX = "ATS location metadata: "
_METADATA_LINE = re.compile("^" + re.escape(METADATA_PREFIX) + "(.*)$", re.M)


def posting_user_content(row: sqlite3.Row, company: str, max_chars: int) -> tuple[str, str]:
    """(user message, plain body text)."""
    raw = RawPosting.model_validate_json(row["raw_json"])
    body = html_to_text(raw.description_html)
    truncated = len(body) > max_chars
    body = body[:max_chars]
    msg = (
        f"Company: {company}\nTitle: {row['title']}\n"
        f"{METADATA_PREFIX}{row['location_raw'] or '(none)'}\n"
        f"Posted: {row['posted_at'] or 'unknown'}\n\n--- Posting ---\n{body}"
        + ("\n[posting truncated]" if truncated else "")
    )
    return msg, body


def _norm_for_quote(s: str) -> str:
    return collapse_ws(ascii_fold(s).lower().replace("’", "'").replace("•", " "))


def verified_evidence(out: ExtractionOutput, seen_text: str) -> tuple[dict[str, str], int]:
    """Quotes that are verbatim in the posting and visibly state the field's value."""
    hay = _norm_for_quote(seen_text)
    kept: dict[str, str] = {}
    dropped = 0
    for ev in out.facts.evidence:
        q = ev.quote.strip().strip('"').strip()
        value = getattr(out.facts, ev.field, None)
        if (
            q
            and _norm_for_quote(q) in hay
            and value is not None
            and evidence_supports(ev.field, value, q)
        ):
            kept.setdefault(ev.field, q)
        else:
            dropped += 1
    return kept, dropped


_WORD_NUM = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def years_stated_verbatim(text: str, years: int, seen_text: str) -> bool:
    """A requirement's years count only when its text is in the posting and shows that number.

    The full filter pass rejects on a must-have requirement's years (policy 2026-10-06), so
    they need the same footing as an evidence quote: verbatim, and visibly the number.
    """
    q = text.strip()
    if not q or _norm_for_quote(q) not in _norm_for_quote(seen_text):
        return False
    if UPPER_BOUND_RE.search(q) or not evidence_supports("min_years", years, q):
        return False
    number = rf"(?<![\d.]){years}(?!\d)"
    if years in _WORD_NUM:
        number += rf"|\b{_WORD_NUM[years]}\b"
    return re.search(number, q, re.I) is not None


def _min_years(out: ExtractionOutput) -> int | None:
    """Never store an upper bound ('less than 2 years') as a minimum."""
    quotes = [ev.quote for ev in out.facts.evidence if ev.field == "min_years"]
    if any(UPPER_BOUND_RE.search(q) for q in quotes):
        return None
    return out.facts.min_years


_UNIT_MONTHS = {"weeks": 12 / 52, "months": 1.0, "years": 12.0}


def term_months(length: float | None, unit: str | None) -> float | None:
    """'16 weeks' → 3.7, '12 months' → 12, '1 years' → 12; None when either is missing."""
    if length is None or unit not in _UNIT_MONTHS or length <= 0:
        return None
    return round(length * _UNIT_MONTHS[unit], 1)


def employment_terms(out: ExtractionOutput, evidence: dict[str, str]) -> dict[str, object]:
    """Part-time / contract facts, each only when a verified quote backs it.

    They only ever cost points (policy 2026-10-06), so an unbacked value is dropped
    rather than flagged: no evidence, no penalty.
    """
    f = out.facts
    hours = f.hours_per_week if "hours_per_week" in evidence else None
    months = term_months(f.term_length, f.term_unit) if "term_length" in evidence else None
    return {
        "part_time": bool(f.part_time and ({"part_time", "hours_per_week"} & evidence.keys())),
        "hours_per_week": hours if hours and hours > 0 else None,
        "contract": bool(f.contract and ({"contract", "term_length"} & evidence.keys())),
        "term_months": months,
    }


def to_facts(out: ExtractionOutput, body: str, seen_text: str) -> tuple[PostingFacts, int]:
    f = out.facts
    city = f.city.strip() if f.city else None
    if city:
        city = _CITY_FIX.get(ascii_fold(city).lower(), ascii_fold(city))
    evidence, dropped = verified_evidence(out, seen_text)
    arrangement, onsite_days, remote_scope = f.work_arrangement, f.onsite_days, f.remote_scope
    meta = _METADATA_LINE.search(seen_text)
    remote_entry = metadata_remote_entry(meta.group(1)) if meta else None
    if remote_entry and arrangement != "remote":
        # A remote option in the ATS location list wins over an office policy (policy 2026-10).
        arrangement, onsite_days = "remote", None
        evidence["work_arrangement"] = remote_entry
        if CANADA_RE.search(remote_entry) and remote_scope not in ("canada", "global"):
            remote_scope = "canada"
    if arrangement == "remote":
        # A remote role has no city (the label convention: city "Remote"). The model sometimes
        # keeps one from the office list (live run 2026-10-06: "Montreal (Remote)", "Ontario -
        # Remote" → Toronto). Province and country stay: they carry the remote scope and the
        # outside-Canada rule.
        city = None
        evidence.pop("city", None)
    facts = PostingFacts(
        city=city,
        province=(f.province or "").strip().upper() or None,
        country=(f.country or "").strip().upper() or None,
        work_arrangement=arrangement,
        onsite_days=onsite_days,
        remote_scope=remote_scope,
        posting_language=detect_language(body),  # deterministic, not the model's call
        french_requirement=f.french_requirement,
        min_years=_min_years(out),
        seniority=f.seniority,
        clearance=f.clearance,
        peng=f.peng,
        work_auth=f.work_auth,
        **employment_terms(out, evidence),
        confidence=max(0.0, min(1.0, f.confidence)),
        evidence_span=evidence,
    )
    return facts, dropped


def store_extraction(
    conn: sqlite3.Connection,
    posting_id: int,
    out: ExtractionOutput,
    body: str,
    seen_text: str,
    model: str,
    prompt_version: str,
) -> PostingFacts:
    facts, dropped = to_facts(out, body, seen_text)
    with transaction(conn):
        conn.execute(
            """INSERT INTO posting_facts (posting_id, city, province, country, work_arrangement,
                   onsite_days, remote_scope, posting_language, french_requirement, min_years,
                   seniority, clearance, peng, work_auth, part_time, hours_per_week, contract,
                   term_months, confidence, evidence_json, model, prompt_version, extracted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
               ON CONFLICT(posting_id) DO UPDATE SET city=excluded.city,
                 province=excluded.province, country=excluded.country,
                 work_arrangement=excluded.work_arrangement, onsite_days=excluded.onsite_days,
                 remote_scope=excluded.remote_scope, posting_language=excluded.posting_language,
                 french_requirement=excluded.french_requirement, min_years=excluded.min_years,
                 seniority=excluded.seniority, clearance=excluded.clearance, peng=excluded.peng,
                 work_auth=excluded.work_auth, part_time=excluded.part_time,
                 hours_per_week=excluded.hours_per_week, contract=excluded.contract,
                 term_months=excluded.term_months, confidence=excluded.confidence,
                 evidence_json=excluded.evidence_json, model=excluded.model,
                 prompt_version=excluded.prompt_version, extracted_at=excluded.extracted_at""",
            (
                posting_id,
                facts.city,
                facts.province,
                facts.country,
                facts.work_arrangement,
                facts.onsite_days,
                facts.remote_scope,
                facts.posting_language,
                facts.french_requirement,
                facts.min_years,
                facts.seniority,
                int(facts.clearance),
                int(facts.peng),
                facts.work_auth,
                int(facts.part_time),
                facts.hours_per_week,
                int(facts.contract),
                facts.term_months,
                facts.confidence,
                json.dumps(facts.evidence_span, ensure_ascii=False),
                model,
                prompt_version,
            ),
        )
        # Requirements are replaced wholesale; any score built on the old ones is stale.
        conn.execute("DELETE FROM requirements WHERE posting_id = ?", (posting_id,))
        conn.execute("DELETE FROM scores WHERE posting_id = ?", (posting_id,))
        cleaned = clean_requirements(out.requirements)
        unverified = 0
        for r in cleaned.kept:
            if r.years is not None and not years_stated_verbatim(r.text, r.years, seen_text):
                r = r.model_copy(update={"years": None})
                unverified += 1
            conn.execute(
                """INSERT INTO requirements (posting_id, text, skill_norm, must_have, years,
                       context, kind, model, prompt_version) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    posting_id,
                    r.text,
                    r.skill_norm.strip(),
                    int(r.must_have),
                    r.years,
                    r.context,
                    r.kind,
                    model,
                    prompt_version,
                ),
            )
    log.info(
        "extracted",
        id=posting_id,
        french=facts.french_requirement,
        arrangement=facts.work_arrangement,
        city=facts.city,
        reqs=len(cleaned.kept),
        made_optional=cleaned.made_optional,
        years_cleared=cleaned.years_cleared,
        years_unverified=unverified,
        conf=facts.confidence,
        evidence_dropped=dropped,
    )
    return facts


def _add_flag(conn: sqlite3.Connection, posting_id: int, flag: str) -> None:
    row = conn.execute("SELECT flags FROM postings WHERE id=?", (posting_id,)).fetchone()
    flags = set(json.loads(row["flags"] or "[]")) | {flag}
    conn.execute("UPDATE postings SET flags=? WHERE id=?", (json.dumps(sorted(flags)), posting_id))


def select_postings(
    settings: Settings,
    conn: sqlite3.Connection,
    limit: int | None,
    posting_ids: list[int] | None,
    force: bool,
) -> list[sqlite3.Row]:
    ids = posting_ids or shortlist_ids(conn, settings.rank.top_n)
    if not ids:
        return []
    pending: set[int] = set()
    in_flight = "SELECT items_json FROM batches WHERE status='submitted' AND stage='extract'"
    for b in conn.execute(in_flight):
        pending |= {v for v in json.loads(b["items_json"]).values() if v is not None}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT p.*, c.name AS company, f.posting_id AS has_facts
            FROM postings p JOIN companies c ON c.id = p.company_id
            LEFT JOIN posting_facts f ON f.posting_id = p.id WHERE p.id IN ({marks})""",
        ids,
    ).fetchall()
    order = {pid: i for i, pid in enumerate(ids)}
    rows = sorted(rows, key=lambda r: order[r["id"]])
    out = []
    with transaction(conn):
        for r in rows:
            if (r["has_facts"] and not force) or r["id"] in pending:
                continue
            body = html_to_text(RawPosting.model_validate_json(r["raw_json"]).description_html)
            if len(body) < MIN_DESC_CHARS:
                _add_flag(conn, r["id"], "no_description")
                continue
            out.append(r)
    return out[:limit] if limit else out


def _prep(rows: list[sqlite3.Row], settings: Settings) -> dict[int, tuple[str, str]]:
    return {
        r["id"]: posting_user_content(r, r["company"], settings.extract.max_desc_chars)
        for r in rows
    }


def collect_batches(client: LLMClient, conn: sqlite3.Connection, settings: Settings) -> list[str]:
    lines = []
    for b in client.pending_batches("extract"):
        status = client.batch_status(b["batch_id"])
        if status != "ended":
            lines.append(f"batch {b['batch_id']}: {status}")
            continue
        results = client.batch_collect(b["batch_id"], TOOL)
        ids = [r.posting_id for r in results if r.posting_id is not None]
        rows = (
            {
                r["id"]: r
                for r in conn.execute(
                    f"""SELECT p.*, c.name AS company FROM postings p
                    JOIN companies c ON c.id = p.company_id
                    WHERE p.id IN ({",".join("?" * len(ids))})""",
                    ids,
                )
            }
            if ids
            else {}
        )
        ok = 0
        for res in results:
            row = rows.get(res.posting_id or -1)
            if res.output is None or row is None:
                log.warning("batch_item_failed", id=res.posting_id, error=res.error)
                continue
            seen, body = posting_user_content(row, row["company"], settings.extract.max_desc_chars)
            store_extraction(
                conn, row["id"], res.output, body, seen, b["model"], b["prompt_version"]
            )
            ok += 1
        cost = sum(r.cost_usd for r in results)
        lines.append(f"batch {b['batch_id']}: collected {ok}/{len(results)} (${cost:.4f})")
    return lines


def run_extract(
    settings: Settings,
    conn: sqlite3.Connection,
    mode: str = "batch",
    submit_only: bool = False,
    collect_only: bool = False,
    limit: int | None = None,
    posting_ids: list[int] | None = None,
    force: bool = False,
    client: LLMClient | None = None,
    prompt: Prompt | None = None,
    wait: bool = True,
    progress: Progress | None = None,
) -> str:
    client = client or LLMClient(settings, conn)
    prompt = prompt or load_prompt("extract")
    lines: list[str] = []
    if mode == "batch" and not submit_only:
        lines += collect_batches(client, conn, settings)
    if collect_only:
        return "\n".join(lines) or "no pending batches"

    rows = select_postings(settings, conn, limit, posting_ids, force)
    if not rows:
        lines.append("extract: nothing to do (shortlist fully extracted)")
        return "\n".join(lines)
    content = _prep(rows, settings)

    if mode == "sync":
        done = failed = 0
        for i, r in enumerate(rows):
            report(progress, i / len(rows), f"extract {i + 1}/{len(rows)}: {r['company']}")
            seen, body = content[r["id"]]
            try:
                res = client.structured(
                    stage="extract",
                    model="haiku",
                    prompt=prompt,
                    user_content=seen,
                    tool=TOOL,
                    max_tokens=settings.extract.max_tokens,
                    posting_id=r["id"],
                )
            except BudgetExceeded as e:
                lines.append(f"stopped: {e}")
                break
            except LLMError as e:
                failed += 1
                log.warning("extract_failed", id=r["id"], error=str(e)[:200])
                continue
            store_extraction(conn, r["id"], res.output, body, seen, res.model, prompt.version)
            done += 1
        lines.append(f"extract (sync): {done} extracted, {failed} failed, {len(rows)} selected")
        return "\n".join(lines)

    items = [BatchItem(f"posting-{r['id']}", content[r["id"]][0], r["id"]) for r in rows]
    batch_id, chosen = client.batch_submit(
        stage="extract",
        model="haiku",
        prompt=prompt,
        items=items,
        tool=TOOL,
        max_tokens=settings.extract.max_tokens,
    )
    trimmed = f" (budget trimmed {len(items) - len(chosen)})" if len(chosen) < len(items) else ""
    lines.append(f"extract: submitted batch {batch_id} with {len(chosen)} postings{trimmed}")
    if submit_only or not wait:
        return "\n".join(lines)
    started = time.monotonic()
    deadline = started + settings.extract.batch_wait_minutes * 60
    while time.monotonic() < deadline:
        status = client.batch_status(batch_id)
        if status == "ended":
            break
        waited = (time.monotonic() - started) / 60
        report(progress, None, f"batch {batch_id}: {status}, {waited:.0f} min so far")
        time.sleep(settings.extract.poll_seconds)
    report(progress, 0.95, f"collecting batch {batch_id}")
    lines += collect_batches(client, conn, settings)
    return "\n".join(lines)


_SPLIT_ALTERNATIVES = re.compile(r"\s*\|\s*")


def alternatives(skill_norm: str) -> list[str]:
    """'Python | Java | Go' → ['Python', 'Java', 'Go'] (any one suffices)."""
    return [p for p in _SPLIT_ALTERNATIVES.split(skill_norm) if p]
