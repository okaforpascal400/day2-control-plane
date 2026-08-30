"""The tool registry: schemas out, gated calls in.

This is the chokepoint. Every tool call the copilot makes goes through
`ToolRegistry.call()`, and that method — not the individual tool modules — is
where the four governance properties are enforced:

1. **Least-privilege.** Each tool declares the `Action` it needs, and the
   registry asks the caller's `PermissionSet` before dispatching. A copilot
   that did not declare `SEARCH_LOGS` cannot search logs, no matter what the
   model asks for.
2. **Redaction.** The handler's return value is piped through `redact()` before
   it leaves this method. A tool module physically cannot return raw data to
   the model, because it does not return to the model — it returns to here.
3. **Audit.** One entry per call, with the arguments, the outcome, how long it
   took and how many redactions fired.
4. **Errors are results, not exceptions.** A refused or failed tool call comes
   back as a structured `is_error` payload the model can read and react to.
   Raising here would abort the whole question over one bad query, when the
   right behaviour is for the model to see "that selector was invalid" and try
   a better one.

The schemas are declared `strict: true`, which requires `additionalProperties:
false` and an explicit `required` list. That costs a little verbosity and buys
a guarantee: the arguments that reach a handler validate against the schema
exactly, so a handler never has to defend against a missing key or a surprise
type. Given these handlers touch paths and refs, that is worth the verbosity.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from day2_agents.audit import AuditLogger
from day2_agents.scopes import Action, PermissionDenied, PermissionSet
from day2_mcp import files, git, loki, prometheus
from day2_mcp.http import ReadOnlyHttp
from day2_mcp.redaction import RedactionError, redact


@dataclass(frozen=True)
class ToolSpec:
    """One read-only tool: its schema, the scope it needs, and its handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    scope: Action
    handler: Callable[..., dict[str, Any]]

    def as_anthropic_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


@dataclass
class CopilotConfig:
    """Where the read-only surfaces live. No credentials by design.

    Prometheus and Loki are reached over cluster-local HTTP with no auth, which
    is why nothing here is a secret. If a deployment ever needs credentials,
    they belong in the environment and must never be echoed into a provenance
    reference — `Provenance.endpoint` records the base URL for exactly that
    reason, and a URL with userinfo would be redacted by `uri_credentials`.
    """

    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    repo_root: Path = field(default_factory=lambda: Path.cwd())
    docs_root: Path | None = None
    dashboards_root: Path | None = None

    def resolved_docs_root(self) -> Path:
        return self.docs_root or (Path(self.repo_root) / "docs")

    def resolved_dashboards_root(self) -> Path:
        return self.dashboards_root or (
            Path(self.repo_root) / "deploy" / "observability" / "dashboards"
        )


