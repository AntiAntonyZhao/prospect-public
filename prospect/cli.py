"""typer entrypoints only; no logic. Stage modules are imported lazily."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

from prospect.config import Settings, load_settings

if TYPE_CHECKING:
    from prospect.eval.metrics import DecisionCtx

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Prospect job-search pipeline.")
db_app = typer.Typer(no_args_is_help=True, help="Database.")
factbank_app = typer.Typer(no_args_is_help=True, help="factbank.yaml tools.")
companies_app = typer.Typer(no_args_is_help=True, help="companies.yaml tools.")
eval_app = typer.Typer(no_args_is_help=True, help="Labeling and model evaluation.")
app.add_typer(db_app, name="db")
app.add_typer(factbank_app, name="factbank")
app.add_typer(companies_app, name="companies")
app.add_typer(eval_app, name="eval")


def _settings() -> Settings:
    return load_settings()


def _conn(settings: Settings) -> sqlite3.Connection:
    from prospect.db import connect, require_schema

    if not settings.db_path.exists():
        _fail("database not found; run `prospect db init` first")
    conn = connect(settings.db_path)
    require_schema(conn)
    return conn


def _fail(msg: str) -> NoReturn:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(1)


def _echo(text: str) -> None:
    typer.echo(text)


# --- db / factbank / companies ------------------------------------------------


@db_app.command("init")
def db_init() -> None:
    """Create or migrate the SQLite database from db/schema.sql (idempotent)."""
    from prospect.db import connect, init_db

    s = _settings()
    conn = connect(s.db_path)
    changes = init_db(conn, s.schema_path.read_text(encoding="utf-8"))
    _echo(f"{s.db_path.name}: " + ("up to date" if not changes else f"{len(changes)} change(s)"))
    for c in changes:
        _echo(f"  {c}")


@factbank_app.command("lint")
def factbank_lint() -> None:
    """Validate factbank.yaml (schema + evidence + alias + metric rules)."""
    from prospect.factbank.lint import lint_file

    s = _settings()
    report = lint_file(s.factbank_path)
    _echo(report.render(s.factbank_path.name))
    if not report.ok:
        raise typer.Exit(1)


@companies_app.command("check")
def companies_check(
    name: str | None = typer.Option(None, help="Only this company (name or token)."),
) -> None:
    """Open every companies.yaml board URL and report job counts (token verification)."""
    from prospect.pipeline.companies import check_companies

    _echo(check_companies(_settings(), name))


# --- daily pipeline -------------------------------------------------------------


@app.command()
def run(
    skip_ingest: bool = typer.Option(False, "--skip-ingest"),
    sync_extract: bool = typer.Option(False, "--sync", help="Extract synchronously, not Batch."),
) -> None:
    """Daily orchestrator: ingest → filter → rank → extract → filter → score → digest."""
    from prospect.pipeline.run import StageError, run_daily

    s = _settings()
    try:
        _echo(run_daily(s, _conn(s), skip_ingest=skip_ingest, sync_extract=sync_extract))
    except StageError as e:
        _fail(str(e))


@app.command()
def ingest(
    source: list[str] = typer.Option(None, "--source", help="Only these sources (repeatable)."),
) -> None:
    """Run the source adapters and upsert postings."""
    from prospect.pipeline.ingest import run_ingest

    s = _settings()
    for stats in run_ingest(s, _conn(s), only=source or None):
        _echo(stats.line())
        for err in stats.errors[:5]:
            _echo(f"    ! {err}")


@app.command("filter")
def filter_cmd(
    full: bool = typer.Option(False, "--full", help="Post-extract pass on posting_facts."),
) -> None:
    """Apply hard rules → postings.status (pre-pass by default; --full after extract)."""
    from prospect.factbank.loader import FactBankError, ensure_lint_clean
    from prospect.pipeline.filter import FilterError, full_pass, prepass

    s = _settings()
    try:
        fb = ensure_lint_clean(s)
        conn = _conn(s)
        _echo((full_pass if full else prepass)(s, conn, fb).summary())
    except (FactBankError, FilterError) as e:
        _fail(str(e))


@app.command()
def rank(
    top: int | None = typer.Option(None, help="Shortlist size (default [rank] top_n)."),
) -> None:
    """Embed candidates locally and shortlist the top-N by cosine vs the profile."""
    from prospect.factbank.loader import FactBankError, ensure_lint_clean
    from prospect.pipeline.rank import run_rank

    s = _settings()
    try:
        fb = ensure_lint_clean(s)
        _echo(run_rank(s, _conn(s), fb, top_n=top).summary())
    except FactBankError as e:
        _fail(str(e))


@app.command()
def extract(
    batch: bool = typer.Option(None, "--batch/--sync", help="Batch API (default from config)."),
    submit: bool = typer.Option(False, "--submit", help="Batch: submit only, don't wait."),
    collect: bool = typer.Option(False, "--collect", help="Batch: collect finished batches."),
    limit: int | None = typer.Option(None, help="At most N postings."),
    posting: list[int] = typer.Option(None, "--posting", help="Only these posting ids."),
    force: bool = typer.Option(False, "--force", help="Re-extract postings that have facts."),
) -> None:
    """Haiku → posting_facts + requirements (Batch API by default)."""
    from prospect.factbank.loader import FactBankError, ensure_lint_clean
    from prospect.llm.client import LLMError
    from prospect.pipeline.extract import run_extract

    s = _settings()
    try:
        ensure_lint_clean(s)
        mode = s.extract.mode if batch is None else ("batch" if batch else "sync")
        _echo(
            run_extract(
                s,
                _conn(s),
                mode=mode,
                submit_only=submit,
                collect_only=collect,
                limit=limit,
                posting_ids=posting or None,
                force=force,
            )
        )
    except (FactBankError, LLMError) as e:
        _fail(str(e))


@app.command()
def score(
    limit: int | None = typer.Option(None, help="At most N postings (default [score] top_n)."),
    posting: list[int] = typer.Option(None, "--posting", help="Only these posting ids."),
    force: bool = typer.Option(
        False, "--force", help="Re-score already scored postings (cached skill tiers are reused)."
    ),
    fresh_tiers: bool = typer.Option(
        False,
        "--fresh-tiers",
        help="Ask the model again for every skill and overwrite the cached tiers.",
    ),
) -> None:
    """Tier classification (Sonnet) + deterministic score."""
    from prospect.factbank.loader import FactBankError, ensure_lint_clean
    from prospect.llm.client import LLMError
    from prospect.pipeline.score import run_score

    s = _settings()
    try:
        fb = ensure_lint_clean(s)
        _echo(
            run_score(
                s,
                _conn(s),
                fb,
                limit=limit,
                posting_ids=posting or None,
                force=force,
                fresh_tiers=fresh_tiers,
            )
        )
    except (FactBankError, LLMError) as e:
        _fail(str(e))


@app.command()
def digest(
    limit: int = typer.Option(25, help="Rows to show."),
    min_score: float = typer.Option(0, help="Hide postings scoring below this."),
) -> None:
    """Print today's ranked shortlist with gap reports."""
    from prospect.pipeline.digest import format_digest

    s = _settings()
    _echo(format_digest(s, _conn(s), limit=limit, min_score=min_score))


