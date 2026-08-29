"""Structured JSON audit trail — one entry per externally-visible action.

Governance pillar 3. The schema is fixed by CLAUDE.md:

    {timestamp, agent, trigger, action, target, decision_summary, approved_by}

Those seven keys are always present, always in that order, and always those
types — `AuditEntry.to_dict` is the only writer and the tests assert it. An
optional eighth key, `metadata`, carries machine-readable extras (token counts,
model cost, run IDs) that would otherwise be prose buried in
`decision_summary`; it is an documented extension, never a substitute for the
seven.

`approved_by` is `null` for every entry an agent writes, because an agent
cannot approve anything (CLAUDE.md rule 3). It is the field a human fills in
when they merge the PR the entry describes — the null is the evidence that the
proposal was still unapproved at the moment it was made.

Entries go to two places, for two different readers:
  * **stdout**, one JSON object per line, so they interleave with the rest of
    the workflow log and are readable in the Actions UI without downloading
    anything (same line format the app services use — see
    `app/shared/day2_shared/logging.py`);
  * **a file**, uploaded as a run artifact, so the trail survives the log
    retention window and can be diffed, replayed and attached to a PR.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

SCHEMA_FIELDS: tuple[str, ...] = (
    "timestamp",
    "agent",
    "trigger",
    "action",
    "target",
    "decision_summary",
    "approved_by",
)

DEFAULT_AUDIT_PATH = "audit.jsonl"


@dataclass(frozen=True)
class AuditEntry:
    agent: str
    trigger: str
    action: str
    target: str
    decision_summary: str
    approved_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "timestamp": self.timestamp,
            "agent": self.agent,
            "trigger": self.trigger,
            "action": self.action,
            "target": self.target,
            "decision_summary": self.decision_summary,
            "approved_by": self.approved_by,
        }
        if self.metadata:
            entry["metadata"] = self.metadata
        return entry

    def to_json(self) -> str:
        # sort_keys stays off: SCHEMA_FIELDS order is the documented shape, and
        # a stable insertion order makes the artifact diffable run-to-run.
        return json.dumps(self.to_dict(), default=str)


class AuditLogger:
    """Writes audit entries for one agent invocation.

    `agent` and `trigger` are fixed for the life of the logger — they describe
    *who ran and why*, which cannot change mid-run — so callers supply only the
    per-action fields and cannot accidentally mislabel an entry's provenance.
    """

    def __init__(
        self,
        agent: str,
        trigger: str,
        path: str | os.PathLike[str] | None = None,
        stream: TextIO | None = None,
    ) -> None:
        self.agent = agent
        self.trigger = trigger
        self.path = Path(path or os.environ.get("DAY2_AUDIT_LOG", DEFAULT_AUDIT_PATH))
        self._stream = stream if stream is not None else sys.stdout
        self.entries: list[AuditEntry] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        action: str,
        target: str,
        decision_summary: str,
        approved_by: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append one entry. Called for every externally-visible action."""
        entry = AuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            agent=self.agent,
            trigger=self.trigger,
            action=action,
            target=target,
            decision_summary=decision_summary,
            approved_by=approved_by,
            metadata=metadata or {},
        )
        line = entry.to_json()

        # Flush both sinks immediately. If the agent dies mid-run — or the job
        # is cancelled — the trail must still show what it had already done.
        print(line, file=self._stream, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

        self.entries.append(entry)
        return entry
