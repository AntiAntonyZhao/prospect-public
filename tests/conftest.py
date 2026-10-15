"""Shared fixtures. Nothing here touches the network or the Anthropic API."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from prospect.config import Settings
from prospect.db import connect, init_db, now_iso
from prospect.factbank.loader import load_factbank
from prospect.llm.client import LLMClient
from prospect.models.factbank import FactBank
from prospect.models.posting import RawPosting
from prospect.pipeline.ingest import upsert_posting

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    shutil.copyfile(FIXTURES / "factbank_fixture.yaml", tmp_path / "factbank.yaml")
    s = Settings(root=tmp_path)
    s.paths.factbank = "factbank.yaml"
    s.paths.schema_sql = str(REPO / "db" / "schema.sql")
    return s


@pytest.fixture
def fb(settings: Settings) -> FactBank:
    return load_factbank(settings.factbank_path)


@pytest.fixture
def conn(settings: Settings) -> sqlite3.Connection:
    c = connect(settings.db_path)
    init_db(c, settings.schema_path.read_text(encoding="utf-8"))
    yield c
    c.close()


def add_posting(
    conn: sqlite3.Connection,
    title: str = "Software Engineer, New Grad",
    company: str = "Acme",
    location: str | None = "Montreal, QC",
    description: str = "<p>We build things. Python, SQL and React.</p>" + "<p>more text</p>" * 30,
    source: str = "greenhouse",
    ext_id: str | None = None,
    status: str | None = None,
) -> int:
    raw = RawPosting(
        source=source,
        ext_id=ext_id or f"{company}-{title}-{location}",
        company=company,
        title=title,
        url=f"https://example.com/{company}/{title}".replace(" ", "-"),
        location_raw=location,
        description_html=description,
    )
    pid, _ = upsert_posting(conn, raw, now_iso())  # "seen now": rank parks stale postings
    if status:
        conn.execute("UPDATE postings SET status=? WHERE id=?", (status, pid))
    conn.commit()
    return pid


# --- a fake Anthropic client ------------------------------------------------------------


class FakeMessages:
    """`messages.create` / `batches` stand-in. `responder(params)` returns the tool input
    dict, a full message object, or an Exception to raise."""

    def __init__(self, responder: Callable[[dict], Any]) -> None:
        self.responder = responder
        self.calls: list[dict] = []
        self.batches = FakeBatches(self)

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        out = self.responder(params)
        if isinstance(out, Exception):
            raise out
        if hasattr(out, "content"):
            return out
        return message_for(params, out)


def message_for(
    params: dict, tool_input: dict, stop_reason: str = "tool_use", usage: dict | None = None
) -> SimpleNamespace:
    name = params["tools"][0]["name"]
    u = {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        **(usage or {}),
    }
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name=name, input=tool_input)],
        usage=SimpleNamespace(**u),
        stop_reason=stop_reason,
        model=params["model"],
    )


class FakeBatches:
    def __init__(self, messages: FakeMessages) -> None:
        self.messages = messages
        self.store: dict[str, list[dict]] = {}

    def create(self, requests: list[dict]) -> SimpleNamespace:
        bid = f"msgbatch_{len(self.store) + 1}"
        self.store[bid] = requests
        return SimpleNamespace(id=bid, processing_status="in_progress")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=batch_id, processing_status="ended")

    def results(self, batch_id: str):
        for req in self.store[batch_id]:
            out = self.messages.responder(req["params"])
            if isinstance(out, Exception):
                yield SimpleNamespace(
                    custom_id=req["custom_id"],
                    result=SimpleNamespace(type="errored", error=SimpleNamespace(type="api_error")),
                )
                continue
            msg = out if hasattr(out, "content") else message_for(req["params"], out)
            yield SimpleNamespace(
                custom_id=req["custom_id"], result=SimpleNamespace(type="succeeded", message=msg)
            )


class FakeAnthropic:
    def __init__(self, responder: Callable[[dict], Any]) -> None:
        self.messages = FakeMessages(responder)
        self.beta = SimpleNamespace(messages=self.messages)


@pytest.fixture
def fake_llm(settings: Settings, conn: sqlite3.Connection):
    """Factory: fake_llm(responder) -> (LLMClient, FakeAnthropic)."""

    def make(responder: Callable[[dict], Any]) -> tuple[LLMClient, FakeAnthropic]:
        fake = FakeAnthropic(responder)
        return LLMClient(settings, conn, anthropic_client=fake), fake

    return make


def extraction_input(**over: Any) -> dict:
    facts = {
        "city": "Montreal",
        "province": "QC",
        "country": "CA",
        "work_arrangement": "hybrid",
        "onsite_days": 2,
        "remote_scope": "city",
        "french_requirement": "none",
        "min_years": 0,
        "seniority": "newgrad",
        "clearance": False,
        "peng": False,
        "work_auth": "not_stated",
        "part_time": False,
        "hours_per_week": None,
        "contract": False,
        "term_length": None,
        "term_unit": None,
        "confidence": 0.9,
        "evidence": [{"field": "city", "quote": "We build things."}],
    }
    facts.update(over.pop("facts", {}))
    reqs = over.pop(
        "requirements",
        [
            {
                "text": "Python",
                "skill_norm": "Python",
                "must_have": True,
                "years": None,
                "context": None,
            },
            {
                "text": "Kafka",
                "skill_norm": "Kafka",
                "must_have": True,
                "years": None,
                "context": None,
            },
        ],
    )
    return {"facts": facts, "requirements": reqs}