# --- applications (read by the duplicate and ghost rules) ------------------------


@app.command("log")
def log_cmd(
    posting_id: int | None = typer.Argument(None, help="Posting id (omit for a manual one)."),
    url: str | None = typer.Option(None, help="Manual posting: URL."),
    company: str | None = typer.Option(None, help="Manual posting: company."),
    title: str | None = typer.Option(None, help="Manual posting: title."),
    channel: str = typer.Option("direct", help="direct | referral | email"),
    referral: bool = typer.Option(False, "--referral"),
    resume: Path | None = typer.Option(None, "--resume", help="Résumé file actually submitted."),
    date: str | None = typer.Option(None, help="Submitted at (YYYY-MM-DD[ HH:MM]); default now."),
    notes: str | None = typer.Option(None),
    force: bool = typer.Option(False, "--force", help="Log even if already submitted."),
) -> None:
    """Record a manual application."""
    from prospect.pipeline.archive import ArchiveError, log_application

    s = _settings()
    try:
        res = log_application(
            s,
            _conn(s),
            posting_id=posting_id,
            url=url,
            company=company,
            title=title,
            channel=channel,
            referral=referral,
            resume=resume,
            submitted_at=date,
            notes=notes,
            force=force,
        )
        _echo(res.message)
    except ArchiveError as e:
        _fail(str(e))


@app.command()
def status(application_id: int, new_status: str = typer.Argument(..., metavar="STATUS")) -> None:
    """Set application status: submitted | screen | interview | offer | rejected | ghosted."""
    from prospect.pipeline.archive import ArchiveError, set_status

    s = _settings()
    try:
        set_status(_conn(s), application_id, new_status)
        _echo(f"application {application_id}: {new_status}")
    except ArchiveError as e:
        _fail(str(e))


@app.command()
def cost(since: str | None = typer.Option(None, help="YYYY-MM-DD")) -> None:
    """Spend by stage and model from the runs table."""
    from prospect.llm.pricing import cost_report

    s = _settings()
    _echo(cost_report(_conn(s), since))


@app.command()
def demo(
    record: bool = typer.Option(
        False,
        "--record",
        help="Call the real API (needs ANTHROPIC_API_KEY, ~$0.13) and re-record demo/responses/.",
    ),
    db: Path | None = typer.Option(None, help="Demo database (default: demo.db, recreated)."),
) -> None:
    """Offline end-to-end run on the fictional demo data: ingest → … → digest, no API key."""
    from prospect.demo import DemoError, run_demo

    try:
        run_demo(load_settings().root, db_path=db, record=record, echo=_echo)
    except DemoError as e:
        _fail(str(e))


