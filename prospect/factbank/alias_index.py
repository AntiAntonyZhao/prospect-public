"""Term → fact bank skill lookup: exact (Tier 0), alias (Tier 1a), adjacent (Tier 2).

Terms are compared in a normalized form (accents stripped, lower-case, only
[a-z0-9+#] kept, with a de-pluralized variant) so "REST API", "REST APIs" and
"rest-api" meet. `CANONICAL` holds spelling synonyms only — names for the
*same* thing (JS = JavaScript). A reframing belongs in a skill's `aliases`
in factbank.yaml, never here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from prospect.models.factbank import FactBank

_DROP = re.compile(r"[^a-z0-9+#]")
_PARENS = re.compile(r"\s*\([^)]*\)")

CANONICAL: dict[str, str] = {
    "js": "javascript",
    "ecmascript": "javascript",
    "es6": "javascript",
    "ts": "typescript",
    "reactjs": "react",
    "node": "nodejs",
    "cpp": "c++",
    "cplusplus": "c++",
    "python3": "python",
    "github": "git",
    "googlecloud": "gcp",
    "googlecloudplatform": "gcp",
    "amazonwebservices": "aws",
    "microsoftazure": "azure",
    "rest": "restapis",
    "restful": "restapis",
    "restfulapis": "restapis",
    "restapi": "restapis",
    "postgres": "postgresql",
    "k8s": "kubernetes",
    "torch": "pytorch",
    "sklearn": "scikitlearn",
}


def norm_term(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return _DROP.sub("", s.lower().replace("&", "and"))


def variants(s: str) -> set[str]:
    n = norm_term(s)
    out = {n}
    if len(n) > 4 and n.endswith("s") and not n.endswith("ss"):
        out.add(n[:-1])
    out |= {CANONICAL[v] for v in list(out) if v in CANONICAL}
    return out - {""}


def label_terms(label: str) -> set[str]:
    """A label plus its paren-stripped form and ' / '-separated parts (len ≥ 3)."""
    terms = {label, _PARENS.sub("", label)}
    for piece in re.split(r"\s*/\s*", _PARENS.sub("", label)):
        if len(norm_term(piece)) >= 3:
            terms.add(piece)
    return terms


MatchKind = Literal["exact", "alias", "adjacent"]


@dataclass(frozen=True)
class Match:
    kind: MatchKind
    skill_id: str
    alias: str | None = None  # the fact bank alias string, for Tier 1(a) renames


@dataclass
class AliasIndex:
    exact: dict[str, str] = field(default_factory=dict)
    alias: dict[str, tuple[str, str]] = field(default_factory=dict)
    adjacent: dict[str, str] = field(default_factory=dict)
    collisions: list[tuple[str, str, str]] = field(default_factory=list)

    @classmethod
    def build(cls, fb: FactBank) -> AliasIndex:
        idx = cls()
        for s in fb.skills:
            for term in {s.id} | label_terms(s.label):
                for v in variants(term):
                    prev = idx.exact.setdefault(v, s.id)
                    if prev != s.id:
                        idx.collisions.append((v, prev, s.id))
        for s in fb.skills:
            for a in s.aliases:
                for v in variants(a):
                    if v not in idx.exact:
                        idx.alias.setdefault(v, (s.id, a))
            for adj in s.adjacent:
                for v in variants(adj):
                    idx.adjacent.setdefault(v, s.id)
        return idx

    def lookup(self, term: str) -> Match | None:
        vs = variants(term)
        for v in vs:
            if v in self.exact:
                return Match("exact", self.exact[v])
        for v in vs:
            if v in self.alias:
                sid, a = self.alias[v]
                return Match("alias", sid, a)
        for v in vs:
            if v in self.adjacent:
                return Match("adjacent", self.adjacent[v])
        return None
