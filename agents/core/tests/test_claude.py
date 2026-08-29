"""Model client: pinned model, capped tokens, real cost, verified output."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from day2_agents.claude import (
    EFFORT,
    MAX_TOKENS_CEILING,
    MODEL,
    PRICE_PER_MTOK,
    ClaudeClient,
    ModelError,
    compute_cost_usd,
    parse_json_object,
)
from day2_agents.scopes import Action, PermissionDenied, PermissionSet


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


def response(text="hello", stop_reason="end_turn", input_tokens=1000, output_tokens=500):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model=MODEL,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        _request_id="req_test",
    )


def test_model_is_pinned_exactly():
    assert MODEL == "claude-opus-5"
    assert MODEL in PRICE_PER_MTOK


def test_cost_uses_the_pinned_price_table():
    # 1M input + 1M output at $5 + $25.
    assert compute_cost_usd(MODEL, 1_000_000, 1_000_000) == pytest.approx(30.00)


def test_an_unpriced_model_raises_rather_than_reporting_zero():
    """A silent 0.0 would be a fabricated metric (CLAUDE.md rule 5)."""
    with pytest.raises(ModelError, match="no pinned price"):
        compute_cost_usd("claude-not-a-model", 10, 10)


def test_call_is_gated_on_the_declared_scope(audit):
    scopes = PermissionSet.declare("triage", [Action.OPEN_PR])
    client = ClaudeClient(scopes, audit, client=FakeClient(response()))
    with pytest.raises(PermissionDenied, match="call_model"):
        client.complete("sys", "usr", "run/1", "why")


def test_max_tokens_is_capped_and_the_clamp_is_audited(audit, full_scopes):
    fake = FakeClient(response())
    client = ClaudeClient(full_scopes, audit, client=fake)
    client.complete("sys", "usr", "run/1", "why", max_tokens=999_999)
    assert fake.messages.kwargs["max_tokens"] == MAX_TOKENS_CEILING
    metadata = audit.entries[0].metadata
    assert metadata["max_tokens"] == MAX_TOKENS_CEILING
    assert metadata["max_tokens_requested"] == 999_999


def test_request_pins_model_and_effort_and_sends_no_sampling_params(audit, full_scopes):
    """`temperature`/`top_p`/`top_k` are rejected with a 400 on this model."""
    fake = FakeClient(response())
    ClaudeClient(full_scopes, audit, client=fake).complete("sys", "usr", "run/1", "why")
    kwargs = fake.messages.kwargs
    assert kwargs["model"] == MODEL
    assert kwargs["output_config"] == {"effort": EFFORT}
    assert not {"temperature", "top_p", "top_k"} & set(kwargs)


def test_every_call_is_audited_with_its_real_cost(audit, full_scopes):
    fake = FakeClient(response(input_tokens=8_000, output_tokens=2_000))
    client = ClaudeClient(full_scopes, audit, client=fake)
    call = client.complete("sys", "usr", "run/42", "diagnosing ci failure")

    expected = (8_000 * 5.00 + 2_000 * 25.00) / 1_000_000
    assert call.cost_usd == pytest.approx(expected)
    assert client.total_cost_usd == pytest.approx(expected)

    entry = audit.entries[0].to_dict()
    assert entry["action"] == "call_model"
    assert entry["target"] == "run/42"
    assert entry["metadata"]["cost_usd"] == pytest.approx(expected)
    assert entry["metadata"]["input_tokens"] == 8_000


def test_cost_accumulates_across_calls(audit, full_scopes):
    client = ClaudeClient(full_scopes, audit, client=FakeClient(response()))
    client.complete("s", "u", "t", "d")
    client.complete("s", "u", "t", "d")
    assert client.call_count == 2
    assert client.total_cost_usd == pytest.approx(2 * (1000 * 5 + 500 * 25) / 1e6)


def test_a_refusal_is_detected_and_still_audited(audit, full_scopes):
    """A safety decline is HTTP 200 with stop_reason 'refusal', not an exception."""
    fake = FakeClient(response(stop_reason="refusal"))
    client = ClaudeClient(full_scopes, audit, client=fake)
    with pytest.raises(ModelError, match="declined"):
        client.complete("s", "u", "run/1", "d")
    assert audit.entries[0].to_dict()["action"] == "call_model"


def test_empty_output_is_an_error_not_a_silent_success(audit, full_scopes):
    client = ClaudeClient(full_scopes, audit, client=FakeClient(response(text="  ")))
    with pytest.raises(ModelError, match="no text"):
        client.complete("s", "u", "run/1", "d")


def test_missing_api_key_names_the_env_var(audit, full_scopes, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = ClaudeClient(full_scopes, audit)
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        client.complete("s", "u", "run/1", "d")


@pytest.mark.parametrize(
    "text",
    [
        '{"confidence": "high"}',
        '```json\n{"confidence": "high"}\n```',
        'Here you go:\n```\n{"confidence": "high"}\n```\nHope that helps.',
        'Some prose {"confidence": "high"} and more prose',
    ],
)
def test_json_is_recovered_from_the_shapes_models_actually_emit(text):
    assert parse_json_object(text) == {"confidence": "high"}


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2, 3]", "{not json}"])
def test_unparseable_model_output_is_rejected(text):
    with pytest.raises(ModelError):
        parse_json_object(text)
