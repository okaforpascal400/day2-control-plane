"""GitHub helper: scope-gated, guardrail-bounded, audited — and never merges."""

from __future__ import annotations

import json

import pytest

from day2_agents.github import GitHubError, GitHubHelper, GitHubRefused
from day2_agents.guardrails import GuardrailViolation
from day2_agents.scopes import Action, PermissionDenied, PermissionSet
from tests.conftest import fail, ok


def helper(scopes, audit, runner, repo="okaforpascal400/day2-control-plane"):
    return GitHubHelper(scopes, audit, repo=repo, runner=runner)


# --------------------------------------------------------------- the refusals


def test_merge_is_refused_unconditionally(full_scopes, audit, runner):
    """No scope enables it and no argument changes it."""
    gh = helper(full_scopes, audit, runner)
    with pytest.raises(GuardrailViolation, match="humans approve"):
        gh.merge_pull_request(7)
    with pytest.raises(GuardrailViolation):
        gh.merge_pull_request(pr=7, method="squash", force=True)
    assert runner.calls == []


def test_auto_merge_and_approve_are_refused_too(full_scopes, audit, runner):
    gh = helper(full_scopes, audit, runner)
    for method in (gh.enable_auto_merge, gh.approve_pull_request):
        with pytest.raises(GuardrailViolation):
            method(7)


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "pr", "merge", "7"],
        ["gh", "pr", "merge", "--squash", "7"],
        ["gh", "pr", "create", "--auto"],
        ["gh", "pr", "merge", "--admin", "7"],
        ["gh", "pr", "review", "--approve", "7"],
        ["gh", "pr", "review", "7", "--approve"],
        ["git", "push", "--force", "origin", "triage/1"],
        ["git", "push", "-f", "origin", "triage/1"],
    ],
)
def test_forbidden_commands_are_refused_before_they_run(full_scopes, audit, runner, argv):
    """Catches a future caller that hand-builds an argv past the typed methods."""
    gh = helper(full_scopes, audit, runner)
    with pytest.raises(GitHubRefused):
        gh._run(argv)
    assert runner.calls == []


@pytest.mark.parametrize("ref", ["main", "master", "phase4/x", "triage", "../x"])
def test_writes_to_non_triage_refs_are_refused(full_scopes, audit, runner, ref):
    gh = helper(full_scopes, audit, runner)
    with pytest.raises(GuardrailViolation):
        gh.create_branch(ref, "abc1234")
    with pytest.raises(GuardrailViolation):
        gh.push(ref)
    assert runner.calls == []


def test_refused_actions_leave_no_audit_entry(full_scopes, audit, runner):
    """The trail records what happened, not what was attempted and blocked."""
    gh = helper(full_scopes, audit, runner)
    with pytest.raises(GuardrailViolation):
        gh.create_branch("main", "abc1234")
    assert audit.entries == []


# ----------------------------------------------------------------- the scopes


def test_each_write_requires_its_own_scope(audit, runner):
    read_only = PermissionSet.declare("triage", [Action.READ_CI_RUN])
    gh = helper(read_only, audit, runner)
    with pytest.raises(PermissionDenied, match="create_branch"):
        gh.create_branch("triage/1-x", "abc1234")
    with pytest.raises(PermissionDenied, match="push_commit"):
        gh.push("triage/1-x")
    with pytest.raises(PermissionDenied, match="open_pr"):
        gh.open_pull_request("triage/1-x", "main", "t", "b")
    with pytest.raises(PermissionDenied, match="comment_on_run"):
        gh.comment_on_commit("abc1234", "hi")


def test_reads_require_the_read_scope(audit, runner):
    write_only = PermissionSet.declare("triage", [Action.OPEN_PR])
    gh = helper(write_only, audit, runner)
    with pytest.raises(PermissionDenied, match="read_ci_run"):
        gh.get_run(42)


# ---------------------------------------------------------------- the actions


