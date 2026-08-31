"""Tool behaviour: schemas, caps, provenance, and errors-as-results.

The file and git tools run against this repository, so these are real reads
rather than mocked ones. Prometheus and Loki are exercised against a fake
transport — the point of those tests is the parsing, capping and citation
logic, and binding the suite to a running cluster would make it fail for
reasons that have nothing to do with the code (the Phase 5 lesson about a test
suite that goes red on someone else's outage).
"""

from __future__ import annotations

from typing import Any

import pytest
from day2_mcp.limits import (
    MAX_LOG_LINES,
    MAX_RESULT_BYTES,
    MAX_SERIES,
    LimitExceeded,
    Truncation,
    cap_list,
    clamp_timeout,
    enforce_result_bytes,
)
from day2_mcp.loki import search_logs
from day2_mcp.prometheus import get_alerts, query_prometheus


class FakeHttp:
    """Stands in for ReadOnlyHttp. Records what was asked for."""

    base_url = "http://fake:9090"

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []

    def get_json(
        self, path: str, params: dict | None = None, timeout: float | None = None
    ):
        self.requests.append((path, params or {}))
        return self.responses[path]


# --- schemas ----------------------------------------------------------------


def test_every_schema_satisfies_the_strict_tool_use_contract(registry) -> None:
    """`strict: true` requires additionalProperties:false and a full required list."""
    for tool in registry.schemas():
        schema = tool["input_schema"]

        assert tool["strict"] is True, f"{tool['name']} is not strict"
        assert schema["additionalProperties"] is False, (
            f"{tool['name']} allows extra props"
        )
        assert set(schema["required"]) == set(schema["properties"]), (
            f"{tool['name']}: strict mode requires every property in required"
        )
        assert tool["description"].strip(), f"{tool['name']} has no description"


def test_schema_order_is_stable_for_prompt_caching(registry) -> None:
    """Tools render before system and messages; a reordering busts the cache."""
    assert [t["name"] for t in registry.schemas()] == sorted(
        t["name"] for t in registry.schemas()
    )


def test_the_six_phase_six_tools_are_present(registry) -> None:
    assert registry.tool_names() == [
        "get_alerts",
        "get_dashboard",
        "git_history",
        "query_prometheus",
        "read_runbook",
        "search_logs",
    ]


# --- limits -----------------------------------------------------------------


def test_timeouts_are_clamped_at_both_ends() -> None:
    assert clamp_timeout(None) == 10.0
    assert clamp_timeout(0.01) == 0.5
    assert clamp_timeout(9999) == 30.0


def test_capping_a_list_reports_what_it_dropped() -> None:
    trunc = Truncation()
    kept = cap_list(list(range(500)), 10, "widgets", trunc)

    assert len(kept) == 10
    assert trunc.truncated
    assert "widgets: kept 10 of 500" in trunc.reasons


def test_truncation_is_never_silent() -> None:
    """An answer built on a partial window must say so."""
    trunc = Truncation()
    assert trunc.as_payload() == {"truncated": False}

    trunc.note("series: kept 200 of 4000")
    payload = trunc.as_payload()

    assert payload["truncated"] is True
    assert payload["truncation_reasons"] == ["series: kept 200 of 4000"]


def test_the_byte_ceiling_reduces_rather_than_producing_invalid_json() -> None:
    trunc = Truncation()
    payload = {"entries": ["x" * 1000 for _ in range(2000)], "keep": "me"}

    reduced = enforce_result_bytes(payload, trunc)

    assert len(str(reduced)) < len(str(payload))
    assert reduced["keep"] == "me"
    assert reduced["truncated"] is True


def test_an_oversized_string_field_is_dropped_and_reported() -> None:
    trunc = Truncation()
    reduced = enforce_result_bytes({"n": 1, "s": "x" * (MAX_RESULT_BYTES * 2)}, trunc)

    assert reduced["s"] == "[dropped: too large]"
    assert reduced["n"] == 1
    assert reduced["truncated"] is True


def test_an_irreducible_oversized_result_raises_rather_than_lying() -> None:
    """No list or string to shrink, so there is nothing honest to return."""
    trunc = Truncation()
    payload = {"nested": {str(i): {"v": "x" * 200} for i in range(3000)}}

    with pytest.raises(LimitExceeded, match="no reducible field"):
        enforce_result_bytes(payload, trunc)


