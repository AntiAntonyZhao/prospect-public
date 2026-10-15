"""Deterministic guards on extraction output — the model is asked, code makes sure.

* Upper-bound years ("less than 2 years") are never a minimum: `years` → None, and a
  bare experience cap becomes an eligibility condition, not a skill.
* "Preferred", "a plus", "nice to have" in a requirement's own text make it optional.
* Soft skills are not filtered here: the score stage labels them and never scores them
  (policy 2026-10-06 — a list of them is never complete).
* Evidence must *show* the value it is quoted for ("Toronto" for city, "hybrid" for
  work_arrangement, "2+ years" for min_years); a verbatim quote about something else is
  dropped. The full filter pass then refuses to reject on a field with no evidence left.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from prospect.models.posting import ExtractedRequirement
from prospect.text import ascii_fold

_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
UPPER_BOUND_RE = re.compile(
    r"\b(?:less than|fewer than|under|no more than|not more than|up to|at most|"
    r"a maximum of|maximum of|max\.?)\s+" + _NUM + r"\+?\s*(?:years?|yrs?)\b",
    re.I,
)
_EXPERIENCE_FILLER = {
    "full",
    "time",
    "fulltime",
    "work",
    "working",
    "professional",
    "industry",
    "experience",
    "experiences",
    "year",
    "years",
    "yrs",
    "of",
    "in",
    "the",
    "relevant",
    "post",
    "graduation",
    "graduate",
    "prior",
    "total",
    "paid",
    "and",
    "or",
}
# Qualifications are tiered (bachelor's → Tier 0; master's, certs, clearance → Tier 3),
# so they are always `skill`, whatever kind the model chose.
QUALIFICATION_RE = re.compile(
    r"\b(?:bachelor|master|ph\.?d|doctorate|degree|diploma|certif\w*|clearance|"
    r"licen[cs]e\w*|p\.?\s?eng)\b",
    re.I,
)
# An upper bound whose noun is experience: "less than 2 years (of full-time work) experience".
_CAP_ON_EXPERIENCE = re.compile(UPPER_BOUND_RE.pattern + r"(?:\W+[\w']+){0,6}?\W+experience", re.I)
# Optional wording inside a requirement's own text (a "Preferred qualifications" heading
# is the model's job; this catches "Master's degree (preferred)", "Rust is a plus").
NICE_TO_HAVE_RE = re.compile(
    r"\b(?:preferred|preferably|nice[- ]to[- ]have|(?:is |are |would be )?an? (?:big |huge |"
    r"strong )?(?:plus|bonus|asset)|bonus points?|desirable|ideally|optional)\b",
    re.I,
)


# A requirement phrased as an interest, not a skill: "Genuine interest in large-scale
# distributed systems", "passion for applied NLP". Policy 2026-10-06 never scores these.
_INTEREST_RE = re.compile(
    r"\binterest(?:ed)? in\b|\bpassion(?:ate)?\b|\benthusias(?:m|tic)\b|\bcurio(?:us|sity)\b|"
    r"\bexcite(?:d|ment)? (?:about|by)\b|\beager to\b|\bdesire to\b|\blove (?:for|of|to)\b|"
    r"\byou love\b|\bappetite for\b|\bfascinat(?:ed|ion)\b|\bmotivated by\b",
    re.I,
)
# ...unless it also claims the skill: "Experience with or a strong interest in Workato",
# "a passionate Python developer".
_SKILL_CLAIM_RE = re.compile(
    r"\b(?:experienced?|experiences|proficien\w*|knowledge|familiar(?:ity)?|expertise|expert|"
    r"skilled|hands-on|track record|years?|background in|understanding of|ability to|"
    r"developer|engineer|programmer)\b",
    re.I,
)


def mentions_interest(text: str) -> bool:
    return bool(_INTEREST_RE.search(text))


def expresses_interest(text: str) -> bool:
    """Soft by its wording, whatever the skill (live rerun 2026-10-08).

    Softness depends on the phrasing, not the skill, so it can't come from the per-skill
    tier cache: Sonnet called "Genuine interest in … distributed systems" soft one run and a
    Tier 3 gap the next, and caching "passion for applied NLP" as soft under `nlp` would
    have unscored every later posting that requires NLP. Code decides it per requirement.
    """
    return mentions_interest(text) and not _SKILL_CLAIM_RE.search(text)


def is_experience_cap(text: str) -> bool:
    """'Less than 2 years full-time work experience' — a cap with no skill in it."""
    m = UPPER_BOUND_RE.search(text)
    if not m:
        return False
    rest = (text[: m.start()] + " " + text[m.end() :]).lower()
    words = re.findall(r"[a-z]+", ascii_fold(rest))
    return all(w in _EXPERIENCE_FILLER for w in words)


def caps_experience(r: ExtractedRequirement) -> bool:
    """The requirement *is* an experience cap, even with a condition around it.

    Live run 2026-10-06: "less than 2 years full-time work experience (master's
    candidates only)" stayed a skill and the degree rule read "master's" as a Tier 3 degree.
    The short name decides: "Python" with "up to 3 years of Python" is still a skill.
    """
    return (
        is_experience_cap(r.text)
        or is_experience_cap(r.skill_norm)
        or bool(_CAP_ON_EXPERIENCE.search(r.skill_norm))
    )


@dataclass
class CleanedRequirements:
    kept: list[ExtractedRequirement]
    years_cleared: int
    made_optional: int


def clean_requirements(reqs: list[ExtractedRequirement]) -> CleanedRequirements:
    kept: list[ExtractedRequirement] = []
    cleared = optional = 0
    for r in reqs:
        update: dict = {}
        if UPPER_BOUND_RE.search(r.text) or UPPER_BOUND_RE.search(r.skill_norm):
            if r.years is not None:
                cleared += 1
            update["years"] = None
        if caps_experience(r):
            update["kind"] = "eligibility"  # wins over the qualification rule below
        elif r.kind == "eligibility" and QUALIFICATION_RE.search(r.skill_norm):
            update["kind"] = "skill"
        if r.must_have and (NICE_TO_HAVE_RE.search(r.text) or NICE_TO_HAVE_RE.search(r.skill_norm)):
            update["must_have"] = False
            optional += 1
        kept.append(r.model_copy(update=update) if update else r)
    return CleanedRequirements(kept, cleared, optional)


# --- evidence must show the value -----------------------------------------------------

_PROVINCES = {
    "QC": "quebec",
    "ON": "ontario",
    "BC": "british columbia",
    "AB": "alberta",
    "MB": "manitoba",
    "SK": "saskatchewan",
    "NS": "nova scotia",
    "NB": "new brunswick",
    "NL": "newfoundland",
    "PE": "prince edward island",
}
# A country is usually stated through a place: "Toronto, ON", "Sunnyvale, CA".
_COUNTRIES = {
    "CA": r"canad|,\s*(?:on|qc|bc|ab|mb|sk|ns|nb|nl|pe)\b|toronto|montreal|vancouver|ottawa|"
    r"waterloo|calgary|edmonton|quebec|ontario|british columbia|alberta",
    "US": r"united states|\busa?\b|u\.s\.|,\s*(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|"
    r"ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|"
    r"vt|va|wa|wv|wi|wy|dc)\b|new york|san francisco|seattle|sunnyvale",
    "GB": r"united kingdom|\buk\b|england|london",
}
_ARRANGEMENT = {
    "remote": r"remote|anywhere|distributed|work from home|\bwfh\b",
    "hybrid": r"hybrid|days? (?:a|per) week|\bin[- ]office\b|flexib",
    "onsite": r"on[- ]?site|in[- ]?office|in[- ]person|office[- ]based|from (?:our|the) office",
}
_SCOPE = {
    "canada": r"canad|province",
    "us": r"united states|\busa?\b|u\.s\.",
    "global": r"anywhere|worldwide|global|any (?:country|location|time ?zone)",
    "city": r"commut|within|near|based in|located in|local",
    "province": r"province|" + "|".join(_PROVINCES.values()),
}
_SENIORITY = {
    "intern": r"\bintern(?:ship)?s?\b|co-?op|\bstage\b|placement|student",
    "newgrad": r"new grad|graduate|entry|early[- ]career|university|campus|recent grad",
    "junior": r"junior|\bjr\b|entry|associate",
    "mid": r"intermediate|\bmid\b|\bii\b|\d+\+? ?(?:years|yrs)",
    "senior": r"senior|\bsr\b|staff|principal|lead|\d+\+? ?(?:years|yrs)",
}
_FRENCH = r"fran[cç]ais|french|bilingu|francophone|anglais"
_FRENCH_WORKING = _FRENCH + r"|qu[eé]bec|gouvernement|government|minist"
_YEARS = (
    _NUM + r"\+?(?:\s+or\s+more|\s+plus)?\s*(?:(?:-|–|—|to)\s*" + _NUM + r"\+?\s*)?(?:years?|yrs?)"
)
_NO_EXPERIENCE = r"new grad|entry[- ]level|no (?:prior |professional )?experience|recent grad"
_HOURS_A_WEEK = r"(?:hours?|hrs?|h)\s*(?:per|a|/|each)\s*w(?:ee)?k|hours? weekly"
_MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_MONTH_RANGE = _MONTHS + r"\w*\.?(?:\s+\d{1,4})?\s*(?:-|to|through|until)\s*" + _MONTHS


def _has(pattern: str, quote: str) -> bool:
    return re.search(pattern, ascii_fold(quote).lower()) is not None


def evidence_supports(field: str, value: object, quote: str) -> bool:
    """Does `quote` visibly state `value` for `field`? Unknown fields pass."""
    v = str(value)
    q = ascii_fold(quote).lower()
    if field == "city":
        return ascii_fold(v).lower().replace(" city", "") in q
    if field == "province":
        name = _PROVINCES.get(v.upper(), "")
        return bool(re.search(rf"\b{v.lower()}\b", q)) or bool(name and name in q)
    if field == "country":
        return bool(re.search(_COUNTRIES.get(v.upper(), re.escape(v.lower())), q))
    if field == "work_arrangement":
        return _has(_ARRANGEMENT.get(v, re.escape(v)), quote)
    if field == "onsite_days":
        return _has(_NUM + r"|days?", quote)
    if field == "remote_scope":
        return _has(_SCOPE.get(v, re.escape(v)), quote)
    if field == "french_requirement":
        if v == "posting_in_french":
            return True  # the quote itself is French; posting_language double-checks
        return _has(_FRENCH_WORKING if v == "required_working" else _FRENCH, quote)
    if field == "min_years":
        if UPPER_BOUND_RE.search(quote):
            return False
        return _has(_YEARS, quote) or (v == "0" and _has(_NO_EXPERIENCE, quote))
    if field == "seniority":
        return _has(_SENIORITY.get(v, re.escape(v)), quote)
    if field == "part_time":
        return _has(r"part[- ]?time|" + _HOURS_A_WEEK, quote)
    if field == "hours_per_week":
        return _has(r"\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:hours?|hrs?|h)\b", quote)
    if field == "contract":
        return _has(
            r"contract|fixed[- ]term|temporary|\btemp\b|freelanc|consultan|"
            r"\d+[- ]?(?:month|week|year)s?\b|" + _MONTH_RANGE,
            quote,
        )
    if field == "term_length":
        return _has(
            r"(?:\d+|one|two|three|four|six|eight|twelve|sixteen)\s*(?:-\s*\d+\s*)?[- ]?"
            r"(?:weeks?|months?|years?|mos?)\b|" + _MONTH_RANGE,
            quote,
        )
    if field == "clearance":
        return _has(r"clearance|reliability|secret", quote)
    if field == "peng":
        return _has(r"p\.?\s?eng|professional engineer|\boiq\b|\bpeo\b|licen", quote)
    if field == "work_auth":
        return _has(
            r"citizen|permanent resident|sponsor|work permit|eligible to work|"
            r"authori[sz]ed to work|legally|entitled to work",
            quote,
        )
    return True


# Which posting_facts field a full-pass rejection rests on.
REJECTION_FIELD = {
    "french:posting_in_french": "french_requirement",
    "location:province": "province",
    "location:country": "country",
    "years": "min_years",
    "clearance": "clearance",
    "peng": "peng",
    "work_auth": "work_auth",
}
