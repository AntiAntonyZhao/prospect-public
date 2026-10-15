"""`prospect run`: ingest → filter → rank → extract → filter (full) → score → digest.

Cheap and deterministic first, expensive and probabilistic last. Each stage is
independently re-runnable; this only sequences them. A budget stop in one LLM
stage skips the later LLM stages but still prints the digest. Nothing here writes
the fact bank (Hard rule 1).
"""

from __future__ import annotations

import sqlite3

from prospect.config import Settings
from prospect.factbank.loader import FactBankError, ensure_lint_clean
from prospect.llm.client import BudgetExceeded, LLMClient, LLMError
from prospect.pipeline.digest import format_digest
from prospect.pipeline.extract import run_extract
from prospect.pipeline.filter import FilterError, full_pass, prepass
from prospect.pipeline.ingest import run_ingest
from prospect.pipeline.rank import run_rank
from prospect.pipeline.score import run_score
from prospect.progress import Progress, report, window


class StageError(RuntimeError):
    pass


def run_daily(
    settings: Settings,
    conn: sqlite3.Connection,
    skip_ingest: bool = False,
    sync_extract: bool = False,
    progress: Progress | None = None,
) -> str:
    try:
        fb = ensure_lint_clean(settings)
    except FactBankError as e:
        raise StageError(str(e)) from e
    out: list[str] = []
    if not skip_ingest:
        out += [s.line() for s in run_ingest(settings, conn, progress=window(progress, 0, 0.25))]
    out.append(prepass(settings, conn, fb, progress=window(progress, 0.25, 0.27)).summary())
    report(progress, 0.27, "rank")
    out.append(run_rank(settings, conn, fb, progress=window(progress, 0.27, 0.37)).summary())

    client = LLMClient(settings, conn)
    llm_ok = True
    try:
        mode = "sync" if sync_extract else settings.extract.mode
        out.append(
            run_extract(
                settings, conn, mode=mode, client=client, progress=window(progress, 0.37, 0.8)
            )
        )
    except BudgetExceeded as e:
        out.append(f"extract stopped: {e}")
        llm_ok = False
    except LLMError as e:
        out.append(f"extract failed: {e}")
        llm_ok = False

    try:
        out.append(full_pass(settings, conn, fb, progress=window(progress, 0.8, 0.82)).summary())
    except FilterError as e:
        out.append(f"full filter NOT run: {e}")
        out.append("score skipped: unfiltered postings are never scored")
        llm_ok = False

    if llm_ok:
        try:
            out.append(
                run_score(settings, conn, fb, client=client, progress=window(progress, 0.82, 1))
            )
        except LLMError as e:
            out.append(f"score stopped: {e}")
    out.append(f"spent today: ${client.spent_today():.4f} of ${settings.budget.daily_usd:.2f}")
    out.append("")
    out.append(format_digest(settings, conn))
    return "\n".join(out)
