"""Read-only access to factbank.yaml. Nothing in this module writes the master.

Hard rule 1: no pipeline stage writes the fact bank; it is edited by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from prospect.config import Settings
from prospect.factbank.lint import lint_file
from prospect.models.factbank import FactBank
from prospect.yamlio import load_yaml


class FactBankError(RuntimeError):
    pass


def load_factbank(path: Path) -> FactBank:
    return _load_cached(path.resolve(), path.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _load_cached(path: Path, mtime_ns: int) -> FactBank:
    del mtime_ns  # part of the cache key only
    try:
        return FactBank.model_validate(load_yaml(path))
    except Exception as e:
        raise FactBankError(f"{path.name} does not load: {e}. Run `prospect factbank lint`.") from e


def ensure_lint_clean(settings: Settings) -> FactBank:
    """Every pipeline stage calls this first: lint errors stop the run."""
    path = settings.factbank_path
    if not path.exists():
        raise FactBankError(f"{path} not found")
    report = lint_file(path)
    if not report.ok:
        raise FactBankError(
            f"{path.name} has {len(report.errors)} lint error(s); no pipeline stage runs until "
            "`prospect factbank lint` is clean."
        )
    return load_factbank(path)


def prompt_text(fb: FactBank) -> str:
    """Deterministic serialization used as the cached prompt prefix.

    Contact details (email, phone, links) are dropped — no stage needs them —
    so they are never sent to the API. Stable byte-for-byte across calls.
    """
    data = fb.model_dump(mode="json", exclude_none=True)
    ident = data["identity"]
    for k in ("email", "phone", "links"):
        ident.pop(k, None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


@dataclass(frozen=True)
class EffectiveTargets:
    roles: tuple[str, ...]
    seniority: tuple[str, ...]
    cities: tuple[str, ...]
    provinces: tuple[str, ...]
    remote_scope_ok: tuple[str, ...]
    max_years: int


def effective_targets(settings: Settings, fb: FactBank) -> EffectiveTargets:
    """factbank.yaml `targets`, overridden by keys set in prospect.toml [targets]."""
    merged = fb.targets.model_dump()
    cfg = settings.targets
    for key in cfg.model_fields_set:
        merged[key] = getattr(cfg, key)
    for key in ("seniority", "cities", "provinces", "remote_scope_ok"):
        if not merged.get(key):
            merged[key] = getattr(cfg, key)
    return EffectiveTargets(
        roles=tuple(merged.get("roles") or ()),
        seniority=tuple(merged["seniority"]),
        cities=tuple(merged["cities"]),
        provinces=tuple(p.upper() for p in merged["provinces"]),
        remote_scope_ok=tuple(merged["remote_scope_ok"]),
        max_years=int(merged["max_years"]),
    )
