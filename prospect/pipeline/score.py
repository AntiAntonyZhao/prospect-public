"""score: tier each requirement (rules first, Sonnet for the rest) + deterministic score.

Hard rule 4: LLMs produce labels; code produces scores. `score_posting` is a
pure function of requirement tiers and posting facts; weights live in
prospect.toml [score] and `weights_version` hashes them.

Tiering order for one requirement (CLAUDE.md "Gap classification"):
  1. human language      → identity.languages (never Tier 2)
  2. years > fact bank    → Tier 3
  3. cert / clearance / licence / graduate degree → Tier 3; bachelor's → Tier 0 (education)
  4. exact skill match    → Tier 0 (strong, or weak when level = familiar)
  5. alias match          → Tier 1 with the skill's evidence chain
  6. adjacent match       → Tier 2 (model supplies est_hours)
  7. everything else      → Sonnet, then `enforce_tier_rules` re-checks its label;
                            a requirement Sonnet calls soft becomes kind 'soft' (never scored)

Step 7 asks once per skill: the label is cached under `tier_key(skill_norm)` and reused
by every later posting and run (`TierCache`), so scores don't move between runs because
the model labeled the same skill differently.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from prospect.config import ScoreCfg, Settings
from prospect.db import transaction
from prospect.factbank.alias_index import AliasIndex
from prospect.factbank.loader import effective_targets, prompt_text
from prospect.llm.client import BudgetExceeded, LLMClient, LLMError, ToolSpec
from prospect.llm.prompts import Prompt, load_prompt
from prospect.logs import get_logger
from prospect.models.factbank import LEVEL_RANK, FactBank
from prospect.models.posting import Requirement, TierLabel, TierOutput
from prospect.pipeline.extract import alternatives
from prospect.pipeline.filter import CANADA_RE, metadata_remote_entry
from prospect.pipeline.guards import expresses_interest, mentions_interest
from prospect.progress import Progress, report
from prospect.text import ascii_fold, collapse_ws

log = get_logger("score")

TOOL = ToolSpec(
    name="classify_requirements",
    description="Assign a gap tier (0-3) with its evidence to each job requirement.",
    model=TierOutput,
)
HUMAN_LANGUAGES = {
    "english",
    "french",
    "francais",
    "mandarin",
    "chinese",
    "cantonese",
    "spanish",
    "german",
    "japanese",
    "korean",
    "portuguese",
    "italian",
    "arabic",
    "hindi",
    "russian",
    "dutch",
    "vietnamese",
    "tagalog",
    "punjabi",
    "urdu",
    "farsi",
    "persian",
    "turkish",
    "bilingual",
    "bilingualism",
}
_LANG_ALIASES = {"francais": "french", "chinese": "mandarin"}
TIER3_RE = re.compile(
    r"\b(?:certif\w*|clearance|p\.?\s?eng|professional engineer|licen[cs]e[ds]?|cpa|cfa|frm|pmp|"
    r"cissp|phd|ph\.d|doctorate|doctoral|master'?s|msc|m\.sc|meng|m\.eng|mba|graduate degree)\b",
    re.I,
)
BACHELOR_RE = re.compile(
    r"\b(?:bachelor'?s?|b\.?sc|bsc|b\.?eng|beng|undergraduate degree|university degree|"
    r"(?:\w+ )?degree in (?:computer|software|engineering|math|mathematics|statistics|"
    r"a related|related|stem|electrical)|four-year degree|4-year degree)\b",
    re.I,
)


@dataclass
class Tiered:
    tier: int | None  # None = needs the model
    substate: str | None = None
    matched_node: str | None = None
    evidence: list[str] = field(default_factory=list)
    est_hours: float | None = None
    nearest_node: str | None = None
    rationale: str = ""
    adjacent_of: str | None = None  # set when an adjacent entry matched (model gives hours)
    soft: bool = False  # the model called it a soft skill: stored as kind 'soft', never scored


def _substate(fb: FactBank, skill_id: str) -> str:
    return "weak" if fb.skill_map[skill_id].level == "familiar" else "strong"


def language_names(term: str) -> list[str]:
    """Human languages a short requirement names ('Fluent English', 'Bilingual EN/FR')."""
    words = re.findall(r"[a-z]+", ascii_fold(term).lower())
    if not words or len(words) > 6:
        return []
    found = []
    for w in words:
        if w in HUMAN_LANGUAGES:
            name = "french" if w.startswith("bilingu") else _LANG_ALIASES.get(w, w)
            if name not in found:
                found.append(name)
    return found


def tier_term(
    term: str,
    req: Requirement,
    fb: FactBank,
    idx: AliasIndex,
    french_requirement: str | None = None,
    years_cap: float | None = None,
) -> Tiered:
    """Deterministic tier for one alternative, or Tiered(tier=None) when it needs the model.

    `years_cap` (targets.max_years): a stated skill-years minimum at or below it is not a
    hard gap — the user accepts roles asking up to that many years.
    """
    langs = language_names(term)
    if langs:
        for name in langs:
            lang = fb.language(name)
            if lang is None:
                return Tiered(3, rationale=f"{name.title()} is not in identity.languages")
            if name == "french" and french_requirement == "posting_in_french":
                # Policy (2026-10): a French requirement in an English CS posting is treated
                # as Quebec language-law boilerplate; only a French-language posting fails.
                return Tiered(3, rationale="the posting itself is in French")
        return Tiered(0, "strong", rationale="human language(s) in identity.languages")
    cap = max(fb.work_years(), years_cap) if years_cap is not None else fb.work_years()
    if req.years is not None and req.years > cap:
        return Tiered(3, rationale=f"{req.years}+ years is above the {cap:g}-year cap")
    if TIER3_RE.search(term) and not BACHELOR_RE.search(term):
        return Tiered(3, rationale="certification / clearance / licence / graduate degree")
    if BACHELOR_RE.search(term):
        edu = next((e for e in fb.education if "bachelor" in e.degree.lower()), None)
        if edu:
            return Tiered(0, "strong", matched_node=edu.id, rationale=edu.degree)
        return Tiered(3, rationale="degree not held")
    m = idx.lookup(term)
    if m and m.kind == "exact":
        return Tiered(0, _substate(fb, m.skill_id), matched_node=m.skill_id, rationale="exact")
    if m and m.kind == "alias":
        skill = fb.skill_map[m.skill_id]
        return Tiered(
            1,
            matched_node=m.skill_id,
            evidence=[m.skill_id, *skill.evidence],
            rationale=f"alias {m.alias!r} of {skill.label}",
        )
    if m and m.kind == "adjacent":
        return Tiered(None, nearest_node=m.skill_id, adjacent_of=m.skill_id)
    return Tiered(None)


def deterministic_tier(
    req: Requirement,
    fb: FactBank,
    idx: AliasIndex,
    french_requirement: str | None,
    years_cap: float | None = None,
) -> Tiered:
    """Best tier over `A | B | C` alternatives; undetermined if the best could be the model's."""
    results = [
        tier_term(t, req, fb, idx, french_requirement, years_cap)
        for t in alternatives(req.skill_norm)
    ]
    decided = [r for r in results if r.tier is not None]
    best = min(decided, key=lambda r: r.tier or 0) if decided else None
    if best is not None and (best.tier == 0 or len(decided) == len(results)):
        return best
    hint = next((r for r in results if r.adjacent_of), None)
    return Tiered(
        None,
        nearest_node=hint.nearest_node if hint else None,
        adjacent_of=hint.adjacent_of if hint else None,
    )