# --- eval ------------------------------------------------------------------------------


@eval_app.command("label")
def eval_label(
    limit: int = typer.Option(20, help="Postings to label this session."),
    posting: str | None = typer.Option(
        None, "--posting", help="Relabel these, even if labeled (e.g. 8396,10098)."
    ),
    ids: str | None = typer.Option(
        None, "--ids", help="Label these unlabeled ones in order (from eval sample)."
    ),
) -> None:
    """Show a posting and enter gold values for the labeled fields."""
    from prospect.eval.label import label_session, parse_ids

    s = _settings()
    try:
        relabel = parse_ids(posting) if posting else None
        id_list = parse_ids(ids) if ids else None
    except ValueError as e:
        _fail(str(e))
    _echo(label_session(_conn(s), limit=limit, relabel=relabel, ids=id_list))


@eval_app.command("sample")
def eval_sample(n: int = typer.Option(25, help="How many postings.")) -> None:
    """Pick unlabeled candidates that differ as much as possible (one per company)."""
    from prospect.eval.sample import diverse_sample, format_sample

    s = _settings()
    _echo(format_sample(diverse_sample(_conn(s), n=n)))


@eval_app.command("export")
def eval_export(
    n: int = typer.Option(30, help="How many postings (a diverse sample of unlabeled ones)."),
    ids: str | None = typer.Option(None, "--ids", help="These postings instead of a sample."),
    out: Path | None = typer.Option(None, help="Default: labels/heldout-<date>.md"),
) -> None:
    """Write postings to a Markdown file to label in an editor (then `eval import`)."""
    from datetime import date

    from prospect.eval.label import parse_ids
    from prospect.eval.labelfile import LabelFileError, export_label_file
    from prospect.eval.sample import diverse_sample

    s = _settings()
    conn = _conn(s)
    try:
        id_list = parse_ids(ids) if ids else [p.id for p in diverse_sample(conn, n=n)]
    except ValueError as e:
        _fail(str(e))
    path = out or s.root / "labels" / f"heldout-{date.today().isoformat()}.md"
    try:
        count = export_label_file(conn, id_list, path)
    except LabelFileError as e:
        _fail(str(e))
    _echo(f"wrote {count} posting(s) to {path}\nfill it in, then: prospect eval import {path}")


@eval_app.command("import")
def eval_import(path: Path) -> None:
    """Save the filled `label` blocks of an exported file (re-runnable; blanks skipped)."""
    from prospect.eval.labelfile import import_label_file

    s = _settings()
    conn = _conn(s)
    if not path.exists():
        _fail(f"no such file: {path}")
    res = import_label_file(conn, path)
    total = conn.execute("SELECT COUNT(DISTINCT posting_id) AS n FROM labels").fetchone()["n"]
    _echo(res.summary(total))


@eval_app.command("extract")
def eval_extract(
    model: str = typer.Option(..., help="haiku | sonnet | opus"),
    limit: int | None = typer.Option(None),
    batch: bool = typer.Option(False, "--batch", help="Batch API (no latency numbers)."),
    label_set: Path | None = typer.Option(
        None, "--set", help="Only a held-out set: a label file written by `eval export`."
    ),
) -> None:
    """Rerun extraction on labeled postings; per-field P/R/F1, $/posting, p50 latency."""
    from prospect.eval.labelfile import label_file_ids
    from prospect.eval.metrics import run_eval_extract
    from prospect.llm.client import LLMError

    s = _settings()
    conn = _conn(s)
    ids = None
    if label_set:
        if not label_set.is_file():
            _fail(f"no such label file: {label_set}")
        ids = label_file_ids(label_set)
        if not ids:
            _fail(f"{label_set} holds no postings")
    try:
        _echo(
            run_eval_extract(
                s,
                conn,
                model_key=model,
                limit=limit,
                batch=batch,
                ctx=_decision_ctx(s),
                ids=ids,
            )
        )
    except LLMError as e:
        _fail(str(e))


@eval_app.command("report")
def eval_report(run_id: int | None = typer.Option(None, "--run", help="Default: latest.")) -> None:
    """Re-print the metrics of an eval run (default: one per model, latest)."""
    from prospect.eval.metrics import format_report

    s = _settings()
    _echo(format_report(_conn(s), run_id, ctx=_decision_ctx(s)))


def _decision_ctx(s: Settings) -> DecisionCtx:
    from prospect.eval.metrics import DecisionCtx
    from prospect.factbank.loader import effective_targets, load_factbank

    fb = load_factbank(s.factbank_path)
    return DecisionCtx(fb.identity.location.city, effective_targets(s, fb).max_years)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    app()


if __name__ == "__main__":
    main()
