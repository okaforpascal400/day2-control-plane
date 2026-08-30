"""The only way an agent touches GitHub — `gh` and `git`, behind guardrails.

Every method here does the same four things in the same order:

    scope check  ->  guardrail check  ->  the action  ->  audit entry

The scope check asks whether *this agent* declared the capability; the
guardrail check asks whether the action is permitted *at all*. Both must pass,
and the audit entry is written by the same method that performed the action, so
an action cannot reach GitHub without a corresponding line in the trail.

Shelling out to `gh` rather than using an SDK is deliberate: the runner already
has `gh`, authenticated as the workflow's `GITHUB_TOKEN`, so the agent inherits
exactly the permissions declared in the workflow file and holds no credential
of its own. Narrowing what the agent can do is then a three-line edit to
`permissions:` that a reviewer reads in the diff — not a code change.

Commands are built as fixed argv lists and run with `shell=False`; no caller
input is ever interpolated into a shell string (governance pillar 2).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any, NoReturn

from day2_agents.audit import AuditLogger
from day2_agents.guardrails import (
    GuardrailViolation,
    assert_paths_allowed,
    assert_writable_ref,
)
from day2_agents.scopes import Action, PermissionSet

Runner = Callable[[Sequence[str], str | None, str], subprocess.CompletedProcess]

# Refused before the argv ever reaches a subprocess. `gh pr merge` is the one
# that matters; the rest close the neighbouring doors (auto-merge is a merge on
# a delay, and a force-push over a protected ref is a merge with extra steps).
FORBIDDEN_ARGV_FRAGMENTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pr", "merge"), "merging is a human decision (CLAUDE.md rule 3)"),
    (("pr", "review"), "approving is a human decision (CLAUDE.md rule 3)"),
    (("--auto",), "auto-merge is a deferred merge"),
    (("--admin",), "admin override bypasses required review"),
    (("--force",), "agents never rewrite published history"),
    (("-f",), "agents never rewrite published history"),
)

# Git identity, derived from the name the agent declared its scopes under.
# `triage` yields exactly the two constants this used to hardcode, so nothing
# about the triage agent's history changes; a second agent no longer signs its
# commits as the first one. Restricted to a conservative character set because
# the value is interpolated into a `git -c user.name=` argument.
BOT_NAME_TEMPLATE = "day2-{agent}-agent[bot]"
BOT_EMAIL_TEMPLATE = "{agent}-agent@users.noreply.github.com"
_SAFE_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")


def bot_identity(agent: str) -> tuple[str, str]:
    """The `(name, email)` an agent's commits are authored under."""
    if not _SAFE_AGENT_NAME.match(agent):
        raise GuardrailViolation(
            f"refusing to author commits as {agent!r}: an agent name must be "
            "lowercase alphanumeric with hyphens"
        )
    return BOT_NAME_TEMPLATE.format(agent=agent), BOT_EMAIL_TEMPLATE.format(agent=agent)


class GitHubRefused(RuntimeError):
    """A GitHub operation was refused by the library itself."""


class GitHubError(RuntimeError):
    """A GitHub operation was attempted and failed."""


