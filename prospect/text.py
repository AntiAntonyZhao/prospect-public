"""Text utilities shared by ingest, filter, extract and score. No I/O."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "section",
    "article",
    "header",
    "footer",
    "table",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Plain text with paragraph breaks. Handles entity-escaped HTML (Greenhouse)."""
    if not markup:
        return ""
    if "&lt;" in markup and "<" not in markup:
        markup = html.unescape(markup)
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Typographic punctuation has no ASCII decomposition and would be dropped: "years’
# experience" → "years experience", "3–5 years" → "35 years". Map it first.
_TYPOGRAPHIC = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "–": "-",
        "—": "-",
        "‐": "-",
        "‑": "-",
        "−": "-",
        " ": " ",
    }
)


def ascii_fold(s: str) -> str:
    folded = unicodedata.normalize("NFKD", s.translate(_TYPOGRAPHIC))
    return folded.encode("ascii", "ignore").decode()


def collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


_COMPANY_SUFFIXES = {"inc", "ltd", "llc", "corp", "corporation", "limited", "co", "plc", "gmbh"}


def norm_company(name: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", ascii_fold(name).lower()).split()
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


# Seniority / location / arrangement tokens stripped from titles so the same role
# re-posted as "Software Engineer (Toronto)" and "Software Engineer - Remote" meets.
_TITLE_STRIP = {
    "senior",
    "sr",
    "junior",
    "jr",
    "new",
    "grad",
    "graduate",
    "newgrad",
    "intern",
    "internship",
    "coop",
    "co",
    "op",
    "entry",
    "level",
    "i",
    "ii",
    "iii",
    "iv",
    "1",
    "2",
    "3",
    "remote",
    "hybrid",
    "onsite",
    "on",
    "site",
    "canada",
    "ca",
    "usa",
    "us",
    "montreal",
    "toronto",
    "vancouver",
    "ottawa",
    "waterloo",
    "calgary",
    "quebec",
    "ontario",
    "qc",
    "bc",
    "mtl",
    "tor",
    "2025",
    "2026",
    "2027",
    "fall",
    "winter",
    "summer",
    "spring",
}


def norm_title(title: str) -> str:
    t = ascii_fold(title).lower()
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)
    words = [w for w in re.sub(r"[^a-z0-9+#]+", " ", t).split() if w not in _TITLE_STRIP]
    return " ".join(words)


def content_hash(company: str, title: str, description_text: str) -> str:
    """sha256(company_norm|title_norm|desc_norm) — schema.sql `postings.hash`."""
    desc = collapse_ws(description_text.lower())
    blob = f"{norm_company(company)}|{norm_title(title)}|{desc}".encode()
    return hashlib.sha256(blob).hexdigest()


def detect_language(text: str) -> str:
    """Deterministic posting_language ∈ {en, fr, bilingual} by stopword share.

    'bilingual' when the text has substantial runs of both languages (the
    common Quebec pattern of an English block followed by a French block).
    """
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", text.lower())
    if not words:
        return "en"
    en = sum(w in _EN_STOP for w in words)
    fr = sum(w in _FR_STOP for w in words)
    total = en + fr
    if total < 10:
        return "en"
    fr_share = fr / total
    if fr_share >= 0.7:
        return "fr"
    if fr_share >= 0.3:
        return "bilingual"
    return "en"


_EN_STOP = set(
    [
        "the",
        "and",
        "of",
        "to",
        "in",
        "for",
        "with",
        "on",
        "is",
        "are",
        "you",
        "we",
        "our",
        "will",
        "be",
        "as",
        "at",
        "an",
        "or",
        "this",
        "that",
        "from",
        "by",
        "your",
        "have",
        "has",
        "team",
        "work",
        "experience",
        "skills",
        "about",
        "role",
        "job",
    ]
)
_FR_STOP = set(
    [
        "le",
        "la",
        "les",
        "et",
        "des",
        "du",
        "de",
        "un",
        "une",
        "pour",
        "avec",
        "sur",
        "est",
        "sont",
        "vous",
        "nous",
        "notre",
        "votre",
        "sera",
        "au",
        "aux",
        "dans",
        "par",
        "ce",
        "cette",
        "qui",
        "que",
        "être",
        "avoir",
        "équipe",
        "travail",
        "expérience",
        "compétences",
        "poste",
    ]
)
