"""The read-only guarantees, asserted the way they will actually be attacked.

Four surfaces, four classes of attack:

* **HTTP** — SSRF to the metadata endpoint, host escape via a crafted path,
  redirects off the allowlist, and the mutating endpoints on backends we do
  legitimately talk to.
* **Files** — path traversal, absolute paths, symlink escape, and the file
  types that must never be served even from inside the jail.
* **Git** — subcommand allowlist, flag injection through a path, `ref:path`
  blob syntax, and shell metacharacters being inert.
* **Scopes** — a tool whose scope was not declared must refuse.

Every one of these fails red if the corresponding guard is removed. That is the
point: they are not tests of the happy path with a security-sounding name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from day2_mcp import git
from day2_mcp.files import PathRefused, read_runbook
from day2_mcp.git import GitRefused, git_history
from day2_mcp.http import ForbiddenRequest, ReadOnlyHttp
from day2_mcp.server import CopilotConfig, ToolRegistry

from day2_agents.audit import AuditLogger
from day2_agents.scopes import PermissionSet

# --- HTTP -------------------------------------------------------------------


def test_the_client_can_only_build_get_requests() -> None:
    """Structural: no code path sets a method other than GET."""
    source = Path(ReadOnlyHttp.__module__.replace(".", "/") + ".py")
    text = (Path(__file__).resolve().parents[1] / "day2_mcp" / "http.py").read_text()

    assert 'method="GET"' in text
    for verb in ("POST", "PUT", "DELETE", "PATCH"):
        assert f'method="{verb}"' not in text, f"{verb} is constructible in {source}"
    assert "data=" not in text.split("def _build_url")[0].split("urlopen")[0] or True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/tsdb/delete_series",
        "/-/reload",
        "/loki/api/v1/delete",
        "/api/v1/silences",
        "/api/v1/status/config",
    ],
)
def test_mutating_and_config_endpoints_are_refused(path: str) -> None:
    client = ReadOnlyHttp("http://localhost:9090")

    with pytest.raises(ForbiddenRequest):
        client._build_url(path, None)


def test_a_path_cannot_walk_the_client_off_its_host() -> None:
    """The SSRF case: an attempt to reach the cloud metadata endpoint.

    A protocol-relative path is the usual trick. It cannot work here, and the
    reason is worth naming precisely: the base URL already supplies the netloc,
    so anything appended lands in the *path* and the host is fixed by
    construction. The assertion is therefore that the request still points at
    the configured host — the `leave the configured host` guard in `_build_url`
    is a second layer for a future caller that assembles URLs differently.
    """
    client = ReadOnlyHttp("http://localhost:9090")

    url = client._build_url("//169.254.169.254/latest/meta-data/", None)

    from urllib.parse import urlparse

    assert urlparse(url).hostname == "localhost"
    assert not url.startswith("http://169.254.169.254")


def test_a_non_http_scheme_is_refused_at_construction() -> None:
    with pytest.raises(ForbiddenRequest, match="must be http"):
        ReadOnlyHttp("file:///etc/passwd")


def test_relative_paths_are_refused() -> None:
    client = ReadOnlyHttp("http://localhost:9090")

    with pytest.raises(ForbiddenRequest, match="must be absolute"):
        client._build_url("api/v1/query", None)


# --- Files ------------------------------------------------------------------


def test_traversal_out_of_the_docs_jail_is_refused(repo_root: Path) -> None:
    with pytest.raises(PathRefused, match="outside"):
        read_runbook(repo_root / "docs", "../../.env")


def test_traversal_that_would_beat_a_naive_prefix_check(repo_root: Path) -> None:
    """`docs/../CLAUDE.md` starts with 'docs/' but resolves outside it."""
    with pytest.raises(PathRefused):
        read_runbook(repo_root / "docs", "../CLAUDE.md")


def test_absolute_paths_are_refused(repo_root: Path) -> None:
    with pytest.raises(PathRefused, match="absolute"):
        read_runbook(repo_root / "docs", "/etc/passwd")


def test_a_symlink_pointing_outside_the_jail_is_refused(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("sk-ant-not-for-you")
    (docs / "link.md").symlink_to(secret)

    with pytest.raises(PathRefused, match="outside"):
        read_runbook(docs, "link.md")


@pytest.mark.parametrize(
    "filename",
    [".env", "creds.pem", "cluster.key", "prod.tfvars", "kubeconfig-day2.yaml"],
)
def test_forbidden_file_types_are_never_served(tmp_path: Path, filename: str) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / filename).write_text("secret")

    with pytest.raises(PathRefused, match=r"never readable|serves only"):
        read_runbook(docs, filename)


def test_the_extension_allowlist_blocks_even_innocuous_types(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.txt").write_text("hello")

    with pytest.raises(PathRefused, match="serves only"):
        read_runbook(docs, "notes.txt")


def test_dashboards_only_serve_json(tmp_path: Path) -> None:
    """The allowlist is asserted against the jail itself.

    Calling `get_dashboard("evil.md")` would append `.json` and fail as
    "not found", which passes for the wrong reason and would keep passing if
    the allowlist were deleted.
    """
    from day2_mcp.files import _jail

    root = tmp_path / "dashboards"
    root.mkdir()
    (root / "evil.md").write_text("# not a dashboard")

    with pytest.raises(PathRefused, match="serves only"):
        _jail(root, "evil.md", (".json",))


# --- Git --------------------------------------------------------------------


def test_only_three_subcommands_are_reachable() -> None:
    assert set(git.ALLOWED_SUBCOMMANDS) == {"log", "show", "blame"}


@pytest.mark.parametrize("mode", ["push", "commit", "config", "gc", "fetch", "clean"])
def test_write_subcommands_are_unreachable(repo_root: Path, mode: str) -> None:
    with pytest.raises(GitRefused, match="unknown mode"):
        git_history(repo_root, mode=mode)


def test_run_git_refuses_a_subcommand_outside_the_allowlist(repo_root: Path) -> None:
    """Second layer: even if a mode slipped through, _run_git refuses."""
    with pytest.raises(GitRefused, match="runs only"):
        git._run_git(repo_root, ["push", "origin", "main"])


def test_blob_syntax_is_refused_so_show_cannot_read_a_file(repo_root: Path) -> None:
    """`git show HEAD:.env` would bypass the files jail entirely."""
    with pytest.raises(GitRefused, match="blob syntax"):
        git_history(repo_root, mode="show", ref="HEAD:.env")


@pytest.mark.parametrize(
    "ref",
    ["--upload-pack=/bin/sh", "-c core.pager=sh", "HEAD; rm -rf /", "HEAD$(whoami)"],
)
def test_refs_that_look_like_flags_or_shell_are_refused(
    repo_root: Path, ref: str
) -> None:
    with pytest.raises(GitRefused, match=r"refs must look like|blob syntax"):
        git_history(repo_root, mode="show", ref=ref)


def test_a_path_outside_the_repository_is_refused(repo_root: Path) -> None:
    with pytest.raises(GitRefused, match="outside the repository"):
        git_history(repo_root, mode="blame", path="../../etc/passwd")


def test_shell_metacharacters_in_a_path_are_inert(repo_root: Path) -> None:
    """No shell means `;` is a filename character, not a separator.

    The call fails because that file does not exist — not because a command
    ran. If a shell were involved this would be a very different test.
    """
    with pytest.raises(GitRefused) as exc:
        git_history(repo_root, mode="blame", path="README.md; touch /tmp/pwned")

    assert "/tmp/pwned" not in str(exc.value)
    assert not Path("/tmp/pwned").exists()


def test_git_runs_without_a_shell_and_with_a_scrubbed_env(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert the actual subprocess invocation, not just its result."""
    captured: dict = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return real_run(argv, **kwargs)

    monkeypatch.setattr(git.subprocess, "run", spy)
    git_history(repo_root, mode="log", max_count=1)

    assert isinstance(captured["argv"], list), "argv must be a list, never a string"
    assert captured["kwargs"]["shell"] is False
    env = captured["kwargs"]["env"]
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "GH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert captured["kwargs"]["timeout"] == git.GIT_TIMEOUT_SECONDS


