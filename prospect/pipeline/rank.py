"""rank: local embedding + cosine vs the fact bank profile → shortlist. No API calls.

The profile is embedded as a *query* (bge's retrieval instruction prefix) and
postings as *passages*, so cosine reads as "how well does this posting answer
a search for this candidate". Vectors are cached in `embeddings`; cosine is
recomputed whenever the profile (i.e. factbank.yaml) changes.

The shortlist is the top-N candidates by cosine; `extract` works on the
shortlisted postings that have no facts yet. Candidates not seen by any feed
for `STALE_DAYS` are parked as ignored/closed so they stop consuming budget.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from prospect.config import Settings
from prospect.db import transaction
from prospect.factbank.loader import effective_targets
from prospect.logs import get_logger
from prospect.models.factbank import LEVEL_RANK, FactBank
from prospect.models.posting import RawPosting
from prospect.progress import Progress, report
from prospect.text import html_to_text

log = get_logger("rank")

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
STALE_DAYS = 4


class Embedder(Protocol):
    name: str

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Unit-normalized float32 vectors, one row per text."""
        ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str) -> None:
        self.name = model_name
        self._model = None

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # slow import; lazy

            self._model = SentenceTransformer(self.name, device="cpu")
        vecs = self._model.encode(
            list(texts), batch_size=32, normalize_embeddings=True, show_progress_bar=False
        )
        return np.asarray(vecs, dtype=np.float32)


def profile_text(settings: Settings, fb: FactBank) -> str:
    """What the candidate is looking for, in words: roles + real skills + best bullets."""
    targets = effective_targets(settings, fb)
    skills = sorted(
        (s for s in fb.skills if s.level != "familiar"),
        key=lambda s: (-LEVEL_RANK[s.level], fb.skill_order[s.id]),
    )
    bullets = [b.text for b in fb.bullet_map.values() if b.strength >= 4]
    return (
        f"{' / '.join(targets.roles)} position for a new graduate or junior engineer "
        f"({', '.join(targets.seniority)}) in {', '.join(targets.cities)}. "
        f"Skills: {', '.join(s.label for s in skills)}. "
        f"Experience: {' '.join(bullets)}"
    )


def posting_text(row: sqlite3.Row, max_chars: int) -> str:
    raw = RawPosting.model_validate_json(row["raw_json"])
    body = html_to_text(raw.description_html)
    head = f"{row['title']} at {raw.company}. {row['location_raw'] or ''}\n"
    return (head + body)[:max_chars]


@dataclass
class RankResult:
    candidates: int
    embedded: int
    closed: int
    top: list[tuple[int, float]]

    def summary(self) -> str:
        head = (
            f"rank: {self.candidates} candidates, {self.embedded} newly embedded, "
            f"{self.closed} parked as closed; shortlist {len(self.top)}"
        )
        if self.top:
            head += f" (cosine {self.top[0][1]:.3f} … {self.top[-1][1]:.3f})"
        return head


def park_closed(conn: sqlite3.Connection, stale_days: int = STALE_DAYS) -> int:
    with transaction(conn):
        cur = conn.execute(
            """UPDATE postings SET status='ignored', reject_reason='closed'
               WHERE status IN ('new', 'candidate') AND source != 'manual'
                 AND last_seen < datetime('now', ?)""",
            (f"-{stale_days} days",),
        )
    return cur.rowcount


def run_rank(
    settings: Settings,
    conn: sqlite3.Connection,
    fb: FactBank,
    top_n: int | None = None,
    embedder: Embedder | None = None,
    progress: Progress | None = None,
) -> RankResult:
    top_n = top_n or settings.rank.top_n
    embedder = embedder or SentenceTransformerEmbedder(settings.rank.model)
    closed = park_closed(conn)

    profile = profile_text(settings, fb)
    profile_hash = hashlib.sha256(f"{embedder.name}|{profile}".encode()).hexdigest()[:16]

    rows = conn.execute(
        """SELECT p.*, e.model AS emb_model, e.vector, e.profile_hash
           FROM postings p LEFT JOIN embeddings e ON e.posting_id = p.id
           WHERE p.status = 'candidate'"""
    ).fetchall()
    todo = [r for r in rows if r["vector"] is None or r["emb_model"] != embedder.name]
    todo_ids = {r["id"] for r in todo}
    stale = [r for r in rows if r["id"] not in todo_ids and r["profile_hash"] != profile_hash]

    report(progress, 0.1, f"embedding {len(todo)} new of {len(rows)} candidates")
    pvec = embedder.encode([QUERY_PREFIX + profile])[0]
    with transaction(conn):
        if todo:
            vecs = embedder.encode([posting_text(r, settings.rank.max_chars) for r in todo])
            for r, v in zip(todo, vecs, strict=True):
                conn.execute(
                    """INSERT INTO embeddings (posting_id, model, vector, cosine, profile_hash)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(posting_id) DO UPDATE SET model=excluded.model,
                         vector=excluded.vector, cosine=excluded.cosine,
                         profile_hash=excluded.profile_hash, created_at=datetime('now')""",
                    (
                        r["id"],
                        embedder.name,
                        v.astype(np.float32).tobytes(),
                        float(v @ pvec),
                        profile_hash,
                    ),
                )
        for r in stale:
            v = np.frombuffer(r["vector"], dtype=np.float32)
            conn.execute(
                "UPDATE embeddings SET cosine=?, profile_hash=? WHERE posting_id=?",
                (float(v @ pvec), profile_hash, r["id"]),
            )
    top = conn.execute(
        """SELECT p.id, e.cosine FROM postings p JOIN embeddings e ON e.posting_id = p.id
           WHERE p.status = 'candidate' ORDER BY e.cosine DESC LIMIT ?""",
        (top_n,),
    ).fetchall()
    log.info("ranked", candidates=len(rows), embedded=len(todo), rescored=len(stale))
    return RankResult(len(rows), len(todo), closed, [(r["id"], r["cosine"]) for r in top])


def shortlist_ids(conn: sqlite3.Connection, top_n: int) -> list[int]:
    rows = conn.execute(
        """SELECT p.id FROM postings p JOIN embeddings e ON e.posting_id = p.id
           WHERE p.status = 'candidate' ORDER BY e.cosine DESC LIMIT ?""",
        (top_n,),
    ).fetchall()
    return [r["id"] for r in rows]
