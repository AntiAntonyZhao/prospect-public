"""Source adapters against canned payloads (httpx.MockTransport) + the daily orchestrator."""

from __future__ import annotations

import json

import httpx
import pytest

from prospect.pipeline import run as run_mod
from prospect.sources.ashby import Ashby
from prospect.sources.base import Company, Http, load_companies
from prospect.sources.github_newgrad import GitHubNewGrad
from prospect.sources.greenhouse import Greenhouse
from prospect.sources.lever import Lever

GH_JOB = {
    "id": 8172508,
    "title": " Software Engineer, New Grad ",
    "company_name": "Acme",
    "absolute_url": "https://example.com/jobs/search?gh_jid=8172508",
    "location": {"name": "Toronto"},
    "offices": [{"name": "Toronto, Canada"}],
    "first_published": "2026-09-03T13:32:53-04:00",
    "updated_at": "2026-09-10T13:11:58-04:00",
    "content": "&lt;p&gt;Build payments in &lt;b&gt;Java&lt;/b&gt;.&lt;/p&gt;",
}
LEVER_POSTING = {
    "id": "0f3c2e1e-dd90-4fda-bfae-28e6d24aaaaa",
    "text": "Backend Developer",
    "hostedUrl": "https://jobs.lever.co/globex/0f3c",
    "createdAt": 1790000000000,
    "categories": {"location": "Toronto, Ontario", "allLocations": ["Toronto, Ontario"]},
    "workplaceType": "hybrid",
    "description": "<p>Intro</p>",
    "lists": [{"text": "Requirements", "content": "<li>Python</li>"}],
    "additional": "<p>EEO</p>",
}
ASHBY_JOB = {
    "id": "7133d205-5421-4336-a8e2-6037b5f55073",
    "title": "Full-Stack Engineer",
    "location": "Toronto",
    "secondaryLocations": [{"location": "Canada"}],
    "isRemote": True,
    "workplaceType": "Remote",
    "isListed": True,
    "publishedAt": "2026-09-15T18:21:37.401+00:00",
    "jobUrl": "https://jobs.ashbyhq.com/initech/7133d205",
    "descriptionHtml": "<p>React</p>",
}