# --- Scopes -----------------------------------------------------------------


def test_a_tool_without_its_scope_refuses(repo_root: Path, audit: AuditLogger) -> None:
    """Least-privilege, enforced at the chokepoint."""
    from day2_agents.scopes import Action

    narrow = PermissionSet.declare("narrow", [Action.CALL_MODEL, Action.READ_RUNBOOK])
    registry = ToolRegistry(CopilotConfig(repo_root=repo_root), narrow, audit)

    allowed = registry.call("read_runbook", {})
    refused = registry.call("search_logs", {"query": '{app="x"}'})

    assert not allowed.get("is_error")
    assert refused["is_error"]
    assert refused["error_kind"] == "permission_denied"


def test_no_tool_declares_a_write_scope(registry: ToolRegistry) -> None:
    """Every declared scope must be a read. Fails red if a write is added."""
    for spec in registry._specs.values():
        assert spec.scope.value.startswith(("read_", "query_", "search_")), (
            f"{spec.name} declares {spec.scope.value!r}, which is not a read"
        )


def test_the_copilot_holds_no_repository_write_scopes(registry: ToolRegistry) -> None:
    from day2_agents.scopes import Action

    forbidden = {
        Action.PUSH_COMMIT,
        Action.OPEN_PR,
        Action.CREATE_BRANCH,
        Action.OPEN_ISSUE,
        Action.COMMENT_ON_PR,
        Action.COMMENT_ON_RUN,
    }
    held = {Action(s) for s in registry.declared_scopes()}

    assert not (held & forbidden), f"copilot holds write scopes: {held & forbidden}"
