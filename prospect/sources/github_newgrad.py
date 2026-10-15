"""Community-maintained new-grad list on GitHub (SimplifyJobs/New-Grad-Positions).

Justification: a public repository published for job seekers to consume; we
read its structured `listings.json` once per run through raw.githubusercontent
(ordinary public-repo access under GitHub's terms, equivalent to `git pull`).
Listings carry no description, so when the apply URL is a Greenhouse, Lever or
Ashby job page the description is hydrated from that ATS's public API (same
endpoints as the board adapters). Other ATSs (Workday, Oracle…) are never
scraped; those postings keep title/location only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime

from prospect.logs import get_logger
from prospect.models.posting import RawPosting
from prospect.sources import ashby, greenhouse, lever
from prospect.sources.base import Feed, Http, Source

DEFAULT_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
CANADA = re.compile(
    r"\bcanada\b|, (?:on|qc|bc|ab|mb|sk|ns|nb|nl|pe)\b|toronto|montr[eé]al|vancouver|ottawa|"
    r"waterloo|calgary|qu[eé]bec",
    re.I,
)
_GH = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)/jobs/(\d+)")
_LEVER = re.compile(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})")
_ASHBY = re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})")

log = get_logger("ingest")


class GitHubNewGrad(Source):
    name = "github_newgrad"
    terms_url = "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service"

    def __init__(self, http: Http, url: str = DEFAULT_URL, hydrate: bool = True) -> None:
        self.http = http
        self.url = url
        self.hydrate = hydrate
        self._ashby_boards: dict[str, dict[str, dict]] = {}

    def feeds(self) -> Iterable[Feed]:
        yield Feed(key="github_newgrad", fetch=self.fetch_list)

    def fetch_list(self) -> list[RawPosting]:
        listings = self.http.get_json(self.url)
        out: list[RawPosting] = []
        for item in listings:
            if not item.get("active") or not item.get("is_visible", True):
                continue
            locations = item.get("locations") or []
            # Canada-relevant only; "Remote" alone stays (scope unknown until extract).
            if not any(CANADA.search(loc) or loc.strip().lower() == "remote" for loc in locations):
                continue
            out.append(self._to_raw(item, locations))
        return out

    def _to_raw(self, item: dict, locations: list[str]) -> RawPosting:
        posted = item.get("date_posted")
        base = RawPosting(
            source=self.name,
            ext_id=str(item["id"]),
            company=item["company_name"].strip(),
            title=item["title"].strip(),
            url=item["url"],
            location_raw="; ".join(locations) or None,
            description_html="",
            posted_at=datetime.fromtimestamp(posted, tz=UTC) if posted else None,
            raw=item,
        )
        if not self.hydrate:
            return base
        try:
            desc = self._hydrate(item["url"], base.company)
        except Exception as e:  # a dead job page must not sink the whole list
            log.info("hydrate_failed", url=item["url"], error=str(e)[:120])
            desc = None
        return base.model_copy(update={"description_html": desc}) if desc else base

    def _hydrate(self, url: str, company: str) -> str | None:
        if "eu.greenhouse.io" in url:
            return None
        if m := _GH.search(url):
            gh = greenhouse.Greenhouse([], self.http)
            return gh.fetch_job(m.group(1), m.group(2), company).description_html
        if m := _LEVER.search(url):
            lv = lever.Lever([], self.http)
            return lv.fetch_posting(m.group(1), m.group(2), company).description_html
        if m := _ASHBY.search(url):
            board, job_id = m.group(1), m.group(2)
            if board not in self._ashby_boards:
                data = self.http.get_json(ashby.API.format(token=board))
                self._ashby_boards[board] = {j["id"]: j for j in data.get("jobs", [])}
            job = self._ashby_boards[board].get(job_id)
            return job.get("descriptionHtml") if job else None
        return None