def transport(routes: dict[str, object]) -> Http:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, payload in routes.items():
            if str(request.url).startswith(prefix):
                if isinstance(payload, int):
                    return httpx.Response(payload)
                return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return Http(delay_s=0, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_greenhouse_parse():
    http = transport({"https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [GH_JOB]}})
    [p] = Greenhouse([Company(name="Acme", ats="greenhouse", token="acme")], http).fetch_board(
        Company(name="Acme", ats="greenhouse", token="acme")
    )
    assert p.title == "Software Engineer, New Grad" and p.ext_id == "8172508"
    assert p.location_raw == "Toronto; Toronto, Canada"
    assert "<b>Java</b>" in p.description_html  # entity-escaped content unescaped
    assert p.posted_at.year == 2026 and "content" not in p.raw


def test_lever_parse():
    http = transport({"https://api.lever.co/v0/postings/globex": [LEVER_POSTING]})
    c = Company(name="Globex", ats="lever", token="globex")
    [p] = Lever([c], http).fetch_board(c)
    assert p.location_raw == "Toronto, Ontario (hybrid)"
    assert "<h3>Requirements</h3>" in p.description_html and "EEO" in p.description_html


def test_ashby_parse_skips_unlisted():
    hidden = {**ASHBY_JOB, "id": "x", "isListed": False}
    http = transport(
        {"https://api.ashbyhq.com/posting-api/job-board/initech": {"jobs": [ASHBY_JOB, hidden]}}
    )
    c = Company(name="Initech", ats="ashby", token="initech")
    [p] = Ashby([c], http).fetch_board(c)
    assert p.location_raw == "Toronto; Canada (Remote)"


def test_github_list_filters_to_canada_and_hydrates():
    listings = [
        {
            "id": "1",
            "company_name": "Acme",
            "title": "SWE",
            "active": True,
            "is_visible": True,
            "locations": ["Toronto, ON, Canada"],
            "date_posted": 1790000000,
            "url": "https://job-boards.greenhouse.io/acme/jobs/8172508",
        },
        {
            "id": "2",
            "company_name": "US Co",
            "title": "SWE",
            "active": True,
            "locations": ["Austin, TX"],
            "url": "https://x",
        },
        {
            "id": "3",
            "company_name": "Old",
            "title": "SWE",
            "active": False,
            "locations": ["Toronto, ON"],
            "url": "https://x",
        },
        {
            "id": "4",
            "company_name": "Workday Co",
            "title": "SWE",
            "active": True,
            "locations": ["Remote"],
            "url": "https://wd5.myworkdayjobs.com/x",
        },
    ]
    http = transport(
        {
            "https://raw.githubusercontent.com/": listings,
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/8172508": GH_JOB,
        }
    )
    posts = GitHubNewGrad(http).fetch_list()
    assert [p.ext_id for p in posts] == ["1", "4"]
    assert "Java" in posts[0].description_html  # hydrated through the public ATS API
    assert posts[1].description_html == ""  # Workday is never scraped


def test_http_retries_then_raises():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    http = Http(delay_s=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    import prospect.sources.base as base

    orig = base.time.sleep
    base.time.sleep = lambda s: None
    try:
        with pytest.raises(httpx.HTTPStatusError):
            http.get_json("https://x")
    finally:
        base.time.sleep = orig
    assert calls["n"] == 3


def test_load_companies(tmp_path):
    p = tmp_path / "companies.yaml"
    p.write_text("companies:\n  - {name: Acme, ats: ashby, token: acme}\n", encoding="utf-8")
    [c] = load_companies(p)
    assert (c.name, c.ats, c.token) == ("Acme", "ashby", "acme")
    assert load_companies(tmp_path / "missing.yaml") == []


def test_run_daily_sequences_the_stages(settings, conn, monkeypatch):
    calls = []

    class Stub:
        def __init__(self, name):
            self.name = name

        def summary(self):
            return f"{self.name} ok"

        def line(self):
            return f"{self.name} line"

    monkeypatch.setattr(
        run_mod,
        "run_ingest",
        lambda s, c, progress: calls.append("ingest") or [Stub("ingest")],
    )
    monkeypatch.setattr(
        run_mod, "prepass", lambda s, c, f, progress: calls.append("prepass") or Stub("prepass")
    )
    monkeypatch.setattr(
        run_mod, "run_rank", lambda s, c, f, progress: calls.append("rank") or Stub("rank")
    )
    monkeypatch.setattr(
        run_mod,
        "run_extract",
        lambda s, c, mode, client, progress: calls.append(f"extract:{mode}") or "extract ok",
    )
    monkeypatch.setattr(
        run_mod, "full_pass", lambda s, c, f, progress: calls.append("full") or Stub("full")
    )
    monkeypatch.setattr(
        run_mod,
        "run_score",
        lambda s, c, f, client, progress: calls.append("score") or "score ok",
    )
    seen: list[tuple[float | None, str]] = []
    out = run_mod.run_daily(settings, conn, progress=lambda f, m: seen.append((f, m)))
    assert calls == ["ingest", "prepass", "rank", "extract:batch", "full", "score"]
    fractions = [f for f, _ in seen if f is not None]
    assert fractions == sorted(fractions)  # the bar only moves forward across the stages
    assert "spent today: $0.0000" in out and "No scored candidates yet" in out


def test_run_daily_skips_score_when_the_full_filter_cannot_run(settings, conn, monkeypatch):
    from prospect.pipeline.filter import FilterError

    class Stub:
        def summary(self):
            return "ok"

    monkeypatch.setattr(run_mod, "prepass", lambda *a, **k: Stub())
    monkeypatch.setattr(run_mod, "run_rank", lambda *a, **k: Stub())  # never load the real model
    monkeypatch.setattr(run_mod, "run_extract", lambda *a, **k: "extract ok")

    def boom(*a, **k):
        raise FilterError("work_authorization is not set")

    monkeypatch.setattr(run_mod, "full_pass", boom)
    monkeypatch.setattr(run_mod, "run_score", lambda *a, **k: pytest.fail("scored unfiltered"))
    out = run_mod.run_daily(settings, conn, skip_ingest=True)
    assert "full filter NOT run" in out and "score skipped" in out


def test_companies_check(settings, monkeypatch):
    from prospect.pipeline import companies

    (settings.root / "companies.yaml").write_text(
        "companies:\n  - {name: Acme, ats: greenhouse, token: acme}\n"
        "  - {name: Gone, ats: lever, token: gone}\n",
        encoding="utf-8",
    )
    http = transport(
        {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [GH_JOB]},
            "https://api.lever.co/v0/postings/gone": 404,
        }
    )
    monkeypatch.setattr(companies, "Http", lambda delay_s: http)
    out = companies.check_companies(settings)
    assert "ok  Acme" in out and "FAIL" in out and "1/2 boards answered" in out
    assert json.dumps(out)  # printable
