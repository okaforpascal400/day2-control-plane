"""GitHub helper: scope-gated, guardrail-bounded, audited — and never merges."""

from __future__ import annotations

import json
import re

import pytest

from day2_agents.github import GitHubError, GitHubHelper, GitHubRefused
from day2_agents.guardrails import GuardrailViolation
from day2_agents.scopes import Action, PermissionDenied, PermissionSet
from tests.conftest import fail, ok


def helper(scopes, audit, runner, repo="okaforpascal400/day2-control-plane"):
    return GitHubHelper(scopes, audit, repo=repo, runner=runner)


def on_branch(scopes, audit, runner, ref="triage/42-x"):
    """A helper whose checkout is already on `ref`, with a clean index."""
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    runner.results["rev-parse HEAD"] = ok("deadbeef1234\n")
    runner.results["diff --cached"] = ok("")
    return helper(scopes, audit, runner)


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
        gh.commit_paths("triage/42-x", "fix: something", ["app/api/requirements.txt"])
    assert not runner.ran("commit")


def test_commit_uses_the_bot_identity(audit, runner):
    """Triage's identity is unchanged by the Phase 5 rework — byte for byte."""
    triage = PermissionSet.declare("triage", list(Action))
    gh = on_branch(triage, audit, runner)
    sha = gh.commit_paths("triage/42-x", "fix: pin the dep", ["app/api/x.txt"])
    assert sha == "deadbeef1234"
    assert runner.ran("user.name=day2-triage-agent[bot]")
    assert runner.ran("user.email=triage-agent@users.noreply.github.com")


def test_each_agent_signs_its_own_commits(audit, runner):
    """A CVE fix must not appear in git history authored by the triage agent."""
    cve = PermissionSet.declare("cve-response", list(Action))
    gh = on_branch(cve, audit, runner, ref="agent/cve-2026-14456")
    gh.commit_paths("agent/cve-2026-14456", "fix: bump libssl3", ["app/web/Dockerfile"])
    assert runner.ran("user.name=day2-cve-response-agent[bot]")
    assert not runner.ran("day2-triage-agent[bot]")


@pytest.mark.parametrize("agent", ["Triage", "a/b", "x y", "--upload-pack=evil", ""])
def test_an_unsafe_agent_name_cannot_reach_the_git_identity(agent, audit, runner):
    """The name is interpolated into `git -c user.name=`; keep it boring."""
    scopes = PermissionSet.declare("placeholder", list(Action))
    object.__setattr__(scopes, "agent", agent)
    gh = helper(scopes, audit, runner)
    with pytest.raises(GuardrailViolation, match="refusing to author"):
        gh.create_branch("agent/x", "abc1234")
    assert runner.calls == []


# ------------------------------------------------- the commit touches only the fix


def test_commit_stages_only_the_paths_the_diff_touched(full_scopes, audit, runner):
    """Never `git add -A`: the working tree is not the proposal."""
    gh = on_branch(full_scopes, audit, runner)
    gh.commit_paths("triage/42-x", "fix: pin the dep", ["app/api/requirements.txt"])

    assert not runner.ran("add", "-A")
    assert runner.ran("add", "--", "app/api/requirements.txt")
    assert runner.ran("commit", "-m", "--", "app/api/requirements.txt")


def test_commit_refuses_when_something_else_reached_the_index(full_scopes, audit, runner):
    """The audit log landing in the workspace is exactly this case."""
    gh = on_branch(full_scopes, audit, runner)
    runner.results["diff --cached"] = ok("app/api/requirements.txt\ntriage-audit.jsonl\n")
    with pytest.raises(GuardrailViolation, match=re.escape("triage-audit.jsonl")):
        gh.commit_paths("triage/42-x", "fix: pin the dep", ["app/api/requirements.txt"])
    assert not runner.ran("commit", "-m")


def test_commit_refuses_an_empty_pathspec(full_scopes, audit, runner):
    gh = on_branch(full_scopes, audit, runner)
    with pytest.raises(GuardrailViolation, match="empty pathspec"):
        gh.commit_paths("triage/42-x", "fix: nothing", [])
    assert runner.calls == []


def test_commit_re_checks_the_paths_against_the_guardrail(full_scopes, audit, runner):
    """A path that could not be diffed cannot be committed by another route."""
    gh = on_branch(full_scopes, audit, runner)
    with pytest.raises(GuardrailViolation, match=re.escape(".github/")):
        gh.commit_paths("triage/42-x", "fix: ci", [".github/workflows/ci.yml"])
    assert not runner.ran("commit")


