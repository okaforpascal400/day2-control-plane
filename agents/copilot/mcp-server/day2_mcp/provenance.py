"""Provenance — the reference every tool result carries back.

The copilot's whole claim is "answers with cited evidence". That claim is only
worth something if a human can *re-run* the evidence and get the same thing, so
a citation here is never prose like "according to Prometheus". It is the exact
query, against the named source, over the named window, at a recorded instant —
enough for a reader to paste it into Grafana and check.

`Provenance.reference()` is what the UI's evidence sidebar shows and what the
audit trail stores. It is deliberately a *reproduction recipe*, not a summary:
the moment a citation becomes unreproducible it stops being evidence and
becomes decoration, and decoration is what makes a confident wrong answer look
right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Provenance:
    """Where one tool result came from, precisely enough to re-run."""

    source: str  # "prometheus" | "loki" | "repo" | "git" | "alertmanager"
    query: str  # the PromQL / LogQL / path / git argv, verbatim
    fetched_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    window: str | None = None  # "2026-08-30T17:00Z..2026-08-30T18:00Z"
    endpoint: str | None = None  # base URL or repo path, never with credentials
    extra: dict[str, Any] = field(default_factory=dict)

    def reference(self) -> dict[str, Any]:
        ref: dict[str, Any] = {
            "source": self.source,
            "query": self.query,
            "fetched_at": self.fetched_at,
        }
        if self.window:
            ref["window"] = self.window
        if self.endpoint:
            ref["endpoint"] = self.endpoint
        if self.extra:
            ref.update(self.extra)
        return ref

    def citation_id(self) -> str:
        """Short stable handle the model cites and the sidebar keys on."""
        digest = abs(hash((self.source, self.query, self.fetched_at))) % 10**8
        return f"{self.source}:{digest:08d}"
