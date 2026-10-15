"""Source protocol + companies.yaml loader + a shared polite HTTP client.

Hard rule 2: sources are public JSON APIs or explicitly permitted feeds.
Every adapter declares `terms_url` and justifies itself in its docstring.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from prospect.models.posting import RawPosting
from prospect.yamlio import load_yaml

USER_AGENT = "prospect/0.1 (personal job-search pipeline; low volume, public APIs only)"
Ats = Literal["greenhouse", "lever", "ashby"]


class Company(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    ats: Ats
    token: str
    careers_url: str | None = None
    verified: str | None = None  # date the token was last verified by opening the URL
    notes: str | None = None


def load_companies(path: Path) -> list[Company]:
    if not path.exists():
        return []
    data = load_yaml(path) or {}
    rows = data.get("companies", []) if isinstance(data, dict) else data
    return [Company.model_validate(r) for r in rows or []]


@dataclass
class Feed:
    """One fetchable unit (one company board, one list file, one search)."""

    key: str
    fetch: Callable[[], list[RawPosting]]


@dataclass
class FetchResult:
    key: str
    postings: list[RawPosting] = field(default_factory=list)
    error: str | None = None


class Source(ABC):
    name: ClassVar[str]
    terms_url: ClassVar[str]

    @abstractmethod
    def feeds(self) -> Iterable[Feed]: ...

    def fetch(self) -> Iterator[RawPosting]:
        for feed in self.feeds():
            yield from feed.fetch()


class Http:
    """httpx client with a UA, timeouts, retry on 429/5xx, and a small delay between calls."""

    def __init__(self, delay_s: float = 0.3, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        self.delay_s = delay_s
        self._last = 0.0

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(3):
            wait = self.delay_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            resp = self.client.get(url, params=params)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    def close(self) -> None:
        self.client.close()
