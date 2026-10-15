"""Source adapters. `build_sources` wires the enabled ones from config + companies.yaml."""

from __future__ import annotations

from prospect.config import Settings
from prospect.sources.ashby import Ashby
from prospect.sources.base import Http, Source, load_companies
from prospect.sources.github_newgrad import GitHubNewGrad
from prospect.sources.greenhouse import Greenhouse
from prospect.sources.lever import Lever

ALL_SOURCES: tuple[type[Source], ...] = (Greenhouse, Lever, Ashby, GitHubNewGrad)


def build_sources(settings: Settings, http: Http, only: list[str] | None = None) -> list[Source]:
    cfg = settings.sources
    wanted = set(only or cfg.enabled)
    companies = load_companies(settings.companies_path)
    out: list[Source] = []
    if "greenhouse" in wanted:
        out.append(Greenhouse(companies, http))
    if "lever" in wanted:
        out.append(Lever(companies, http))
    if "ashby" in wanted:
        out.append(Ashby(companies, http))
    if "github_newgrad" in wanted:
        out.append(GitHubNewGrad(http, cfg.github_newgrad_url, cfg.github_newgrad_hydrate))
    return out