def enforce_tier_rules(
    label: TierLabel, fb: FactBank, t2_max_hours: float, adjacent_of: str | None
) -> Tiered:
    """Code has the last word on a model label (CLAUDE.md tie-breaks)."""
    skill_ids = set(fb.skill_map)
    edu_ids = {e.id for e in fb.education}
    nearest = label.nearest_node if label.nearest_node in skill_ids else None
    matched = label.matched_node if label.matched_node in skill_ids | edu_ids else None
    tier = label.tier
    evidence = [e for e in label.evidence if e in fb.node_ids]
    hours = label.est_hours
    why = label.rationale

    if tier == 0 and matched is None:
        tier = 1 if evidence else 2
        why += " [tier 0 without a valid skill id]"
    if tier == 1 and not evidence:
        tier = 2
        why += " [tier 1 without a valid evidence chain]"
    if adjacent_of and tier >= 2:
        return Tiered(
            2,
            est_hours=hours,
            nearest_node=nearest or adjacent_of,
            rationale=why + " [in an adjacent list]",
        )
    if tier == 2 and not (hours is not None and hours <= t2_max_hours):
        tier = 3
        why += f" [no estimate ≤ {t2_max_hours:g}h]"
    elif (
        tier == 3
        and label.t3_reason in (None, "depth")
        and hours is not None
        and hours <= t2_max_hours
    ):
        # "Not in the fact bank" is not a hard gap: by the model's own estimate it is small.
        tier = 2
        why += f" [Tier 3 with an estimate ≤ {t2_max_hours:g}h is Tier 2]"
    if tier == 0:
        return Tiered(
            0,
            _substate(fb, matched) if matched in skill_ids else "strong",
            matched_node=matched,
            rationale=why,
        )
    if tier == 1:
        return Tiered(1, matched_node=matched, evidence=evidence, est_hours=hours, rationale=why)
    return Tiered(tier, est_hours=hours, nearest_node=nearest, rationale=why)