def test_commit_records_the_paths_in_the_trail(full_scopes, audit, runner):
    gh = on_branch(full_scopes, audit, runner)
    gh.commit_paths("triage/42-x", "fix: pin the dep", ["app/api/requirements.txt"])
    entry = audit.entries[0].to_dict()
    assert entry["action"] == "commit"
    assert entry["metadata"]["paths"] == ["app/api/requirements.txt"]


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


# ------------------------------------------------------------ the log transports


def test_the_job_log_comes_from_the_api_when_that_works(full_scopes, audit, runner):
    runner.results["jobs/7/logs"] = ok("2026-01-01T00:00:00.0Z ##[error]boom\n")
    gh = helper(full_scopes, audit, runner)
    assert "boom" in gh.get_job_log(7)
    assert not runner.ran("run", "view")
    assert audit.entries == []


def test_the_job_log_falls_back_to_a_second_transport(full_scopes, audit, runner):
    """The API endpoint 302s to a blob store, and that redirect can fail alone."""
    runner.results["jobs/7/logs"] = fail("redirect not followed", code=1)
    runner.results["run view"] = ok("job\tstep\t2026-01-01T00:00:00.0Z ##[error]boom\n")
    gh = helper(full_scopes, audit, runner)

    assert "boom" in gh.get_job_log(7)
    assert runner.ran("run", "view", "--job", "7", "--log")
    entry = audit.entries[0].to_dict()
    assert entry["action"] == "read_job_log"
    assert "run-view" in entry["decision_summary"]


def test_an_empty_body_with_a_zero_exit_is_not_treated_as_a_log(
    full_scopes, audit, runner
):
    """The defect that shipped: no bytes, no error, no trace of either."""
    runner.results["jobs/7/logs"] = ok("")
    runner.results["run view"] = ok("2026-01-01T00:00:00.0Z ##[error]boom\n")
    gh = helper(full_scopes, audit, runner)
    assert "boom" in gh.get_job_log(7)


def test_a_log_no_transport_can_reach_is_audited_not_swallowed(
    full_scopes, audit, runner
):
    runner.results["jobs/7/logs"] = fail("gone", code=1)
    runner.results["run view"] = fail("expired", code=1)
    gh = helper(full_scopes, audit, runner)

    assert gh.get_job_log(7) == ""
    entry = audit.entries[0].to_dict()
    assert entry["action"] == "log_unavailable"
    assert "gone" in entry["decision_summary"]
    assert "expired" in entry["decision_summary"]


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


# ------------------------------------------------- Phase 5: PRs and issues
#
# Three capabilities were added for the CVE Response and Upgrade agents. Each
# is either a read or the weakest write GitHub offers, and the tests below are
# mostly about what they still cannot do.


def upgrade_scopes():
    """The Upgrade agent's real declaration: read a PR, comment on it, nothing else."""
    return PermissionSet.declare(
        "upgrade", [Action.CALL_MODEL, Action.READ_PR, Action.COMMENT_ON_PR]
    )


def test_pr_reads_require_the_read_pr_scope(audit, runner):
    gh = helper(PermissionSet.declare("upgrade", [Action.CALL_MODEL]), audit, runner)
    for call in (
        lambda: gh.get_pull_request(19),
        lambda: gh.get_pull_request_diff(19),
        lambda: gh.list_issues(),
    ):
        with pytest.raises(PermissionDenied, match="read_pr"):
            call()
    assert runner.calls == []


def test_get_pull_request_parses_the_api_payload(full_scopes, audit, runner):
    runner.results["pulls/19"] = ok(
        json.dumps({"number": 19, "user": {"login": "renovate[bot]"}})
    )
    gh = helper(full_scopes, audit, runner)
    assert gh.get_pull_request(19)["user"]["login"] == "renovate[bot]"


def test_the_pr_diff_is_asked_for_by_accept_header_not_a_review_endpoint(
    full_scopes, audit, runner
):
    """Same resource, different representation — and never `/pulls/{n}/comments`."""
    runner.results["pulls/19"] = ok("diff --git a/x b/x\n")
    gh = helper(full_scopes, audit, runner)
    assert "diff --git" in gh.get_pull_request_diff(19)
    assert runner.ran("--header", "Accept: application/vnd.github.diff")


