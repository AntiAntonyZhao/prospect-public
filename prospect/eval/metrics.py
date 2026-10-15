"""`prospect eval extract`: per-field P/R/F1, $/posting, p50 latency, per model.

Predictions go to eval_predictions, never to posting_facts. Every call is
logged in `runs` like any other (stage `eval_extract`, Hard rule 3).

Each labeled field is scored the way the score and filter use it (policy
2026-10-06), not by exact label — a junior/mid or "which of the listed cities"
difference changes nothing downstream:
  * `location` (from `city`) — `score.location_key` on the label or prediction plus
    the posting's ATS location list: home | remote | elsewhere | unknown. A city
    label of "Remote" means a remote option.
  * `posting_language` — as labeled (it scores the deterministic detector).
  * `min_years` — as labeled; 0 and "not stated" both mean "no minimum".
  * `internship` (from `seniority`) — intern or not; the only thing seniority drives.
Metric definitions (keep stable once numbers are published):
  * a prediction is *made* when it is not null / 'unknown' (for `internship`: intern);
  * a gold value is *present* likewise;
  * TP = made ∧ present ∧ equal;  precision = TP / made;  recall = TP / present;
  * accuracy = agreement over all labeled postings (null == null counts).
Exact label agreement on `city` and `seniority` is printed as a reference line.

The decisions section scores what the filter and score act on (CLAUDE.md
"Match score" / "Hard filters"), which is what a wrong field actually costs.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from dataclasses import dataclass

from prospect.config import Settings
from prospect.db import now_iso, transaction
from prospect.llm.client import BatchItem, LLMClient, LLMError
from prospect.llm.prompts import load_prompt
from prospect.models.posting import LABELED_FIELDS
from prospect.pipeline.extract import TOOL, posting_user_content, to_facts
from prospect.pipeline.score import location_key
from prospect.text import ascii_fold

NULLISH = {None, "", "unknown", "none_stated"}
# Stored next to the labeled fields: the location view needs the arrangement.
PREDICTED_FIELDS = (*LABELED_FIELDS, "work_arrangement")
# What the report scores: each labeled field as the pipeline uses it (view ← label field).
VIEWS = {
    "location": "city",
    "posting_language": "posting_language",
    "min_years": "min_years",
    "internship": "seniority",
}


@dataclass
class DecisionCtx:
    home_city: str  # identity.location.city
    years_cap: int  # effective targets.max_years


def norm_value(field: str, value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if field == "city":
        city = ascii_fold(s).lower()
        return None if city == "remote" else city
    if field == "province":
        return s.upper()
    if field == "min_years":
        try:
            years = int(float(s))
        except ValueError:
            return None
        return str(years) if years > 0 else None  # 0 = no minimum = not stated
    return s.lower()


Labels = dict[tuple[int, str], str | None]


def view_value(
    view: str, d: Labels, pid: int, location_raw: str | None, home_city: str, gold: bool
) -> str | None:
    """One labeled (gold=True) or predicted field, seen the way the pipeline uses it."""
    if view == "location":
        city = d.get((pid, "city"))
        if gold:
            remote = (city or "").strip().lower() == "remote"
            facts = {
                "city": None if remote else city,
                "work_arrangement": "remote" if remote else None,
            }
        else:
            facts = {"city": city, "work_arrangement": d.get((pid, "work_arrangement"))}
        key = location_key({**facts, "location_raw": location_raw}, home_city)
        return None if key == "unknown" else key
    if view == "internship":
        return "intern" if norm_value("seniority", d.get((pid, "seniority"))) == "intern" else None
    return norm_value(view, d.get((pid, view)))


@dataclass
class FieldScore:
    field: str
    n: int
    made: int
    present: int
    tp: int
    agree: int

    @property
    def precision(self) -> float:
        return self.tp / self.made if self.made else 0.0

    @property
    def recall(self) -> float:
        return self.tp / self.present if self.present else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def accuracy(self) -> float:
        return self.agree / self.n if self.n else 0.0


def score_field(field: str, pairs: list[tuple[str | None, str | None]]) -> FieldScore:
    made = present = tp = agree = 0
    for gold, pred in pairs:
        g_in = gold not in NULLISH
        p_in = pred not in NULLISH
        made += p_in
        present += g_in
        tp += g_in and p_in and gold == pred
        agree += (gold if g_in else None) == (pred if p_in else None)
    return FieldScore(field, len(pairs), made, present, tp, agree)


def run_eval_extract(
    settings: Settings,
    conn: sqlite3.Connection,
    model_key: str,
    limit: int | None = None,
    batch: bool = False,
    client: LLMClient | None = None,
    *,
    ctx: DecisionCtx,
    ids: list[int] | None = None,
) -> str:
    """`ids` restricts the run to these labeled postings (e.g. a held-out label file)."""
    if model_key not in ("haiku", "sonnet", "opus"):
        raise LLMError("--model must be haiku, sonnet or opus")
    client = client or LLMClient(settings, conn)
    prompt = load_prompt("extract")
    labeled = [
        r["posting_id"]
        for r in conn.execute("SELECT DISTINCT posting_id FROM labels ORDER BY posting_id")
    ]
    wanted = None if ids is None else set(ids)
    ids = [i for i in labeled if wanted is None or i in wanted]
    if limit:
        ids = ids[:limit]
    if not ids:
        return "no labeled postings yet — run `prospect eval label` first"
    rows = {
        r["id"]: r
        for r in conn.execute(
            f"""SELECT p.*, c.name AS company FROM postings p
                JOIN companies c ON c.id = p.company_id
                WHERE p.id IN ({",".join("?" * len(ids))})""",
            ids,
        )
    }
    model = settings.model_id(model_key)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO eval_runs (stage, model, prompt_version, mode) VALUES (?,?,?,?)",
            ("extract", model, prompt.version, "batch" if batch else "sync"),
        )
    run_id = int(cur.lastrowid)
    content = {
        pid: posting_user_content(rows[pid], rows[pid]["company"], settings.extract.max_desc_chars)
        for pid in ids
    }

    def store(pid: int, output, latency: float | None, cost: float, error: str | None) -> None:
        with transaction(conn):
            conn.execute(
                """INSERT OR REPLACE INTO eval_items (eval_run_id, posting_id, latency_ms, cost_usd,
                       error) VALUES (?,?,?,?,?)""",
                (run_id, pid, latency, cost, error),
            )
            if output is None:
                return
            seen, body = content[pid]
            facts, _ = to_facts(output, body, seen)
            for field in PREDICTED_FIELDS:
                conn.execute(
                    """INSERT OR REPLACE INTO eval_predictions
                           (eval_run_id, posting_id, field, value) VALUES (?,?,?,?)""",
                    (
                        run_id,
                        pid,
                        field,
                        None if getattr(facts, field) is None else str(getattr(facts, field)),
                    ),
                )

    if batch:
        items = [BatchItem(f"eval-{run_id}-{pid}", content[pid][0], pid) for pid in ids]
        batch_id, _chosen = client.batch_submit(
            stage="eval_extract",
            model=model,
            prompt=prompt,
            items=items,
            tool=TOOL,
            max_tokens=settings.extract.max_tokens,
        )
        while client.batch_status(batch_id) != "ended":
            time.sleep(settings.extract.poll_seconds)
        for res in client.batch_collect(batch_id, TOOL):
            if res.posting_id is not None:
                store(res.posting_id, res.output, None, res.cost_usd, res.error)
    else:
        for pid in ids:
            try:
                res = client.structured(
                    stage="eval_extract",
                    model=model,
                    prompt=prompt,
                    user_content=content[pid][0],
                    tool=TOOL,
                    max_tokens=settings.extract.max_tokens,
                    posting_id=pid,
                )
                store(pid, res.output, res.latency_ms, res.cost_usd, None)
            except LLMError as e:
                store(pid, None, None, 0.0, str(e)[:300])
                if "budget" in str(e).lower():
                    break
    with transaction(conn):
        conn.execute("UPDATE eval_runs SET finished_at=? WHERE id=?", (now_iso(), run_id))
    return format_report(conn, run_id, ctx=ctx)


def _run_report(
    conn: sqlite3.Connection, run: sqlite3.Row, show_misses: bool, ctx: DecisionCtx
) -> list[str]:
    rid = run["id"]
    gold: Labels = {
        (r["posting_id"], r["field"]): r["gold_value"] for r in conn.execute("SELECT * FROM labels")
    }
    preds: Labels = {
        (r["posting_id"], r["field"]): r["value"]
        for r in conn.execute("SELECT * FROM eval_predictions WHERE eval_run_id=?", (rid,))
    }
    items = conn.execute("SELECT * FROM eval_items WHERE eval_run_id=?", (rid,)).fetchall()
    ok_ids = sorted(i["posting_id"] for i in items if i["error"] is None)
    where = {
        r["id"]: r["location_raw"]
        for r in conn.execute(
            f"SELECT id, location_raw FROM postings WHERE id IN ({','.join('?' * len(ok_ids))})",
            ok_ids,
        )
    }
    has_arrangement = any((pid, "work_arrangement") in preds for pid in ok_ids)

    def pairs(view: str) -> list[tuple[int, str | None, str | None]]:
        label = VIEWS[view]
        return [
            (
                pid,
                view_value(view, gold, pid, where.get(pid), ctx.home_city, gold=True),
                view_value(view, preds, pid, where.get(pid), ctx.home_city, gold=False),
            )
            for pid in ok_ids
            if (pid, label) in gold
        ]

    lines = [
        f"eval run {rid}: {run['model']} ({run['prompt_version']}, {run['mode']}) — "
        f"{len(ok_ids)}/{len(items)} postings extracted",
        f"  {'field (as scored)':<20}{'n':>5}{'P':>8}{'R':>8}{'F1':>8}{'acc':>8}",
    ]
    scores: dict[str, FieldScore] = {}
    for view in VIEWS:
        if view == "location" and not has_arrangement:
            lines.append(f"  {view:<20}  n/a: run predates stored work_arrangement")
            continue
        fs = scores[view] = score_field(view, [(g, p) for _, g, p in pairs(view)])
        lines.append(
            f"  {view:<20}{fs.n:>5}{fs.precision:>8.1%}{fs.recall:>8.1%}"
            f"{fs.f1:>8.1%}{fs.accuracy:>8.1%}"
        )
    macro = [fs for view, fs in scores.items() if view != "posting_language"]
    if macro:
        lines.append(
            f"  {'macro (LLM fields)':<20}{'':>5}"
            f"{statistics.mean(m.precision for m in macro):>8.1%}"
            f"{statistics.mean(m.recall for m in macro):>8.1%}"
            f"{statistics.mean(m.f1 for m in macro):>8.1%}"
        )
    exact = []
    for field in ("city", "seniority"):
        both = [
            (norm_value(field, gold[(pid, field)]), norm_value(field, preds.get((pid, field))))
            for pid in ok_ids
            if (pid, field) in gold
        ]
        exact.append(f"{field} {score_field(field, both).agree}/{len(both)}")
    lines.append(f"  exact labels (reference, not scored): {', '.join(exact)}")
    costs = [i["cost_usd"] or 0.0 for i in items if i["error"] is None]
    lat = [i["latency_ms"] for i in items if i["latency_ms"] is not None]
    lines.append(
        f"  $/posting {statistics.mean(costs) if costs else 0:.5f}   "
        f"p50 latency {statistics.median(lat) / 1000 if lat else float('nan'):.2f}s"
        + ("" if lat else " (batch: no latency)")
    )
    lines += _decisions(pairs, has_arrangement, ctx)
    if show_misses:
        lines += _disagreements(conn, {v: pairs(v) for v in scores}, gold, preds)
    return lines


def _decisions(pairs, has_arrangement: bool, ctx: DecisionCtx) -> list[str]:
    """Agreement on the four things the pipeline acts on, per labeled posting."""
    rules = [
        ("French posting -> reject", "posting_language", lambda v: v == "fr"),
        (
            f"{ctx.years_cap + 1}+ years -> reject",
            "min_years",
            lambda v: v is not None and int(v) > ctx.years_cap,
        ),
        ("internship (term penalty)", "internship", lambda v: v == "intern"),
        (f"no move ({ctx.home_city} or remote)", "location", lambda v: v in ("home", "remote")),
    ]
    lines = [f"  {'decision':<34}{'gold+':>6}{'pred+':>6}{'agree':>9}"]
    for name, view, fires in rules:
        if view == "location" and not has_arrangement:
            lines.append(f"  {name:<34}  n/a: run predates stored work_arrangement")
            continue
        rows = pairs(view)
        g = [fires(gv) for _, gv, _ in rows]
        p = [fires(pv) for _, _, pv in rows]
        agree = sum(a == b for a, b in zip(g, p, strict=True))
        lines.append(f"  {name:<34}{sum(g):>6}{sum(p):>6}{f'{agree}/{len(rows)}':>9}")
    return lines


def _disagreements(
    conn: sqlite3.Connection,
    by_view: dict[str, list[tuple[int, str | None, str | None]]],
    gold: Labels,
    preds: Labels,
) -> list[str]:
    """Every scored field where the run disagrees with gold — the failure cases."""
    misses = []
    for view, rows in by_view.items():
        for pid, g, p in rows:
            g, p = (None if g in NULLISH else g), (None if p in NULLISH else p)
            if g != p:
                misses.append((pid, view, g, p))
    if not misses:
        return ["  disagreements: none"]
    ids = sorted({m[0] for m in misses})
    names = {
        r["id"]: f"{r['company']} — {r['title']}"
        for r in conn.execute(
            f"""SELECT p.id, p.title, c.name AS company FROM postings p
                JOIN companies c ON c.id = p.company_id
                WHERE p.id IN ({",".join("?" * len(ids))})""",
            ids,
        )
    }
    lines = [f"  disagreements ({len(misses)}):"]
    for pid, view, g, p in sorted(misses):
        label = VIEWS[view]
        raw = f" [label {gold.get((pid, label)) or '-'} / pred {preds.get((pid, label)) or '-'}]"
        lines.append(
            f"    {pid:>6} {view:<17} gold {g or '-':<10} pred {p or '-':<10} "
            f"{names.get(pid, '')[:44]}" + (raw if view == "location" else "")
        )
    return lines


def format_report(
    conn: sqlite3.Connection,
    run_id: int | None = None,
    show_misses: bool = True,
    *,
    ctx: DecisionCtx,
) -> str:
    if run_id is not None:
        runs = conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchall()
    else:  # latest run per model
        runs = conn.execute(
            """SELECT * FROM eval_runs WHERE id IN (SELECT MAX(id) FROM eval_runs GROUP BY model)
               ORDER BY model"""
        ).fetchall()
    if not runs:
        return "no eval runs yet — `prospect eval extract --model haiku`"
    out: list[str] = []
    for run in runs:
        out += _run_report(conn, run, show_misses, ctx)
        out.append("")
    return "\n".join(out).rstrip()