def _string(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _nullable_string(desc: str) -> dict[str, Any]:
    # `strict: true` requires every property to be listed in `required`, so an
    # optional argument is expressed as a nullable type rather than by omission.
    return {"type": ["string", "null"], "description": desc}


def _nullable_integer(desc: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": desc}


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


class ToolRegistry:
    """Holds the tool set and is the only way to invoke one."""

    def __init__(
        self,
        config: CopilotConfig,
        scopes: PermissionSet,
        audit: AuditLogger,
    ) -> None:
        self.config = config
        self._scopes = scopes
        self._audit = audit
        self._prom = ReadOnlyHttp(config.prometheus_url)
        self._loki = ReadOnlyHttp(config.loki_url)
        self._specs: dict[str, ToolSpec] = {
            spec.name: spec for spec in self._build_specs()
        }
        self.calls: list[dict[str, Any]] = []

    # -- schemas ---------------------------------------------------------
    def _build_specs(self) -> list[ToolSpec]:
        cfg = self.config
        return [
            ToolSpec(
                name="query_prometheus",
                description=(
                    "Run a PromQL query against the cluster's Prometheus. Omit start/end "
                    "for an instant query; supply them for a range query, which is what "
                    "you want when investigating how a value changed over time. Returns "
                    "series with their labels and samples, plus a provenance reference "
                    "you must cite. Results are capped; check the `truncated` field."
                ),
                input_schema=_schema(
                    {
                        "query": _string(
                            "PromQL expression, e.g. "
                            "histogram_quantile(0.95, sum by (le) "
                            "(rate(http_request_duration_seconds_bucket[5m])))"
                        ),
                        "start": _nullable_string(
                            "RFC3339 start of the range, e.g. 2026-08-30T17:00:00Z. "
                            "Null for an instant query."
                        ),
                        "end": _nullable_string(
                            "RFC3339 end of the range. Null for instant."
                        ),
                        "step": _nullable_string(
                            "Range resolution, e.g. '30s'. Null lets the server pick a "
                            "step that covers the whole window without truncation."
                        ),
                    }
                ),
                scope=Action.QUERY_METRICS,
                handler=lambda query, start=None, end=None, step=None: (
                    prometheus.query_prometheus(self._prom, query, start, end, step)
                ),
            ),
            ToolSpec(
                name="search_logs",
                description=(
                    "Run a LogQL query against Loki. The query MUST include a stream "
                    "selector, e.g. "
                    '\'{namespace="default", app="day2-worker"} |= "error"\'. '
                    "Returns matching log lines with timestamps and labels. Secrets are "
                    "redacted from every line before you see them."
                ),
                input_schema=_schema(
                    {
                        "query": _string("LogQL expression including a stream selector"),
                        "start": _nullable_string(
                            "RFC3339 start of the window. Null defaults to one hour ago."
                        ),
                        "end": _nullable_string("RFC3339 end. Null defaults to now."),
                        "limit": _nullable_integer(
                            "Max lines (capped at 300). Null for the cap."
                        ),
                    }
                ),
                scope=Action.SEARCH_LOGS,
                handler=lambda query, start=None, end=None, limit=None: loki.search_logs(
                    self._loki, query, start, end, limit
                ),
            ),
            ToolSpec(
                name="get_alerts",
                description=(
                    "Current alert state from Prometheus, plus every alerting rule that "
                    "is defined. Read both: the rules tell you what this system is "
                    "capable of noticing, so 'nothing fired' means something different "
                    "when no rule covers the condition being asked about."
                ),
                input_schema=_schema(
                    {
                        "state": _nullable_string(
                            "Filter to 'firing', 'pending' or 'inactive'. Null for all."
                        ),
                    }
                ),
                scope=Action.READ_ALERTS,
                handler=lambda state=None: prometheus.get_alerts(self._prom, state),
            ),
            ToolSpec(
                name="get_dashboard",
                description=(
                    "Read a Grafana dashboard as committed in this repository. Call with "
                    "a null name first to list what exists. Returns panel titles, types "
                    "and their queries — the queries are the useful part, because they "
                    "show exactly how this system measures the thing you were "
                    "asked about."
                ),
                input_schema=_schema(
                    {
                        "name": _nullable_string(
                            "Dashboard name without .json, e.g. 'app-overview'. "
                            "Null lists the available dashboards."
                        ),
                    }
                ),
                scope=Action.READ_DASHBOARD,
                handler=lambda name=None: files.get_dashboard(
                    cfg.resolved_dashboards_root(), name
                ),
            ),
            ToolSpec(
                name="read_runbook",
                description=(
                    "Read a markdown document from the repository's docs/ tree (ADRs, "
                    "environment notes, runbooks). Call with a null path first to list "
                    "what exists rather than guessing a filename."
                ),
                input_schema=_schema(
                    {
                        "path": _nullable_string(
                            "Path relative to docs/, e.g. 'adr/0001-k3s-over-eks.md'. "
                            "Null lists available documents."
                        ),
                    }
                ),
                scope=Action.READ_RUNBOOK,
                handler=lambda path=None: files.read_runbook(
                    cfg.resolved_docs_root(), path
                ),
            ),
            ToolSpec(
                name="git_history",
                description=(
                    "Read this repository's git history. mode='log' lists commits "
                    "(optionally for one path — this is how you find out *why* a line "
                    "exists); mode='show' returns one commit's message and diff; "
                    "mode='blame' attributes each line of a file to its commit. Cite the "
                    "commit sha in your answer."
                ),
                input_schema=_schema(
                    {
                        "mode": {
                            "type": "string",
                            "enum": ["log", "show", "blame"],
                            "description": "Which read to perform.",
                        },
                        "ref": _nullable_string(
                            "Branch, tag or sha. Null means HEAD. "
                            "'ref:path' blob syntax is refused."
                        ),
                        "path": _nullable_string(
                            "Repository-relative path. Required for blame; optional for "
                            "log, where it restricts history to that file."
                        ),
                        "max_count": _nullable_integer(
                            "For mode='log', how many commits (capped at 100)."
                        ),
                    }
                ),
                scope=Action.READ_GIT_HISTORY,
                handler=lambda mode="log", ref=None, path=None, max_count=None: (
                    git.git_history(cfg.repo_root, mode, ref, path, max_count)
                ),
            ),
        ]

    def schemas(self) -> list[dict[str, Any]]:
        """Anthropic tool definitions, in a stable order.

        Stable because tool definitions are rendered before `system` and
        `messages` for prompt caching — a set that reorders itself between
        requests would invalidate the cache prefix on every question and
        quietly multiply the cost of a session.
        """
        return [self._specs[name].as_anthropic_tool() for name in sorted(self._specs)]

    def tool_names(self) -> list[str]:
        return sorted(self._specs)

    def declared_scopes(self) -> list[str]:
        return self._scopes.as_list()

    # -- the chokepoint --------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one tool. The only path from a model to a data source.

        Never raises for an ordinary failure: a refusal, a bad query or a
        backend error comes back as `{"is_error": True, "error": ...}` so the
        model can correct course. Only a redaction failure is treated as fatal
        to the *result* — and even then the payload is dropped, never returned.
        """
        started = time.monotonic()
        spec = self._specs.get(name)
        if spec is None:
            return self._error_result(
                name,
                arguments,
                f"no such tool {name!r}; available: {', '.join(self.tool_names())}",
            )

        try:
            self._scopes.require(spec.scope)
        except PermissionDenied as exc:
            return self._error_result(name, arguments, str(exc), kind="permission_denied")

        # `strict: true` guarantees shape, but a null for an optional argument
        # arrives as an explicit None — strip those so handler defaults apply.
        cleaned = {k: v for k, v in (arguments or {}).items() if v is not None}

        try:
            raw = spec.handler(**cleaned)
        except TypeError as exc:
            return self._error_result(name, arguments, f"bad arguments: {exc}")
        except Exception as exc:
            return self._error_result(name, arguments, f"{type(exc).__name__}: {exc}")

        try:
            cleaned_result, report = redact(raw)
        except RedactionError as exc:
            # Fail closed: the unredacted payload is discarded here and never
            # reaches the model, the UI, or the audit file.
            return self._error_result(
                name,
                arguments,
                f"result withheld: redaction failed ({exc})",
                kind="redaction_failed",
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        record = {
            "tool": name,
            "arguments": cleaned,
            "citation_id": cleaned_result.get("citation_id"),
            "provenance": cleaned_result.get("provenance"),
            "elapsed_ms": elapsed_ms,
            "is_error": False,
            **report.as_metadata(),
        }
        self.calls.append(record)

        self._audit.record(
            action=f"mcp_tool:{name}",
            target=str(cleaned_result.get("citation_id") or name),
            decision_summary=(
                f"read {name} ({elapsed_ms}ms, {report.substitutions} redactions)"
            ),
            metadata={
                "arguments": cleaned,
                "provenance": cleaned_result.get("provenance"),
                "elapsed_ms": elapsed_ms,
                **report.as_metadata(),
            },
        )
        return cleaned_result

    def _error_result(
        self,
        name: str,
        arguments: dict[str, Any],
        message: str,
        kind: str = "tool_error",
    ) -> dict[str, Any]:
        record = {
            "tool": name,
            "arguments": arguments,
            "is_error": True,
            "error": message,
            "error_kind": kind,
        }
        self.calls.append(record)
        self._audit.record(
            action=f"mcp_tool:{name}",
            target=name,
            decision_summary=f"refused or failed: {message}",
            metadata={"arguments": arguments, "error_kind": kind},
        )
        return {"is_error": True, "error": message, "error_kind": kind}


# The scopes the copilot declares. Read-only, and every one of them is a read
# of an operational surface — see the Phase 6 block in `scopes.py`.
COPILOT_SCOPES: tuple[Action, ...] = (
    Action.CALL_MODEL,
    Action.QUERY_METRICS,
    Action.SEARCH_LOGS,
    Action.READ_DASHBOARD,
    Action.READ_RUNBOOK,
    Action.READ_GIT_HISTORY,
    Action.READ_ALERTS,
)
