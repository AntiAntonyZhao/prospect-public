"""Ashby public Job Posting API.

Justification: Ashby documents `api.ashbyhq.com/posting-api/job-board/{name}` as a
public, unauthenticated API for embedding a company's job board elsewhere;
read-only GET, one call per board.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from prospect.models.posting import RawPosting
from prospect.sources.base import Company, Feed, Http, Source

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class Ashby(Source):
    name = "ashby"
    terms_url = "https://developers.ashbyhq.com/docs/public-job-posting-api"

    def __init__(self, companies: list[Company], http: Http) -> None:
        self.companies = [c for c in companies if c.ats == "ashby"]
        self.http = http

    def feeds(self) -> Iterable[Feed]:
        for c in self.companies:
            yield Feed(key=f"ashby:{c.token}", fetch=lambda c=c: self.fetch_board(c))

    def fetch_board(self, company: Company) -> list[RawPosting]:
        data = self.http.get_json(API.format(token=company.token))
        return [parse_job(j, company.name) for j in data.get("jobs", []) if j.get("isListed", True)]


def parse_job(j: dict, company: str) -> RawPosting:
    locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
    location_raw = "; ".join(dict.fromkeys(x for x in locs if x))
    wt = j.get("workplaceType") or ("Remote" if j.get("isRemote") else None)
    if wt:
        location_raw = f"{location_raw} ({wt})" if location_raw else wt
    raw = {k: v for k, v in j.items() if k not in ("descriptionHtml", "descriptionPlain")}
    published = j.get("publishedAt")
    return RawPosting(
        source="ashby",
        ext_id=j["id"],
        company=company,
        title=j["title"].strip(),
        url=j.get("jobUrl") or j.get("applyUrl") or "",
        location_raw=location_raw or None,
        description_html=j.get("descriptionHtml") or "",
        posted_at=datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None,
        raw=raw,
    )
