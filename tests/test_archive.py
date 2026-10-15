from __future__ import annotations

import pytest

from prospect.pipeline.archive import ArchiveError, log_application, set_status
from tests.conftest import add_posting


def test_log_manual_application(settings, conn):
    res = log_application(
        settings,
        conn,
        url="https://x/1",
        company="Acme",
        title="Backend Developer",
        channel="email",
    )
    app = conn.execute("SELECT * FROM applications").fetchone()
    assert res.created and app["channel"] == "email" and app["status"] == "submitted"
    assert len(app["factbank_version"]) == 40
    status = conn.execute("SELECT status, source FROM postings").fetchone()
    assert tuple(status) == ("applied", "manual")
    with pytest.raises(ArchiveError, match="already has submitted"):
        log_application(settings, conn, posting_id=res.posting_id)
    again = log_application(
        settings,
        conn,
        url="https://x/1",
        company="Acme",
        title="Backend Developer",
        force=True,
    )
    assert again.posting_id == res.posting_id  # same URL → same posting


def test_log_an_ingested_posting(settings, conn):
    pid = add_posting(conn)
    res = log_application(settings, conn, posting_id=pid, referral=True, resume=None)
    app = conn.execute("SELECT * FROM applications").fetchone()
    assert res.posting_id == pid and app["referral"] == 1 and app["status"] == "submitted"
    with pytest.raises(ArchiveError, match="no posting"):
        log_application(settings, conn, posting_id=pid + 100)


def test_log_validation(settings, conn):
    with pytest.raises(ArchiveError, match="channel"):
        log_application(settings, conn, url="u", company="c", title="t", channel="carrier pigeon")
    with pytest.raises(ArchiveError, match="--url"):
        log_application(settings, conn)


def test_status_values(settings, conn):
    res = log_application(settings, conn, url="https://x/2", company="Acme", title="Dev")
    set_status(conn, res.application_id, "screen")
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "screen"
    with pytest.raises(ArchiveError):
        set_status(conn, res.application_id, "hired!!")
    with pytest.raises(ArchiveError, match="no application"):
        set_status(conn, 999, "screen")
