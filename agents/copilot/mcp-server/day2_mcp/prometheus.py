"""PromQL queries and alert state, read-only.

Two tools live here because they read the same backend: `query_prometheus` and
`get_alerts`.

PromQL is a query language with no write forms — there is no `INSERT`, and the
mutating surface of Prometheus lives on separate admin endpoints that `http.py`
refuses. So the safety story here is not "parse the query and look for
dangerous verbs", which would be a losing game against a language we do not
own. It is: the transport can only GET, the admin paths are blocked, and the
query is passed through verbatim so the citation is exactly reproducible.

What *is* checked here is cost, not safety. A query with no time bound or a
high-cardinality label can return an enormous result, and the caps in
`limits.py` apply before anything is serialised.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from day2_mcp.http import ReadOnlyHttp
from day2_mcp.limits import (
    MAX_ALERTS,
    MAX_POINTS_PER_SERIES,
    MAX_SERIES,
    Truncation,
    cap_list,
    clamp_timeout,
    enforce_result_bytes,
)
from day2_mcp.provenance import Provenance


def _parse_time(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    text = value.strip()
    # Accept RFC3339 with or without the trailing Z, and bare unix seconds.
    try:
        if text.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(text), tz=UTC)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise ValueError(
            f"could not parse time {value!r}; use RFC3339 (2026-08-30T17:00:00Z) "
            "or unix seconds"
        ) from exc


def query_prometheus(
    http: ReadOnlyHttp,
    query: str,
    start: str | None = None,
    end: str | None = None,
    step: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run a PromQL query. Instant if no window is given, range if one is.

    The range form is the one that answers "why did X spike at 14:20" — an
    instant query at a past timestamp tells you the value but not the shape,
    and the shape is usually the answer.
    """
    if not query or not query.strip():
        raise ValueError("query_prometheus requires a non-empty PromQL query")

    trunc = Truncation()
    now = datetime.now(UTC)
    is_range = bool(start or end)

    if is_range:
        start_dt = _parse_time(start, now - timedelta(hours=1))
        end_dt = _parse_time(end, now)
        if end_dt <= start_dt:
            raise ValueError(f"end ({end_dt.isoformat()}) must be after start")
        resolved_step = step or _auto_step(start_dt, end_dt)
        path = "/api/v1/query_range"
        params: dict[str, Any] = {
            "query": query,
            "start": start_dt.timestamp(),
            "end": end_dt.timestamp(),
            "step": resolved_step,
            "timeout": f"{clamp_timeout(timeout_seconds):.0f}s",
        }
        window = f"{start_dt.isoformat()}..{end_dt.isoformat()}"
    else:
        path = "/api/v1/query"
        params = {"query": query, "timeout": f"{clamp_timeout(timeout_seconds):.0f}s"}
        window = None
        resolved_step = None

    body = http.get_json(path, params, timeout=timeout_seconds)
    if body.get("status") != "success":
        raise RuntimeError(
            f"prometheus rejected the query: {body.get('errorType')}: {body.get('error')}"
        )

    data = body.get("data") or {}
    result = data.get("result") or []
    result = cap_list(list(result), MAX_SERIES, "series", trunc)

    series: list[dict[str, Any]] = []
    for item in result:
        entry: dict[str, Any] = {"metric": item.get("metric", {})}
        if "values" in item:
            points = cap_list(
                list(item["values"]), MAX_POINTS_PER_SERIES, "samples", trunc
            )
            entry["values"] = [[float(t), v] for t, v in points]
        elif "value" in item:
            timestamp, value = item["value"]
            entry["value"] = [float(timestamp), value]
        series.append(entry)

    prov = Provenance(
        source="prometheus",
        query=query,
        window=window,
        endpoint=http.base_url,
        extra={"step": resolved_step} if resolved_step else {},
    )

    payload = {
        "result_type": data.get("resultType"),
        "series_count": len(series),
        "series": series,
        "provenance": prov.reference(),
        "citation_id": prov.citation_id(),
    }
    payload.update(trunc.as_payload())
    return enforce_result_bytes(payload, trunc)


def _auto_step(start: datetime, end: datetime) -> str:
    """Pick a step that keeps a range query under the per-series point cap.

    Without this, a one-hour range at the scrape interval is ~240 points and
    fine, but a 24-hour range is ~5,760 — which the cap would then truncate
    into a misleading partial window. Choosing the step up front means the
    model gets the *whole* window at a coarser resolution, which is the honest
    trade: a gap in the middle of a time series is far more misleading than a
    lower sample rate.
    """
    span = (end - start).total_seconds()
    # ceil, not truncate: int() rounding *down* makes the step slightly too
    # small, which puts the window back over the cap and reintroduces the
    # truncation this function exists to avoid.
    step = max(15, math.ceil(span / MAX_POINTS_PER_SERIES))
    return f"{step}s"


def get_alerts(
    http: ReadOnlyHttp,
    state: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Current alert state, plus the rule definitions behind them.

    Returns both because they answer different halves of one question. The
    firing list says what is wrong now; the rules say what the system is even
    capable of noticing — and "no alert fired" means something very different
    depending on whether a rule for that condition exists at all.
    """
    trunc = Truncation()
    alerts_body = http.get_json("/api/v1/alerts", timeout=timeout_seconds)
    if alerts_body.get("status") != "success":
        raise RuntimeError(f"prometheus /alerts failed: {alerts_body.get('error')}")

    alerts = list((alerts_body.get("data") or {}).get("alerts") or [])
    if state:
        wanted = state.strip().lower()
        if wanted not in ("firing", "pending", "inactive"):
            raise ValueError(f"state must be firing, pending or inactive; got {state!r}")
        alerts = [a for a in alerts if str(a.get("state", "")).lower() == wanted]
    alerts = cap_list(alerts, MAX_ALERTS, "alerts", trunc)

    rules_body = http.get_json("/api/v1/rules", timeout=timeout_seconds)
    rules: list[dict[str, Any]] = []
    for group in (rules_body.get("data") or {}).get("groups") or []:
        for rule in group.get("rules") or []:
            if rule.get("type") != "alerting":
                continue
            rules.append(
                {
                    "name": rule.get("name"),
                    "group": group.get("name"),
                    "expr": rule.get("query"),
                    "for": rule.get("duration"),
                    "state": rule.get("state"),
                    "labels": rule.get("labels", {}),
                    "annotations": rule.get("annotations", {}),
                }
            )
    rules = cap_list(rules, MAX_ALERTS, "rules", trunc)

    prov = Provenance(
        source="prometheus",
        query=f"/api/v1/alerts{f'?state={state}' if state else ''} + /api/v1/rules",
        endpoint=http.base_url,
    )
    payload = {
        "firing_count": sum(1 for a in alerts if a.get("state") == "firing"),
        "pending_count": sum(1 for a in alerts if a.get("state") == "pending"),
        "alerts": [
            {
                "name": (a.get("labels") or {}).get("alertname"),
                "state": a.get("state"),
                "active_at": a.get("activeAt"),
                "value": a.get("value"),
                "labels": a.get("labels", {}),
                "annotations": a.get("annotations", {}),
            }
            for a in alerts
        ],
        "rules_defined": len(rules),
        "rules": rules,
        "provenance": prov.reference(),
        "citation_id": prov.citation_id(),
    }
    payload.update(trunc.as_payload())
    return enforce_result_bytes(payload, trunc)
