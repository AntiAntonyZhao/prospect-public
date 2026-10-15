"""The single LLM wrapper: model routing, prompt caching, Batch API, cost logging.

Every call goes through `LLMClient` and lands one row in `runs` (Hard rule 3),
eval reruns included. Structured output is a tool call whose input schema is
derived from a pydantic model and parsed with `Model.model_validate` — a
model's prose is never regexed.

Prompt layout (cache-friendly: stable first, volatile last):
    tools (fixed per stage) → system: prompt file [+ factbank] ← cache breakpoint
    → user message (the posting / requirements / bullets)

`budget.daily_usd` is enforced before every call: when today's logged spend
(local day) plus the call's estimate exceeds the cap, `BudgetExceeded` aborts.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from prospect.config import Settings
from prospect.db import now_iso, transaction
from prospect.llm.pricing import compute_cost, price_for
from prospect.llm.prompts import Prompt
from prospect.llm.schema import tool_definition
from prospect.logs import get_logger

log = get_logger("llm")

# Forced tool_choice returns a 400 on these (they need `auto` + an instruction).
NO_FORCED_TOOL_CHOICE = ("claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1")
# output_config.effort is rejected by these.
NO_EFFORT = ("claude-haiku-4-5",)
# Server-side refusal fallback (sync Messages API only; never on Batches).
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EST_OUTPUT_TOKENS = 700  # budget estimate per call when the real number is unknown


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


class LLMRefusal(LLMError):
    pass


class LLMOutputError(LLMError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def of(cls, usage: Any) -> Usage:
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )


@dataclass
class CallResult[T: BaseModel]:
    output: T
    model: str
    usage: Usage
    cost_usd: float
    latency_ms: float
    run_id: int


@dataclass
class ToolSpec[T: BaseModel]:
    name: str
    description: str
    model: type[T]


@dataclass
class BatchItem:
    custom_id: str
    user_content: str
    posting_id: int | None = None


@dataclass
class BatchItemResult[T: BaseModel]:
    custom_id: str
    posting_id: int | None
    output: T | None
    error: str | None
    cost_usd: float = 0.0


def spent_today(conn: sqlite3.Connection) -> float:
    """Logged spend since local midnight — what `budget.daily_usd` is checked against."""
    row = conn.execute(
        """SELECT COALESCE(SUM(cost_usd), 0) FROM runs
           WHERE started_at >= datetime('now', 'localtime', 'start of day', 'utc')"""
    ).fetchone()
    return float(row[0])


class LLMClient:
    def __init__(
        self, settings: Settings, conn: sqlite3.Connection, anthropic_client: Any | None = None
    ) -> None:
        self.settings = settings
        self.conn = conn
        self._client = anthropic_client

    # -- plumbing -------------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.settings.anthropic_api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY is not set: put it in .env (never export it in your shell)."
                )
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=self.settings.anthropic_api_key,
                timeout=self.settings.llm.timeout_s,
                max_retries=self.settings.llm.max_retries,
            )
        return self._client

    def spent_today(self) -> float:
        return spent_today(self.conn)

    def check_budget(self, estimate_usd: float = 0.0) -> None:
        cap = self.settings.budget.daily_usd
        spent = self.spent_today()
        if spent + estimate_usd > cap:
            raise BudgetExceeded(
                f"daily budget ${cap:.2f} would be exceeded (spent ${spent:.4f} today, "
                f"next call ~${estimate_usd:.4f}); raise budget.daily_usd or wait until tomorrow"
            )

    def estimate_cost(self, model: str, prompt_chars: int, max_out: int | None = None) -> float:
        p_in, p_out = price_for(model)
        out = min(max_out or EST_OUTPUT_TOKENS, EST_OUTPUT_TOKENS * 4)
        return (prompt_chars / 4 * p_in + out * p_out) / 1_000_000

    def build_params(
        self,
        *,
        model: str,
        prompt: Prompt,
        cached_prefix: str | None,
        user_content: str,
        tool: ToolSpec,
        max_tokens: int,
        effort: str | None,
    ) -> dict[str, Any]:
        system: list[dict[str, Any]] = [{"type": "text", "text": prompt.text}]
        if cached_prefix:
            system.append({"type": "text", "text": cached_prefix})
        system[-1]["cache_control"] = {"type": "ephemeral"}
        forced = not model.startswith(NO_FORCED_TOOL_CHOICE)
        content = user_content
        if not forced:
            content += f"\n\nRespond by calling the `{tool.name}` tool exactly once."
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": [
                tool_definition(
                    tool.name, tool.description, tool.model, strict=self.settings.llm.strict_tools
                )
            ],
            "tool_choice": {"type": "tool", "name": tool.name} if forced else {"type": "auto"},
            "messages": [{"role": "user", "content": content}],
        }
        if effort and not model.startswith(NO_EFFORT):
            params["output_config"] = {"effort": effort}
        return params

    def _log_run(
        self,
        *,
        stage: str,
        model: str,
        prompt_version: str,
        started: str,
        usage: Usage | None,
        cost: float,
        posting_id: int | None = None,
        batch_id: str | None = None,
        error: str | None = None,
    ) -> int:
        u = usage or Usage()
        with transaction(self.conn):
            cur = self.conn.execute(
                """INSERT INTO runs (stage, model, prompt_version, started_at, finished_at, n_in,
                       n_out, input_tokens, output_tokens, cached_tokens, cache_write_tokens,
                       cost_usd, batch_id, posting_id, error)
                   VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)""",
                (
                    stage,
                    model,
                    prompt_version,
                    started,
                    now_iso(),
                    0 if error else 1,
                    u.input_tokens,
                    u.output_tokens,
                    u.cache_read_tokens,
                    u.cache_write_tokens,
                    cost,
                    batch_id,
                    posting_id,
                    error,
                ),
            )
        return int(cur.lastrowid)

    @staticmethod
    def _parse[T: BaseModel](message: Any, tool: ToolSpec[T]) -> T:
        if getattr(message, "stop_reason", None) == "refusal":
            raise LLMRefusal("model refused the request")
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise LLMOutputError("output truncated at max_tokens; raise the stage's max_tokens")
        for block in message.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool.name:
                data = block.input
                if isinstance(data, str):  # defensive: some transports hand back JSON text
                    data = json.loads(data)
                try:
                    return tool.model.model_validate(data)
                except ValidationError as e:
                    raise LLMOutputError(f"tool input failed validation: {e}") from e
        raise LLMOutputError(f"model did not call `{tool.name}`")

    # -- sync -------------------------------------------------------------------
    def structured[T: BaseModel](
        self,
        *,
        stage: str,
        model: str,
        prompt: Prompt,
        user_content: str,
        tool: ToolSpec[T],
        max_tokens: int,
        cached_prefix: str | None = None,
        effort: str | None = None,
        posting_id: int | None = None,
        allow_fallback: bool = False,
    ) -> CallResult[T]:
        model = self.settings.model_id(model)
        chars = len(prompt.text) + len(cached_prefix or "") + len(user_content)
        self.check_budget(self.estimate_cost(model, chars, max_tokens))
        params = self.build_params(
            model=model,
            prompt=prompt,
            cached_prefix=cached_prefix,
            user_content=user_content,
            tool=tool,
            max_tokens=max_tokens,
            effort=effort,
        )
        started = now_iso()
        t0 = time.monotonic()
        try:
            message = self._create(params, allow_fallback)
        except LLMError:
            raise
        except Exception as e:
            self._log_run(
                stage=stage,
                model=model,
                prompt_version=prompt.version,
                started=started,
                usage=None,
                cost=0.0,
                posting_id=posting_id,
                error=f"{type(e).__name__}: {str(e)[:300]}",
            )
            raise LLMError(f"{stage}: API call failed: {type(e).__name__}: {e}") from e
        latency = (time.monotonic() - t0) * 1000
        served = getattr(message, "model", None) or model
        usage = Usage.of(message.usage)
        cost = compute_cost(
            served,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_write_tokens,
            usage.cache_read_tokens,
        )
        try:
            output = self._parse(message, tool)
        except LLMError as e:
            self._log_run(
                stage=stage,
                model=served,
                prompt_version=prompt.version,
                started=started,
                usage=usage,
                cost=cost,
                posting_id=posting_id,
                error=str(e)[:300],
            )
            raise
        run_id = self._log_run(
            stage=stage,
            model=served,
            prompt_version=prompt.version,
            started=started,
            usage=usage,
            cost=cost,
            posting_id=posting_id,
        )
        log.info(
            "llm_call",
            stage=stage,
            model=served,
            posting=posting_id,
            tin=usage.input_tokens,
            tout=usage.output_tokens,
            cache_read=usage.cache_read_tokens,
            cache_write=usage.cache_write_tokens,
            usd=round(cost, 5),
            ms=round(latency),
        )
        return CallResult(output, served, usage, cost, latency, run_id)

    def _create(self, params: dict[str, Any], allow_fallback: bool) -> Any:
        model = params["model"]
        use_fallback = (
            allow_fallback and self.settings.llm.refusal_fallbacks and model in FALLBACK_MODELS
        )
        try:
            if use_fallback:
                return self.client.beta.messages.create(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            return self.client.messages.create(**params)
        except Exception as e:
            # A model that rejects forced tool_choice: retry once with `auto` + instruction.
            if "tool_choice" in str(e) and params["tool_choice"].get("type") == "tool":
                retry = dict(params)
                retry["tool_choice"] = {"type": "auto"}
                name = params["tool_choice"]["name"]
                msg = retry["messages"][0]
                retry["messages"] = [
                    {
                        "role": "user",
                        "content": f"{msg['content']}\n\n"
                        f"Respond by calling the `{name}` tool exactly once.",
                    }
                ]
                return self.client.messages.create(**retry)
            raise

    # -- batch ------------------------------------------------------------------
    def batch_submit(
        self,
        *,
        stage: str,
        model: str,
        prompt: Prompt,
        items: Sequence[BatchItem],
        tool: ToolSpec,
        max_tokens: int,
        cached_prefix: str | None = None,
        effort: str | None = None,
    ) -> tuple[str, list[BatchItem]]:
        """Submit; returns (batch_id, the items actually submitted — trimmed to the budget)."""
        model = self.settings.model_id(model)
        cap = self.settings.budget.daily_usd - self.spent_today()
        chosen: list[BatchItem] = []
        est_total = 0.0
        base = len(prompt.text) + len(cached_prefix or "")
        for it in items:
            est = self.estimate_cost(model, base + len(it.user_content)) * 0.5
            if est_total + est > cap:
                break
            chosen.append(it)
            est_total += est
        if not chosen:
            self.check_budget(self.estimate_cost(model, base) * 0.5)
            raise BudgetExceeded("daily budget leaves no room for a batch today")
        requests = [
            {
                "custom_id": it.custom_id,
                "params": self.build_params(
                    model=model,
                    prompt=prompt,
                    cached_prefix=cached_prefix,
                    user_content=it.user_content,
                    tool=tool,
                    max_tokens=max_tokens,
                    effort=effort,
                ),
            }
            for it in chosen
        ]
        batch = self.client.messages.batches.create(requests=requests)
        items_map = {it.custom_id: it.posting_id for it in chosen}
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO batches (batch_id, stage, model, prompt_version, items_json)
                   VALUES (?,?,?,?,?)""",
                (batch.id, stage, model, prompt.version, json.dumps(items_map)),
            )
        log.info(
            "batch_submitted",
            stage=stage,
            batch=batch.id,
            n=len(chosen),
            est_usd=round(est_total, 4),
        )
        return batch.id, chosen

    def batch_status(self, batch_id: str) -> str:
        return self.client.messages.batches.retrieve(batch_id).processing_status

    def pending_batches(self, stage: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batches WHERE stage = ? AND status = 'submitted' ORDER BY submitted_at",
            (stage,),
        ).fetchall()

    def batch_collect[T: BaseModel](
        self, batch_id: str, tool: ToolSpec[T]
    ) -> list[BatchItemResult[T]]:
        row = self.conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if not row:
            raise LLMError(f"unknown batch {batch_id}")
        items: dict[str, int | None] = json.loads(row["items_json"])
        out: list[BatchItemResult[T]] = []
        for result in self.client.messages.batches.results(batch_id):
            cid = result.custom_id
            pid = items.get(cid)
            started = now_iso()
            kind = result.result.type
            if kind != "succeeded":
                err = f"batch item {kind}"
                if kind == "errored":
                    err += f": {getattr(result.result.error, 'type', '')}"
                self._log_run(
                    stage=row["stage"],
                    model=row["model"],
                    prompt_version=row["prompt_version"],
                    started=started,
                    usage=None,
                    cost=0.0,
                    posting_id=pid,
                    batch_id=batch_id,
                    error=err,
                )
                out.append(BatchItemResult(cid, pid, None, err))
                continue
            message = result.result.message
            served = getattr(message, "model", None) or row["model"]
            usage = Usage.of(message.usage)
            cost = compute_cost(
                served,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_write_tokens,
                usage.cache_read_tokens,
                batch=True,
            )
            try:
                parsed = self._parse(message, tool)
                self._log_run(
                    stage=row["stage"],
                    model=served,
                    prompt_version=row["prompt_version"],
                    started=started,
                    usage=usage,
                    cost=cost,
                    posting_id=pid,
                    batch_id=batch_id,
                )
                out.append(BatchItemResult(cid, pid, parsed, None, cost))
            except LLMError as e:
                self._log_run(
                    stage=row["stage"],
                    model=served,
                    prompt_version=row["prompt_version"],
                    started=started,
                    usage=usage,
                    cost=cost,
                    posting_id=pid,
                    batch_id=batch_id,
                    error=str(e)[:300],
                )
                out.append(BatchItemResult(cid, pid, None, str(e), cost))
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE batches SET status='collected', collected_at=? WHERE batch_id=?",
                (now_iso(), batch_id),
            )
        return out
