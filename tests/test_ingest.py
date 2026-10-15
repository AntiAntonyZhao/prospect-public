from __future__ import annotations

from prospect.models.posting import RawPosting
from prospect.pipeline.ingest import ingest_source, upsert_posting
from prospect.sources.base import Feed, Source


def raw(
    ext_id: str,
    title: str = "Software Engineer",
    loc: str = "Toronto, ON",
    desc: str = "<p>Build APIs in Python.</p>",
    source: str = "greenhouse",
    company: str = "Acme",
) -> RawPosting:
    return RawPosting(
        source=source,
        ext_id=ext_id,
        company=company,
        title=title,
        url=f"https://x/{ext_id}",
        location_raw=loc,
        description_html=desc,
    )


class ListSource(Source):
    name = "greenhouse"
    terms_url = "https://developers.greenhouse.io/job-board.html"

    def __init__(self, batches: list[list[RawPosting] | Exception]) -> None:
        self.batches = batches

    def feeds(self):
        def fetch():
            item = self.batches.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        yield Feed("greenhouse:acme", fetch)


def count(conn, sql="SELECT COUNT(*) FROM postings"):
    return conn.execute(sql).fetchone()[0]


def test_upsert_is_idempotent(conn):
    upsert_posting(conn, raw("1"), "2026-09-01 00:00:00")
    upsert_posting(conn, raw("1"), "2026-09-02 00:00:00")
    conn.commit()
    assert count(conn) == 1
    assert count(conn, "SELECT last_seen FROM postings") == "2026-09-02 00:00:00"


def test_repost_after_disappearing_from_a_successful_fetch(conn, monkeypatch):
    times = iter(
        [
            "2026-09-01 07:00:00",
            "2026-09-02 07:00:00",
            "2026-09-03 07:00:00",
            "2026-09-03 07:00:01",
            "2026-09-04 07:00:00",
            "2026-09-04 07:00:01",
        ]
    )
    monkeypatch.setattr("prospect.pipeline.ingest.now_iso", lambda: next(times))
    src = ListSource([[raw("1")], [], [raw("1")]])
    ingest_source(conn, src, "2026-09-01 07:00:00")  # seen
    ingest_source(conn, src, "2026-09-02 07:00:00")  # gone (successful, empty)
    stats = ingest_source(conn, src, "2026-09-03 07:00:00")  # back
    assert stats.reposts == 1
    assert count(conn, "SELECT repost_count FROM postings") == 1


def test_failed_fetch_does_not_create_a_repost(conn, monkeypatch):
    times = iter(["2026-09-01 07:00:00", "2026-09-02 07:00:00", "2026-09-03 07:00:00"])
    monkeypatch.setattr("prospect.pipeline.ingest.now_iso", lambda: next(times))
    src = ListSource([[raw("1")], RuntimeError("503"), [raw("1")]])
    ingest_source(conn, src, "2026-09-01 07:00:00")
    failed = ingest_source(conn, src, "2026-09-02 07:00:00")
    assert failed.errors
    stats = ingest_source(conn, src, "2026-09-03 07:00:00")
    assert stats.reposts == 0


def test_new_req_id_for_a_closed_role_counts_as_repost(conn):
    upsert_posting(conn, raw("1"), "2026-09-01 00:00:00")
    pid, outcome = upsert_posting(
        conn,
        raw("2", desc="<p>Build APIs in Python. Apply now!</p>"),
        "2026-09-10 00:00:00",
        run_started="2026-09-10 00:00:00",
    )
    assert outcome == "repost"
    assert conn.execute("SELECT repost_count FROM postings WHERE id=?", (pid,)).fetchone()[0] == 1


def test_sibling_city_merges_location_and_reopens(conn):
    pid, _ = upsert_posting(conn, raw("1", loc="New York, NY"), "2026-09-01 00:00:00")
    conn.execute("UPDATE postings SET status='rejected', reject_reason='location_metadata'")
    same_desc_other_city = raw("2", loc="Toronto, ON")
    pid2, outcome = upsert_posting(conn, same_desc_other_city, "2026-09-01 00:00:00")
    row = conn.execute("SELECT location_raw, status FROM postings WHERE id=?", (pid,)).fetchone()
    assert pid2 == pid and outcome == "seen"
    assert row["location_raw"] == "New York, NY; Toronto, ON"
    assert row["status"] == "new"


def test_cross_source_duplicate_is_not_a_repost(conn):
    upsert_posting(conn, raw("1"), "2026-09-01 00:00:00")
    _, outcome = upsert_posting(
        conn,
        raw("gh-1", source="github_newgrad"),
        "2026-09-05 00:00:00",
        feed_prev_ok="2026-09-04 00:00:00",
    )
    assert outcome == "seen"
    assert count(conn) == 1


class BoardsSource(Source):
    """One source with several boards, each returning one posting."""

    terms_url = "https://example.com/terms"

    def __init__(self, name: str, boards: list[str]) -> None:
        self.name = name
        self.boards = boards

    def feeds(self):
        for b in self.boards:
            yield Feed(f"{self.name}:{b}", lambda b=b: [raw(b, company=b, source=self.name)])


def test_ingest_progress_moves_per_board_weighted_by_source(conn, settings):
    """The pull's bar: a source with 3 boards takes 3/4 of it, one with 1 board 1/4."""
    from prospect.pipeline.ingest import run_ingest

    seen = []
    sources = [BoardsSource("greenhouse", ["a", "b", "c"]), BoardsSource("lever", ["d"])]
    run_ingest(settings, conn, sources=sources, progress=lambda f, m: seen.append((f, m)))
    assert [m for _, m in seen] == [
        "greenhouse:a (1/3)",
        "greenhouse:b (2/3)",
        "greenhouse:c (3/3)",
        "lever:d (1/1)",
    ]
    assert [round(f, 2) for f, _ in seen] == [0.0, 0.25, 0.5, 0.75]
    assert count(conn) == 4
