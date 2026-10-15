"""Settings from prospect.toml + .env (ANTHROPIC_API_KEY).

prospect.toml holds nothing secret. The API key is read from `.env` by
python-dotenv and is never expected in the shell environment. Every section
has defaults, so a missing prospect.toml still yields a working configuration:
out of the box the fact bank is the fictional demo profile
(`demo/factbank_demo.yaml`); point `[paths] factbank` at your own.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict

ROOT_MARKERS = ("prospect.toml", "factbank.yaml", "db/schema.sql")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsCfg(_Section):
    factbank: str = "demo/factbank_demo.yaml"
    companies: str = "companies.yaml"
    schema_sql: str = "db/schema.sql"
    db: str = "prospect.db"


class TargetsCfg(_Section):
    cities: list[str] = ["Montreal", "Toronto"]
    provinces: list[str] = ["QC", "ON"]
    remote_scope_ok: list[str] = ["canada", "global"]
    max_years: int = 2
    seniority: list[str] = ["intern", "newgrad", "junior"]


class DedupCfg(_Section):
    window_days: int = 90


class BudgetCfg(_Section):
    daily_usd: float = 2.00


class TiersCfg(_Section):
    t2_max_hours: float = 8


class ScoreCfg(_Section):
    w_must_t3: float = 25
    w_must_t2: float = 10
    w_nice_gap: float = 3
    w_weak_have: float = 2
    # Employment terms, both linear and both 0 at full time / full term:
    #   part-time  = w_part_time  * (1 - hours / full_time_hours)
    #   short term = w_short_term * (1 - months / full_term_months)   contracts and internships
    # A 4-month internship costs 20, the flat intern penalty this replaces.
    w_part_time: float = 30
    full_time_hours: float = 37.5
    part_time_default_hours: float = 20  # "part-time" with no hours stated
    w_short_term: float = 30
    full_term_months: float = 12
    intern_default_months: float = 4  # an internship with no length stated
    contract_default_months: float = 6  # a contract with no length stated
    ghost_max: float = 20
    ghost_per_repost: float = 4
    ghost_no_response: float = 10
    no_response_days: int = 21
    top_n: int = 30
    effort: str = "low"
    max_tokens: int = 8000
    # home = identity.location.city; remote = no move needed either; elsewhere = a move.
    fit_location: dict[str, float] = {"home": 0, "remote": 0, "elsewhere": -10, "unknown": -5}


class ModelsCfg(_Section):
    haiku: str = "claude-haiku-4-5-20251001"
    sonnet: str = "claude-sonnet-5"
    opus: str = "claude-opus-5"


class LLMCfg(_Section):
    strict_tools: bool = True
    # Server-side refusal fallback on Opus-family sync calls (not allowed on Batches).
    refusal_fallbacks: bool = True
    timeout_s: float = 300
    max_retries: int = 2


class RankCfg(_Section):
    model: str = "BAAI/bge-small-en-v1.5"
    top_n: int = 150
    max_chars: int = 4000


class ExtractCfg(_Section):
    mode: Literal["batch", "sync"] = "batch"
    max_tokens: int = 4096
    batch_wait_minutes: int = 60
    poll_seconds: int = 30
    max_desc_chars: int = 16000


class FilterCfg(_Section):
    prepass_reject_location: bool = True
    prepass_reject_senior_titles: bool = True
    min_confidence_to_reject: float = 0.6


class SourcesCfg(_Section):
    enabled: list[str] = ["greenhouse", "lever", "ashby", "github_newgrad"]
    github_newgrad_url: str = (
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
        ".github/scripts/listings.json"
    )
    github_newgrad_hydrate: bool = True
    request_delay_s: float = 0.3


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path
    paths: PathsCfg = PathsCfg()
    targets: TargetsCfg = TargetsCfg()
    dedup: DedupCfg = DedupCfg()
    budget: BudgetCfg = BudgetCfg()
    tiers: TiersCfg = TiersCfg()
    score: ScoreCfg = ScoreCfg()
    models: ModelsCfg = ModelsCfg()
    llm: LLMCfg = LLMCfg()
    rank: RankCfg = RankCfg()
    extract: ExtractCfg = ExtractCfg()
    filter: FilterCfg = FilterCfg()
    sources: SourcesCfg = SourcesCfg()

    anthropic_api_key: str | None = None

    # -- paths ---------------------------------------------------------------
    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.path(self.paths.db)

    @property
    def factbank_path(self) -> Path:
        return self.path(self.paths.factbank)

    @property
    def companies_path(self) -> Path:
        return self.path(self.paths.companies)

    @property
    def schema_path(self) -> Path:
        return self.path(self.paths.schema_sql)

    # -- models ---------------------------------------------------------------
    def model_id(self, key_or_id: str) -> str:
        """Resolve `haiku|sonnet|opus` to a model id; pass full ids through."""
        return getattr(self.models, key_or_id, None) or key_or_id

    def score_weights_version(self) -> str:
        blob = json.dumps(self.score.model_dump(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def find_root(start: Path | None = None) -> Path:
    """PROSPECT_HOME, else the nearest ancestor of `start` holding a root marker."""
    env = os.environ.get("PROSPECT_HOME")
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if any((d / m).exists() for m in ROOT_MARKERS):
            return d
    return here


def load_settings(root: Path | None = None) -> Settings:
    root = (root or find_root()).resolve()
    data: dict[str, Any] = {}
    toml_path = root / "prospect.toml"
    if toml_path.exists():
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    # .env is read directly, never exported into os.environ, so the key cannot
    # leak into child processes (and Claude Code never bills to it).
    env = {**dotenv_values(root / ".env"), **_env_overrides()}
    return Settings(root=root, **data, anthropic_api_key=_real(env.get("ANTHROPIC_API_KEY")))


def _env_overrides() -> dict[str, str]:
    keys = ("ANTHROPIC_API_KEY",)
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def _real(value: str | None) -> str | None:
    """Treat an empty value (as in .env.example) as unset."""
    if not value or not value.strip():
        return None
    return value.strip()
