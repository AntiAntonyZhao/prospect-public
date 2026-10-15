"""USD pricing and the `prospect cost` report (Hard rule 3: every call is costed).

Per-million-token list prices (Claude API, first-party). Cache writes bill at
1.25x input (5-minute TTL), cache reads at 0.1x, Message Batches at 0.5x of
everything. An unknown model is priced at the most expensive tier we know, so
the cost log can over-count but never under-count.
"""

from __future__ import annotations

import sqlite3

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
}
FALLBACK_PRICE = (10.00, 50.00)
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10
BATCH_MULT = 0.50


def price_for(model: str) -> tuple[float, float]:
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    # Dated snapshots (claude-haiku-4-5-20251001) price like their alias. Longest
    # prefix wins so claude-opus-5-5 is never priced as claude-opus-5.
    for key in sorted(PRICES_PER_MTOK, key=len, reverse=True):
        if model.startswith(key + "-"):
            return PRICES_PER_MTOK[key]
    return FALLBACK_PRICE


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    batch: bool = False,
) -> float:
    p_in, p_out = price_for(model)
    usd = (
        input_tokens * p_in
        + cache_write_tokens * p_in * CACHE_WRITE_MULT
        + cache_read_tokens * p_in * CACHE_READ_MULT
        + output_tokens * p_out
    ) / 1_000_000
    return round(usd * (BATCH_MULT if batch else 1.0), 6)


def cost_report(conn: sqlite3.Connection, since: str | None = None) -> str:
    where = "WHERE model IS NOT NULL" + (" AND started_at >= ?" if since else "")
    args: tuple = (since,) if since else ()
    rows = conn.execute(
        f"""SELECT stage, model, COUNT(*) AS calls, SUM(COALESCE(input_tokens,0)) AS tin,
                   SUM(COALESCE(output_tokens,0)) AS tout, SUM(COALESCE(cached_tokens,0)) AS tcache,
                   SUM(COALESCE(cache_write_tokens,0)) AS twrite, SUM(COALESCE(cost_usd,0)) AS usd,
                   SUM(batch_id IS NOT NULL) AS batched, SUM(error IS NOT NULL) AS errors,
                   COUNT(DISTINCT posting_id) AS postings
            FROM runs {where} GROUP BY stage, model ORDER BY usd DESC""",
        args,
    ).fetchall()
    total = sum(r["usd"] for r in rows)
    lines = [
        f"Spend {'since ' + since if since else 'all time'}: ${total:.4f}",
        f"{'stage':<16}{'model':<28}{'calls':>6}{'in':>10}{'out':>9}{'c.read':>9}{'c.write':>9}"
        f"{'USD':>10}{'$/posting':>11}",
    ]
    for r in rows:
        per = f"{r['usd'] / r['postings']:.5f}" if r["postings"] else "-"
        lines.append(
            f"{r['stage']:<16}{r['model']:<28}{r['calls']:>6}{r['tin']:>10}{r['tout']:>9}"
            f"{r['tcache']:>9}{r['twrite']:>9}{r['usd']:>10.4f}{per:>11}"
            + (f"  ({r['batched']} batched)" if r["batched"] else "")
            + (f"  ({r['errors']} errors)" if r["errors"] else "")
        )
    days = conn.execute(
        f"""SELECT date(started_at) AS d, SUM(COALESCE(cost_usd,0)) AS usd FROM runs {where}
            GROUP BY d ORDER BY d DESC LIMIT 14""",
        args,
    ).fetchall()
    if days:
        lines.append("Last days: " + ", ".join(f"{d['d']} ${d['usd']:.3f}" for d in days))
    return "\n".join(lines)
