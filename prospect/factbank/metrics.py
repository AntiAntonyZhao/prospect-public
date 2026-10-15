"""Numeric tokens in bullets (docs/FACTBANK.md `metrics`).

`NUMERIC_TOKEN_RE` is the documented regex, verbatim. Lint uses `metric_tokens`,
the same regex with a left boundary: digits glued to a preceding letter (`GPT-4`,
`STL10`, `S3`) are part of a name, not a metric, and the hand-written `metrics`
lists in the fact bank deliberately omit them.
"""

from __future__ import annotations

import re

NUMERIC_TOKEN_RE = re.compile(r"~?\d+(?:[.,]\d+)?(?:%|[KMB]|x)?\+?")
_METRIC_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?<![A-Za-z]-)" + NUMERIC_TOKEN_RE.pattern)


def metric_tokens(text: str) -> list[str]:
    """Numeric tokens that are metrics, in order of appearance (lint side)."""
    return _METRIC_TOKEN_RE.findall(text)
