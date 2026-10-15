"""Posting-side contracts: RawPosting (sources), PostingFacts + Requirement (extract/score).

The `Extracted*` / `Tier*` models are the exact shapes the LLM fills through a
tool call; the pipeline turns them into PostingFacts / Requirement rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

WorkArrangement = Literal["onsite", "hybrid", "remote", "unknown"]
RemoteScope = Literal["city", "province", "canada", "us", "global", "unknown"]
PostingLanguage = Literal["en", "fr", "bilingual"]
FrenchRequirement = Literal[
    "none", "asset", "required_pro_forma", "required_working", "posting_in_french"
]
Seniority = Literal["intern", "newgrad", "junior", "mid", "senior", "unknown"]
# What the posting demands, compared against identity.work_authorization in filter.
WorkAuthRequirement = Literal[
    "not_stated", "citizen_only", "citizen_or_pr", "no_sponsorship", "sponsorship_available"
]
TierSubstate = Literal["strong", "weak"]
# skill: tiered and scored. eligibility: enrollment, graduation window, term/availability,
# work authorization, relocation, experience caps — shown, never tiered as a skill gap.
ExtractedKind = Literal["skill", "eligibility"]
# soft: set by the score stage when the model labels a requirement a soft skill, trait,
# interest or work style — kept, never tiered or scored (policy 2026-10-06).
RequirementKind = Literal["skill", "eligibility", "soft"]
T3Reason = Literal["credential", "degree", "language", "depth"]
TermUnit = Literal["weeks", "months", "years"]

FACT_FIELDS = (
    "city",
    "province",
    "country",
    "work_arrangement",
    "onsite_days",
    "remote_scope",
    "posting_language",
    "french_requirement",
    "min_years",
    "seniority",
    "clearance",
    "peng",
    "work_auth",
    "part_time",
    "hours_per_week",
    "contract",
    "term_months",
)
# Fields hand-labeled in `labels` and scored by `prospect eval extract` (CLAUDE.md "Eval"):
# exactly the facts that drive a decision — city (home bonus), posting_language (French
# posting → reject), min_years (above targets.max_years → reject), seniority (intern penalty).
LABELED_FIELDS = ("city", "posting_language", "min_years", "seniority")


class RawPosting(BaseModel):
    source: str
    ext_id: str
    company: str
    title: str
    url: str
    location_raw: str | None = None
    description_html: str = ""
    posted_at: datetime | None = None
    raw: dict[str, Any] = {}


class PostingFacts(BaseModel):
    city: str | None = None
    province: str | None = None
    country: str | None = None
    work_arrangement: WorkArrangement = "unknown"
    onsite_days: int | None = None
    remote_scope: RemoteScope = "unknown"
    posting_language: PostingLanguage = "en"
    french_requirement: FrenchRequirement = "none"
    min_years: int | None = None
    seniority: Seniority = "unknown"
    clearance: bool = False
    peng: bool = False
    work_auth: WorkAuthRequirement = "not_stated"
    # Employment terms (policy 2026-10-06): part-time and short fixed terms cost points.
    part_time: bool = False
    hours_per_week: float | None = None
    contract: bool = False
    term_months: float | None = None  # contract or internship length, from term_length/unit
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_span: dict[str, str] = {}


class Requirement(BaseModel):
    id: int | None = None
    text: str
    skill_norm: str
    must_have: bool
    years: int | None = None
    context: str | None = None
    kind: RequirementKind = "skill"
    tier: int | None = None
    tier_substate: TierSubstate | None = None
    matched_node: str | None = None
    evidence: list[str] = []
    est_hours: float | None = None
    nearest_node: str | None = None


# --- LLM tool shapes --------------------------------------------------------


class _Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceSpan(_Tool):
    field: str = Field(description="PostingFacts field this quote supports, e.g. 'city'.")
    quote: str = Field(description="Verbatim span copied from the posting text.")


class ExtractedFacts(_Tool):
    city: str | None = Field(
        description="City of the job in English without accents (Montreal, Quebec City). "
        "null if not stated or remote with no city."
    )
    province: str | None = Field(description="Two-letter province/state code (QC, ON, BC, NY).")
    country: str | None = Field(description="ISO-3166 alpha-2 country code (CA, US).")
    work_arrangement: WorkArrangement
    onsite_days: int | None = Field(description="Required office days per week, if stated.")
    remote_scope: RemoteScope = Field(
        description="Where a remote/hybrid hire may live. 'unknown' for onsite roles."
    )
    french_requirement: FrenchRequirement
    min_years: int | None = Field(
        description="Minimum years of professional experience required overall, only when "
        "stated. An upper bound ('less than 2 years') is not a minimum: null."
    )
    seniority: Seniority
    clearance: bool = Field(description="A government security clearance is required.")
    peng: bool = Field(description="A PEng / professional engineering licence is required.")
    work_auth: WorkAuthRequirement
    part_time: bool = Field(description="The role is part-time (stated hours or 'part-time').")
    hours_per_week: float | None = Field(
        description="Weekly hours when stated ('16 hours per week' → 16; a range → midpoint)."
    )
    contract: bool = Field(
        description="Fixed-term, temporary, freelance or independent-contractor engagement. "
        "Internships are false here; permanent employment is false."
    )
    term_length: float | None = Field(
        description="Stated length of a contract or internship, in `term_unit`; else null."
    )
    term_unit: TermUnit | None = Field(description="Unit of `term_length`; null when it is.")
    confidence: float = Field(description="0-1: how sure you are about location + French fields.")
    evidence: list[EvidenceSpan] = Field(
        description="One verbatim quote per non-default field you set."
    )


class ExtractedRequirement(_Tool):
    text: str = Field(description="The requirement, quoted from the posting.")
    skill_norm: str = Field(
        description="Canonical short skill name, e.g. 'Kafka', 'React', 'SQL', "
        "'Bachelor degree in CS'."
    )
    must_have: bool = Field(description="False for 'nice to have', 'bonus', 'asset', 'plus'.")
    years: int | None = Field(
        description="Minimum years with this skill, if stated. Upper bounds ('less than', "
        "'up to', 'at most') are not minimums: null."
    )
    context: str | None = Field(
        description="Depth signals around the skill (e.g. 'design patterns', 'concurrency'), "
        "else null."
    )
    kind: ExtractedKind = Field(
        default="skill",
        description="'skill' for technologies, methods, domains, degrees, certifications, "
        "clearances, licences; 'eligibility' for enrollment status, graduation window, "
        "internship term or availability, start date, work authorization, relocation, and "
        "experience caps.",
    )


class ExtractionOutput(_Tool):
    facts: ExtractedFacts
    requirements: list[ExtractedRequirement]


class TierLabel(_Tool):
    index: int = Field(description="Index of the requirement in the input list.")
    soft: bool = Field(
        description="True for a soft skill, personality trait, interest, value or work style "
        "(communication, passion for a mission, thriving in a startup, comfort with "
        "ambiguity). Soft requirements are never gaps and never scored; tier is ignored."
    )
    tier: Literal[0, 1, 2, 3]
    t3_reason: T3Reason | None = Field(
        description="Tier 3 only: credential (certification, designation, clearance, "
        "licence), degree (not held), language (human language above the candidate's "
        "level), or depth (needs more than the Tier 2 hour budget). Null below Tier 3."
    )
    matched_node: str | None = Field(
        description="Fact bank skill id for Tier 0/1 (or education id for a degree)."
    )
    evidence: list[str] = Field(
        description="Fact bank node ids (bullets/projects/skills) proving a Tier 1 match."
    )
    est_hours: float | None = Field(
        description="Hours to reach 'can discuss it and show a small demo' (Tier 2/3)."
    )
    nearest_node: str | None = Field(description="Closest fact bank skill id for a Tier 2/3 gap.")
    rationale: str = Field(description="One short sentence.")


class TierOutput(_Tool):
    labels: list[TierLabel]
