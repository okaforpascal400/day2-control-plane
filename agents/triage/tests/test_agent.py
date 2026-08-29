"""The agent's decisions: what it verifies, what it refuses, what it pushes."""

from __future__ import annotations

import shlex
import subprocess

import pytest

from day2_agents.claude import ClaudeClient, ModelError
from day2_agents.github import GitHubHelper
from day2_agents.guardrails import GuardrailViolation
from tests.conftest import (
    REPO,
    RUN_ID,
    SHA,
    FakeClaude,
    diagnosis_payload,
    gh_runner,
    ok,
)
from triage.agent import (
    FIX_CONFIDENCE,
    PR_MARKER,
    SCOPES,
    slugify,
    triage_run,
    validate_diagnosis,
)

BAD_DEP_LOG = (
    "ERROR: Could not find a version that satisfies the requirement "
    "httpx==0.99.99 (from -r requirements.txt (line 3))\n"
    "##[error]Process completed with exit code 1."
)


# ------------------------------------------------- output verification (part 1)


def test_a_well_formed_diagnosis_is_accepted():
    diagnosis = validate_diagnosis(diagnosis_payload())
    assert diagnosis.confidence == "high"
    assert diagnosis.proposes_fix


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"confidence": "very sure"}, "confidence must be one of"),
        ({"confidence": ""}, "required"),
        ({"root_cause": ""}, "required"),
        ({"summary": ""}, "required"),
        ({"fix_available": "yes"}, "must be a boolean"),
        ({"files_changed": "one.py"}, "list of strings"),
        ({"not_changed": []}, "non-empty list"),
        ({"not_changed": [{"why": "x"}]}, "'considered'"),
        ({"diff": ""}, "'diff' is empty"),
        ({"files_changed": []}, "'files_changed' is empty"),
        ({"root_cause": 42}, "must be a string"),
    ],
)
def test_a_malformed_diagnosis_is_rejected(overrides, message):
    with pytest.raises(ModelError, match=message):
        validate_diagnosis(diagnosis_payload(**overrides))


def test_low_confidence_never_proposes_a_fix_even_with_a_diff():
    diagnosis = validate_diagnosis(diagnosis_payload(confidence="low"))
    assert diagnosis.fix_available and diagnosis.diff
    assert not diagnosis.proposes_fix
    assert "low" not in FIX_CONFIDENCE


def test_a_diagnosis_without_a_fix_is_valid():
    diagnosis = validate_diagnosis(
        diagnosis_payload(fix_available=False, diff="", files_changed=[])
    )
    assert not diagnosis.proposes_fix


@pytest.mark.parametrize(
    "text,expected",
    [
        ("pin httpx to 0.28.1", "pin-httpx-to-0-28-1"),
        ("Fix the WORKER env!", "fix-the-worker-env"),
        ("!!!", "diagnosis"),
        ("", "diagnosis"),
    ],
)
def test_slugify_yields_a_branch_safe_suffix(text, expected):
    from day2_agents.guardrails import assert_writable_ref

    slug = slugify(text)
    assert slug == expected
    assert_writable_ref(f"triage/{RUN_ID}-{slug}")


# --------------------------------------------------------------- the fix path


def build(runner, payload, audit, scopes, repo_root):
    gh = GitHubHelper(scopes, audit, repo=REPO, repo_root=str(repo_root), runner=runner)
    claude = ClaudeClient(scopes, audit, client=FakeClaude(payload))
    return gh, claude


def test_a_verified_fix_becomes_a_branch_a_commit_a_push_and_a_pr(
    audit, scopes, git_repo
):
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)

    assert triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a") == 0

    assert runner.ran("checkout", "-b", ref, SHA)
    assert runner.ran("commit", "-m", "fix(api): pin httpx to 0.28.1")
    assert runner.ran("push", "origin", f"{ref}:{ref}")
    assert runner.ran("pr", "create", "--head", ref)
    # The PR targets the branch that failed, not main.
    assert runner.ran("--base", "phase4/scenario-bad-dep")


def test_the_diff_is_actually_applied_to_the_tree(audit, scopes, git_repo):
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")
    assert "fastapi==0.139.3" in (git_repo / "app/api/requirements.txt").read_text()


def test_the_pr_body_carries_the_diagnosis_marker_and_provenance(audit, scopes, git_repo):
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)
    triage_run(
        gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "https://audit/url"
    )

    body = runner.body_for("pr create")
    assert PR_MARKER in body
    assert "**Confidence: high**" in body
    assert "https://audit/url" in body
    assert "unpinning the version" in body  # the "not changed" section
    assert "$0.0900" in body  # 9000 in + 1800 out, priced
    assert "Model cost" in body


def test_the_failing_commit_gets_a_comment_linking_the_pr(audit, scopes, git_repo):
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    comment = runner.comment_body(f"commits/{SHA}/comments")
    assert f"https://github.com/{REPO}/pull/11" in comment
    assert PR_MARKER in comment


# ------------------------------------- the fix path when `gh pr create` fails


