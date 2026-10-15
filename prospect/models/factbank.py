"""Pydantic schema for factbank.yaml (docs/FACTBANK.md). Read-only in the pipeline."""

from __future__ import annotations

import datetime as dt
from functools import cached_property
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

Level = Literal["familiar", "working", "proficient", "expert"]
LEVEL_RANK: dict[str, int] = {"familiar": 0, "working": 1, "proficient": 2, "expert": 3}
Category = Literal[
    "language",
    "framework",
    "ml",
    "cloud",
    "devops",
    "db",
    "testing",
    "methodology",
    "modeling",
    "tool",
]
Domain = Literal[
    "frontend",
    "backend",
    "data",
    "ml",
    "llm",
    "cloud",
    "devops",
    "testing",
    "systems",
    "algorithms",
    "design",
    "process",
    "tooling",
]
CEFR = Literal["A1", "A2", "B1", "B2", "C1", "C2", "native"]
CEFR_RANK: dict[str, int] = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "native": 7}
WorkAuthorization = Literal["citizen", "pr", "pgwp", "needs_sponsorship"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _year_month(v: object) -> object:
    """YAML may hand us a date for `2024-10-01`; normalize to 'YYYY-MM'."""
    if isinstance(v, dt.date):
        return f"{v.year:04d}-{v.month:02d}"
    return v


YearMonth = Annotated[str, BeforeValidator(_year_month)]


class Language(_Strict):
    lang: str
    cefr: CEFR
    render: str
    basis: str | None = None


class Location(_Strict):
    city: str
    province: str
    country: str


class Identity(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    email: str
    phone: str
    location: Location
    links: dict[str, str | None] = {}
    work_authorization: WorkAuthorization | None = None
    languages: list[Language] = []


class Targets(_Strict):
    roles: list[str] = []
    seniority: list[str] = []
    cities: list[str] = []
    provinces: list[str] = []
    remote_scope_ok: list[str] = []
    max_years: int = 2


class Education(_Strict):
    id: str
    institution: str
    degree: str
    start: YearMonth
    end: YearMonth | None = None
    location: str | None = None


class Skill(_Strict):
    id: str
    label: str
    category: Category
    domains: list[Domain]
    level: Level
    evidence: list[str] = []
    basis: str | None = None
    aliases: list[str] = []
    adjacent: list[str] = []


class Bullet(_Strict):
    id: str
    text: str
    skills: list[str] = []
    metrics: list[str] = []
    tags: list[str] = []
    strength: int = Field(ge=1, le=5)

    @field_validator("metrics", mode="before")
    @classmethod
    def metrics_as_str(cls, v: object) -> object:
        return [str(m) for m in v] if isinstance(v, list) else v


class Experience(_Strict):
    id: str
    org: str
    title: str
    start: YearMonth
    end: YearMonth | None = None
    location: str | None = None
    kind: Literal["work", "research", "volunteer"]
    bullets: list[Bullet] = []


class Project(_Strict):
    id: str
    name: str
    start: YearMonth
    end: YearMonth | None = None
    kind: Literal["team", "solo", "course"]
    course: str | None = None
    url: str | None = None
    stack: list[str] = []
    bullets: list[Bullet] = []


Owner = Experience | Project


class FactBank(_Strict):
    version: int
    identity: Identity
    targets: Targets
    education: list[Education] = []
    skills: list[Skill] = []
    experiences: list[Experience] = []
    projects: list[Project] = []

    # -- indexes (the model is never mutated after load) ---------------------
    @cached_property
    def skill_map(self) -> dict[str, Skill]:
        return {s.id: s for s in self.skills}

    @cached_property
    def skill_order(self) -> dict[str, int]:
        return {s.id: i for i, s in enumerate(self.skills)}

    @cached_property
    def owners(self) -> dict[str, Owner]:
        out: dict[str, Owner] = {e.id: e for e in self.experiences}
        out.update({p.id: p for p in self.projects})
        return out

    @cached_property
    def bullet_map(self) -> dict[str, Bullet]:
        return {b.id: b for o in self.owners.values() for b in o.bullets}

    @cached_property
    def bullet_owner(self) -> dict[str, str]:
        return {b.id: o.id for o in self.owners.values() for b in o.bullets}

    @cached_property
    def node_ids(self) -> set[str]:
        """Every id an evidence chain may reference."""
        ids = set(self.skill_map) | set(self.owners) | set(self.bullet_map)
        ids |= {e.id for e in self.education}
        return ids

    def work_years(self, today: dt.date | None = None) -> float:
        """Total work experience in years; part-time roles count half.

        Used for Tier 3 (years beyond factbank) and ATS form fields.
        """
        months = 0.0
        for e in self.experiences:
            if e.kind != "work":
                continue
            m = months_between(e.start, e.end, today)
            months += m * (0.5 if "part-time" in e.title.lower() else 1.0)
        return round(months / 12, 2)

    def language(self, name: str) -> Language | None:
        for lang in self.identity.languages:
            if lang.lang.lower() == name.lower():
                return lang
        return None


def months_between(start: str, end: str | None, today: dt.date | None) -> int:
    y1, m1 = (int(x) for x in start.split("-")[:2])
    if end:
        y2, m2 = (int(x) for x in end.split("-")[:2])
    else:
        t = today or dt.date.today()
        y2, m2 = t.year, t.month
    return max(0, (y2 - y1) * 12 + (m2 - m1) + 1)
