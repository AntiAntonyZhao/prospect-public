"""Lever Postings API.

Justification: Lever documents `api.lever.co/v0/postings/{site}` as a public,
unauthenticated API for companies to list their published postings on other
sites; read-only GET, one call per board.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from prospect.models.posting import RawPosting
from prospect.sources.base import Company, Feed, Http, Source

API = "https://api.lever.co/v0/postings/{token}"
API_EU = "https://api.eu.lever.co/v0/postings/{token}"


class Lever(Source):
    name = "lever"
    terms_url = "https://github.com/lever/postings-api"

    def __init__(self, companies: list[Company], http: Http) -> None:
        self.companies = [c for c in companies if c.ats == "lever"]
        self.http = http

    def feeds(self) -> Iterable[Feed]:
        for c in self.companies:
            yield Feed(key=f"lever:{c.token}", fetch=lambda c=c: self.fetch_board(c))

    def fetch_board(self, company: Company) -> list[RawPosting]:
        base = API_EU if getattr(company, "region", None) == "eu" else API
        data = self.http.get_json(base.format(token=company.token), params={"mode": "json"})
        return [parse_posting(p, company.name) for p in data]

    def fetch_posting(self, token: str, posting_id: str, company: str) -> RawPosting:
        data = self.http.get_json(
            f"{API.format(token=token)}/{posting_id}", params={"mode": "json"}
        )
        return parse_posting(data, company)


def parse_posting(p: dict, company: str) -> RawPosting:
    cats = p.get("categories") or {}
    locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
    wt = p.get("workplaceType")
    location_raw = "; ".join(locs)
    if wt and wt != "unspecified":
        location_raw = f"{location_raw} ({wt})" if location_raw else wt
    parts = [p.get("description") or ""]
    for lst in p.get("lists") or []:
        parts.append(f"<h3>{lst.get('text', '')}</h3><ul>{lst.get('content', '')}</ul>")
    parts.append(p.get("additional") or "")
    created = p.get("createdAt")
    raw = {k: v for k, v in p.items() if k not in ("description", "lists", "additional")}
    return RawPosting(
        source="lever",
        ext_id=p["id"],
        company=company,
        title=p["text"].strip(),
        url=p.get("hostedUrl") or p.get("applyUrl") or "",
        location_raw=location_raw or None,
        description_html="\n".join(parts),
        posted_at=datetime.fromtimestamp(created / 1000, tz=UTC) if created else None,
        raw=raw,
    )
