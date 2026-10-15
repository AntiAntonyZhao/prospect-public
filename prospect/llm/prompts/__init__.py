"""Versioned prompt files: `{stage}_v{N}.md`. Never inline prompts in code.

Bumping a prompt = adding `{stage}_v{N+1}.md`; the new version string lands in
`runs.prompt_version` and `posting_facts.prompt_version`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).parent
_NAME = re.compile(r"^(?P<stage>[a-z_]+)_v(?P<n>\d+)\.md$")


@dataclass(frozen=True)
class Prompt:
    stage: str
    version: str  # e.g. "extract_v1"
    text: str


def available(stage: str) -> list[int]:
    out = []
    for p in PROMPT_DIR.glob(f"{stage}_v*.md"):
        m = _NAME.match(p.name)
        if m and m["stage"] == stage:
            out.append(int(m["n"]))
    return sorted(out)


def load_prompt(stage: str, version: int | None = None) -> Prompt:
    versions = available(stage)
    if not versions:
        raise FileNotFoundError(f"no prompt files for stage {stage!r} in {PROMPT_DIR}")
    n = version if version is not None else versions[-1]
    path = PROMPT_DIR / f"{stage}_v{n}.md"
    return Prompt(stage=stage, version=f"{stage}_v{n}", text=path.read_text(encoding="utf-8"))
