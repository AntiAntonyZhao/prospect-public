"""`prospect companies check`: open every board URL in companies.yaml and report.

CLAUDE.md: "verify every token by opening the URL". A token that returns zero
jobs is not necessarily wrong (a board can be empty), but one that errors is.
"""

from __future__ import annotations

from prospect.config import Settings
from prospect.pipeline.filter import classify_location_raw
from prospect.sources.ashby import Ashby
from prospect.sources.base import Company, Http, load_companies
from prospect.sources.greenhouse import Greenhouse
from prospect.sources.lever import Lever

BOARD_URL = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
}


def fetch_board(company: Company, http: Http) -> list:
    if company.ats == "greenhouse":
        return Greenhouse([company], http).fetch_board(company)
    if company.ats == "lever":
        return Lever([company], http).fetch_board(company)
    return Ashby([company], http).fetch_board(company)


def check_companies(settings: Settings, name: str | None = None) -> str:
    companies = load_companies(settings.companies_path)
    if name:
        key = name.lower()
        companies = [c for c in companies if key in (c.name.lower(), c.token.lower())]
    if not companies:
        return f"no companies matched in {settings.companies_path.name}"
    http = Http(delay_s=settings.sources.request_delay_s)
    lines = [f"{'':4}{'company':<28}{'board':<34}{'jobs':>6}{'canada':>8}"]
    bad = 0
    try:
        for c in companies:
            board = f"{c.ats}:{c.token}"
            try:
                jobs = fetch_board(c, http)
            except Exception as e:
                bad += 1
                lines.append(f"FAIL{c.name:<28}{board:<34}  {type(e).__name__}: {str(e)[:60]}")
                continue
            ca = sum(classify_location_raw(j.location_raw) == "canada" for j in jobs)
            mark = "ok  " if jobs else "0?  "
            lines.append(f"{mark}{c.name:<28}{board:<34}{len(jobs):>6}{ca:>8}")
    finally:
        http.close()
    lines.append(f"{len(companies) - bad}/{len(companies)} boards answered")
    return "\n".join(lines)