# --- tier cache -----------------------------------------------------------------


def tier_key(skill_norm: str) -> str:
    """Cache key for a requirement's skill. Case, accents, spacing and the order of
    `A | B` alternatives don't change what is asked for."""
    parts = {collapse_ws(ascii_fold(p).lower()).strip(" .,;:") for p in alternatives(skill_norm)}
    return " | ".join(sorted(p for p in parts if p))


def tier_context(model_id: str, prompt: Prompt, fb: FactBank) -> str:
    """What a cached label depends on: the model, the score prompt (hour budget and years
    cap filled in) and the fact bank as the model sees it. Change any → a fresh cache."""
    blob = "\n\0".join((model_id, prompt.version, prompt.text, prompt_text(fb)))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class TierCache:
    """One model label per skill (live run 2026-10-06: re-tiering moved scores by up to
    25 points when one requirement flipped between Tier 2 and Tier 3).

    The model's label is stored, not the final tier: `enforce_tier_rules` re-applies on
    every read, so a change to the code rules takes effect without asking again.
    `refresh` skips reads and overwrites (`prospect score --fresh-tiers`).
    """

    conn: sqlite3.Connection
    context: str
    refresh: bool = False
    hits: int = 0

    def get(self, key: str) -> TierLabel | None:
        if self.refresh:
            return None
        row = self.conn.execute(
            "SELECT label_json FROM tier_cache WHERE skill_key = ? AND context_hash = ?",
            (key, self.context),
        ).fetchone()
        if row is None:
            return None
        self.hits += 1
        return TierLabel.model_validate_json(row[0])

    def put(
        self,
        key: str,
        label: TierLabel,
        model: str,
        prompt_version: str,
        requirement_id: int | None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO tier_cache (skill_key, context_hash, label_json, model,
                   prompt_version, requirement_id) VALUES (?,?,?,?,?,?)
               ON CONFLICT(skill_key, context_hash) DO UPDATE SET
                 label_json=excluded.label_json, model=excluded.model,
                 prompt_version=excluded.prompt_version,
                 requirement_id=excluded.requirement_id, created_at=datetime('now')""",
            (key, self.context, label.model_dump_json(), model, prompt_version, requirement_id),
        )


# --- the score ------------------------------------------------------------------


def _clamp_fit(v: float) -> float:
    return max(-30.0, min(0.0, float(v)))


def location_key(facts: dict[str, Any], home_city: str) -> str:
    """home | remote | elsewhere | unknown — does taking the job need a move?

    Policy 2026-10-06: the posting's *set* of locations counts. Montreal anywhere in the
    ATS location list, or any remote option, means no move — "Montreal; Toronto" and
    "Montreal; Remote" score like "Montreal", never lower for listing more, never higher
    for listing both.
    """
    home = ascii_fold(home_city).lower()
    city = ascii_fold(facts.get("city") or "").lower()
    listed = ascii_fold(facts.get("location_raw") or "").lower()
    if city == home or (home and re.search(rf"\b{re.escape(home)}\b", listed)):
        return "home"
    if facts.get("work_arrangement") == "remote" or metadata_remote_entry(
        facts.get("location_raw")
    ):
        return "remote"
    if city or facts.get("province") or CANADA_RE.search(listed):
        return "elsewhere"
    return "unknown"


def location_fit(facts: dict[str, Any], home_city: str, table: dict[str, float]) -> float:
    """No move needed (home city, or a remote option) vs a move; anywhere in Canada is allowed."""
    return _clamp_fit(table.get(location_key(facts, home_city), 0.0))


def employment_penalties(facts: dict[str, Any], w: ScoreCfg) -> dict[str, Any]:
    """Part-time and short-term costs (policy 2026-10-06), each linear and 0 when "full".

    Fewer weekly hours cost more (37.5 h = full time). A shorter contract or internship
    costs more (12 months = full term); an internship counts as a fixed term. A value the
    posting does not state falls back to a typical one (`*_default_*` in [score]).
    """
    hours = facts.get("hours_per_week")
    if hours is None and facts.get("part_time"):
        hours = w.part_time_default_hours
    part = w.w_part_time * max(0.0, 1 - hours / w.full_time_hours) if hours else 0.0

    intern = facts.get("seniority") == "intern"
    months = None
    if intern or facts.get("contract"):
        months = facts.get("term_months") or (
            w.intern_default_months if intern else w.contract_default_months
        )
    short = w.w_short_term * max(0.0, 1 - months / w.full_term_months) if months else 0.0
    return {
        "part_time": round(part, 1),
        "short_term": round(short, 1),
        "hours": hours,
        "months": months,
    }


def score_posting(
    reqs: list[Requirement],
    facts: dict[str, Any],
    repost_count: int,
    prior_no_response: bool,
    settings: Settings,
    home_city: str,
) -> tuple[float, dict[str, Any]]:
    """Pure function: requirement tiers + posting facts → (score in [0,100], breakdown)."""
    w = settings.score
    must_t3 = sum(1 for r in reqs if r.must_have and r.tier == 3)
    must_t2 = sum(1 for r in reqs if r.must_have and r.tier == 2)
    nice_gap = sum(1 for r in reqs if not r.must_have and r.tier in (2, 3))
    weak_must = sum(1 for r in reqs if r.must_have and r.tier == 0 and r.tier_substate == "weak")
    fit_loc = location_fit(facts, home_city, w.fit_location)
    terms = employment_penalties(facts, w)
    ghost = min(
        w.ghost_max,
        w.ghost_per_repost * repost_count + (w.ghost_no_response if prior_no_response else 0.0),
    )
    raw = (
        100
        - w.w_must_t3 * must_t3
        - w.w_must_t2 * must_t2
        - w.w_nice_gap * nice_gap
        - w.w_weak_have * weak_must
        + fit_loc
        - terms["part_time"]
        - terms["short_term"]
        - ghost
    )
    score = max(0.0, min(100.0, raw))
    breakdown = {
        "base": 100,
        "must_t3": must_t3,
        "must_t2": must_t2,
        "nice_gap": nice_gap,
        "weak_must": weak_must,
        "fit_location": fit_loc,
        "location": location_key(facts, home_city),
        "part_time": -terms["part_time"],
        "hours_per_week": terms["hours"],
        "short_term": -terms["short_term"],
        "term_months": terms["months"],
        "ghost": -ghost,
        "repost_count": repost_count,
        "prior_no_response": prior_no_response,
        "raw": raw,
        "score": score,
    }
    return score, breakdown


def prior_no_response(
    conn: sqlite3.Connection, company_id: int, title_norm: str, days: int
) -> bool:
    return bool(
        conn.execute(
            """SELECT 1 FROM applications a JOIN postings p ON p.id = a.posting_id
               WHERE p.company_id = ? AND p.title_norm = ?
                 AND a.status IN ('submitted', 'ghosted')
                 AND a.submitted_at <= datetime('now', ?)""",
            (company_id, title_norm, f"-{days} days"),
        ).fetchone()
    )


# --- stage -------------------------------------------------------------------------


def _load_reqs(
    conn: sqlite3.Connection, posting_id: int, kinds: tuple[str, ...] = ("skill",)
) -> list[Requirement]:
    """Requirements of these kinds. Only `skill` rows are ever scored; the score stage also
    reloads `soft` rows, because "soft" is the score model's own label (re-judged like a
    tier, through the cache) — not the extractor's, which only emits skill / eligibility."""
    rows = conn.execute(
        f"SELECT * FROM requirements WHERE posting_id = ? AND kind IN "
        f"({','.join('?' * len(kinds))}) ORDER BY id",
        (posting_id, *kinds),
    ).fetchall()
    return [
        Requirement(
            id=r["id"],
            text=r["text"],
            skill_norm=r["skill_norm"],
            must_have=bool(r["must_have"]),
            years=r["years"],
            context=r["context"],
            kind=r["kind"],
            tier=r["tier"],
            tier_substate=r["tier_substate"],
            matched_node=r["matched_node"],
            evidence=json.loads(r["evidence_json"] or "[]"),
            est_hours=r["est_hours"],
            nearest_node=r["nearest_node"],
        )
        for r in rows
    ]