def fail(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", stderr)


# The error the platform actually returned on the agent's first live run.
PR_DENIED = (
    "pull request create failed: GraphQL: GitHub Actions is not permitted "
    "to create or approve pull requests (createPullRequest)"
)


def stranded_run(audit, scopes, git_repo, stderr: str = PR_DENIED):
    """A run where everything works until `gh pr create`, which fails."""
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    runner.results["pr create"] = fail(stderr)
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)
    code = triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")
    return runner, ref, code


def test_a_pr_open_failure_still_comments_the_diagnosis(audit, scopes, git_repo):
    """The defect: the diagnosis died with the exception and nothing said so."""
    runner, ref, _ = stranded_run(audit, scopes, git_repo)

    comment = runner.comment_body(f"commits/{SHA}/comments")
    assert "could not be" in comment and "opened" in comment
    # The reviewer gets the branch, the failure, and the diagnosis itself.
    assert ref in comment
    assert "GitHub Actions is not permitted" in comment
    assert "The api dev requirements pin a version of httpx" in comment
    assert "**Confidence: high**" in comment


def test_the_stranded_comment_gives_the_command_that_finishes_the_job(
    audit, scopes, git_repo
):
    runner, ref, _ = stranded_run(audit, scopes, git_repo)
    comment = runner.comment_body(f"commits/{SHA}/comments")

    assert f"gh pr create --head {ref} --base phase4/scenario-bad-dep" in comment
    # Non-interactive: `gh pr create` without --body opens an editor prompt,
    # which is not something a reviewer can paste out of a comment.
    assert "--body" in comment and "--title" in comment
    # And how to get rid of it, since the agent has no scope to delete a branch.
    assert f"git push origin --delete {ref}" in comment


def test_the_recovery_command_survives_a_summary_containing_a_quote(
    audit, scopes, git_repo
):
    """The title comes from the model, so it is not assumed to be shell-safe."""
    from triage.agent import build_stranded_block

    block = build_stranded_block(
        "triage/1-x",
        "main",
        "[triage] don't drop the operator's log",
        "boom",
        "https://run",
        SHA,
    )
    quoted = shlex.split(block.split("--title ")[1].split(" \\")[0])
    assert quoted == ["[triage] don't drop the operator's log"]


def test_a_pr_open_failure_is_audited_as_its_own_action(audit, scopes, git_repo):
    _, ref, _ = stranded_run(audit, scopes, git_repo)

    actions = [e.action for e in audit.entries]
    assert "open_pr_failed" in actions
    # The trail records the outcome as distinct from a plain diagnosis, so a
    # stranded branch is greppable rather than looking like a quiet no-fix run.
    finish = next(e for e in audit.entries if e.action == "finish")
    assert finish.metadata["outcome"] == "branch_without_pr"
    assert finish.metadata["pr_url"] is None
    failed = next(e for e in audit.entries if e.action == "open_pr_failed")
    assert failed.metadata["ref"] == ref


def test_a_pr_open_failure_makes_the_run_go_red(audit, scopes, git_repo):
    """A stranded fix is not a success; the operator has to be told."""
    _, _, code = stranded_run(audit, scopes, git_repo)
    assert code == 1


def test_the_fix_is_still_pushed_when_the_pr_cannot_be_opened(audit, scopes, git_repo):
    """The commit is the work; losing it would be worse than losing the PR."""
    runner, ref, _ = stranded_run(audit, scopes, git_repo)
    assert runner.ran("push", "origin", f"{ref}:{ref}")
    assert "fastapi==0.139.3" in (git_repo / "app/api/requirements.txt").read_text()


def test_a_governance_refusal_opening_a_pr_stays_fatal(audit, scopes, git_repo):
    """Platform failures degrade to a comment. Guardrail violations must not.

    A refusal here means the agent tried something it must never do, and that
    is a bug to be made loud — not an outcome to absorb into a tidy comment.
    """
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)

    def refuse(*_a, **_k):
        raise GuardrailViolation("refusing to open a PR from a protected ref")

    gh.open_pull_request = refuse

    with pytest.raises(GuardrailViolation):
        triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    assert "open_pr_failed" not in [e.action for e in audit.entries]


# -------------------------------------------------------- the diagnosis path


def test_low_confidence_comments_and_pushes_nothing(audit, scopes, git_repo):
    runner = gh_runner(log=BAD_DEP_LOG)
    gh, claude = build(
        runner, diagnosis_payload(confidence="low"), audit, scopes, git_repo
    )
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    assert not runner.ran("checkout", "-b")
    assert not runner.ran("push")
    assert not runner.ran("pr", "create")
    comment = runner.comment_body(f"commits/{SHA}/comments")
    assert "Diagnosis only" in comment
    assert "speculative patch is worse than none" in comment