def _default_runner(
    argv: Sequence[str], stdin: str | None, cwd: str
) -> subprocess.CompletedProcess:
    # Fixed argv, shell=False: nothing a model produced reaches a shell.
    return subprocess.run(
        list(argv),
        input=stdin,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


class GitHubHelper:
    def __init__(
        self,
        scopes: PermissionSet,
        audit: AuditLogger,
        repo: str,
        repo_root: str = ".",
        runner: Runner = _default_runner,
    ) -> None:
        self._scopes = scopes
        self._audit = audit
        self.repo = repo
        self.repo_root = repo_root
        self._runner = runner

    # ---------------------------------------------------------------- plumbing

    def _run(self, argv: Sequence[str], stdin: str | None = None) -> str:
        argv = list(argv)
        _assert_argv_allowed(argv)
        result = self._runner(argv, stdin, self.repo_root)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise GitHubError(f"`{' '.join(argv[:3])}` failed: {detail}")
        return result.stdout or ""

    def _git(self, *args: str, stdin: str | None = None) -> str:
        # Identity is set per-invocation rather than in global config so the
        # bot can never author a commit outside this helper.
        name, email = bot_identity(self._scopes.agent)
        return self._run(
            [
                "git",
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                *args,
            ],
            stdin=stdin,
        )

    # ------------------------------------------------------------------- reads

    def get_run(self, run_id: str | int) -> dict[str, Any]:
        """Metadata for a workflow run: conclusion, head SHA, branch, URL."""
        self._scopes.require(Action.READ_CI_RUN)
        return json.loads(
            self._run(["gh", "api", f"repos/{self.repo}/actions/runs/{run_id}"])
        )

    def get_run_jobs(self, run_id: str | int) -> list[dict[str, Any]]:
        """Every job in a run, with per-step conclusions."""
        self._scopes.require(Action.READ_CI_RUN)
        payload = json.loads(
            self._run(
                [
                    "gh",
                    "api",
                    "--paginate",
                    f"repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=100",
                ]
            )
        )
        return list(payload.get("jobs", []))

    def get_job_log(self, job_id: str | int) -> str:
        """Raw log for one job, over two independent transports.

        On run 33257066844 this returned nothing and said nothing: the window
        was 0 chars, the agent diagnosed the failure from the commit diff alone,
        and it opened a PR. Why the fetch came back empty is not recoverable,
        and that is the first defect — the old body was `return result.stdout
        or ""`, which discards the exit code and stderr both, so a log that
        never arrived is indistinguishable from one that expired. The evidence
        needed to explain it was thrown away at the moment it existed.

        So: every attempt now records its exit code, byte count and stderr, and
        an empty result is an audited event rather than a silent "". The
        `api` transport is the documented endpoint and answers with a 302 to a
        blob store — the redirect is the obvious suspect, but it is a suspect,
        not a finding. `gh run view` reads the same bytes out of the run-level
        log archive over a different code path, so it is a genuine second
        chance rather than a retry of whatever just failed.
        """
        self._scopes.require(Action.READ_CI_RUN)
        transports: tuple[tuple[str, list[str]], ...] = (
            ("api", ["gh", "api", f"repos/{self.repo}/actions/jobs/{job_id}/logs"]),
            (
                "run-view",
                ["gh", "run", "view", "--repo", self.repo, "--job", str(job_id), "--log"],
            ),
        )

        attempts: list[str] = []
        for name, argv in transports:
            _assert_argv_allowed(argv)
            result = self._runner(argv, None, self.repo_root)
            text = result.stdout or ""
            if result.returncode == 0 and text.strip():
                if attempts:
                    self._audit.record(
                        action="read_job_log",
                        target=f"{self.repo}/actions/jobs/{job_id}",
                        decision_summary=(
                            f"primary log transport failed, {name!r} succeeded "
                            f"with {len(text)} chars — {'; '.join(attempts)}"
                        ),
                    )
                return text
            attempts.append(
                f"{name}: exit {result.returncode}, {len(text)} chars, "
                f"stderr={(result.stderr or '').strip()[:200]!r}"
            )

        self._audit.record(
            action="log_unavailable",
            target=f"{self.repo}/actions/jobs/{job_id}",
            decision_summary=(
                "no job log could be retrieved by any transport; the diagnosis "
                f"that follows was made without it — {'; '.join(attempts)}"
            ),
        )
        return ""

    # --------------------------------------------------------- pull requests

    def get_pull_request(self, number: int | str) -> dict[str, Any]:
        """Metadata for one PR: title, body, head/base refs, author, state.

        Read-only, and it is someone else's PR — the Upgrade agent's whole job
        is to annotate a bot's dependency PR, which it must first be able to
        read. `author.login` is what tells it a PR came from Renovate rather
        than from a human, and that check belongs to the agent, not here.
        """
        self._scopes.require(Action.READ_PR)
        return json.loads(self._run(["gh", "api", f"repos/{self.repo}/pulls/{number}"]))

    def get_pull_request_diff(self, number: int | str) -> str:
        """The PR's diff, as text.

        The same endpoint as `get_pull_request` with a different `Accept`
        header, which is the documented way to ask for the diff and keeps this
        on `gh api` alongside every other read. `gh pr diff` would need a git
        remote configured; this works from any checkout.

        No cap is applied here. A caller that puts this in a prompt must bound
        it — the agent knows what its budget is, and a library that silently
        truncated evidence would make the agent's reasoning unexplainable.
        """
        self._scopes.require(Action.READ_PR)
        return self._run(
            [
                "gh",
                "api",
                "--header",
                "Accept: application/vnd.github.diff",
                f"repos/{self.repo}/pulls/{number}",
            ]
        )

    def list_issues(self, state: str = "open") -> list[dict[str, Any]]:
        """Issues **and** pull requests, which is deliberate.

        GitHub's `/issues` endpoint returns both; a PR is an issue with a
        `pull_request` key. That is exactly the shape the CVE agent's dedupe
        needs — "is there already an open PR *or* issue for this CVE" is one
        question and this is one call, so the two halves cannot answer
        inconsistently.

        GitHub's own state is the ledger. The agent keeps no "already filed"
        file of its own: a file drifts from reality the moment someone closes
        an issue by hand, and a stale ledger suppressing a real CVE is a much
        worse failure than filing a duplicate.
        """
        self._scopes.require(Action.READ_PR)
        if state not in ("open", "closed", "all"):
            raise GitHubRefused(f"unknown issue state {state!r}")
        payload = json.loads(
            self._run(
                [
                    "gh",
                    "api",
                    "--paginate",
                    f"repos/{self.repo}/issues?state={state}&per_page=100",
                ]
            )
        )
        return list(payload)

    # ------------------------------------------------------------------ writes

    def create_branch(self, ref: str, base_sha: str) -> str:
        self._scopes.require(Action.CREATE_BRANCH)
        assert_writable_ref(ref)
        self._git("checkout", "-b", ref, base_sha)
        self._audit.record(
            action="create_branch",
            target=f"{self.repo}@{ref}",
            decision_summary=f"branched from {base_sha[:12]} to hold a proposed fix",
        )
        return ref

    def commit_paths(self, ref: str, message: str, paths: Sequence[str]) -> str:
        """Commit exactly `paths` and nothing else. `ref` is re-checked, not trusted.

        `paths` must be the list `apply_diff` returned — the paths git actually
        patched — not the model's `files_changed`, which is a claim.

        This used to be `git add -A`, which commits whatever is in the working
        tree. On a runner that tree is not just the checkout: the agent's own
        audit log was being written into the workspace, so the first fix PR the
        agent opened carried `triage-audit.jsonl` alongside the one-line
        dependency fix it was proposing. A reviewer reading that diff cannot
        tell the proposal from the agent's exhaust, which defeats the point of
        proposing a diff for review at all.

        So the pathspec is explicit and then checked twice: the paths go through
        the same guardrail the diff did, and the index is compared against them
        after staging, so anything that arrives by another route fails the
        commit rather than riding along in it.
        """
        self._scopes.require(Action.PUSH_COMMIT)
        assert_writable_ref(ref)
        if not paths:
            raise GuardrailViolation("refusing to commit an empty pathspec")
        # The paths were permitted as diff targets; they are re-checked here
        # because this is the call that writes them into history.
        assert_paths_allowed(list(paths))

        current = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if current != ref:
            raise GuardrailViolation(
                f"refusing to commit: on {current!r}, expected {ref!r}"
            )

        self._git("add", "--", *paths)
        staged = {
            line
            for line in self._git("diff", "--cached", "--name-only").splitlines()
            if line
        }
        unexpected = sorted(staged - set(paths))
        if unexpected:
            raise GuardrailViolation(
                f"refusing to commit: {len(unexpected)} path(s) staged that the "
                f"diff did not touch: {', '.join(unexpected[:10])}"
            )

        # `-- <paths>` again, so even a pre-populated index cannot widen this.
        self._git("commit", "-m", message, "--", *paths)
        sha = self._git("rev-parse", "HEAD").strip()
        self._audit.record(
            action="commit",
            target=f"{self.repo}@{ref}#{sha[:12]}",
            decision_summary=(
                f"{message.splitlines()[0]} [{len(paths)} path(s): {', '.join(paths)}]"
            ),
            metadata={"paths": list(paths)},
        )
        return sha

    def push(self, ref: str) -> None:
        self._scopes.require(Action.PUSH_COMMIT)
        assert_writable_ref(ref)
        self._git("push", "origin", f"{ref}:{ref}")
        self._audit.record(
            action="push_branch",
            target=f"{self.repo}@{ref}",
            decision_summary="pushed the proposed fix; no CI gate is bypassed by this",
        )

    def open_pull_request(self, head: str, base: str, title: str, body: str) -> str:
        """Open a PR from a `triage/*` head. Opening a PR is not merging one."""
        self._scopes.require(Action.OPEN_PR)
        assert_writable_ref(head)
        url = self._run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                self.repo,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            stdin=body,
        ).strip()
        self._audit.record(
            action="open_pr",
            target=url or f"{self.repo}:{head}->{base}",
            decision_summary=f"proposed a fix for human review: {title}",
        )
        return url

    def comment_on_commit(self, sha: str, body: str) -> str:
        """Post a comment on a commit.

        GitHub has no comment API for workflow runs, so a note "on the failed
        run" lands on that run's head commit — which is linked from the run
        page and from every PR containing the commit, and needs no permission
        beyond the `contents: write` the branch push already requires.
        """
        self._scopes.require(Action.COMMENT_ON_RUN)
        payload = json.loads(
            self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{self.repo}/commits/{sha}/comments",
                    "--input",
                    "-",
                ],
                stdin=json.dumps({"body": body}),
            )
        )
        url = payload.get("html_url", "")
        self._audit.record(
            action="comment_on_run",
            target=url or f"{self.repo}@{sha[:12]}",
            decision_summary="linked the triage outcome from the failing commit",
        )
        return url

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> str:
        """File an issue. This is the "I could not fix it" ending, not a failure.

        The CVE agent reaches here when it is not confident enough to propose a
        patch, or when no clean remediation exists — a CVE in a pinned action
        under `.github/`, say, which it may never edit. A speculative security
        PR spends the review attention the real fix needed, so the honest issue
        is the better outcome and is given a first-class path rather than being
        left as silence.

        `labels` comes from the calling agent's own code, never from a model:
        GitHub creates unknown labels on the fly, so a hallucinated label would
        quietly pollute the repository's label set.
        """
        self._scopes.require(Action.OPEN_ISSUE)
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        response = json.loads(
            self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{self.repo}/issues",
                    "--input",
                    "-",
                ],
                stdin=json.dumps(payload),
            )
        )
        url = response.get("html_url", "")
        self._audit.record(
            action="open_issue",
            target=url or f"{self.repo}/issues",
            decision_summary=f"filed a diagnosis for human action: {title}",
            metadata={"labels": list(labels)},
        )
        return url

    def comment_on_pull_request(self, number: int | str, body: str) -> str:
        """Post a comment in a pull request's conversation.

        The endpoint is `/issues/{n}/comments`, not `/pulls/{n}/comments`, and
        the difference matters: the `pulls` endpoint posts a *review* comment
        anchored to a line of the diff, which is a review, and reviewing is a
        human decision (CLAUDE.md rule 3). This posts an ordinary timeline
        comment — the weakest write GitHub offers. It changes no code, no
        label, and no PR state; a human still reads it and decides.
        """
        self._scopes.require(Action.COMMENT_ON_PR)
        response = json.loads(
            self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{self.repo}/issues/{number}/comments",
                    "--input",
                    "-",
                ],
                stdin=json.dumps({"body": body}),
            )
        )
        url = response.get("html_url", "")
        self._audit.record(
            action="comment_on_pr",
            target=url or f"{self.repo}#{number}",
            decision_summary=f"annotated PR #{number} for the human reviewing it",
        )
        return url

    # --------------------------------------------------------------- forbidden

    def merge_pull_request(self, *args: object, **kwargs: object) -> NoReturn:
        """Always raises. Present so the refusal is explicit and testable.

        There is no scope that enables this and no argument that changes it.
        Merging is the human's half of "agents propose, humans approve"; a
        library that could do it would make the rule a convention rather than a
        control.
        """
        raise GuardrailViolation(
            "merging is not implemented: agents propose, humans approve "
            "(CLAUDE.md rule 3)"
        )

    # Same refusal, for the neighbouring verbs someone might reach for.
    enable_auto_merge = merge_pull_request
    approve_pull_request = merge_pull_request


def _assert_argv_allowed(argv: Sequence[str]) -> None:
    """Last line of defence: refuse forbidden commands before they run.

    The typed methods above already prevent these, but this catches a future
    caller that reaches `_run` directly with a hand-built argv.
    """
    tokens = [str(a) for a in argv]
    for fragment, reason in FORBIDDEN_ARGV_FRAGMENTS:
        if len(fragment) == 1:
            hit = fragment[0] in tokens
        else:
            hit = any(
                tuple(tokens[i : i + len(fragment)]) == fragment
                for i in range(len(tokens) - len(fragment) + 1)
            )
        if hit:
            raise GitHubRefused(f"refusing `{' '.join(tokens[:4])}`: {reason}")
