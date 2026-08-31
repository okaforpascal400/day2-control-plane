"""Incident replay: reconstruct a time window as a cited, chronological timeline.

## Why the evidence gathering is scripted rather than model-driven

The chat path lets the model choose its own tools, which is right when the
question is open-ended. Replay is not open-ended: the question is always "what
happened between A and B", and the answer always needs the same four sources —
metrics, logs, alert state, and what was deployed. Two consequences follow.

**Coverage becomes a property of the code.** A model deciding its own queries
might not think to check git, and the timeline would then silently omit the
deploy that caused everything. Here the four sources are always read, so a gap
in the timeline means the data is genuinely empty rather than unexamined.

**Cost becomes predictable.** Gathering is a fixed number of tool calls with no
model in the loop, then exactly one model call to narrate. A replay costs about
what one chat question costs, instead of scaling with how curious the model
feels.

The model's job is therefore narrowed to what models are actually good at:
ordering heterogeneous events into a readable narrative and saying what they
suggest. It does not choose the evidence, and it cannot add a timeline entry
that has no citation — `_validate` drops any entry whose `citation_id` was not
produced by this replay's own gathering, and records that it did.

## Inflections, not raw series

Handing the model 500 raw samples per metric would be expensive and hard to
reason over. `_inflections` reduces each series to the points where it actually
*changed* — first non-zero, local maxima, return to baseline — which is what a
human reading a graph picks out, and what a timeline entry needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from copilot.receipts import AnswerRecord, CostRecord, EvidenceEntry
from copilot.runtime import CopilotSession, Turn, _looks_like_a_decline
from day2_agents.claude import parse_json_object

# The four sources, always read. Each entry is (label, tool, arguments-builder).
REPLAY_METRICS: tuple[tuple[str, str], ...] = (
    ("job queue depth (pending)", 'sum(day2_job_queue_depth{status="pending"})'),
    ("job queue depth (completed)", 'sum(day2_job_queue_depth{status="completed"})'),
    (
        "request rate",
        "sum(rate(http_request_duration_highr_seconds_count[2m]))",
    ),
    (
        "p95 request latency",
        "histogram_quantile(0.95, sum by (le) "
        "(rate(http_request_duration_highr_seconds_bucket[5m])))",
    ),
    (
        "job processing p95",
        "histogram_quantile(0.95, sum by (le) "
        "(rate(day2_job_processing_seconds_bucket[5m])))",
    ),
)

REPLAY_SYSTEM = """You are reconstructing what happened in a Kubernetes system \
during a specific time window, from evidence that has already been gathered for \
you. You are not investigating — the evidence is fixed; you are ordering and \
explaining it.

Return ONE JSON object, no prose around it:

{
  "summary": "two or three sentences: what happened in this window, in plain \
language",
  "timeline": [
    {
      "timestamp": "RFC3339, from the evidence",
      "source": "metrics|logs|alerts|deploy",
      "headline": "short, specific, past tense",
      "detail": "one or two sentences on what this is and why it matters",
      "citation_id": "the citation_id of the evidence item this comes from"
    }
  ],
  "conclusion": "what the sequence suggests, or an explicit statement that the \
evidence does not establish a cause"
}

RULES

