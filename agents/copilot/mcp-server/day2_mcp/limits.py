"""Timeouts and result-size caps, in one place.

Two different failure modes, and they need different answers.

**A slow backend** is the ordinary case: Prometheus is compacting, Loki is
scanning a wide time range. The answer is a timeout — fail the tool call, tell
the model the query timed out, and let it narrow the range. A copilot that
hangs for two minutes on one query has already failed the question.

**A huge result** is the dangerous case, because it succeeds. A `search_logs`
over a busy hour can return tens of megabytes; a PromQL query with a
high-cardinality label can return thousands of series. Feed that to a model and
the cost of a single question jumps by orders of magnitude — an unbounded
result set is a spend bug wearing a correctness bug's clothes, and it is the
one that would blow the per-session cap in a single call.

So results are capped by *count* before they are serialised, and by *bytes*
after. Truncation is always reported in the payload (`truncated: true` plus
what the limit was), never silent: an answer built on the first 200 of 40,000
log lines is a different answer, and the model has to be told so it can say so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Wall-clock ceiling for one backend call. Prometheus and Loki both accept a
# server-side timeout too, and we send both: the server-side one lets the
# backend abort cleanly and return a proper error, the client-side one is the
# backstop for a backend that has stopped answering entirely.
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0

# Per-tool result caps.
MAX_SERIES = 200  # PromQL result series
MAX_POINTS_PER_SERIES = 500  # samples in a range query
MAX_LOG_LINES = 300
MAX_ALERTS = 100
MAX_COMMITS = 100
MAX_FILE_BYTES = 256_000  # a runbook or dashboard file
MAX_RESULT_BYTES = 400_000  # hard ceiling on any single serialised tool result


class LimitExceeded(RuntimeError):
    """A result was too large to return even after truncation."""


@dataclass
class Truncation:
    """Record of what was dropped, surfaced in the tool result."""

    truncated: bool = False
    reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []

    def note(self, reason: str) -> None:
        self.truncated = True
        self.reasons.append(reason)

    def as_payload(self) -> dict[str, Any]:
        if not self.truncated:
            return {"truncated": False}
        return {"truncated": True, "truncation_reasons": list(self.reasons)}


def clamp_timeout(seconds: float | None) -> float:
    if seconds is None:
        return DEFAULT_TIMEOUT_SECONDS
    return max(0.5, min(float(seconds), MAX_TIMEOUT_SECONDS))


def cap_list(items: list[Any], limit: int, what: str, trunc: Truncation) -> list[Any]:
    """Trim a list to `limit`, recording the trim."""
    if len(items) <= limit:
        return items
    trunc.note(f"{what}: kept {limit} of {len(items)}")
    return items[:limit]


def enforce_result_bytes(payload: dict[str, Any], trunc: Truncation) -> dict[str, Any]:
    """Final byte ceiling on a serialised result.

    Reached only if the count caps above were not enough — a few enormous log
    lines, say. Rather than truncate the JSON into something unparseable, drop
    the payload's bulkiest field and say so.
    """
    encoded = json.dumps(payload, default=str)
    if len(encoded) <= MAX_RESULT_BYTES:
        return payload

    bulky = max(
        (k for k in payload if isinstance(payload[k], list | str)),
        key=lambda k: len(json.dumps(payload[k], default=str)),
        default=None,
    )
    if bulky is None:
        raise LimitExceeded(
            f"result is {len(encoded)} bytes, over the {MAX_RESULT_BYTES} ceiling, "
            "and has no reducible field"
        )

    payload = dict(payload)
    original = payload[bulky]

    if isinstance(original, str):
        payload[bulky] = "[dropped: too large]"
    else:
        # Halve until it fits. A single fixed cut is not enough — a list of a
        # few very large items can still be over the ceiling at a quarter of
        # its length, and returning it anyway would defeat the ceiling.
        keep = len(original)
        while keep > 0:
            keep //= 2
            payload[bulky] = original[:keep]
            if len(json.dumps(payload, default=str)) <= MAX_RESULT_BYTES:
                break
        else:
            payload[bulky] = []

    trunc.note(f"{bulky}: dropped to fit the {MAX_RESULT_BYTES}-byte result ceiling")
    payload.update(trunc.as_payload())

    encoded = json.dumps(payload, default=str)
    if len(encoded) > MAX_RESULT_BYTES:
        raise LimitExceeded(f"result still {len(encoded)} bytes after reducing {bulky!r}")
    return payload
