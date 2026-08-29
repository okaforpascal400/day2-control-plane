from __future__ import annotations

import io
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from day2_agents.audit import AuditLogger
from day2_agents.scopes import Action, PermissionSet

ALL_ACTIONS = list(Action)


@dataclass
class FakeRunner:
    """Records argv instead of running it, and replays canned results.

    Keyed by a substring of the joined argv so a test can pin one command's
    output without describing every command the helper happens to make.
    """

    results: dict[str, subprocess.CompletedProcess] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    stdins: list[str | None] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], stdin: str | None, cwd: str
    ) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        self.stdins.append(stdin)
        joined = " ".join(argv)
        for key, result in self.results.items():
            if key in joined:
                return result
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ran(self, *fragment: str) -> bool:
        return any(all(f in " ".join(call) for f in fragment) for call in self.calls)


def ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, "", stderr)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def audit(tmp_path) -> AuditLogger:
    return AuditLogger(
        agent="test-agent",
        trigger="pytest",
        path=tmp_path / "audit.jsonl",
        stream=io.StringIO(),
    )


@pytest.fixture
def full_scopes() -> PermissionSet:
    return PermissionSet.declare("test-agent", ALL_ACTIONS)


@pytest.fixture
def no_scopes() -> PermissionSet:
    return PermissionSet.declare("test-agent", [])