- Every timeline entry MUST carry a citation_id that appears in the evidence \
you were given. An entry you cannot cite must be left out.
- Order strictly by timestamp, earliest first.
- Use the timestamps in the evidence. Do not invent or round them.
- If the evidence shows nothing happened in this window, say so in the summary \
and return an empty timeline. That is a correct answer, and inventing activity \
to fill a window is the worst thing you can do here.
- Do not assert causation the evidence does not establish. "The queue climbed \
while the alert was pending" is supportable; "the queue climbed because the \
worker was starved" is not, unless something in the evidence says so."""


@dataclass
class ReplayWindow:
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def as_dict(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": int(self.duration.total_seconds()),
        }


@dataclass
class TimelineEntry:
    timestamp: str
    source: str
    headline: str
    detail: str
    citation_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "headline": self.headline,
            "detail": self.detail,
            "citation_id": self.citation_id,
        }


@dataclass
class Replay:
    window: ReplayWindow
    summary: str = ""
    conclusion: str = ""
    timeline: list[TimelineEntry] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    turn: Turn | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.as_dict(),
            "summary": self.summary,
            "conclusion": self.conclusion,
            "timeline": [e.as_dict() for e in self.timeline],
            "dropped_uncited_entries": self.dropped,
        }


def parse_window(start: str, end: str) -> ReplayWindow:
    def _parse(value: str) -> datetime:
        text = value.strip()
        try:
            if text.replace(".", "", 1).isdigit():
                return datetime.fromtimestamp(float(text), tz=UTC)
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError as exc:
            raise ValueError(
                f"could not parse {value!r}; use RFC3339 (2026-08-30T17:00:00Z)"
            ) from exc

    window = ReplayWindow(_parse(start), _parse(end))
    if window.end <= window.start:
        raise ValueError("end must be after start")
    if window.duration > timedelta(days=2):
        raise ValueError(
            f"window is {window.duration}; replay is capped at 48h "
            "(Loki's retention, and beyond that the timeline stops being readable)"
        )
    return window


def _inflections(series: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    """Reduce a time series to the points where it meaningfully changed.

    Deliberately simple and explainable rather than statistical: a reader
    checking the timeline against Grafana should be able to see why each point
    was picked. Reports the first sample, the peak, the first rise off
    baseline, the return to baseline, and the last sample.
    """
    points: list[dict[str, Any]] = []
    for item in series:
        values = item.get("values") or []
        if not values:
            continue
        numeric = [
            (float(t), float(v))
            for t, v in values
            if v not in (None, "NaN", "+Inf", "-Inf")
        ]
        if not numeric:
            continue

        baseline = numeric[0][1]
        peak_t, peak_v = max(numeric, key=lambda p: p[1])
        marks: dict[str, tuple[float, float]] = {
            "window start": numeric[0],
            "window end": numeric[-1],
        }
        if peak_v > baseline:
            marks["peak"] = (peak_t, peak_v)
            rise = next((p for p in numeric if p[1] > baseline), None)
            if rise:
                marks["first rise above baseline"] = rise
            after_peak = [p for p in numeric if p[0] > peak_t and p[1] <= baseline]
            if after_peak:
                marks["returned to baseline"] = after_peak[0]

        for what, (timestamp, value) in marks.items():
            points.append(
                {
                    "metric": label,
                    "labels": item.get("metric", {}),
                    "what": what,
                    "timestamp": datetime.fromtimestamp(timestamp, tz=UTC).isoformat(),
                    "value": round(value, 4),
                }
            )
    points.sort(key=lambda p: p["timestamp"])
    return points


def gather(
    session: CopilotSession, window: ReplayWindow
) -> tuple[list[dict], list[EvidenceEntry]]:
    """Read all four sources for the window. No model involved."""
    registry = session.registry
    bundle: list[dict[str, Any]] = []
    evidence: list[EvidenceEntry] = []
    start_iso, end_iso = window.start.isoformat(), window.end.isoformat()

    def _record(result: dict[str, Any]) -> dict[str, Any]:
        entry = EvidenceEntry.from_tool_call(len(evidence), registry.calls[-1], result)
        evidence.append(entry)
        return result

    # 1. Metrics — reduced to inflections.
    for label, query in REPLAY_METRICS:
        result = _record(
            registry.call(
                "query_prometheus",
                {"query": query, "start": start_iso, "end": end_iso, "step": None},
            )
        )
        if result.get("is_error"):
            bundle.append(
                {
                    "kind": "metrics",
                    "metric": label,
                    "error": result["error"],
                    "citation_id": None,
                }
            )
            continue
        points = _inflections(result.get("series") or [], label)
        bundle.append(
            {
                "kind": "metrics",
                "metric": label,
                "query": query,
                "citation_id": result.get("citation_id"),
                "inflections": points,
                "note": "no data in this window" if not points else None,
            }
        )

    # 2. Logs — errors and warnings first, then a sample of everything.
    for label, logql in (
        (
            "errors and warnings",
            '{namespace="default"} |~ "(?i)error|warn|exception|fail"',
        ),
        ("all application logs", '{namespace="default"}'),
    ):
        result = _record(
            registry.call(
                "search_logs",
                {"query": logql, "start": start_iso, "end": end_iso, "limit": 60},
            )
        )
        if result.get("is_error"):
            bundle.append({"kind": "logs", "selection": label, "error": result["error"]})
            continue
        bundle.append(
            {
                "kind": "logs",
                "selection": label,
                "query": logql,
                "citation_id": result.get("citation_id"),
                "line_count": result.get("line_count", 0),
                "entries": [
                    {
                        "timestamp": e["timestamp"],
                        "pod": (e.get("labels") or {}).get("pod"),
                        "line": e["line"][:400],
                    }
                    for e in (result.get("entries") or [])[:40]
                ],
            }
        )

    # 3. Alerts — current state and the rules that exist.
    result = _record(registry.call("get_alerts", {"state": None}))
    if not result.get("is_error"):
        bundle.append(
            {
                "kind": "alerts",
                "citation_id": result.get("citation_id"),
                "firing_count": result.get("firing_count"),
                "pending_count": result.get("pending_count"),
                "alerts": result.get("alerts", []),
                "rules_defined": [r.get("name") for r in result.get("rules", [])],
                "note": (
                    "Alert state is read at replay time, not historically — "
                    "Prometheus does not retain past alert instances. An alert "
                    "listed here as firing may have started before this window."
                ),
            }
        )

    # 4. Deploys — commits landed in the window.
    result = _record(registry.call("git_history", {"mode": "log", "max_count": 40}))
    if not result.get("is_error"):
        in_window = [
            c for c in result.get("commits", []) if _within(c.get("date"), window)
        ]
        bundle.append(
            {
                "kind": "deploy",
                "citation_id": result.get("citation_id"),
                "commits_in_window": in_window,
                "note": "no commits landed in this window" if not in_window else None,
            }
        )

    return bundle, evidence


def _within(date: str | None, window: ReplayWindow) -> bool:
    if not date:
        return False
    try:
        when = datetime.fromisoformat(str(date).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return False
    return window.start <= when <= window.end


def run_replay(session: CopilotSession, start: str, end: str) -> Replay:
    """Gather the window, narrate it once, validate citations, sign a receipt."""
    window = parse_window(start, end)
    replay = Replay(window=window)
    question = f"Replay the window {window.start.isoformat()} to {window.end.isoformat()}"

    bundle, evidence = gather(session, window)
    available = {e.citation_id for e in evidence if e.citation_id}

    prompt = (
        f"Window: {window.start.isoformat()} to {window.end.isoformat()} "
        f"({int(window.duration.total_seconds())}s).\n\n"
        "Evidence gathered from the four sources:\n\n"
        + json.dumps(bundle, indent=2, default=str)
    )

    turn = session.ask_raw(
        question=question,
        system=REPLAY_SYSTEM,
        user=prompt,
        evidence=evidence,
        mode="replay",
        extensions={"window": window.as_dict()},
    )
    replay.turn = turn

    try:
        parsed = parse_json_object(turn.answer)
    except Exception as exc:
        turn.supported = False
        turn.unsupported_reason = f"replay narration was not valid JSON: {exc}"
        replay.summary = turn.answer
        # Re-sign before returning. `ask_raw` has already appended a receipt
        # that defaults to supported=True, and returning here without replacing
        # it would leave a signed receipt claiming success for a replay that
        # failed — a receipt that flatters the system, which is the one thing
        # this artifact must never do. Found by a live run whose narration was
        # truncated: the terminal said UNSUPPORTED and the receipt said true.
        _rewrite_receipt(session, turn, replay)
        return replay

    replay.summary = str(parsed.get("summary", "")).strip()
    replay.conclusion = str(parsed.get("conclusion", "")).strip()

    for raw in parsed.get("timeline") or []:
        citation = str(raw.get("citation_id") or "")
        if citation not in available:
            # Same cited-or-flagged rule, applied per entry: an uncited
            # timeline entry is dropped rather than rendered, and the drop is
            # recorded so the omission is visible instead of silent.
            replay.dropped.append(
                {
                    "headline": raw.get("headline"),
                    "citation_id": citation or None,
                    "reason": "citation_id was not produced by this replay's evidence",
                }
            )
            continue
        replay.timeline.append(
            TimelineEntry(
                timestamp=str(raw.get("timestamp", "")),
                source=str(raw.get("source", "unknown")),
                headline=str(raw.get("headline", "")),
                detail=str(raw.get("detail", "")),
                citation_id=citation,
            )
        )
    replay.timeline.sort(key=lambda e: e.timestamp)

    _regrade(replay, turn, available)
    _rewrite_receipt(session, turn, replay)
    return replay


def _regrade(replay: Replay, turn: Turn, available: set[str]) -> None:
    """A replay is supported when its timeline is fully cited, or honestly empty."""
    turn.citations = sorted({e.citation_id for e in replay.timeline})
    empty_is_honest = not replay.timeline and _looks_like_a_decline(
        f"{replay.summary} {replay.conclusion}"
    )

    if replay.dropped:
        turn.supported = False
        turn.unsupported_reason = (
            f"{len(replay.dropped)} timeline entr"
            f"{'y was' if len(replay.dropped) == 1 else 'ies were'} dropped for "
            "citing evidence this replay did not produce"
        )
    elif not replay.timeline and not empty_is_honest:
        turn.supported = False
        turn.unsupported_reason = (
            "replay produced no timeline entries and did not state that the "
            "window was empty"
        )
    elif not turn.citations and not empty_is_honest:
        turn.supported = False
        turn.unsupported_reason = "timeline entries carry no citations"
    else:
        turn.supported = True
        turn.unsupported_reason = None
    assert available is not None  # available is used by the caller's filter


def _rewrite_receipt(session: CopilotSession, turn: Turn, replay: Replay) -> None:
    """Re-sign the turn's receipt with the validated timeline in extensions.

    The receipt built during `ask_raw` recorded the raw narration; the timeline
    is only known to be citation-clean after validation. Rather than sign twice,
    the provisional receipt is discarded and replaced, so the chain contains
    exactly one receipt per answer.
    """
    session.chain.receipts.pop()
    turn.receipt = session.chain.append(
        model=turn.receipt["model"],
        question=turn.receipt["question"],
        evidence=turn.evidence,
        answer=AnswerRecord(
            text=replay.summary or turn.answer,
            citations=turn.citations,
            supported=turn.supported,
            unsupported_reason=turn.unsupported_reason,
        ),
        cost=CostRecord(
            amount=turn.cost_usd,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            model_calls=turn.model_calls,
        ),
        extensions={
            "copilot": {
                "mode": "replay",
                "elapsed_ms": turn.elapsed_ms,
                "session_total_usd": round(session.total_cost_usd, 6),
                "replay": replay.as_dict(),
            }
        },
    )