def test_list_issues_returns_pull_requests_too(full_scopes, audit, runner):
    """The dedupe question is 'an open PR *or* issue', and this is one call."""
    runner.results["issues?state=open"] = ok(
        json.dumps(
            [
                {"number": 20, "title": "CVE-2026-14456", "pull_request": {"url": "u"}},
                {"number": 21, "title": "something else"},
            ]
        )
    )
    gh = helper(full_scopes, audit, runner)
    items = gh.list_issues()
    assert [i["number"] for i in items] == [20, 21]
    assert "pull_request" in items[0]


def test_list_issues_refuses_an_unknown_state(full_scopes, audit, runner):
    gh = helper(full_scopes, audit, runner)
    with pytest.raises(GitHubRefused, match="unknown issue state"):
        gh.list_issues("everything")
    assert runner.calls == []


def test_reads_leave_no_audit_entry(full_scopes, audit, runner):
    """The trail records what an agent *did*, and reading is not doing."""
    runner.results["pulls/19"] = ok(json.dumps({"number": 19}))
    runner.results["issues?state=open"] = ok("[]")
    gh = helper(full_scopes, audit, runner)
    gh.get_pull_request(19)
    gh.list_issues()
    assert audit.entries == []


def test_create_issue_requires_its_own_scope(audit, runner):
    gh = helper(upgrade_scopes(), audit, runner)
    with pytest.raises(PermissionDenied, match="open_issue"):
        gh.create_issue("CVE-2026-1", "body")
    assert runner.calls == []
    assert audit.entries == []


def test_create_issue_posts_json_and_audits_the_url(full_scopes, audit, runner):
    runner.results["issues"] = ok(json.dumps({"html_url": "https://x/issues/22"}))
    gh = helper(full_scopes, audit, runner)
    url = gh.create_issue("CVE-2026-14456 in libssl3", "## Diagnosis", ["cve"])
    assert url == "https://x/issues/22"
    assert json.loads(runner.stdins[0]) == {
        "title": "CVE-2026-14456 in libssl3",
        "body": "## Diagnosis",
        "labels": ["cve"],
    }
    entry = audit.entries[0].to_dict()
    assert entry["action"] == "open_issue"
    assert entry["target"] == "https://x/issues/22"
    assert entry["approved_by"] is None


def test_commenting_on_a_pr_requires_its_own_scope(audit, runner):
    run_only = PermissionSet.declare("triage", [Action.COMMENT_ON_RUN])
    gh = helper(run_only, audit, runner)
    with pytest.raises(PermissionDenied, match="comment_on_pr"):
        gh.comment_on_pull_request(19, "risk: low")
    assert runner.calls == []


def test_a_pr_comment_is_a_timeline_comment_not_a_review(full_scopes, audit, runner):
    """`/pulls/{n}/comments` would post a review comment. Reviewing is a human's job."""
    runner.results["comments"] = ok(json.dumps({"html_url": "https://x/pull/19#c1"}))
    gh = helper(full_scopes, audit, runner)
    assert gh.comment_on_pull_request(19, "risk: low") == "https://x/pull/19#c1"

    posted = [c for c in runner.calls if "POST" in c]
    assert any("issues/19/comments" in " ".join(c) for c in posted)
    assert not any("pulls/19/comments" in " ".join(c) for c in posted)
    assert json.loads(runner.stdins[0]) == {"body": "risk: low"}
    assert audit.entries[0].to_dict()["action"] == "comment_on_pr"


def test_the_upgrade_agent_shape_can_read_and_comment_and_nothing_else(audit, runner):
    """The whole point of the Upgrade agent: it cannot change the repository."""
    gh = helper(upgrade_scopes(), audit, runner)
    for call in (
        lambda: gh.create_branch("agent/x", "abc1234"),
        lambda: gh.push("agent/x"),
        lambda: gh.open_pull_request("agent/x", "main", "t", "b"),
        lambda: gh.create_issue("t", "b"),
        lambda: gh.comment_on_commit("abc1234", "b"),
        lambda: gh.get_run(42),
    ):
        with pytest.raises(PermissionDenied):
            call()
    with pytest.raises(GuardrailViolation):
        gh.merge_pull_request(19)
    assert runner.calls == []
    assert audit.entries == []
