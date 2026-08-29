"""Verification of model-proposed unified diffs.

Governance pillar 6 (output verification). A diff from a model is a claim, not
a change. Before an agent is allowed to put one in front of a human it must
survive three checks, in this order:

1. **Parse.** Every file the diff touches is extracted from its headers. A diff
   whose headers cannot be read is refused — failing closed, because an
   unparseable header is exactly how a forbidden path would sneak past a
   path check.
2. **Permit.** Those paths are run through `guardrails.assert_paths_allowed`,
   which refuses the whole diff if any of them is off-limits.
3. **Apply.** `git apply --check` proves the patch applies cleanly to the real
   tree. This is what stops a plausible-looking but hallucinated diff (wrong
   line numbers, context that does not exist) from reaching a PR.

Only after all three does the agent create a branch.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence

from day2_agents.guardrails import (
    GuardrailViolation,
    assert_paths_allowed,
    normalise_diff_path,
)

Runner = Callable[[Sequence[str], str | None, str], subprocess.CompletedProcess]

# `/dev/null` is git's marker for "no file on this side" (an added or deleted
# file), not a path the diff writes to.
_NULL_PATH = "/dev/null"


class DiffRejected(RuntimeError):
    """The proposed diff did not survive verification."""


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


def diff_paths(diff_text: str) -> list[str]:
    """Every repo-relative path the diff touches, in first-seen order.

    Reads `diff --git` headers when present and falls back to `---`/`+++`
    lines, so a diff that omits the `diff --git` line (models often do) is
    still fully accounted for rather than silently reported as touching
    nothing.
    """
    if not diff_text or not diff_text.strip():
        raise DiffRejected("empty diff")

    seen: list[str] = []

    def note(raw: str) -> None:
        if raw == _NULL_PATH:
            return
        path = normalise_diff_path(raw)
        if path not in seen:
            seen.append(path)

    for line in diff_text.splitlines():
        try:
            if line.startswith("diff --git "):
                parts = shlex.split(line[len("diff --git ") :])
                if len(parts) != 2:
                    raise DiffRejected(f"unreadable diff header: {line!r}")
                for part in parts:
                    note(part)
            elif line.startswith(("--- ", "+++ ")):
                # Strip a trailing tab-separated timestamp if one is present.
                note(line[4:].split("\t", 1)[0])
        except GuardrailViolation as exc:
            raise DiffRejected(str(exc)) from exc

    if not seen:
        raise DiffRejected("diff contains no recognisable file headers")
    return seen


def validate_diff(
    diff_text: str,
    repo_root: str = ".",
    runner: Runner = _default_runner,
) -> list[str]:
    """Run all three checks. Returns the touched paths, or raises `DiffRejected`.

    Nothing is written: `git apply --check` only reports whether the patch
    *would* apply. The agent applies it for real, on a `triage/*` branch, only
    once this has passed.
    """
    paths = diff_paths(diff_text)

    try:
        assert_paths_allowed(paths)
    except GuardrailViolation as exc:
        raise DiffRejected(str(exc)) from exc

    patch = diff_text if diff_text.endswith("\n") else diff_text + "\n"
    result = runner(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"], patch, repo_root
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DiffRejected(f"diff does not apply cleanly: {detail}")
    return paths


def apply_diff(
    diff_text: str,
    repo_root: str = ".",
    runner: Runner = _default_runner,
) -> list[str]:
    """Verify, then actually apply. Never call this without `validate_diff`."""
    paths = validate_diff(diff_text, repo_root=repo_root, runner=runner)
    patch = diff_text if diff_text.endswith("\n") else diff_text + "\n"
    result = runner(["git", "apply", "--whitespace=nowarn", "-"], patch, repo_root)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DiffRejected(f"diff passed --check but failed to apply: {detail}")
    return paths