def test_a_diff_that_does_not_apply_is_discarded_not_pushed(audit, scopes, git_repo):
    """High confidence, plausible text, wrong context — the check earns its keep."""
    runner = gh_runner(log=BAD_DEP_LOG)
    hallucinated = diagnosis_payload(
        diff=(
            "--- a/app/api/requirements.txt\n"
            "+++ b/app/api/requirements.txt\n"
            "@@ -1,2 +1,2 @@\n"
            "-httpx==0.99.99\n"
            "+httpx==0.28.1\n"
            " some line that is not in the file\n"
        )
    )
    gh, claude = build(runner, hallucinated, audit, scopes, git_repo)
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    assert not runner.ran("checkout", "-b")
    assert not runner.ran("pr", "create")
    comment = runner.comment_body(f"commits/{SHA}/comments")
    assert "did not survive verification" in comment
    assert "reject_diff" in [e.action for e in audit.entries]


def test_a_diff_touching_github_is_refused_and_downgraded(audit, scopes, git_repo):
    runner = gh_runner(log=BAD_DEP_LOG)
    self_editing = diagnosis_payload(
        diff=(
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -1 +1 @@\n"
            "-name: ci\n"
            "+name: ci\n"
        ),
        files_changed=[".github/workflows/ci.yml"],
    )
    gh, claude = build(runner, self_editing, audit, scopes, git_repo)
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    assert not runner.ran("checkout", "-b")
    assert "not agent-editable" in runner.comment_body(f"commits/{SHA}/comments")


def test_malformed_model_output_raises_rather_than_acting(audit, scopes, git_repo):
    runner = gh_runner(log=BAD_DEP_LOG)
    gh, claude = build(
        runner, "I could not figure it out, sorry.", audit, scopes, git_repo
    )
    with pytest.raises(ModelError):
        triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")
    assert not runner.ran("pr", "create")


# ------------------------------------------------------------------- the loops


def test_the_agent_refuses_to_triage_its_own_branches(audit, scopes, git_repo):
    """A failing triage PR must not spawn triage of triage."""
    runner = gh_runner(branch=f"triage/{RUN_ID}-something")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)

    assert triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a") == 0
    assert claude.call_count == 0
    assert not runner.ran("pr", "create")
    assert audit.entries[-1].action == "skip"


def test_a_run_with_no_failing_job_is_left_alone(audit, scopes, git_repo):
    import json

    runner = gh_runner()
    runner.results[f"actions/runs/{RUN_ID}/jobs"] = ok(
        json.dumps({"jobs": [{"name": "ruff", "conclusion": "success"}]})
    )
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)

    assert triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a") == 0
    assert claude.call_count == 0


# ------------------------------------------------------- scopes and the trail


def test_the_declared_scopes_are_exactly_what_the_flow_uses(scopes):
    from day2_agents.scopes import Action

    assert set(SCOPES) == set(scopes.actions)
    # Nothing outside the flow, and nothing that could merge or deploy.
    assert set(SCOPES) <= set(Action)


def test_every_externally_visible_action_leaves_an_audit_entry(audit, scopes, git_repo):
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    actions = [e.action for e in audit.entries]
    for expected in (
        "read_ci_run",
        "call_model",
        "diagnose",
        "create_branch",
        "commit",
        "push_branch",
        "open_pr",
        "comment_on_run",
        "finish",
    ):
        assert expected in actions, f"{expected} left no audit entry"
    assert all(e.approved_by is None for e in audit.entries)


def test_the_final_entry_records_the_real_cost(audit, scopes, git_repo):
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    finish = next(e for e in audit.entries if e.action == "finish")
    expected = (9000 * 5.00 + 1800 * 25.00) / 1e6
    assert finish.metadata["total_cost_usd"] == pytest.approx(expected, rel=1e-6)
    assert finish.metadata["outcome"] == "fix_pr"


def test_the_prompt_contains_the_log_and_the_file_but_never_a_secret(
    audit, scopes, git_repo, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-appear")
    runner = gh_runner(log=BAD_DEP_LOG)
    fake = FakeClaude(diagnosis_payload())
    gh = GitHubHelper(scopes, audit, repo=REPO, repo_root=str(git_repo), runner=runner)
    claude = ClaudeClient(scopes, audit, client=fake)
    runner.results["rev-parse --abbrev-ref"] = ok(
        f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists\n"
    )
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    prompt = fake.prompts[0]["messages"][0]["content"]
    assert "Could not find a version" in prompt
    assert "fastapi==0.139.2" in prompt  # the file it must patch
    assert "sk-ant-should-never-appear" not in prompt
    system = fake.prompts[0]["system"]
    assert "never merge" in system
    assert ".github/**" in system


def test_the_pr_body_says_ci_will_not_start_by_itself(audit, scopes, git_repo):
    """GITHUB_TOKEN cannot trigger workflows — the body must not imply it can."""
    runner = gh_runner(log=BAD_DEP_LOG)
    ref = f"triage/{RUN_ID}-pin-httpx-to-a-version-that-exists"
    runner.results["rev-parse --abbrev-ref"] = ok(ref + "\n")
    gh, claude = build(runner, diagnosis_payload(), audit, scopes, git_repo)
    triage_run(gh, claude, audit, scopes, RUN_ID, REPO, str(git_repo), "a")

    body = runner.body_for("pr create")
    assert "CI has **not** run on this branch" in body
    assert f"gh workflow run ci.yml --ref {ref}" in body
