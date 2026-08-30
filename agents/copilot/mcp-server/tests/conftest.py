"""Shared fixtures. Nothing here reaches the network or a real cluster."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # day2_mcp
sys.path.insert(0, str(_HERE.parent.parent.parent / "core"))  # day2_agents

from day2_mcp.server import COPILOT_SCOPES, CopilotConfig, ToolRegistry  # noqa: E402

from day2_agents.audit import AuditLogger  # noqa: E402
from day2_agents.scopes import PermissionSet  # noqa: E402


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    import io

    return AuditLogger(
        agent="copilot-test",
        trigger="pytest",
        path=tmp_path / "audit.jsonl",
        stream=io.StringIO(),
    )


@pytest.fixture
def full_scopes() -> PermissionSet:
    return PermissionSet.declare("copilot-test", COPILOT_SCOPES)


@pytest.fixture
def repo_root() -> Path:
    # tests -> mcp-server -> copilot -> agents -> repo root
    return Path(__file__).resolve().parents[4]


@pytest.fixture
def registry(
    repo_root: Path, full_scopes: PermissionSet, audit: AuditLogger
) -> ToolRegistry:
    return ToolRegistry(
        config=CopilotConfig(repo_root=repo_root),
        scopes=full_scopes,
        audit=audit,
    )
