from __future__ import annotations

import io
import json
import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from day2_agents.audit import AuditLogger
from day2_agents.scopes import PermissionSet
from triage.agent import SCOPES

REPO = "okaforpascal400/day2-control-plane"
RUN_ID = "17042"
SHA = "abc1234def5678901234567890abcdef12345678"
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"


@dataclass
class FakeRunner:
    """Answers `gh`/`git` calls from a substring-keyed table."""

    results: dict[str, subprocess.CompletedProcess] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    stdins: list[str | None] = field(default_factory=list)

    def __call__(self, argv, stdin, cwd):
        argv = list(argv)
        self.calls.append(argv)
        self.stdins.append(stdin)
        joined = " ".join(argv)
        for key, result in self.results.items():
            if key in joined:
                return result
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ran(self, *fragment: str) -> bool:
        return any(all(f in " ".join(c) for f in fragment) for c in self.calls)

    def body_for(self, fragment: str) -> str | None:
        for call, stdin in zip(self.calls, self.stdins, strict=True):
            if fragment in " ".join(call):
                return stdin
        return None

    def comment_body(self, fragment: str) -> str:
        """The `body` field of a `gh api --input -` POST, JSON-decoded."""
        raw = self.body_for(fragment)
        return json.loads(raw)["body"] if raw else ""


def ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def gh_runner(
    branch: str = "phase4/scenario-bad-dep",
    job_name: str = "pytest (api)",
    step_name: str = "Install dependencies",
    log: str = "",
    pr_url: str = f"https://github.com/{REPO}/pull/11",
) -> FakeRunner:
    runner = FakeRunner()
    runner.results[f"actions/runs/{RUN_ID}/jobs"] = ok(
        json.dumps(
            {
                "jobs": [
                    {"name": "ruff", "conclusion": "success", "id": 1, "steps": []},
                    {
                        "name": job_name,
                        "conclusion": "failure",
                        "id": 2,
                        "steps": [
                            {"name": "Checkout", "conclusion": "success"},
                            {"name": step_name, "conclusion": "failure"},
                        ],
                    },
                ]
            }
        )
    )
    runner.results[f"actions/runs/{RUN_ID}"] = ok(
        json.dumps(
            {
                "head_sha": SHA,
                "head_branch": branch,
                "html_url": RUN_URL,
                "run_number": 42,
                "name": "ci",
            }
        )
    )
    runner.results["jobs/2/logs"] = ok(log)
    runner.results["pr create"] = ok(pr_url + "\n")
    runner.results["commits"] = ok(json.dumps({"html_url": "https://x/c/1"}))
    runner.results["rev-parse --abbrev-ref"] = ok("")  # overridden per test
    runner.results["rev-parse HEAD"] = ok("cafebabe0001\n")
    return runner


class FakeClaude:
    """Stands in for the Anthropic client; returns one canned JSON payload."""

    def __init__(self, payload: dict | str, stop_reason: str = "end_turn"):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.prompts: list[dict] = []
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.prompts.append(kwargs)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=text)],
                    model="claude-opus-5",
                    stop_reason=stop_reason,
                    usage=SimpleNamespace(
                        input_tokens=9000,
                        output_tokens=1800,
                        cache_creation_input_tokens=0,
                        cache_read_input_tokens=0,
                    ),
                    _request_id="req_fake",
                )

        self.messages = Messages()


@pytest.fixture
def audit(tmp_path) -> AuditLogger:
    return AuditLogger(AGENT_NAME, "pytest", tmp_path / "audit.jsonl", io.StringIO())


AGENT_NAME = "triage"


@pytest.fixture
def scopes() -> PermissionSet:
    return PermissionSet.declare(AGENT_NAME, SCOPES)


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo, so `git apply --check` is genuinely exercised."""
    root = tmp_path / "repo"
    (root / "app" / "api").mkdir(parents=True)
    (root / "app" / "api" / "requirements.txt").write_text(
        "fastapi==0.139.2\nuvicorn[standard]==0.51.0\n"
    )
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    return root


def diagnosis_payload(**overrides) -> dict:
    payload = {
        "root_cause": "The api dev requirements pin a version of httpx that "
        "does not exist on PyPI, so `pip install` fails before any test runs.",
        "confidence": "high",
        "confidence_reason": "The resolver names the exact package and version.",
        "fix_available": True,
        "summary": "pin httpx to a version that exists",
        "commit_message": "fix(api): pin httpx to 0.28.1",
        "diff": (
            "--- a/app/api/requirements.txt\n"
            "+++ b/app/api/requirements.txt\n"
            "@@ -1,2 +1,2 @@\n"
            "-fastapi==0.139.2\n"
            "+fastapi==0.139.3\n"
            " uvicorn[standard]==0.51.0\n"
        ),
        "files_changed": ["app/api/requirements.txt"],
        "not_changed": [
            {"considered": "unpinning the version", "why": "CLAUDE.md rule 2"}
        ],
        "verification": "Re-run the pytest (api) job.",
    }
    payload.update(overrides)
    return payload
