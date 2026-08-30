"""The upgrade agent: it reads, it comments, and that is the whole of it."""

from __future__ import annotations

import pytest
from upgrade import agent as up

from day2_agents.claude import ClaudeClient, ModelError
from day2_agents.github import GitHubHelper
from day2_agents.guardrails import GuardrailViolation
from day2_agents.scopes import Action, PermissionDenied, PermissionSet
from tests.conftest import PR, PR_URL, REPO, FakeClaude, annotation_payload, gh_runner


def annotate(scopes, audit, runner, payload, repo_root, simulate=False):
    claude = ClaudeClient(scopes, audit, client=FakeClaude(payload))
    gh = GitHubHelper(scopes, audit, repo=REPO, repo_root=str(repo_root), runner=runner)
    code = up.annotate_pull_request(
        gh, claude, audit, scopes, PR, REPO, str(repo_root), "https://audit", simulate
    )
    return code, claude


@pytest.fixture
def repo_root(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\n")
    return tmp_path


# ------------------------------------------------------------- the declaration


def test_the_agent_cannot_change_the_repository():
    """The central claim of this agent, asserted against the scope set itself."""
    scopes = PermissionSet.declare(up.AGENT, up.SCOPES)
    assert scopes.as_list() == ["call_model", "comment_on_pr", "read_pr"]
    for denied in (
        Action.CREATE_BRANCH,
        Action.PUSH_COMMIT,
        Action.OPEN_PR,
        Action.OPEN_ISSUE,
        Action.COMMENT_ON_RUN,
        Action.READ_CI_RUN,
    ):
        with pytest.raises(PermissionDenied):
            scopes.require(denied)


def test_the_helper_refuses_every_write_this_agent_might_reach_for(audit, tmp_path):
    scopes = PermissionSet.declare(up.AGENT, up.SCOPES)
    gh = GitHubHelper(scopes, audit, repo=REPO, repo_root=str(tmp_path))
    for call in (
        lambda: gh.create_branch("agent/x", "abc1234"),
        lambda: gh.push("agent/x"),
        lambda: gh.open_pull_request("agent/x", "main", "t", "b"),
        lambda: gh.create_issue("t", "b"),
    ):
        with pytest.raises(PermissionDenied):
            call()
    with pytest.raises(GuardrailViolation):
        gh.merge_pull_request(23)


# ------------------------------------------------------------------- the skips


def test_a_human_pr_is_not_annotated(scopes, audit, repo_root):
    runner = gh_runner(author="okaforpascal400")
    code, claude = annotate(scopes, audit, runner, annotation_payload(), repo_root)

    assert code == 0
    assert claude.call_count == 0
    assert not runner.ran("comments")
    skip = next(e for e in audit.entries if e.action == "skip")
    assert "dependency bots only" in skip.decision_summary


@pytest.mark.parametrize("head_ref", ["agent/cve-2026-14456", "triage/123-x"])
def test_agents_do_not_annotate_agents(scopes, audit, repo_root, head_ref):
    """Two models talking to each other at a reviewer's expense."""
    runner = gh_runner(head_ref=head_ref)
    code, claude = annotate(scopes, audit, runner, annotation_payload(), repo_root)

    assert code == 0
    assert claude.call_count == 0
    skip = next(e for e in audit.entries if e.action == "skip")
    assert "agents do not annotate agents" in skip.decision_summary


def test_the_agent_branch_check_runs_before_the_author_check(scopes, audit, repo_root):
    """An agent branch is refused even when the author looks like a bot."""
    runner = gh_runner(author="renovate[bot]", head_ref="agent/cve-2026-1")
    _, claude = annotate(scopes, audit, runner, annotation_payload(), repo_root)
    assert claude.call_count == 0


def test_a_pr_that_moves_no_pin_costs_nothing(scopes, audit, repo_root):
    runner = gh_runner(diff="--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n")
    code, claude = annotate(scopes, audit, runner, annotation_payload(), repo_root)

    assert code == 0
    assert claude.call_count == 0
    skip = next(e for e in audit.entries if e.action == "skip")
    assert "no dependency pin moved" in skip.decision_summary


def test_simulation_annotates_a_human_pr_and_says_so_loudly(scopes, audit, repo_root):
    """The seeded-verification bypass must be visible in the trail, not inferred."""
    runner = gh_runner(author="okaforpascal400")
    code, claude = annotate(
        scopes, audit, runner, annotation_payload(), repo_root, simulate=True
    )

    assert code == 0
    assert claude.call_count == 1
    sim = next(e for e in audit.entries if e.action == "simulate")
    assert sim.metadata["simulated"] is True
    assert "not a production annotation" in sim.decision_summary


# ------------------------------------------------------------------ the output


def test_a_renovate_pr_gets_one_comment_and_no_writes(scopes, audit, repo_root):
    runner = gh_runner()
    code, claude = annotate(scopes, audit, runner, annotation_payload(), repo_root)

    assert code == 0
    assert claude.call_count == 1
    # The comment goes to the timeline endpoint, never the review endpoint.
    assert runner.ran(f"issues/{PR}/comments")
    assert not runner.ran(f"pulls/{PR}/comments")
    assert not runner.ran("pr", "create")
    assert not runner.ran("push")
    assert all(e.approved_by is None for e in audit.entries)


def test_the_comment_carries_the_risk_the_action_and_the_disclaimer(
    scopes, audit, repo_root
):
    runner = gh_runner()
    annotate(scopes, audit, runner, annotation_payload(), repo_root)
    body = runner.comment_body()

    assert "🔴 HIGH" in body
    assert "Recommendation: `review`" in body
    assert "app/api/api/deps.py" in body
    assert "Depends no longer caches by default" in body
    assert up.COMMENT_MARKER in body
    # The scope list is printed, so a reader can check the claim themselves.
    assert "call_model" in body and "comment_on_pr" in body


def test_a_missing_release_notes_section_is_declared_in_the_comment(
    scopes, audit, repo_root
):
    """Silence about missing evidence would read as evidence of safety."""
    runner = gh_runner(body="Just a bump.")
    annotate(scopes, audit, runner, annotation_payload(risk="unknown"), repo_root)
    body = runner.comment_body()
    assert "carried no release-notes section" in body


def test_an_unused_dependency_is_stated_as_such(scopes, audit, tmp_path):
    runner = gh_runner()
    annotate(scopes, audit, runner, annotation_payload(risk="low"), tmp_path)
    assert "No file in this repository references the dependency" in runner.comment_body()


# ---------------------------------------------------------------- the contract


def test_an_invented_risk_level_is_refused():
    with pytest.raises(ModelError, match="risk must be one of"):
        up.validate_annotation(annotation_payload(risk="catastrophic"))


def test_an_invented_recommendation_is_refused():
    with pytest.raises(ModelError, match="recommendation must be one of"):
        up.validate_annotation(annotation_payload(recommendation="think about it"))


def test_the_reason_and_the_action_are_both_required():
    for field in ("risk_reason", "recommended_action", "upstream_changes", "our_usage"):
        with pytest.raises(ModelError, match=field):
            up.validate_annotation(annotation_payload(**{field: "  "}))


def test_empty_lists_are_a_real_answer():
    """Better than an invented breaking change."""
    annotation = up.validate_annotation(
        annotation_payload(affected_paths=[], breaking_changes=[])
    )
    assert annotation.affected_paths == []
    body = up.build_comment_body(
        annotation,
        "table",
        "https://audit",
        0.05,
        PermissionSet.declare(up.AGENT, up.SCOPES),
        had_release_notes=True,
        usage_hits=2,
    )
    assert "None identified" in body


def test_a_non_list_of_paths_is_refused():
    with pytest.raises(ModelError, match="affected_paths"):
        up.validate_annotation(annotation_payload(affected_paths="app/api/deps.py"))


def test_every_risk_level_has_a_badge():
    """A missing badge raises a KeyError while rendering — after the call is paid for."""
    for level in up.VALID_RISK:
        assert level in up.RISK_BADGE


def test_the_pr_url_is_what_the_trail_records(scopes, audit, repo_root):
    runner = gh_runner()
    annotate(scopes, audit, runner, annotation_payload(), repo_root)
    finish = next(e for e in audit.entries if e.action == "finish")
    assert PR_URL in finish.target
    assert finish.metadata["risk"] == "high"
