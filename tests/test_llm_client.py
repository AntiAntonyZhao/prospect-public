"""llm/client.py against a fake Anthropic client — no network, no API key."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from prospect.llm.client import BudgetExceeded, LLMOutputError, LLMRefusal
from prospect.llm.pricing import compute_cost, price_for
from prospect.llm.prompts import load_prompt
from prospect.llm.schema import tool_input_schema
from prospect.models.posting import ExtractionOutput, TierOutput
from prospect.pipeline.extract import TOOL
from tests.conftest import extraction_input, message_for


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@pytest.mark.parametrize("model", [ExtractionOutput, TierOutput])
def test_tool_schemas_are_strict(model):
    schema = tool_input_schema(model)
    for node in _walk(schema):
        assert "$ref" not in node and "$defs" not in node
        if node.get("type") == "object" and "properties" in node:
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])


def test_pricing():
    assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert price_for("claude-opus-5-5") == (4.0, 20.0)  # never priced as opus-5
    assert price_for("unknown-model") == (10.0, 50.0)  # over-count, never under-count
    full = compute_cost("claude-sonnet-5", 1_000_000, 100_000)
    assert full == pytest.approx(2.0 + 1.0)
    assert compute_cost("claude-sonnet-5", 1_000_000, 100_000, batch=True) == pytest.approx(1.5)
    cached = compute_cost(
        "claude-opus-5", 0, 0, cache_write_tokens=1_000_000, cache_read_tokens=1_000_000
    )
    assert cached == pytest.approx(5 * 1.25 + 5 * 0.1)


def test_structured_call_logs_a_run(fake_llm, conn):
    client, fake = fake_llm(lambda p: extraction_input())
    res = client.structured(
        stage="extract",
        model="haiku",
        prompt=load_prompt("extract"),
        user_content="posting",
        tool=TOOL,
        max_tokens=4096,
        posting_id=None,
    )
    assert res.output.facts.city == "Montreal"
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert run["model"] == "claude-haiku-4-5-20251001"
    assert run["prompt_version"] == "extract_v4"  # latest file wins
    assert run["input_tokens"] == 1200 and run["output_tokens"] == 300
    assert run["cost_usd"] == pytest.approx((1200 * 1 + 300 * 5) / 1e6)
    params = fake.messages.calls[0]
    assert params["tool_choice"] == {"type": "tool", "name": "record_posting"}
    assert params["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "output_config" not in params  # Haiku 4.5 rejects effort
    assert params["tools"][0]["strict"] is True


def test_failed_call_is_still_logged(fake_llm, conn):
    client, _ = fake_llm(lambda p: RuntimeError("boom"))
    with pytest.raises(Exception, match="boom"):
        client.structured(
            stage="score",
            model="sonnet",
            prompt=load_prompt("extract"),
            user_content="x",
            tool=TOOL,
            max_tokens=100,
        )
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert run["error"] and "boom" in run["error"]


def test_budget_cap_aborts_before_calling(fake_llm, conn, settings):
    settings.budget.daily_usd = 0.01
    conn.execute(
        "INSERT INTO runs (stage, model, started_at, cost_usd) "
        "VALUES ('x', 'm', datetime('now'), 0.02)"
    )
    conn.commit()
    client, fake = fake_llm(lambda p: extraction_input())
    with pytest.raises(BudgetExceeded):
        client.structured(
            stage="extract",
            model="haiku",
            prompt=load_prompt("extract"),
            user_content="x",
            tool=TOOL,
            max_tokens=100,
        )
    assert fake.messages.calls == []


def test_refusal_and_truncation(fake_llm):
    client, _ = fake_llm(lambda p: message_for(p, extraction_input(), stop_reason="refusal"))
    with pytest.raises(LLMRefusal):
        client.structured(
            stage="extract",
            model="haiku",
            prompt=load_prompt("extract"),
            user_content="x",
            tool=TOOL,
            max_tokens=100,
        )
    client, _ = fake_llm(lambda p: message_for(p, extraction_input(), stop_reason="max_tokens"))
    with pytest.raises(LLMOutputError, match="truncated"):
        client.structured(
            stage="extract",
            model="haiku",
            prompt=load_prompt("extract"),
            user_content="x",
            tool=TOOL,
            max_tokens=100,
        )


def test_invalid_tool_input_is_an_error(fake_llm):
    client, _ = fake_llm(lambda p: {"facts": {"city": 3}})
    with pytest.raises(LLMOutputError, match="validation"):
        client.structured(
            stage="extract",
            model="haiku",
            prompt=load_prompt("extract"),
            user_content="x",
            tool=TOOL,
            max_tokens=100,
        )


def test_forced_tool_choice_rejection_retries_with_auto(fake_llm):
    def responder(p):
        if p["tool_choice"]["type"] == "tool":
            return RuntimeError("tool_choice: type 'tool' is not supported for this model")
        return extraction_input()

    client, fake = fake_llm(responder)
    res = client.structured(
        stage="extract",
        model="haiku",
        prompt=load_prompt("extract"),
        user_content="x",
        tool=TOOL,
        max_tokens=100,
    )
    assert res.output.facts.province == "QC"
    assert fake.messages.calls[-1]["tool_choice"] == {"type": "auto"}


def test_opus_5_5_never_gets_forced_tool_choice(fake_llm):
    client, fake = fake_llm(lambda p: extraction_input())
    client.structured(
        stage="eval",
        model="claude-opus-5-5",
        prompt=load_prompt("extract"),
        user_content="x",
        tool=TOOL,
        max_tokens=100,
        effort="medium",
    )
    params = fake.messages.calls[0]
    assert params["tool_choice"] == {"type": "auto"}
    assert params["output_config"] == {"effort": "medium"}
    assert "record_posting" in params["messages"][0]["content"]


def test_opus_fallback_uses_beta_endpoint(settings, conn):
    from prospect.llm.client import LLMClient

    seen = {}

    def beta_create(**params):
        seen.update(params)
        return message_for(params, extraction_input())

    fake = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **p: pytest.fail("non-beta endpoint used")),
        beta=SimpleNamespace(messages=SimpleNamespace(create=beta_create)),
    )
    client = LLMClient(settings, conn, anthropic_client=fake)
    client.structured(
        stage="eval",
        model="opus",
        prompt=load_prompt("extract"),
        user_content="x",
        tool=TOOL,
        max_tokens=100,
        allow_fallback=True,
    )
    assert seen["fallbacks"] == "default"
    assert seen["betas"] == ["server-side-fallback-2026-07-01"]


def test_missing_api_key_is_a_clear_error(settings, conn):
    from prospect.llm.client import LLMClient, LLMError

    with pytest.raises(LLMError, match=r"\.env"):
        _ = LLMClient(settings, conn).client
