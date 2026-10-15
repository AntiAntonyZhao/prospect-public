"""ingest: sources → `postings` (content hash, repost_count). No AI. Idempotent.

A posting is matched to an existing row by (source, ext_id) or content hash.
`repost_count` counts how often (company, title_norm) came back after
disappearing:
  * an existing row that was missing from a *successful* fetch of its feed and
    is now seen again → +1;
  * a brand-new row whose (company, title_norm) exists only on rows that are no
    longer live (closed and re-opened under a new id) → prior max + 1.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from prospect.config import Settings
from prospect.db import now_iso, transaction
from prospect.logs import get_logger
from prospect.models.posting import RawPosting
from prospect.progress import Progress, report, window
from prospect.sources import build_sources
from prospect.sources.base import Feed, Http, Source
from prospect.text import content_hash, html_to_text, norm_company, norm_title

log = get_logger("ingest")


@dataclass
class IngestStats:
    source: str
    feeds: int = 0
    fetched: int = 0
    new: int = 0
    seen: int = 0
    reposts: int = 0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        err = f", {len(self.errors)} feed errors" if self.errors else ""
        return (
            f"{self.source:<15} feeds={self.feeds:<4} fetched={self.fetched:<5} new={self.new:<4} "
            f"seen={self.seen:<5} reposts={self.reposts}{err}"
        )


def ensure_company(
    conn: sqlite3.Connection,
    name: str,
    ats: str = "other",
    token: str | None = None,
    careers_url: str | None = None,
) -> int:
    key = norm_company(name) or name.lower()
    row = conn.execute("SELECT id FROM companies WHERE name_norm = ?", (key,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO companies (name, name_norm, ats, board_token, careers_url) VALUES (?,?,?,?,?)",
        (name, key, ats, token, careers_url),
    )
    return int(cur.lastrowid)


def upsert_posting(
    conn: sqlite3.Connection,
    raw: RawPosting,
    now: str,
    feed_prev_ok: str | None = None,
    ats: str = "other",
    run_started: str | None = None,
) -> tuple[int, str]:
    """Insert or refresh one posting. Returns (posting_id, 'new'|'seen'|'repost').

    `now` is when this posting's feed was fetched; `run_started` is when the whole
    ingest invocation began (rows seen since then are live, whichever feed saw them).
    """
    company_id = ensure_company(conn, raw.company, ats=ats)
    desc_text = html_to_text(raw.description_html)
    h = content_hash(raw.company, raw.title, desc_text or raw.url)
    title_norm = norm_title(raw.title) or raw.title.lower()
    raw_json = raw.model_dump_json()
    posted = raw.posted_at.isoformat() if raw.posted_at else None

    row = (
        conn.execute(
            "SELECT * FROM postings WHERE source = ? AND ext_id = ?", (raw.source, raw.ext_id)
        ).fetchone()
        or conn.execute("SELECT * FROM postings WHERE hash = ?", (h,)).fetchone()
    )

    if row:
        same_source = row["source"] == raw.source and row["ext_id"] == raw.ext_id
        # Only this feed can say the posting was gone: a hash match through another
        # source (list entry hydrated from the same ATS job) is a duplicate, not a repost.
        reappeared = bool(same_source and feed_prev_ok and row["last_seen"] < feed_prev_ok)
        repost_count = row["repost_count"] + (1 if reappeared else 0)
        hash_free = not conn.execute(
            "SELECT 1 FROM postings WHERE hash = ? AND id != ?", (h, row["id"])
        ).fetchone()
        sibling = row["source"] == raw.source and row["ext_id"] != raw.ext_id
        if sibling and raw.location_raw:
            # Same description posted for another city: keep one row, but keep every
            # location, and give a location-rejected row another pre-pass.
            merged = _merge_locations(row["location_raw"], raw.location_raw)
            if merged != row["location_raw"]:
                reopen = row["status"] == "rejected" and row["reject_reason"] == "location_metadata"
                conn.execute(
                    """UPDATE postings SET location_raw=?,
                           status=CASE WHEN ? THEN 'new' ELSE status END,
                           reject_reason=CASE WHEN ? THEN NULL ELSE reject_reason END
                       WHERE id=?""",
                    (merged, reopen, reopen, row["id"]),
                )
        if same_source and h != row["hash"] and hash_free:
            # The source edited the posting: keep the row, refresh its content.
            conn.execute(
                """UPDATE postings SET hash=?, title=?, title_norm=?, url=?, location_raw=?,
                   posted_at=COALESCE(?, posted_at), raw_json=?, last_seen=?, repost_count=?
                   WHERE id=?""",
                (
                    h,
                    raw.title,
                    title_norm,
                    raw.url,
                    raw.location_raw,
                    posted,
                    raw_json,
                    now,
                    repost_count,
                    row["id"],
                ),
            )
        else:
            conn.execute(
                "UPDATE postings SET last_seen=?, repost_count=? WHERE id=?",
                (now, repost_count, row["id"]),
            )
        if row["status"] == "ignored" and row["reject_reason"] == "closed":
            # Parked by rank as closed, now live again: back through the pre-pass.
            conn.execute(
                "UPDATE postings SET status='new', reject_reason=NULL WHERE id=?", (row["id"],)
            )
        return row["id"], "repost" if reappeared else "seen"

    prior = conn.execute(
        """SELECT MAX(repost_count) AS m, COUNT(*) AS n FROM postings
           WHERE company_id = ? AND title_norm = ? AND last_seen < ?""",
        (company_id, title_norm, run_started or now),
    ).fetchone()
    repost_count = (prior["m"] + 1) if prior["n"] else 0
    cur = conn.execute(
        """INSERT INTO postings (company_id, source, ext_id, hash, title, title_norm, url,
               location_raw, posted_at, first_seen, last_seen, repost_count, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            company_id,
            raw.source,
            raw.ext_id,
            h,
            raw.title,
            title_norm,
            raw.url,
            raw.location_raw,
            posted,
            now,
            now,
            repost_count,
            raw_json,
        ),
    )
    return int(cur.lastrowid), "repost" if repost_count else "new"


