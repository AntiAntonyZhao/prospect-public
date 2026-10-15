"""Label in an editor: `prospect eval export` writes a Markdown file, `eval import` reads it.

The terminal labeler (`eval label`) wants one sitting; this lets the user fill a held-out
set over several sessions in VS Code. Each posting gets its full description (what the
model sees, no truncation) and one fenced block to fill:

    ```label 10124
    city: Toronto
    posting_language: en
    min_years: -
    seniority: newgrad
    ```

`-` means "not stated". A block left entirely blank is "not labeled yet" and skipped, so
importing a half-done file is safe and can be repeated (labels are upserted). A block
with some fields blank is reported, not saved. No model prediction is written into the
file (no anchoring).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from prospect.eval.label import CHOICES, FREE, _parse, save_labels
from prospect.models.posting import LABELED_FIELDS, RawPosting
from prospect.text import html_to_text

BLOCK_RE = re.compile(r"^```label (\d+)[ \t]*\n(.*?)^```", re.M | re.S)
# A Markdown formatter on save (VS Code + Prettier, first real use 2026-10-06) drops the
# fence's info string ("```label 41" → "```"), so the posting's `## 41 · …` heading is
# the fallback id: the last fenced block in a section is its label block.
SECTION_RE = re.compile(r"^## (\d+) ·", re.M)
FENCE_RE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)^\1[ \t]*$", re.M | re.S)


def label_blocks(text: str) -> list[tuple[int, str]]:
    """[(posting id, block body)] in file order, with or without the fence's id."""
    text = text.replace("\r\n", "\n")
    heads = list(SECTION_RE.finditer(text))
    out = []
    for i, h in enumerate(heads):
        section = text[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        tagged = BLOCK_RE.search(section)
        if tagged:
            out.append((int(tagged.group(1)), tagged.group(2)))
            continue
        fences = list(FENCE_RE.finditer(section))
        if fences:
            out.append((int(h.group(1)), fences[-1].group(2)))
    if not heads:  # a file of bare tagged blocks
        out = [(int(m.group(1)), m.group(2)) for m in BLOCK_RE.finditer(text)]
    return out


_HELP = {
    "city": FREE["city"].replace("'-' = not stated", "- = not stated"),
    "min_years": "integer lower bound of the general experience ask "
    '("3-5 years" → 3); - = not stated',
}


def _guide() -> str:
    lines = []
    for f in LABELED_FIELDS:
        if f in CHOICES:
            opts = " | ".join(CHOICES[f].values())
            keys = ", ".join(f"{k}={v}" for k, v in CHOICES[f].items())
            lines.append(f"- `{f}`: {opts} (shortcuts: {keys})")
        else:
            lines.append(f"- `{f}`: {_HELP.get(f, FREE.get(f, ''))}")
    return "\n".join(lines)


class LabelFileError(ValueError):
    pass


def export_label_file(conn: sqlite3.Connection, ids: list[int], path: Path) -> int:
    if path.suffix.lower() != ".md":
        raise LabelFileError(f"{path}: a label file must be a .md file")
    if path.exists():
        raise LabelFileError(f"{path} exists — it may hold labels; choose another --out")
    rows = {
        r["id"]: r
        for r in conn.execute(
            f"""SELECT p.*, c.name AS company FROM postings p JOIN companies c ON c.id=p.company_id
                WHERE p.id IN ({",".join("?" * len(ids))})""",
            ids,
        )
    }
    out = [
        f"# Held-out labels — {date.today().isoformat()}",
        "",
        f"{len(rows)} postings the extract prompt has never been tuned on. For each one, read "
        "the description and fill its `label` block. Save as you go; run "
        f"`uv run prospect eval import {path.as_posix()}` any time — blank blocks are skipped, "
        "so a half-done file imports safely.",
        "",
        "Values (same conventions as `prospect eval label`):",
        "",
        _guide(),
        "",
        "- A remote role (any remote option) is city `Remote`.",
        "- `-` = not stated. Leave a whole block blank to come back to it later.",
        "- Judge the posting, not this pipeline's policy: label what the text says.",
    ]
    for pid in ids:
        r = rows.get(pid)
        if r is None:
            continue
        raw = RawPosting.model_validate_json(r["raw_json"])
        text = html_to_text(raw.description_html).strip()
        out += [
            "",
            "---",
            "",
            f"## {pid} · {r['company']} — {r['title']}",
            "",
            f"- ATS location: {r['location_raw'] or '(none)'}",
            f"- URL: {r['url']}",
            "",
            *(f"> {ln}" if ln.strip() else ">" for ln in text.splitlines()),
            "",
            f"```label {pid}",
            *(f"{f}: " for f in LABELED_FIELDS),
            "```",
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only ever a new .md file: never factbank.yaml (Hard rule 1), never a half-labeled set.
    with path.open("x", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return len(rows)


@dataclass
class ImportResult:
    saved: list[int] = field(default_factory=list)
    blank: list[int] = field(default_factory=list)
    incomplete: dict[int, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self, total_labeled: int) -> str:
        lines = [
            f"imported {len(self.saved)} posting(s); {len(self.blank)} not labeled yet; "
            f"{total_labeled} labeled in total (target 200)"
        ]
        for pid, missing in self.incomplete.items():
            lines.append(f"  incomplete {pid}: fill {', '.join(missing)} (or '-' if not stated)")
        lines += [f"  error {e}" for e in self.errors]
        return "\n".join(lines)


def parse_block(body: str) -> tuple[dict[str, str], list[str]]:
    """'field: value  # comment' lines → ({field: raw}, unknown field names)."""
    values: dict[str, str] = {}
    unknown = []
    for line in body.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, raw = line.partition(":")
        name = name.strip()
        if name in LABELED_FIELDS:
            values[name] = raw.strip().removeprefix("\\")  # a formatter's "\-" is "-"
        else:
            unknown.append(name)
    return values, unknown


def import_label_file(conn: sqlite3.Connection, path: Path) -> ImportResult:
    res = ImportResult()
    text = path.read_text(encoding="utf-8")
    known = {r["id"] for r in conn.execute("SELECT id FROM postings")}
    for pid, body in label_blocks(text):
        raw, unknown = parse_block(body)
        if pid not in known:
            res.errors.append(f"{pid}: no such posting")
            continue
        if unknown:
            res.errors.append(f"{pid}: unknown field(s) {', '.join(unknown)}")
            continue
        filled = {f: v for f, v in raw.items() if v}
        if not filled:
            res.blank.append(pid)
            continue
        missing = [f for f in LABELED_FIELDS if not raw.get(f)]
        if missing:
            res.incomplete[pid] = missing
            continue
        values: dict[str, str | None] = {}
        try:
            for f in LABELED_FIELDS:
                values[f] = _parse(f, raw[f])
        except ValueError as e:
            res.errors.append(f"{pid} {f}: expected {e}")
            continue
        save_labels(conn, pid, values)
        res.saved.append(pid)
    return res


def label_file_ids(path: Path) -> list[int]:
    return [pid for pid, _ in label_blocks(path.read_text(encoding="utf-8"))]