# --- prometheus -------------------------------------------------------------


def test_an_instant_query_returns_series_with_a_citable_provenance() -> None:
    http = FakeHttp(
        {
            "/api/v1/query": {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"__name__": "up", "job": "api"}, "value": [1.0, "1"]}
                    ],
                },
            }
        }
    )
    result = query_prometheus(http, "up")

    assert result["series_count"] == 1
    assert result["provenance"]["query"] == "up"
    assert result["provenance"]["source"] == "prometheus"
    assert result["citation_id"].startswith("prometheus:")
    assert http.requests[0][0] == "/api/v1/query"


def test_a_windowed_query_uses_the_range_endpoint_and_records_the_window() -> None:
    http = FakeHttp(
        {
            "/api/v1/query_range": {
                "status": "success",
                "data": {"resultType": "matrix", "result": []},
            }
        }
    )
    result = query_prometheus(
        http, "up", start="2026-08-30T17:00:00Z", end="2026-08-30T18:00:00Z"
    )

    assert http.requests[0][0] == "/api/v1/query_range"
    assert (
        result["provenance"]["window"]
        == "2026-08-30T17:00:00+00:00..2026-08-30T18:00:00+00:00"
    )


def test_the_step_is_chosen_to_cover_the_whole_window() -> None:
    """A gap in the middle of a series misleads worse than a coarser one."""
    http = FakeHttp(
        {
            "/api/v1/query_range": {
                "status": "success",
                "data": {"resultType": "matrix", "result": []},
            }
        }
    )
    query_prometheus(http, "up", start="2026-08-29T00:00:00Z", end="2026-08-30T00:00:00Z")

    step = int(http.requests[0][1]["step"].rstrip("s"))
    span = 24 * 3600
    assert span / step <= 500, "step must keep the window under the per-series point cap"


def test_too_many_series_are_capped_and_reported() -> None:
    http = FakeHttp(
        {
            "/api/v1/query": {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"i": str(i)}, "value": [1.0, "1"]}
                        for i in range(MAX_SERIES + 50)
                    ],
                },
            }
        }
    )
    result = query_prometheus(http, "up")

    assert result["series_count"] == MAX_SERIES
    assert result["truncated"] is True


def test_a_rejected_query_raises_with_the_backend_reason() -> None:
    http = FakeHttp(
        {
            "/api/v1/query": {
                "status": "error",
                "errorType": "bad_data",
                "error": "parse error",
            }
        }
    )
    with pytest.raises(RuntimeError, match="parse error"):
        query_prometheus(http, "not a query{{{")


def test_an_empty_query_is_refused_before_any_request() -> None:
    http = FakeHttp({})
    with pytest.raises(ValueError, match="non-empty"):
        query_prometheus(http, "   ")


def test_alerts_return_both_state_and_the_rules_that_define_it() -> None:
    http = FakeHttp(
        {
            "/api/v1/alerts": {
                "status": "success",
                "data": {
                    "alerts": [
                        {
                            "labels": {"alertname": "JobQueueStuck"},
                            "state": "firing",
                            "activeAt": "2026-08-30T17:00:00Z",
                            "annotations": {"summary": "queue stuck"},
                        }
                    ]
                },
            },
            "/api/v1/rules": {
                "status": "success",
                "data": {
                    "groups": [
                        {
                            "name": "day2",
                            "rules": [
                                {
                                    "type": "alerting",
                                    "name": "JobQueueStuck",
                                    "query": "day2_queue_depth > 10",
                                    "duration": 300,
                                    "state": "firing",
                                },
                                {"type": "recording", "name": "ignored"},
                            ],
                        }
                    ]
                },
            },
        }
    )
    result = get_alerts(http)

    assert result["firing_count"] == 1
    assert result["rules_defined"] == 1, "recording rules are not alerting rules"
    assert result["rules"][0]["expr"] == "day2_queue_depth > 10"


# --- loki -------------------------------------------------------------------


def _loki_response(lines: int) -> dict:
    return {
        "/loki/api/v1/query_range": {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"namespace": "default", "app": "day2-worker"},
                        "values": [
                            [str(1756500000000000000 + i), f"line {i}"]
                            for i in range(lines)
                        ],
                    }
                ],
            },
        }
    }


