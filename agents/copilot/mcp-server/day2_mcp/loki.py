"""LogQL queries against Loki, read-only.

Logs are the highest-risk surface in this whole server, and it is worth being
explicit about why: metrics are numbers with labels, dashboards and runbooks are
files we wrote, git history is code we reviewed — but **logs are arbitrary text
produced at runtime**, and an application that logs a connection string, a
bearer token or a stack trace containing credentials will put that text here.
Loki will faithfully return it.

That is precisely the case `redaction.py` exists for, and it is why the
redactor runs at the registry chokepoint on the way out rather than being
something this module could forget to call. This module's own job is narrower:
bound the query, bound the result, and cite it reproducibly.

One deliberate non-feature: there is no `delete` and no `tail`. Loki's delete
API is blocked in `http.py`; live tailing is a websocket, which this client
cannot open, and a copilot that holds a streaming connection open is a
different (and much more expensive) shape of thing than one that answers a
question and stops.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from day2_mcp.http import ReadOnlyHttp
from day2_mcp.limits import (
    MAX_LOG_LINES,
    Truncation,
    cap_list,
    enforce_result_bytes,
)
from day2_mcp.provenance import Provenance

MAX_LINE_CHARS = 4_000


def search_logs(
    http: ReadOnlyHttp,
    query: str,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    direction: str = "backward",
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run a LogQL query over a time window.

    `query` is LogQL, so it needs a stream selector — `{namespace="default"}`
    at minimum. A bare search term is not valid LogQL and Loki will reject it;
    the error is passed through rather than guessed at, because silently
    wrapping a term in a selector we invented would produce results the citation
    could not explain.
    """
    if not query or not query.strip():
        raise ValueError("search_logs requires a non-empty LogQL query")
    if "{" not in query:
        raise ValueError(
            "LogQL needs a stream selector, e.g. "
            '\'{namespace="default"} |= "error"\'; '
            f"got {query!r}"
        )
    if direction not in ("backward", "forward"):
        raise ValueError(f"direction must be 'backward' or 'forward', got {direction!r}")

    trunc = Truncation()
    now = datetime.now(UTC)
    start_dt = _parse_time(start, now - timedelta(hours=1))
    end_dt = _parse_time(end, now)
    if end_dt <= start_dt:
        raise ValueError(f"end ({end_dt.isoformat()}) must be after start")

    resolved_limit = min(int(limit or MAX_LOG_LINES), MAX_LOG_LINES)

    body = http.get_json(
        "/loki/api/v1/query_range",
        {
            "query": query,
            # Loki wants nanosecond epochs.
            "start": int(start_dt.timestamp() * 1e9),
            "end": int(end_dt.timestamp() * 1e9),
            "limit": resolved_limit,
            "direction": direction,
        },
        timeout=timeout_seconds,
    )
    if body.get("status") != "success":
        raise RuntimeError(f"loki rejected the query: {body.get('error') or body}")

    data = body.get("data") or {}
    result_type = data.get("resultType")
    entries: list[dict[str, Any]] = []

    for stream in data.get("result") or []:
        labels = stream.get("stream", {})
        # "streams" results carry `values`; metric results (from a LogQL metric
        # query like rate()) carry the same key with numeric samples. Both are
        # (timestamp, value) pairs, so one path handles them and the
        # result_type tells the model which it is looking at.
        for timestamp, line in stream.get("values") or []:
            text = str(line)
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + "…[line truncated]"
                trunc.note(f"line: truncated to {MAX_LINE_CHARS} chars")
            entries.append(
                {
                    "timestamp": _ns_to_iso(timestamp),
                    "labels": labels,
                    "line": text,
                }
            )

    entries.sort(key=lambda e: e["timestamp"], reverse=(direction == "backward"))
    entries = cap_list(entries, MAX_LOG_LINES, "log lines", trunc)

    prov = Provenance(
        source="loki",
        query=query,
        window=f"{start_dt.isoformat()}..{end_dt.isoformat()}",
        endpoint=http.base_url,
        extra={"limit": resolved_limit, "direction": direction},
    )
    payload = {
        "result_type": result_type,
        "line_count": len(entries),
        "entries": entries,
        "provenance": prov.reference(),
        "citation_id": prov.citation_id(),
    }
    payload.update(trunc.as_payload())
    return enforce_result_bytes(payload, trunc)


def _parse_time(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    text = value.strip()
    try:
        if text.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(text), tz=UTC)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise ValueError(
            f"could not parse time {value!r}; use RFC3339 (2026-08-30T17:00:00Z) "
            "or unix seconds"
        ) from exc


def _ns_to_iso(nanoseconds: str | int) -> str:
    return datetime.fromtimestamp(int(nanoseconds) / 1e9, tz=UTC).isoformat()
