"""Shared library for every agent in this repo.

The governance pillars from CLAUDE.md map onto these modules:

| Pillar               | Where it lives                                      |
|----------------------|-----------------------------------------------------|
| Least-privilege      | `scopes.py` — declared at startup, enforced per call |
| Sandboxed execution  | `github.py` — fixed argv, no shell, `gh` token only  |
| Audit trails         | `audit.py` — one entry per externally-visible action |
| Human-in-the-loop    | `guardrails.py` — merge/main/`.github` refused       |
| Secrets via env      | `claude.py` — `ANTHROPIC_API_KEY` from env, never disk|
| Output verification  | `diffs.py` — parse, permit, then `git apply --check` |
"""

from day2_agents.audit import AuditEntry, AuditLogger
from day2_agents.claude import ClaudeClient, ModelCall, ModelError
from day2_agents.diffs import DiffRejected, apply_diff, diff_paths, validate_diff
from day2_agents.github import GitHubError, GitHubHelper, GitHubRefused
from day2_agents.guardrails import GuardrailViolation
from day2_agents.scopes import Action, PermissionDenied, PermissionSet

__all__ = [
    "Action",
    "AuditEntry",
    "AuditLogger",
    "ClaudeClient",
    "DiffRejected",
    "GitHubError",
    "GitHubHelper",
    "GitHubRefused",
    "GuardrailViolation",
    "ModelCall",
    "ModelError",
    "PermissionDenied",
    "PermissionSet",
    "apply_diff",
    "diff_paths",
    "validate_diff",
]