def score_prompt(settings: Settings, fb: FactBank) -> Prompt:
    base = load_prompt("score")
    text = base.text.replace("{t2_max_hours}", f"{settings.tiers.t2_max_hours:g}").replace(
        "{years_cap}", f"{effective_targets(settings, fb).max_years:g}"
    )
    return Prompt(stage=base.stage, version=base.version, text=text)


def tier_requirements(
    client: LLMClient,
    settings: Settings,
    fb: FactBank,
    posting: sqlite3.Row,
    reqs: list[Requirement],
    french_requirement: str | None,
    prompt: Prompt,
    cache: TierCache | None = None,
) -> tuple[list[tuple[Requirement, Tiered]], str | None]:
    """Returns ([(req, tiered)], model used or None).

    The model sees each skill once: a cached label is reused, and a skill listed twice
    in the posting is asked once.
    """
    idx = AliasIndex.build(fb)
    years_cap = effective_targets(settings, fb).max_years
    decided: dict[int, Tiered] = {}
    pending: list[tuple[int, Requirement, Tiered, str]] = []
    for i, r in enumerate(reqs):
        if expresses_interest(r.text):  # wording, not skill: never asked, never cached
            decided[i] = Tiered(None, soft=True, rationale="phrased as an interest (code rule)")
            continue
        t = deterministic_tier(r, fb, idx, french_requirement, years_cap)
        if t.tier is None:
            pending.append((i, r, t, tier_key(r.skill_norm)))
        else:
            decided[i] = t
    model_used = None
    by_key: dict[str, TierLabel] = {}
    cached: set[str] = set()
    ask: dict[str, int] = {}  # skill key → index of the requirement sent to the model
    for i, _r, _t, key in pending:
        if key in by_key or key in ask:
            continue
        hit = cache.get(key) if cache else None
        if hit is not None:
            by_key[key] = hit
            cached.add(key)
        else:
            ask[key] = i
    if ask:
        lines = [f"Posting: {posting['title']} at {posting['company']}", "", "Requirements:"]
        for i, r, t, key in pending:
            if ask.get(key) != i:
                continue
            item = {
                "index": i,
                "text": r.text,
                "skill_norm": r.skill_norm,
                "must_have": r.must_have,
                "years": r.years,
                "context": r.context,
            }
            if t.adjacent_of:
                item["note"] = f"listed in the `adjacent` list of skill {t.adjacent_of}"
            lines.append(json.dumps(item, ensure_ascii=False))
        res = client.structured(
            stage="score",
            model="sonnet",
            prompt=prompt,
            user_content="\n".join(lines),
            tool=TOOL,
            max_tokens=settings.score.max_tokens,
            cached_prefix=prompt_text(fb),
            effort=settings.score.effort,
            posting_id=posting["id"],
        )
        model_used = res.model
        got = {lab.index: lab for lab in res.output.labels}
        for key, i in ask.items():
            if i in got:
                by_key[key] = got[i]
                # A soft label on interest wording ("real enthusiasm for AI and hands-on
                # experience…") is about that sentence, not the skill: not cached.
                if cache and not (got[i].soft and mentions_interest(reqs[i].text)):
                    cache.put(key, got[i], res.model, prompt.version, reqs[i].id)
    for i, _r, t, key in pending:
        lab = by_key.get(key)
        if lab is None:  # the model skipped it: the cautious default (never cached)
            decided[i] = Tiered(
                3, nearest_node=t.nearest_node, rationale="not labeled by the model"
            )
            if t.adjacent_of:
                decided[i] = Tiered(
                    2,
                    nearest_node=t.adjacent_of,
                    rationale="adjacent; not labeled by the model",
                )
            continue
        if lab.soft:
            decided[i] = Tiered(None, soft=True, rationale=lab.rationale)
        else:
            decided[i] = enforce_tier_rules(lab, fb, settings.tiers.t2_max_hours, t.adjacent_of)
        if key in cached:
            decided[i].rationale += " [cached]"
    return [(r, decided[i]) for i, r in enumerate(reqs)], model_used