def test_logql_without_a_stream_selector_is_refused_with_an_example() -> None:
    """Guessing a selector would produce results the citation cannot explain."""
    http = FakeHttp({})
    with pytest.raises(ValueError, match="stream selector"):
        search_logs(http, "error")


def test_log_lines_come_back_with_timestamps_labels_and_provenance() -> None:
    http = FakeHttp(_loki_response(3))
    result = search_logs(http, '{namespace="default"} |= "error"')

    assert result["line_count"] == 3
    assert result["entries"][0]["labels"]["app"] == "day2-worker"
    from datetime import datetime

    parsed = datetime.fromisoformat(result["entries"][0]["timestamp"])
    assert parsed.tzinfo is not None, "timestamps must carry their timezone"
    assert result["provenance"]["source"] == "loki"
    assert result["provenance"]["window"]


def test_too_many_log_lines_are_capped() -> None:
    http = FakeHttp(_loki_response(MAX_LOG_LINES + 100))
    result = search_logs(http, '{namespace="default"}')

    assert result["line_count"] == MAX_LOG_LINES
    assert result["truncated"] is True


def test_an_invalid_direction_is_refused() -> None:
    http = FakeHttp({})
    with pytest.raises(ValueError, match="direction"):
        search_logs(http, '{a="b"}', direction="sideways")


# --- registry behaviour -----------------------------------------------------


def test_a_failing_tool_returns_an_error_result_rather_than_raising(registry) -> None:
    """The model must be able to see its mistake and try again."""
    result = registry.call("search_logs", {"query": "no selector"})

    assert result["is_error"] is True
    assert "stream selector" in result["error"]


def test_an_unknown_tool_is_reported_with_the_available_ones(registry) -> None:
    result = registry.call("drop_database", {})

    assert result["is_error"] is True
    assert "no such tool" in result["error"]
    assert "query_prometheus" in result["error"]


def test_every_call_is_audited_with_its_provenance(registry, tmp_path) -> None:
    registry.call("read_runbook", {})
    registry.call("nonexistent", {})

    actions = [e.action for e in registry._audit.entries]
    assert actions == ["mcp_tool:read_runbook", "mcp_tool:nonexistent"]
    assert all(e.approved_by is None for e in registry._audit.entries), (
        "an agent cannot approve anything (CLAUDE.md rule 3)"
    )


def test_reading_a_real_runbook_from_this_repository(registry) -> None:
    index = registry.call("read_runbook", {"path": None})
    assert index["index"] is True
    assert "environment.md" in index["documents"]

    doc = registry.call("read_runbook", {"path": "environment.md"})
    assert doc["index"] is False
    assert "Local Environment" in doc["content"]
    assert doc["provenance"]["query"] == "docs/environment.md"


def test_reading_a_real_dashboard_summarises_panels_not_layout(registry) -> None:
    index = registry.call("get_dashboard", {"name": None})
    assert "app-overview" in index["dashboards"]

    dash = registry.call("get_dashboard", {"name": "app-overview"})
    assert dash["panel_count"] > 0
    assert any(p["queries"] for p in dash["panels"]), "panels should carry their queries"
    assert "gridPos" not in str(dash), "layout is noise for a copilot and costs tokens"


def test_reading_real_git_history(registry) -> None:
    """Asserts the shape of what comes back, not how deep the clone is.

    An earlier version required exactly three commits and went red in CI, where
    `actions/checkout` makes a shallow clone with one. That was the test
    measuring its environment rather than the tool — `max_count` is an upper
    bound, and a repository with fewer commits than that is not a defect.
    """
    result = registry.call("git_history", {"mode": "log", "max_count": 3})

    assert not result.get("is_error"), result.get("error")
    assert 1 <= result["commit_count"] <= 3
    assert result["commit_count"] == len(result["commits"])
    assert all(len(c["sha"]) == 40 for c in result["commits"])
    assert all(c["short_sha"] == c["sha"][:7] for c in result["commits"])
    assert all(c["subject"] for c in result["commits"])
    assert all(c["date"] for c in result["commits"])
    assert result["provenance"]["source"] == "git"
