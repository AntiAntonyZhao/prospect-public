"""`prospect demo`: the whole pipeline, offline, on fictional data.

    ingest → filter (pre-pass) → rank → extract → filter (full) → score → digest

Every stage is the real stage function; the demo only swaps what sits behind the
network. Postings come from `demo/postings/<ats>/<token>.json` (the response shapes
of the public Greenhouse, Lever and Ashby APIs) through the real source adapters.
Model calls go through the real `LLMClient` (budget check, Batch path, tool parsing,
cost logging to `runs`), with the Anthropic SDK client replaced by a replay of
responses recorded from the real API (`demo/responses/`). `prospect demo --record`
makes those recordings again with a key.

A replayed response is found by a hash of the full request (model, prompt, fact bank,
tool schema, posting text), so a change to any of them shows up as a replay miss
instead of a silently stale answer.

The candidate (`demo/factbank_demo.yaml`), the companies and the postings are all
fictional. Settings are the code defaults, never your prospect.toml, so the requests
match the recording on every machine.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from prospect.config import PathsCfg, Settings, load_settings
from prospect.db import connect, init_db
from prospect.factbank.loader import FactBankError, ensure_lint_clean
from prospect.llm.client import LLMClient, LLMError
from prospect.llm.pricing import cost_report
from prospect.pipeline.digest import format_digest
from prospect.pipeline.extract import run_extract
from prospect.pipeline.filter import FilterError, full_pass, prepass
from prospect.pipeline.ingest import run_ingest
from prospect.pipeline.rank import Embedder, SentenceTransformerEmbedder, posting_text, run_rank
from prospect.pipeline.score import run_score
from prospect.sources.ashby import API as ASHBY_API
from prospect.sources.ashby import Ashby
from prospect.sources.base import Http, load_companies
from prospect.sources.greenhouse import API as GREENHOUSE_API
from prospect.sources.greenhouse import Greenhouse
from prospect.sources.lever import API as LEVER_API
from prospect.sources.lever import Lever

Echo = Callable[[str], None]

STAGES = ("extract", "score")


class DemoError(RuntimeError):
    pass


class ReplayMiss(LLMError):
    """No recorded response for this exact request."""


def demo_settings(root: Path, db_path: Path) -> Settings:
    return Settings(
        root=root,
        paths=PathsCfg(
            factbank="demo/factbank_demo.yaml",
            companies="demo/companies_demo.yaml",
            db=str(db_path),
        ),
    )


# --- postings: the real adapters over local files ----------------------------------------


class DemoHttp(Http):
    """Serves a board API URL from demo/postings/<ats>/<token>.json. No network."""

    ROUTES = (
        ("greenhouse", GREENHOUSE_API),
        ("lever", LEVER_API),
        ("ashby", ASHBY_API),
    )

    def __init__(self, postings_dir: Path) -> None:
        self.postings_dir = postings_dir

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        for ats, api in self.ROUTES:
            m = re.fullmatch(re.escape(api).replace(r"\{token\}", "(?P<token>[^/]+)"), url)
            if m:
                path = self.postings_dir / ats / f"{m['token']}.json"
                if not path.exists():
                    raise DemoError(f"demo has no board file {path}")
                return json.loads(path.read_text(encoding="utf-8"))
        raise DemoError(f"demo has no route for {url}")

    def close(self) -> None:
        pass


# --- model responses: replay and record ------------------------------------------------


def request_key(params: dict[str, Any]) -> str:
    blob = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _label(params: dict[str, Any]) -> str:
    content = params["messages"][0]["content"]
    lines = [ln for ln in content.splitlines() if ln.strip()][:2]
    return " | ".join(lines)[:160]


def message_to_dict(message: Any) -> dict[str, Any]:
    usage = message.usage
    return {
        "model": message.model,
        "stop_reason": message.stop_reason,
        "usage": {
            "input_tokens": usage.input_tokens or 0,
            "output_tokens": usage.output_tokens or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        },
        # Only the tool call is kept: it is all `LLMClient` reads from a response.
        "content": [
            {"type": "tool_use", "name": b.name, "input": b.input}
            for b in message.content
            if getattr(b, "type", None) == "tool_use"
        ],
    }


def dict_to_message(d: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        model=d["model"],
        stop_reason=d["stop_reason"],
        usage=SimpleNamespace(**d["usage"]),
        content=[SimpleNamespace(**b) for b in d["content"]],
    )


class ResponseStore:
    """demo/responses/<stage>.json: recorded responses keyed by request hash.

    A recording starts empty (`load=False`), so the files hold exactly the responses of
    the last `--record` run and nothing stale.
    """

    def __init__(self, directory: Path, load: bool = True) -> None:
        self.directory = directory
        self.entries: dict[str, dict[str, Any]] = {}
        for stage in STAGES if load else ():
            path = directory / f"{stage}.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                for e in data["responses"]:
                    self.entries[e["key"]] = e

    def get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        e = self.entries.get(request_key(params))
        return e["message"] if e else None

    def put(self, stage: str, params: dict[str, Any], message: Any) -> None:
        self.entries[request_key(params)] = {
            "key": request_key(params),
            "stage": stage,
            "request": _label(params),
            "message": message_to_dict(message),
        }

    def save(self, recorded_at: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for stage in STAGES:
            rows = sorted(
                (e for e in self.entries.values() if e["stage"] == stage),
                key=lambda e: (e["request"], e["key"]),
            )
            doc = {
                "about": (
                    f"Responses to `prospect demo --record` ({stage} stage), recorded "
                    f"{recorded_at} from the Anthropic API for the fictional demo postings. "
                    "Keyed by a hash of the full request; replayed by `prospect demo`."
                ),
                "responses": rows,
            }
            (self.directory / f"{stage}.json").write_text(
                json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )


def _stage_of(params: dict[str, Any]) -> str:
    tool = params["tools"][0]["name"]
    return "extract" if tool == "record_posting" else "score"


class _ReplayBatches:
    def __init__(self, parent: ReplayAnthropic) -> None:
        self.parent = parent
        self.pending: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def create(self, requests: list[dict[str, Any]]) -> SimpleNamespace:
        found = []
        for r in requests:
            msg = self.parent.lookup(r["params"])
            found.append((r["custom_id"], msg))
        batch_id = f"demo_replay_batch_{len(self.pending) + 1}"
        self.pending[batch_id] = found
        return SimpleNamespace(id=batch_id)

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=batch_id, processing_status="ended")

    def results(self, batch_id: str) -> Iterator[SimpleNamespace]:
        for cid, msg in self.pending.pop(batch_id):
            yield SimpleNamespace(
                custom_id=cid,
                result=SimpleNamespace(type="succeeded", message=dict_to_message(msg)),
            )


class _ReplayMessages:
    def __init__(self, parent: ReplayAnthropic) -> None:
        self.parent = parent
        self.batches = _ReplayBatches(parent)

    def create(self, **params: Any) -> SimpleNamespace:
        return dict_to_message(self.parent.lookup(params))


class ReplayAnthropic:
    """Stands in for `anthropic.Anthropic`: answers from the recorded responses."""

    def __init__(self, store: ResponseStore) -> None:
        self.store = store
        self.messages = _ReplayMessages(self)
        self.served = Counter[str]()
        self.misses: list[str] = []

    def lookup(self, params: dict[str, Any]) -> dict[str, Any]:
        msg = self.store.get(params)
        if msg is None:
            self.misses.append(f"{_stage_of(params)}: {_label(params)}")
            raise ReplayMiss(
                "no recorded response for this request; the prompts, the demo fact bank or "
                "the demo postings changed since demo/responses/ was recorded "
                "(re-record with `prospect demo --record`)"
            )
        self.served[_stage_of(params)] += 1
        return msg


class _RecordBatches:
    def __init__(self, parent: RecordingAnthropic) -> None:
        self.parent = parent
        self.params: dict[str, dict[str, Any]] = {}

    def create(self, requests: list[dict[str, Any]]) -> Any:
        for r in requests:
            self.params[r["custom_id"]] = r["params"]
        return self.parent.real.messages.batches.create(requests=requests)

    def retrieve(self, batch_id: str) -> Any:
        return self.parent.real.messages.batches.retrieve(batch_id)

    def results(self, batch_id: str) -> Iterator[Any]:
        for r in self.parent.real.messages.batches.results(batch_id):
            if r.result.type == "succeeded":
                params = self.params[r.custom_id]
                self.parent.store.put(_stage_of(params), params, r.result.message)
            yield r


class _RecordMessages:
    def __init__(self, parent: RecordingAnthropic) -> None:
        self.parent = parent
        self.batches = _RecordBatches(parent)

    def create(self, **params: Any) -> Any:
        message = self.parent.real.messages.create(**params)
        self.parent.store.put(_stage_of(params), params, message)
        return message


class RecordingAnthropic:
    """Wraps a real `anthropic.Anthropic` and keeps every successful response."""

    def __init__(self, real: Any, store: ResponseStore) -> None:
        self.real = real
        self.store = store
        self.messages = _RecordMessages(self)


# --- rank fallback ---------------------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9+#]+")


class TfidfEmbedder:
    """Demo-only stand-in for bge when the model can't be downloaded.

    Sublinear TF-IDF over the demo corpus, L2-normalized, so cosine still means "shares
    distinctive words". It ranks worse than bge; `prospect run` never uses it.
    """

    name = "tfidf-demo-fallback"

    def __init__(self, corpus: Sequence[str]) -> None:
        docs = [set(_TOKEN.findall(t.lower())) for t in corpus]
        df = Counter(tok for d in docs for tok in d)
        n = len(docs)
        self.vocab = {tok: i for i, tok in enumerate(sorted(df))}
        self.idf = np.array(
            [math.log((1 + n) / (1 + df[tok])) + 1 for tok in sorted(df)], dtype=np.float32
        )

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), len(self.vocab)), dtype=np.float32)
        for row, text in enumerate(texts):
            for tok, count in Counter(_TOKEN.findall(text.lower())).items():
                col = self.vocab.get(tok)
                if col is not None:
                    out[row, col] = (1 + math.log(count)) * self.idf[col]
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


def pick_embedder(settings: Settings, conn: sqlite3.Connection, echo: Echo) -> Embedder:
    """bge (downloaded on first use); TF-IDF over the demo postings only if that fails."""
    bge = SentenceTransformerEmbedder(settings.rank.model)
    try:
        bge.encode(["warm-up"])
        return bge
    except Exception as e:  # no network, no disk space, a proxy …
        rows = conn.execute("SELECT * FROM postings WHERE status = 'candidate'").fetchall()
        echo(
            f"WARNING: could not load {settings.rank.model} ({type(e).__name__}: "
            f"{str(e)[:120]}). The demo ranks with a TF-IDF fallback instead; cosine values "
            "below are TF-IDF, not bge. `prospect run` has no fallback."
        )
        return TfidfEmbedder([posting_text(r, settings.rank.max_chars) for r in rows])


# --- the run -----------------------------------------------------------------------------


def _fresh_db(path: Path, schema_sql: str) -> sqlite3.Connection:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    conn = connect(path)
    init_db(conn, schema_sql)
    return conn


def _posting_table(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """SELECT p.id, c.name AS company, p.title, p.status, p.reject_reason, s.score
           FROM postings p JOIN companies c ON c.id = p.company_id
           LEFT JOIN scores s ON s.posting_id = p.id ORDER BY p.id"""
    ).fetchall()
    lines = [f"{'id':>3}  {'outcome':<34} posting"]
    for r in rows:
        if r["status"] == "rejected":
            outcome = f"rejected: {r['reject_reason']}"
        elif r["score"] is not None:
            outcome = f"scored {r['score']:.0f}"
        else:
            outcome = r["status"]
        lines.append(f"{r['id']:>3}  {outcome:<34} {r['company']} — {r['title']}")
    return "\n".join(lines)


def run_demo(
    root: Path,
    db_path: Path | None = None,
    record: bool = False,
    echo: Echo = print,
    embedder: Embedder | None = None,
) -> ReplayAnthropic | RecordingAnthropic:
    demo_dir = root / "demo"
    db_path = db_path or root / "demo.db"
    settings = demo_settings(root, db_path)
    try:
        fb = ensure_lint_clean(settings)
    except FactBankError as e:
        raise DemoError(str(e)) from e
    conn = _fresh_db(db_path, settings.schema_path.read_text(encoding="utf-8"))
    store = ResponseStore(demo_dir / "responses", load=not record)
    if record:
        key = load_settings(root).anthropic_api_key
        if not key:
            raise DemoError("--record calls the API: put ANTHROPIC_API_KEY in .env first")
        import anthropic

        settings.anthropic_api_key = key
        fake: ReplayAnthropic | RecordingAnthropic = RecordingAnthropic(
            anthropic.Anthropic(api_key=key, timeout=settings.llm.timeout_s), store
        )
        echo("Prospect demo — RECORDING: real API calls on the fictional demo data")
    else:
        fake = ReplayAnthropic(store)
        echo(
            "Prospect demo — fictional candidate (demo/factbank_demo.yaml), 20 synthetic postings "
            "at fictional companies,\nmodel responses replayed from demo/responses/ (no API key, "
            f"no network for the LLM stages). Database: {db_path.name}\n"
        )
    client = LLMClient(settings, conn, anthropic_client=fake)

    echo("== ingest (real Greenhouse / Lever / Ashby adapters over demo/postings/)")
    http = DemoHttp(demo_dir / "postings")
    companies = load_companies(settings.companies_path)
    sources = [Greenhouse(companies, http), Lever(companies, http), Ashby(companies, http)]
    for stats in run_ingest(settings, conn, sources=sources):
        echo(stats.line())

    echo("\n== filter, pre-pass (rules on ATS metadata, before any model call)")
    echo(prepass(settings, conn, fb).summary())

    echo("\n== rank (local embedding vs the fact bank profile)")
    echo(
        run_rank(
            settings, conn, fb, embedder=embedder or pick_embedder(settings, conn, echo)
        ).summary()
    )

    echo("\n== extract (Haiku, Batch API path)")
    try:
        echo(run_extract(settings, conn, mode="batch", client=client))
    except LLMError as e:
        raise DemoError(f"extract: {e}") from e

    echo("\n== filter, full pass (rules on the extracted facts)")
    try:
        echo(full_pass(settings, conn, fb).summary())
    except FilterError as e:
        raise DemoError(str(e)) from e

    echo("\n== score (rules first, Sonnet for the rest, one cached label per skill)")
    # One posting at a time in id order, not the cosine order `prospect run` uses: which
    # posting first asks the model about a skill decides what the request looks like, and
    # the replay must not depend on the embedder (bge or the TF-IDF fallback).
    ids = [
        r["id"]
        for r in conn.execute(
            """SELECT p.id FROM postings p JOIN posting_facts f ON f.posting_id = p.id
               WHERE p.status = 'candidate' ORDER BY p.id"""
        )
    ]
    notes = []
    for pid in ids:
        out = run_score(settings, conn, fb, posting_ids=[pid], client=client)
        notes += out.splitlines()[1:]
    calls = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE stage = 'score' AND error IS NULL"
    ).fetchone()[0]
    echo(f"score: {len(ids)} postings scored with {calls} Sonnet calls")
    for n in notes:
        echo(f"  {n}")

    echo("\n== every posting")
    echo(_posting_table(conn))
    echo("\n== digest")
    echo(format_digest(settings, conn))
    echo(
        "\n== cost (from the runs table"
        + ("" if record else "; the recorded calls' real usage")
        + ")"
    )
    echo(cost_report(conn, None))

    if isinstance(fake, RecordingAnthropic):
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        store.save(stamp)
        echo(f"\nrecorded {len(store.entries)} responses to {store.directory}")
    elif fake.misses:
        raise DemoError(
            f"{len(fake.misses)} request(s) had no recorded response:\n  "
            + "\n  ".join(fake.misses)
            + "\nre-record with `prospect demo --record` (needs ANTHROPIC_API_KEY)"
        )
    conn.close()
    return fake