def _merge_locations(old: str | None, new: str) -> str:
    parts = [p.strip() for p in (old or "").split(";") if p.strip()]
    for p in new.split(";"):
        if p.strip() and p.strip().lower() not in {x.lower() for x in parts}:
            parts.append(p.strip())
    return "; ".join(parts)


def _feed_prev_ok(conn: sqlite3.Connection, feed_key: str, before: str) -> str | None:
    row = conn.execute(
        """SELECT MAX(fetched_at) AS t FROM fetch_log
           WHERE feed_key = ? AND ok = 1 AND fetched_at < ?""",
        (feed_key, before),
    ).fetchone()
    return row["t"] if row else None


def ingest_source(
    conn: sqlite3.Connection,
    source: Source,
    run_started: str | None = None,
    feeds: list[Feed] | None = None,
    progress: Progress | None = None,
) -> IngestStats:
    """`feeds`: the source's feeds when the caller already listed them (to weigh progress
    by board count); listing them makes no network call."""
    stats = IngestStats(source=source.name)
    run_started = run_started or now_iso()
    ats = {"github_newgrad": "github"}.get(source.name, source.name)
    feeds = feeds if feeds is not None else list(source.feeds())
    for i, feed in enumerate(feeds):
        report(progress, i / len(feeds), f"{feed.key} ({i + 1}/{len(feeds)})")
        stats.feeds += 1
        started = now_iso()
        try:
            postings = feed.fetch()
        except Exception as e:
            msg = f"{feed.key}: {type(e).__name__}: {str(e)[:160]}"
            stats.errors.append(msg)
            log.warning("feed_failed", feed=feed.key, error=msg)
            with transaction(conn):
                conn.execute(
                    """INSERT INTO fetch_log (source, feed_key, fetched_at, ok, error)
                       VALUES (?,?,?,0,?)""",
                    (source.name, feed.key, started, msg),
                )
            continue
        prev_ok = _feed_prev_ok(conn, feed.key, started)
        with transaction(conn):
            for raw in postings:
                pid, outcome = upsert_posting(
                    conn, raw, started, prev_ok, ats=ats, run_started=run_started
                )
                stats.fetched += 1
                if outcome == "new":
                    stats.new += 1
                    log.info(
                        "posting",
                        id=pid,
                        outcome=outcome,
                        company=raw.company,
                        title=raw.title[:60],
                    )
                elif outcome == "repost":
                    stats.reposts += 1
                    log.info(
                        "posting",
                        id=pid,
                        outcome=outcome,
                        company=raw.company,
                        title=raw.title[:60],
                    )
                else:
                    stats.seen += 1
            conn.execute(
                """INSERT INTO fetch_log (source, feed_key, fetched_at, ok, n_postings)
                   VALUES (?,?,?,1,?)""",
                (source.name, feed.key, started, len(postings)),
            )
    return stats


def run_ingest(
    settings: Settings,
    conn: sqlite3.Connection,
    only: list[str] | None = None,
    sources: list[Source] | None = None,
    progress: Progress | None = None,
) -> list[IngestStats]:
    http = Http(delay_s=settings.sources.request_delay_s)
    try:
        srcs = sources if sources is not None else build_sources(settings, http, only)
        results = []
        run_started = now_iso()
        # The bar moves per board, so a source counts by how many boards it has.
        plan = [(src, list(src.feeds())) for src in srcs]
        total = max(1, sum(len(feeds) for _, feeds in plan))
        done = 0
        for src, feeds in plan:
            started = now_iso()
            stats = ingest_source(
                conn,
                src,
                run_started,
                feeds=feeds,
                progress=window(progress, done / total, (done + len(feeds)) / total),
            )
            done += len(feeds)
            with transaction(conn):
                conn.execute(
                    """INSERT INTO runs (stage, started_at, finished_at, n_in, n_out, error)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        f"ingest:{src.name}",
                        started,
                        now_iso(),
                        stats.fetched,
                        stats.new,
                        json.dumps(stats.errors) if stats.errors else None,
                    ),
                )
            results.append(stats)
        return results
    finally:
        http.close()
