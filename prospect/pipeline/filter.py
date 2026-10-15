"""filter: hard rules → postings.status. No AI (CLAUDE.md "Hard filters").

Runs twice:
  * `prepass` on raw metadata, before rank/extract. The French regex only
    FLAGS here; rejection on French happens after extract classifies it. The
    pre-pass rejects only what raw metadata settles unambiguously: a location
    list with no Canadian or ambiguous entry, an explicitly senior title, or a
    duplicate of an application inside the dedup window.
  * `full_pass` on posting_facts after extract. A rule backed by an extraction
    below `filter.min_confidence_to_reject`, or resting on a field that kept no
    supporting evidence quote, flags instead of rejecting. Years are the
    exception (policy 2026-10-06): a verbatim quote stating them, or a must-have
    requirement whose own text states them, rejects whatever the confidence.
    It re-judges postings it rejected before, so a rule change can restore them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from prospect.config import Settings
from prospect.db import transaction
from prospect.factbank.loader import EffectiveTargets, effective_targets
from prospect.logs import get_logger
from prospect.models.factbank import FactBank
from prospect.models.posting import RawPosting
from prospect.pipeline.guards import NICE_TO_HAVE_RE, REJECTION_FIELD
from prospect.progress import Progress, report
from prospect.text import html_to_text

log = get_logger("filter")

FRENCH_RE = re.compile(r"fran[cç]ais|bilingu|french", re.I)
SENIOR_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|vice president)\b", re.I
)
_PROV = r"(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)"
CANADA_RE = re.compile(
    r"\bcanad(?:a|ian)\b|,\s*" + _PROV + r"\b|\b(?:toronto|montr[eé]al|vancouver|ottawa|waterloo|"
    r"kitchener|calgary|edmonton|qu[eé]bec|winnipeg|halifax|mississauga|markham|burnaby|laval|"
    r"gatineau|oakville|kanata|guelph|burlington|richmond hill|vaughan|brampton|sherbrooke|"
    r"longueuil|boucherville|saint-laurent|l[eé]vis|trois-rivi[eè]res|kelowna|saskatoon|regina|"
    r"fredericton|moncton|charlottetown|greater toronto|gta|"
    r"ontario|british columbia|alberta|manitoba|saskatchewan|nova scotia|"
    r"new brunswick|newfoundland)\b",
    re.I,
)
_US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|"
    "NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_NON_CANADA_WORDS = (
    # countries / regions
    r"united states|usa|u\.s\.a?\.?|us|uk|gb|united kingdom|england|scotland|wales|ireland|"
    r"germany|france|netherlands|spain|portugal|poland|india|singapore|japan|australia|brazil|"
    r"mexico|israel|china|hong kong|korea|taiwan|philippines|argentina|colombia|emea|apac|latam|"
    r"europe|switzerland|sweden|denmark|italy|romania|czech(?:ia| republic)?|austria|belgium|"
    r"finland|norway|estonia|ukraine|serbia|egypt|uae|dubai|south africa|nigeria|kenya|vietnam|"
    r"indonesia|malaysia|thailand|new zealand|chile|peru|costa rica|senegal|ghana|morocco|"
    r"tunisia|turkey|t[uü]rkiye|greece|hungary|bulgaria|croatia|slovakia|slovenia|lithuania|"
    r"latvia|luxembourg|iceland|pakistan|bangladesh|sri lanka|nepal|saudi arabia|qatar|kuwait|"
    r"bahrain|jordan|lebanon|uruguay|paraguay|bolivia|ecuador|venezuela|guatemala|panama|"
    r"puerto rico|dominican republic|jamaica|cyprus|malta|armenia|kazakhstan|uzbekistan|"
    # US states
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    r"hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    r"michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|"
    r"south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west virginia|wisconsin|wyoming|d\.c\.|"
    # cities
    r"san francisco|new york|nyc|seattle|austin|boston|chicago|los angeles|denver|atlanta|miami|"
    r"palo alto|mountain view|sunnyvale|san jose|san mateo|redmond|nashville|portland|"
    r"philadelphia|pittsburgh|detroit|minneapolis|dallas|houston|phoenix|salt lake city|raleigh|"
    r"durham|san diego|irvine|santa monica|el segundo|menlo park|cupertino|bellevue|kirkland|"
    r"cambridge|jersey city|stamford|charlotte|columbus|cincinnati|st\.? louis|kansas city|"
    r"indianapolis|baltimore|reston|arlington|tampa|orlando|sacramento|oakland|berkeley|boulder|"
    r"lehi|scottsdale|tempe|london|dublin|cork|galway|berlin|munich|hamburg|frankfurt|cologne|"
    r"stuttgart|paris|lyon|toulouse|amsterdam|rotterdam|eindhoven|antwerp|madrid|barcelona|"
    r"valencia|lisbon|porto|warsaw|krakow|wroclaw|gdansk|bucharest|cluj|kyiv|kiev|lviv|belgrade|"
    r"tallinn|helsinki|gothenburg|zurich|geneva|lausanne|basel|stockholm|copenhagen|aarhus|oslo|"
    r"milan|rome|prague|vienna|brussels|edinburgh|manchester|belfast|bristol|glasgow|leeds|"
    r"bangalore|bengaluru|hyderabad|pune|mumbai|delhi|gurgaon|gurugram|noida|chennai|kolkata|"
    r"ahmedabad|tokyo|osaka|sydney|melbourne|brisbane|auckland|s[aã]o paulo|rio de janeiro|"
    r"buenos aires|santiago|lima|bogot[aá]|medell[ií]n|mexico city|guadalajara|monterrey|"
    r"tel aviv|haifa|jerusalem|herzliya|shanghai|beijing|shenzhen|hangzhou|taipei|seoul|manila|"
    r"jakarta|kuala lumpur|bangkok|ho chi minh|hanoi|cairo|lagos|nairobi|cape town|"
    r"johannesburg|istanbul|athens|budapest|sofia|zagreb|vilnius|riga|riyadh|doha|dakar"
)
NON_CANADA_RE = re.compile(
    r",\s*(?:" + _US_STATES + r")\b|\b(?:" + _NON_CANADA_WORDS + r")\b", re.I
)
# Parts that leave Canada possible: bare remote, worldwide, the Americas.
OPEN_SCOPE_RE = re.compile(
    r"\b(?:remote|worldwide|anywhere|global(?:ly)?|americas|north america|distributed)\b", re.I
)
_REMOTE_WORD_RE = re.compile(r"\bremote\b", re.I)
_NOT_REMOTE_RE = re.compile(r"\b(?:not|non|no)[- ]remote\b", re.I)


def metadata_remote_entry(location_raw: str | None) -> str | None:
    """The first ATS location entry offering remote work open to Canada, e.g. 'Remote - Canada'.

    Policy (2026-10): any remote option makes a role remote, even when the body also
    describes an office policy. An entry naming only a place outside Canada
    ('Remote, United States') is not an option for this candidate.
    """
    for part in (location_raw or "").split(";"):
        entry = part.strip()
        if not _REMOTE_WORD_RE.search(entry) or _NOT_REMOTE_RE.search(entry):
            continue
        if NON_CANADA_RE.search(entry) and not CANADA_RE.search(entry):
            continue
        return entry
    return None


_PLACEHOLDER_PARTS = re.compile(r"^\s*(?:n/?a|tbd|multiple locations?|various)\s*$", re.I)

CANADIAN_PROVINCES = {"QC", "ON", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU"}

INCOMPATIBLE_AUTH: dict[str, set[str]] = {
    "citizen": set(),
    "pr": {"citizen_only"},
    "pgwp": {"citizen_only", "citizen_or_pr"},
    "needs_sponsorship": {"citizen_only", "citizen_or_pr", "no_sponsorship"},
}


class FilterError(RuntimeError):
    pass


@dataclass
class FilterResult:
    name: str
    examined: int = 0
    candidates: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    flagged: Counter[str] = field(default_factory=Counter)
    restored: int = 0  # rejected by an earlier full pass, candidates again under today's rules

    def summary(self) -> str:
        rej = ", ".join(f"{k} {v}" for k, v in self.rejected.most_common()) or "none"
        flg = ", ".join(f"{k} {v}" for k, v in self.flagged.most_common()) or "none"
        back = f" ({self.restored} restored)" if self.restored else ""
        return (
            f"{self.name}: {self.examined} examined → {sum(self.rejected.values())} rejected "
            f"({rej}); {self.candidates} candidates{back}; flags: {flg}"
        )


def classify_location_raw(location_raw: str | None) -> str:
    """'canada' | 'non_canada' | 'unknown' from ATS metadata alone.

    'non_canada' when no part is Canadian or open-scope (bare remote, worldwide,
    the Americas) and at least one part is recognisably elsewhere. Unrecognised
    sub-labels ("Nashville AV Depot", "N/A") don't block the verdict.
    """
    if not location_raw or not location_raw.strip():
        return "unknown"
    if CANADA_RE.search(location_raw):
        return "canada"
    parts = [p for p in re.split(r"[;|]|\bor\b|\n", location_raw) if p.strip()]
    parts = [re.sub(r"\((?:remote|hybrid|on-?site|in office)\)", "", p, flags=re.I) for p in parts]
    parts = [p for p in parts if p.strip() and not _PLACEHOLDER_PARTS.match(p)]
    foreign = [p for p in parts if NON_CANADA_RE.search(p)]
    open_scope = [p for p in parts if OPEN_SCOPE_RE.search(p) and not NON_CANADA_RE.search(p)]
    if foreign and not open_scope:
        return "non_canada"
    return "unknown"


def _flags(row: sqlite3.Row) -> set[str]:
    try:
        return set(json.loads(row["flags"] or "[]"))
    except (TypeError, ValueError):
        return set()


def _duplicate_application(conn: sqlite3.Connection, row: sqlite3.Row, window_days: int) -> bool:
    return bool(
        conn.execute(
            """SELECT 1 FROM applications a JOIN postings p ON p.id = a.posting_id
               WHERE p.company_id = ? AND p.title_norm = ? AND p.id != ?
                 AND a.created_at >= datetime('now', ?)""",
            (row["company_id"], row["title_norm"], row["id"], f"-{window_days} days"),
        ).fetchone()
    )


def _ghost(conn: sqlite3.Connection, row: sqlite3.Row, no_response_days: int) -> bool:
    """repost_count ≥ 3 and a prior application to the same (company, title_norm) got no reply."""
    if row["repost_count"] < 3:
        return False
    return bool(
        conn.execute(
            """SELECT 1 FROM applications a JOIN postings p ON p.id = a.posting_id
               WHERE p.company_id = ? AND p.title_norm = ?
                 AND a.status IN ('submitted', 'ghosted')
                 AND a.submitted_at <= datetime('now', ?)""",
            (row["company_id"], row["title_norm"], f"-{no_response_days} days"),
        ).fetchone()
    )


def prepass(
    settings: Settings,
    conn: sqlite3.Connection,
    fb: FactBank,
    progress: Progress | None = None,
) -> FilterResult:
    del fb  # the pre-pass reads raw metadata only
    cfg = settings.filter
    res = FilterResult("prepass")
    rows = conn.execute("SELECT * FROM postings WHERE status = 'new'").fetchall()
    step = max(1, len(rows) // 25)
    with transaction(conn):  # one pass, one transaction: a stop keeps nothing of it
        for i, row in enumerate(rows):
            if i % step == 0:
                report(progress, i / len(rows), f"pre-pass {i}/{len(rows)}")
            res.examined += 1
            flags = _flags(row)
            raw = RawPosting.model_validate_json(row["raw_json"])
            if FRENCH_RE.search(f"{row['title']}\n{html_to_text(raw.description_html)}"):
                flags.add("french_regex")
            if _ghost(conn, row, settings.score.no_response_days):
                flags.add("ghost")
            reason = None
            if _duplicate_application(conn, row, settings.dedup.window_days):
                reason = "duplicate_application"
            elif cfg.prepass_reject_senior_titles and SENIOR_TITLE_RE.search(row["title"]):
                reason = "senior_title"
            elif cfg.prepass_reject_location and classify_location_raw(row["location_raw"]) == (
                "non_canada"
            ):
                reason = "location_metadata"
            for f in flags - _flags(row):
                res.flagged[f] += 1
            if reason:
                res.rejected[reason] += 1
            else:
                res.candidates += 1
            conn.execute(
                "UPDATE postings SET status=?, reject_reason=?, flags=? WHERE id=?",
                (
                    "rejected" if reason else "candidate",
                    reason,
                    json.dumps(sorted(flags)),
                    row["id"],
                ),
            )
            log.debug(
                "prepass", id=row["id"], status="rejected" if reason else "candidate", reason=reason
            )
    return res


def full_pass_reason(
    facts: sqlite3.Row,
    targets: EffectiveTargets,
    work_authorization: str,
    required_years: int | None = None,
) -> str | None:
    """First failing hard rule for one posting's facts, or None. Pure function.

    `required_years`: the most years any must-have skill requirement asks for. Extract kept
    those numbers only where the requirement's text states them verbatim.
    """
    # Policy (2026-10): an English posting's French requirement is Quebec boilerplate for
    # CS roles — never a reason to reject. Only a posting written in French is.
    if facts["posting_language"] == "fr" or facts["french_requirement"] == "posting_in_french":
        return "french:posting_in_french"
    # Anywhere in Canada; outside Canada only when remote work from Canada is allowed.
    province = (facts["province"] or "").upper() or None
    country = (facts["country"] or "").upper() or None
    remote_ok = facts["remote_scope"] in targets.remote_scope_ok
    if country and country != "CA" and not remote_ok:
        return "location:country"
    if not country and province and province not in CANADIAN_PROVINCES and not remote_ok:
        return "location:province"
    if facts["min_years"] is not None and facts["min_years"] > targets.max_years:
        return "years"
    if required_years is not None and required_years > targets.max_years:
        return "years:requirement"
    if facts["clearance"]:
        return "clearance"
    if facts["peng"]:
        return "peng"
    if facts["work_auth"] in INCOMPATIBLE_AUTH.get(work_authorization, set()):
        return "work_auth"
    return None


def full_pass(
    settings: Settings,
    conn: sqlite3.Connection,
    fb: FactBank,
    progress: Progress | None = None,
) -> FilterResult:
    auth = fb.identity.work_authorization
    if auth is None:
        raise FilterError(
            "identity.work_authorization is not set in factbank.yaml "
            "(citizen | pr | pgwp | needs_sponsorship); the work-auth rule cannot run. "
            "Fill it in by hand."
        )
    targets = effective_targets(settings, fb)
    min_conf = settings.filter.min_confidence_to_reject
    res = FilterResult("full pass")
    # Postings this pass rejected earlier are judged again: the rules may have changed
    # (policy 2026-10-06 raised the years cap), and a stage re-run reflects today's rules.
    own = (*REJECTION_FIELD, "years:requirement")
    rows = conn.execute(
        f"""SELECT p.*, f.city, f.province, f.country, f.work_arrangement, f.remote_scope,
                  f.posting_language, f.french_requirement, f.min_years, f.seniority,
                  f.clearance, f.peng, f.work_auth, f.confidence, f.evidence_json,
                  (SELECT MAX(r.years) FROM requirements r WHERE r.posting_id = p.id
                     AND r.kind = 'skill' AND r.must_have = 1) AS required_years
           FROM postings p JOIN posting_facts f ON f.posting_id = p.id
           WHERE p.status = 'candidate'
              OR (p.status = 'rejected' AND p.reject_reason IN ({",".join("?" * len(own))}))""",
        own,
    ).fetchall()
    step = max(1, len(rows) // 25)
    with transaction(conn):  # one pass, one transaction: a stop keeps nothing of it
        for i, row in enumerate(rows):
            if i % step == 0:
                report(progress, i / len(rows), f"full pass {i}/{len(rows)}")
            res.examined += 1
            # Flags from an earlier pass are recomputed, not accumulated.
            flags = {f for f in _flags(row) if not f.startswith(_PASS_FLAGS)}
            reason = full_pass_reason(row, targets, auth, row["required_years"])
            # A rejection must rest on a field whose value a verbatim quote visibly states.
            evidence = json.loads(row["evidence_json"] or "{}")
            quote = evidence.get(REJECTION_FIELD.get(reason or "", ""))
            confident = (row["confidence"] or 0) >= min_conf
            flag = None
            if reason == "french:posting_in_french" and row["posting_language"] == "fr":
                reject = True  # deterministic language detection
            elif reason == "years:requirement":
                reject = True  # extract kept that number only where the text states it
            elif reason == "years" and quote and NICE_TO_HAVE_RE.search(quote):
                reject, flag = False, "optional:years"  # "5+ years preferred" is no minimum
            elif reason == "years":
                # Policy 2026-10-06: a verbatim "5+ years" is enough. Posting-level confidence
                # mostly reflects other fields (a long location list), not this quote.
                reject = quote is not None
            else:
                reject = bool(reason) and confident and quote is not None
            if reason and reject:
                res.rejected[reason] += 1
                conn.execute(
                    "UPDATE postings SET status='rejected', reject_reason=?, flags=? WHERE id=?",
                    (reason, json.dumps(sorted(flags)), row["id"]),
                )
                log.info("full_pass", id=row["id"], status="rejected", reason=reason)
                continue
            if reason:
                kind = "weak_evidence" if confident or reason == "years" else "low_confidence"
                flags.add(flag or f"{kind}:{reason}")
            if _ghost(conn, row, settings.score.no_response_days):
                flags.add("ghost")
            for f in flags - _flags(row):
                res.flagged[f] += 1
            if row["status"] == "rejected":
                res.restored += 1
            res.candidates += 1
            conn.execute(
                "UPDATE postings SET status='candidate', reject_reason=NULL, flags=? WHERE id=?",
                (json.dumps(sorted(flags)), row["id"]),
            )
    return res


_PASS_FLAGS = ("low_confidence:", "weak_evidence:", "optional:")