def run_score(
    settings: Settings,
    conn: sqlite3.Connection,
    fb: FactBank,
    limit: int | None = None,
    posting_ids: list[int] | None = None,
    force: bool = False,
    client: LLMClient | None = None,
    fresh_tiers: bool = False,
    progress: Progress | None = None,
) -> str:
    client = client or LLMClient(settings, conn)
    prompt = score_prompt(settings, fb)
    cache = TierCache(conn, tier_context(settings.model_id("sonnet"), prompt, fb), fresh_tiers)
    limit = limit or settings.score.top_n
    where = "p.status = 'candidate'" + ("" if force else " AND s.posting_id IS NULL")
    if posting_ids:
        where = f"p.id IN ({','.join(str(int(i)) for i in posting_ids)})"
    rows = conn.execute(
        f"""SELECT p.*, c.name AS company, f.city, f.province, f.work_arrangement, f.remote_scope,
                   f.french_requirement, f.posting_language, f.seniority, f.min_years,
                   f.part_time, f.hours_per_week, f.contract, f.term_months, e.cosine
            FROM postings p JOIN companies c ON c.id = p.company_id
            JOIN posting_facts f ON f.posting_id = p.id
            LEFT JOIN scores s ON s.posting_id = p.id
            LEFT JOIN embeddings e ON e.posting_id = p.id
            WHERE {where}
              AND f.french_requirement != 'posting_in_french'
              AND f.posting_language != 'fr'
            ORDER BY e.cosine DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    done = 0
    notes: list[str] = []
    for n, p in enumerate(rows):
        report(progress, n / len(rows), f"score {n + 1}/{len(rows)}: {p['company']}")
        # Soft rows too: a soft label set before the tier cache (2026-10-08) was never asked
        # again, so the same skill could be soft in one posting and a gap in the next.
        reqs = _load_reqs(conn, p["id"], kinds=("skill", "soft"))
        try:
            tiered, model = tier_requirements(
                client, settings, fb, p, reqs, p["french_requirement"], prompt, cache
            )
        except BudgetExceeded as e:
            notes.append(f"stopped: {e}")
            break
        except LLMError as e:
            notes.append(f"posting {p['id']}: {e}")
            continue
        final: list[Requirement] = []
        with transaction(conn):
            for r, t in tiered:
                if t.soft:  # policy 2026-10-06: soft skills are never gaps and never scored
                    conn.execute(
                        """UPDATE requirements SET kind='soft', tier=NULL, rationale=?,
                               model=COALESCE(?, model), prompt_version=? WHERE id=?""",
                        (t.rationale[:300], model, prompt.version, r.id),
                    )
                    continue
                conn.execute(
                    """UPDATE requirements SET kind='skill', tier=?, tier_substate=?,
                           matched_node=?,
                           evidence_json=?, est_hours=?, nearest_node=?, rationale=?,
                           model=COALESCE(?, model), prompt_version=COALESCE(?, prompt_version)
                       WHERE id=?""",
                    (
                        t.tier,
                        t.substate if t.tier == 0 else None,
                        t.matched_node,
                        json.dumps(t.evidence),
                        t.est_hours,
                        t.nearest_node,
                        t.rationale[:300],
                        model,
                        prompt.version if model else None,
                        r.id,
                    ),
                )
                final.append(
                    r.model_copy(
                        update={
                            "tier": t.tier,
                            "tier_substate": t.substate if t.tier == 0 else None,
                            "matched_node": t.matched_node,
                            "evidence": t.evidence,
                            "est_hours": t.est_hours,
                            "nearest_node": t.nearest_node,
                        }
                    )
                )
            facts = {
                k: p[k]
                for k in (
                    "city",
                    "province",
                    "work_arrangement",
                    "remote_scope",
                    "french_requirement",
                    "seniority",
                    "location_raw",
                    "part_time",
                    "hours_per_week",
                    "contract",
                    "term_months",
                )
            }
            no_resp = prior_no_response(
                conn, p["company_id"], p["title_norm"], settings.score.no_response_days
            )
            score, breakdown = score_posting(
                final, facts, p["repost_count"], no_resp, settings, fb.identity.location.city
            )
            conn.execute(
                """INSERT INTO scores (posting_id, score, breakdown_json, cosine, scored_at,
                       weights_version) VALUES (?,?,?,?, datetime('now'), ?)
                   ON CONFLICT(posting_id) DO UPDATE SET score=excluded.score,
                     breakdown_json=excluded.breakdown_json, cosine=excluded.cosine,
                     scored_at=excluded.scored_at, weights_version=excluded.weights_version""",
                (
                    p["id"],
                    score,
                    json.dumps(breakdown),
                    p["cosine"],
                    settings.score_weights_version(),
                ),
            )
        done += 1
        log.info(
            "scored",
            id=p["id"],
            score=score,
            llm=bool(model),
            t3=breakdown["must_t3"],
            t2=breakdown["must_t2"],
        )
    head = f"score: {done} scored of {len(rows)} selected" + (
        f"; {cache.hits} skill tiers reused from the cache" if cache.hits else ""
    )
    return "\n".join([head, *notes])


def level_rank(fb: FactBank, skill_id: str) -> int:
    return LEVEL_RANK[fb.skill_map[skill_id].level]
