"""Greenhouse Job Board API.

Justification: Greenhouse documents `boards-api.greenhouse.io/v1/boards/{token}/jobs`
as a public, unauthenticated API intended for publishing a company's open jobs
on other sites; no scraping, no login, one GET per board.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import datetime

from prospect.models.posting import RawPosting
from prospect.sources.base import Company, Feed, Http, Source

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
JOB_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"


class Greenhouse(Source):
    name = "greenhouse"
    terms_url = "https://developers.greenhouse.io/job-board.html"

    def __init__(self, companies: list[Company], http: Http) -> None:
        self.companies = [c for c in companies if c.ats == "greenhouse"]
        self.http = http

    def feeds(self) -> Iterable[Feed]:
        for c in self.companies:
            yield Feed(key=f"greenhouse:{c.token}", fetch=lambda c=c: self.fetch_board(c))

    def fetch_board(self, company: Company) -> list[RawPosting]:
        data = self.http.get_json(API.format(token=company.token), params={"content": "true"})
        return [parse_job(j, company.name) for j in data.get("jobs", [])]

    def fetch_job(self, token: str, job_id: str, company: str) -> RawPosting:
        data = self.http.get_json(JOB_API.format(token=token, job_id=job_id))
        return parse_job(data, company)


def parse_job(j: dict, company: str) -> RawPosting:
    loc = (j.get("location") or {}).get("name")
    offices = [o.get("name") for o in j.get("offices") or [] if o.get("name")]
    location_raw = "; ".join(dict.fromkeys([x for x in [loc, *offices] if x])) or None
    raw = {k: v for k, v in j.items() if k != "content"}
    return RawPosting(
        source="greenhouse",
        ext_id=str(j["id"]),
        company=j.get("company_name") or company,
        title=j["title"].strip(),
        url=j["absolute_url"],
        location_raw=location_raw,
        description_html=html.unescape(j.get("content") or ""),
        posted_at=_dt(j.get("first_published") or j.get("updated_at")),
        raw=raw,
    )


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
