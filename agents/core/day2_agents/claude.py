"""The one place any agent talks to the Claude API.

Everything that costs money or varies between runs is pinned here as a module
constant, so a reviewer can read the whole cost-and-determinism story in one
screen:

* `MODEL` — pinned exactly, no alias, no `latest` (CLAUDE.md rule 2). Bumping
  it is a reviewed commit, not an env var someone sets on a runner.
* `MAX_TOKENS_CEILING` — a hard cap on output tokens. Callers may ask for less;
  a caller asking for more is clamped and the clamp is audited. Output tokens
  are the expensive half, so this is the real spend ceiling per call.
* `EFFORT` — `output_config.effort`, the reasoning-depth dial on current
  models. This is the knob that replaced `temperature` for this purpose:
  sampling parameters (`temperature`, `top_p`, `top_k`) were removed on the
  Claude 5 family and are rejected with a 400, so "run it at low temperature"
  is no longer expressible. Determinism is instead pursued with a rigid output
  contract (see `parse_json_object`) and verification of what comes back
  (`diffs.validate_diff`) rather than by nudging the sampler.

Cost is computed from the response's own token counts against a pinned price
table and returned on every call, so `agents/README.md` can quote a real
per-triage figure and never a fabricated one (CLAUDE.md rule 5).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from day2_agents.audit import AuditLogger
from day2_agents.scopes import Action, PermissionSet

MODEL = "claude-opus-5"
MAX_TOKENS_CEILING = 8_000
EFFORT = "high"

# USD per million tokens for MODEL. Cache rates are the published multiples of
# the base input rate (write 1.25x, read 0.1x); they are listed so a future
# caching change reports honest numbers without anyone re-deriving them.
PRICE_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5": {
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
}

_JSON_FENCE = re.compile(r"```(?:json)?\s*(?P<body>\{.*?\})\s*```", re.DOTALL)


class ModelError(RuntimeError):
    """The model could not be used for this call — no usable output."""


@dataclass(frozen=True)
class ModelCall:
    """One completed API call: what came back, and what it cost."""

    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float
    request_id: str | None

    def usage_summary(self) -> dict[str, Any]:
        """Machine-readable usage for the audit entry's `metadata`."""
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "request_id": self.request_id,
        }


def compute_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Cost of one call in USD, from real token counts and the pinned table.

    An unpriced model raises rather than returning 0.0: a silent zero would put
    a fabricated cost in the audit trail, which is worse than a failed run.
    """
    try:
        price = PRICE_PER_MTOK[model]
    except KeyError:
        raise ModelError(
            f"no pinned price for {model!r}; add it to PRICE_PER_MTOK before use"
        ) from None
    return (
        input_tokens * price["input"]
        + output_tokens * price["output"]
        + cache_write_tokens * price["cache_write"]
        + cache_read_tokens * price["cache_read"]
    ) / 1_000_000


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the single JSON object the model was asked to return.

    Tolerates a ```json fence and surrounding prose, because that is the one
    formatting liberty models actually take; it does not tolerate anything
    else. Output verification (governance pillar 6) starts here — the caller
    then checks the object's *fields*, and `diffs.validate_diff` checks the
    patch actually applies.
    """
    candidate = text.strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group("body")
    elif not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ModelError("model response contained no JSON object")
        candidate = candidate[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ModelError(f"model response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ModelError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


class ClaudeClient:
    """Scope-gated, cost-accounted wrapper around the Messages API."""

    def __init__(
        self,
        scopes: PermissionSet,
        audit: AuditLogger,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self._scopes = scopes
        self._audit = audit
        self._client = client
        self._api_key = api_key
        self.total_cost_usd = 0.0
        self.call_count = 0

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Imported lazily so the guardrail and audit tests — the ones that
            # matter most — run without the SDK or a key present.
            import anthropic

            key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise ModelError(
                    "ANTHROPIC_API_KEY is not set; secrets come from the "
                    "environment only (governance pillar 5)"
                )
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def complete(
        self,
        system: str,
        user: str,
        target: str,
        decision_summary: str,
        max_tokens: int = 6_000,
    ) -> ModelCall:
        """Make one call, audit it with its cost, and return the result."""
        self._scopes.require(Action.CALL_MODEL)

        capped = min(max_tokens, MAX_TOKENS_CEILING)
        clamped = capped != max_tokens

        response = self._ensure_client().messages.create(
            model=MODEL,
            max_tokens=capped,
            output_config={"effort": EFFORT},
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        usage = response.usage
        call = ModelCall(
            text="".join(
                block.text for block in response.content if block.type == "text"
            ),
            model=getattr(response, "model", MODEL),
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cost_usd=0.0,
            request_id=getattr(response, "_request_id", None),
        )
        call = _with_cost(call)

        self.total_cost_usd += call.cost_usd
        self.call_count += 1

        metadata = call.usage_summary()
        metadata["max_tokens"] = capped
        if clamped:
            metadata["max_tokens_requested"] = max_tokens
        self._audit.record(
            action="call_model",
            target=target,
            decision_summary=(
                f"{decision_summary} "
                f"[{MODEL}, effort={EFFORT}, stop={call.stop_reason}, "
                f"${call.cost_usd:.4f}]"
            ),
            metadata=metadata,
        )

        # A safety decline arrives as HTTP 200 with stop_reason "refusal", not
        # an exception — check before trusting the content.
        if call.stop_reason == "refusal":
            raise ModelError(f"model declined the request (target={target})")
        if not call.text.strip():
            raise ModelError(f"model returned no text (stop={call.stop_reason})")
        return call


def _with_cost(call: ModelCall) -> ModelCall:
    from dataclasses import replace

    return replace(
        call,
        cost_usd=compute_cost_usd(
            call.model,
            call.input_tokens,
            call.output_tokens,
            call.cache_write_tokens,
            call.cache_read_tokens,
        ),
    )
