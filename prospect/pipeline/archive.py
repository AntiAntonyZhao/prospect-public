"""archive: the applications table.

`prospect log` records a manual application and `prospect status` updates it.
The filter reads these rows for its duplicate-application reject and the
ghost flag, and score reads them for the no-response penalty. Nothing here
calls an LLM.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from prospect.config import Settings
from prospect.db import now_iso, transaction
from prospect.factbank.version import blob_hash
from prospect.models.posting import RawPosting
from prospect.pipeline.ingest import upsert_posting

CHANNELS = ("direct", "referral", "email")
APP_STATUSES = ("submitted", "screen", "interview", "offer", "rejected", "ghosted")


class ArchiveError(RuntimeError):
    pass


@dataclass
class LogResult:
    application_id: int
    posting_id: int
    created: bool
    message: str


def log_application(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    posting_id: int | None = None,
    url: str | None = None,
    company: str | None = None,
    title: str | None = None,
    channel: str = "direct",
    referral: bool = False,
    resume: Path | None = None,
    submitted_at: str | None = None,
    notes: str | None = None,
    force: bool = False,
) -> LogResult:
    if channel not in CHANNELS:
        raise ArchiveError(f"channel must be one of {CHANNELS}")
    referral = referral or channel == "referral"
    when = submitted_at or now_iso()
    fb_version = blob_hash(settings.factbank_path)
    resume_path = str(resume.resolve()) if resume else None

    with transaction(conn):
        if posting_id is None:
            if not (url and company and title):
                raise ArchiveError("give a POSTING_ID, or all of --url, --company and --title")
            existing = conn.execute("SELECT id FROM postings WHERE url = ?", (url,)).fetchone()
            if existing:
                posting_id = existing["id"]
            else:
                raw = RawPosting(source="manual", ext_id=url, company=company, title=title, url=url)
                posting_id, _ = upsert_posting(conn, raw, now_iso())
        elif not conn.execute("SELECT 1 FROM postings WHERE id = ?", (posting_id,)).fetchone():
            raise ArchiveError(f"no posting {posting_id}")

        apps = conn.execute(
            "SELECT * FROM applications WHERE posting_id = ? ORDER BY id DESC", (posting_id,)
        ).fetchall()
        submitted = [a for a in apps if a["submitted_at"]]
        if submitted and not force:
            raise ArchiveError(
                f"posting {posting_id} already has submitted application {submitted[0]['id']} "
                "(use --force to log another)"
            )
        cur = conn.execute(
            """INSERT INTO applications (posting_id, resume_path, factbank_version,
                   submitted_at, channel, referral, status, status_at, notes)
               VALUES (?,?,?,?,?,?, 'submitted', ?, ?)""",
            (posting_id, resume_path, fb_version, when, channel, int(referral), when, notes),
        )
        app_id = int(cur.lastrowid)
        conn.execute("UPDATE postings SET status='applied' WHERE id=?", (posting_id,))

    return LogResult(
        app_id, posting_id, True, f"logged application {app_id} (posting {posting_id})"
    )


def set_status(conn: sqlite3.Connection, application_id: int, status: str) -> None:
    if status not in APP_STATUSES:
        raise ArchiveError(f"status must be one of {APP_STATUSES}")
    with transaction(conn):
        cur = conn.execute(
            "UPDATE applications SET status=?, status_at=? WHERE id=?",
            (status, now_iso(), application_id),
        )
        if not cur.rowcount:
            raise ArchiveError(f"no application {application_id}")
