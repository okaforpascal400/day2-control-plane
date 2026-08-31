"""Read-only MCP server over this project's operational surfaces.

Six tools, all reads: `query_prometheus`, `search_logs`, `get_alerts`,
`get_dashboard`, `read_runbook`, `git_history`. The constraints that make it
safe to point a model at a production cluster are enforced in code and asserted
by tests that fail red — see `server.ToolRegistry.call`, `redaction.redact`,
`http.ReadOnlyHttp`, `files._jail` and `git._run_git`.
"""

from day2_mcp.server import COPILOT_SCOPES, CopilotConfig, ToolRegistry, ToolSpec

__all__ = ["COPILOT_SCOPES", "CopilotConfig", "ToolRegistry", "ToolSpec"]
