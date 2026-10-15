"""`prospect factbank lint` — every rule in docs/FACTBANK.md, plus the metric regex.

Errors block the pipeline (`ensure_lint_clean`); warnings do not.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from prospect.factbank.alias_index import AliasIndex, label_terms, variants
from prospect.factbank.metrics import metric_tokens
from prospect.models.factbank import FactBank
from prospect.yamlio import load_yaml_text, yaml11_ambiguous_scalars

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TODO = "???"
# Stand-ins used only so semantic checks can still run while `???` fields exist.
_PLACEHOLDERS: dict[str, Any] = {
    "level": "familiar",
    "category": "tool",
    "domains": [],
    "start": "1970-01",
    "kind": None,  # resolved per section below
}


@dataclass
class LintIssue:
    level: Literal["error", "warning"]
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.level:<8} {self.path}  {self.message}"


@dataclass
class LintReport:
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, path: str, msg: str) -> None:
        self.issues.append(LintIssue("error", path, msg))

    def warn(self, path: str, msg: str) -> None:
        self.issues.append(LintIssue("warning", path, msg))

    def render(self, name: str = "factbank.yaml") -> str:
        head = f"{name}: {len(self.errors)} errors, {len(self.warnings)} warnings"
        return "\n".join([head, *(f"  {i}" for i in self.issues)])


def lint_file(path: Path) -> LintReport:
    return lint_text(path.read_text(encoding="utf-8"))


def lint_text(text: str) -> LintReport:
    report = LintReport()
    try:
        raw = load_yaml_text(text)
    except Exception as e:  # ruamel raises several parser error types
        report.error("<yaml>", f"not valid YAML: {e}")
        return report
    if not isinstance(raw, dict):
        report.error("<root>", "top level must be a mapping")
        return report

    for path, value in yaml11_ambiguous_scalars(text):
        report.warn(
            path,
            f"unquoted {value!r} is a boolean to YAML 1.1 parsers (PyYAML); quote it as "
            f'"{value}" so every tool reads the same value',
        )

    todo_paths = _find_todo(raw)
    for p in todo_paths:
        report.error(p, "is ??? — fill in by hand (factbank sync never guesses this)")

    fb = _validate(raw, todo_paths, report)
    if fb is None:
        return report
    _semantic_checks(fb, report)
    return report


# --- schema ------------------------------------------------------------------


def _find_todo(node: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _find_todo(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _find_todo(v, f"{path}[{i}]")
    elif isinstance(node, str) and node.strip() == TODO:
        out.append(path)
    return out


def _validate(raw: dict, todo_paths: list[str], report: LintReport) -> FactBank | None:
    data = _fill_placeholders(copy.deepcopy(raw)) if todo_paths else raw
    try:
        return FactBank.model_validate(data)
    except ValidationError as e:
        for err in e.errors():
            loc = _loc_to_path(raw, err["loc"])
            report.error(loc, f"schema: {err['msg']}")
        return None


def _fill_placeholders(node: Any, section: str = "") -> Any:
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str) and v.strip() == TODO:
                if k == "kind":
                    node[k] = {"experiences": "work", "projects": "course"}.get(section, "work")
                else:
                    node[k] = _PLACEHOLDERS.get(k)
            else:
                node[k] = _fill_placeholders(v, k if section == "" else section)
    elif isinstance(node, list):
        return [_fill_placeholders(v, section) for v in node]
    return node


def _loc_to_path(raw: Any, loc: tuple) -> str:
    """Render a pydantic error location with ids instead of list indexes."""
    parts: list[str] = []
    node = raw
    for key in loc:
        if isinstance(key, int) and isinstance(node, list) and key < len(node):
            item = node[key]
            ident = item.get("id") if isinstance(item, dict) else None
            parts[-1] = f"{parts[-1]}[{ident or key}]" if parts else f"[{ident or key}]"
            node = item
        else:
            parts.append(str(key))
            node = node.get(key) if isinstance(node, dict) else None
    return ".".join(parts)


# --- semantics -----------------------------------------------------------------


def _semantic_checks(fb: FactBank, report: LintReport) -> None:
    ident = fb.identity
    if ident.work_authorization is None:
        report.warn(
            "identity.work_authorization",
            "not set — `prospect filter` refuses to run until it is "
            "(citizen | pr | pgwp | needs_sponsorship)",
        )
    for name, url in ident.links.items():
        if not url:
            report.warn(f"identity.links.{name}", "not set")

    # ids: unique across every node kind, slug-cased
    all_ids: list[tuple[str, str]] = [(f"skills[{s.id}]", s.id) for s in fb.skills]
    all_ids += [(f"education[{e.id}]", e.id) for e in fb.education]
    for sec, owners in (("experiences", fb.experiences), ("projects", fb.projects)):
        for o in owners:
            all_ids.append((f"{sec}[{o.id}]", o.id))
            all_ids += [(f"{sec}[{o.id}].bullets[{b.id}]", b.id) for b in o.bullets]
    counts = Counter(i for _, i in all_ids)
    for path, i in all_ids:
        if counts[i] > 1:
            report.error(path, f"id {i!r} is used {counts[i]} times; ids must be unique")
        if not SLUG.match(i):
            report.error(path, f"id {i!r} is not slug-cased (a-z, 0-9, single hyphens)")

    skill_ids = set(fb.skill_map)
    evidence_targets = set(fb.bullet_map) | {p.id for p in fb.projects}
    idx = AliasIndex.build(fb)
    for v, first, second in idx.collisions:
        report.warn(
            f"skills[{second}]",
            f"id/label normalizes to {v!r}, same as skill {first!r}; exact matches are ambiguous",
        )

    for s in fb.skills:
        base = f"skills[{s.id}]"
        for ev in s.evidence:
            if ev not in evidence_targets:
                report.error(f"{base}.evidence", f"{ev!r} is not a bullet or project id")
        if not s.evidence and not s.basis:
            report.warn(base, "no evidence and no basis — on the résumé without support")
        own_terms = {v for t in {s.id} | label_terms(s.label) for v in variants(t)}
        for a in s.aliases:
            for v in variants(a):
                other = idx.exact.get(v)
                if other and other != s.id and v not in own_terms:
                    report.error(
                        f"{base}.aliases",
                        f"alias {a!r} is skill {other!r}'s id/label — that is a Tier 0 match "
                        "masquerading as Tier 1",
                    )
                    break
        for adj in s.adjacent:
            hit = next((idx.exact[v] for v in variants(adj) if v in idx.exact), None)
            if hit:
                report.error(
                    f"{base}.adjacent",
                    f"{adj!r} is skill {hit!r} — `adjacent` declares a skill you do NOT have",
                )
                continue
            alias_hit = next((idx.alias[v] for v in variants(adj) if v in idx.alias), None)
            if alias_hit:
                report.warn(
                    f"{base}.adjacent",
                    f"{adj!r} is also an alias of {alias_hit[0]!r} (have vs. don't-have)",
                )

    for sec, owners in (("experiences", fb.experiences), ("projects", fb.projects)):
        for o in owners:
            if sec == "projects":
                for st in o.stack:  # type: ignore[union-attr]
                    if st not in skill_ids:
                        report.error(f"projects[{o.id}].stack", f"{st!r} is not a skill id")
            for b in o.bullets:
                bpath = f"{sec}[{o.id}].bullets[{b.id}]"
                for sk in b.skills:
                    if sk not in skill_ids:
                        report.error(f"{bpath}.skills", f"{sk!r} is not a skill id")
                tokens = metric_tokens(b.text)
                missing = [t for t in dict.fromkeys(tokens) if t not in b.metrics]
                if missing:
                    report.error(
                        f"{bpath}.metrics",
                        f"numeric tokens {missing} appear in text but not in metrics",
                    )
                stale = [m for m in b.metrics if m not in tokens]
                if stale:
                    report.warn(f"{bpath}.metrics", f"{stale} listed but not found in text")