def test_create_branch_branches_from_the_failing_sha_and_audits(
    full_scopes, audit, runner
):
    gh = helper(full_scopes, audit, runner)
    gh.create_branch("triage/42-bad-dep", "abc1234def567")
    assert runner.ran("checkout", "-b", "triage/42-bad-dep", "abc1234def567")
    entry = audit.entries[0].to_dict()
    assert entry["action"] == "create_branch"
    assert entry["target"].endswith("@triage/42-bad-dep")
    assert entry["approved_by"] is None


def test_commit_refuses_when_the_checkout_is_not_the_triage_branch(
    full_scopes, audit, runner
):
    runner.results["rev-parse --abbrev-ref"] = ok("main\n")
    gh = helper(full_scopes, audit, runner)
    with pytest.raises(GuardrailViolation, match="expected"):
        gh.commit_all("triage/42-x", "fix: something")
    assert not runner.ran("commit")


def test_commit_uses_the_bot_identity(full_scopes, audit, runner):
    runner.results["rev-parse --abbrev-ref"] = ok("triage/42-x\n")
    runner.results["rev-parse HEAD"] = ok("deadbeef1234\n")
    gh = helper(full_scopes, audit, runner)
    assert gh.commit_all("triage/42-x", "fix: pin the dep") == "deadbeef1234"
    assert runner.ran("user.name=day2-triage-agent[bot]")


def test_push_targets_only_the_triage_ref(full_scopes, audit, runner):
    gh = helper(full_scopes, audit, runner)
    gh.push("triage/42-x")
    assert runner.ran("push", "origin", "triage/42-x:triage/42-x")
    assert audit.entries[0].to_dict()["action"] == "push_branch"


def test_open_pr_passes_the_body_on_stdin_and_returns_the_url(full_scopes, audit, runner):
    url = "https://github.com/okaforpascal400/day2-control-plane/pull/9"
    runner.results["pr create"] = ok(url + "\n")
    gh = helper(full_scopes, audit, runner)
    body = "## Diagnosis\n\nBackticks ``` and $VARS survive stdin unharmed."
    assert gh.open_pull_request("triage/42-x", "main", "[triage] x", body) == url
    assert runner.stdins[0] == body
    assert audit.entries[0].to_dict()["target"] == url


def test_comment_on_commit_posts_json_and_audits_the_url(full_scopes, audit, runner):
    runner.results["commits"] = ok(json.dumps({"html_url": "https://x/comment/1"}))
    gh = helper(full_scopes, audit, runner)
    assert gh.comment_on_commit("abc1234", "see PR #9") == "https://x/comment/1"
    assert json.loads(runner.stdins[0]) == {"body": "see PR #9"}
    assert audit.entries[0].to_dict()["action"] == "comment_on_run"


def test_get_run_jobs_parses_the_api_payload(full_scopes, audit, runner):
    runner.results["actions/runs"] = ok(
        json.dumps({"jobs": [{"name": "pytest (api)", "conclusion": "failure"}]})
    )
    gh = helper(full_scopes, audit, runner)
    jobs = gh.get_run_jobs(42)
    assert jobs[0]["conclusion"] == "failure"


def test_an_expired_log_returns_empty_rather_than_raising(full_scopes, audit, runner):
    runner.results["jobs/7/logs"] = fail("gone", code=1)
    gh = helper(full_scopes, audit, runner)
    assert gh.get_job_log(7) == ""


def test_a_failed_command_raises_with_the_stderr(full_scopes, audit, runner):
    runner.results["pr create"] = fail("pull request already exists")
    gh = helper(full_scopes, audit, runner)
    with pytest.raises(GitHubError, match="already exists"):
        gh.open_pull_request("triage/42-x", "main", "t", "b")


def test_no_command_is_ever_built_as_a_shell_string(full_scopes, audit, runner):
    """Fixed argv, shell=False — nothing the model produced reaches a shell."""
    runner.results["pr create"] = ok("url\n")
    gh = helper(full_scopes, audit, runner)
    gh.open_pull_request("triage/42-x", "main", "$(rm -rf /)", "; drop table")
    for call in runner.calls:
        assert isinstance(call, list)
        assert all(isinstance(arg, str) for arg in call)
