"""Refusals that no scope can grant and no config can turn off.

`scopes.py` answers "was this agent allowed to try?". This module answers "is
this permitted at all?", and the answer does not depend on the caller, the
environment, or any argument. The constants below are module-level literals
with no env-var override and no constructor parameter reaching them — that is
the point. A guardrail you can configure is a guardrail an agent can be talked
into disabling.

Three rules:

1. **Writable refs are the agent namespaces only.** Every branch an agent
   creates or pushes must match `AGENT_REF` — `triage/*` or `agent/*`, and
   nothing else. `main` is not merely absent from the allow-list; it is
   separately blocked by `PROTECTED_REFS` so a future pattern change cannot
   accidentally let it through.

   The second prefix was added in Phase 5. `triage/*` was the whole namespace
   while triage was the only agent, and a CVE remediation branch named
   `triage/cve-2026-14456` misnames itself in the one place a reviewer looks
   first. Widening it is a guardrail change and is written to be *exact*: the
   alternation is anchored and must be followed by `/`, so `agents/x`,
   `agent-x/y` and `agentfoo/z` are all still refused. A guardrail loosened by
   a sloppy regex is a guardrail removed.
2. **Merging is not implemented.** `GitHubHelper.merge_pull_request` exists only
   to raise. Humans approve (CLAUDE.md rule 3), so the capability is absent from
   the library rather than gated behind a flag.
3. **Some paths are never in a proposed diff.** A triage agent that can edit
   `.github/` can edit the workflow that triggers it and the CI gates that judge
   it — it could propose a "fix" that disables the test that caught the bug. It
   likewise may not edit `agents/core/`, which is this file: an agent that can
   rewrite its own guardrails has none.
"""

from __future__ import annotations

import posixpath
import re

# Anchored at both ends; the alternation is a whole path segment, so only an
# exact `triage` or `agent` followed by `/` matches. The first character after
# the slash must be alphanumeric, which is what keeps `agent/../x`, `agent//x`
# and `agent/.hidden` out without a second check.
#
# `\Z`, not `$`. In Python `$` also matches immediately *before* a trailing
# newline, so `^...$` accepted `triage/x\n` — a ref carrying a newline into
# `git push origin <ref>:<ref>`. That was true of the Phase 4 pattern as well;
# the boundary tests written for the `agent/*` widening are what surfaced it.
# `\Z` matches only at the very end of the string.
AGENT_REF = re.compile(r"^(?:triage|agent)/[A-Za-z0-9][A-Za-z0-9._-]{0,98}\Z")

# The namespaces themselves, for error messages and documentation. Kept beside
# the pattern so the two cannot drift.
AGENT_REF_PREFIXES: tuple[str, ...] = ("triage/", "agent/")

PROTECTED_REFS = frozenset({"main", "master", "HEAD"})

# Prefixes an agent-proposed diff may never touch, with the reason each is here.
FORBIDDEN_DIFF_PREFIXES: tuple[tuple[str, str], ...] = (
    (".github/", "CI gates and agent triggers are not agent-editable"),
    ("agents/core/", "an agent may not rewrite the guardrails that bind it"),
)


class GuardrailViolation(RuntimeError):
    """Raised when an action is refused outright, regardless of scopes."""


def assert_writable_ref(ref: str) -> str:
    """Return `ref` if an agent may write to it, else raise.

    Applies to branch creation, commits and pushes alike, so there is one place
    to read and one place to test.
    """
    if not isinstance(ref, str) or not ref:
        raise GuardrailViolation("refusing to write to an empty ref")
    if ref in PROTECTED_REFS or ref.startswith("refs/heads/main"):
        raise GuardrailViolation(
            f"refusing to write to protected ref {ref!r} "
            "(CLAUDE.md rule 3: never push main)"
        )
    if not AGENT_REF.match(ref):
        allowed = ", ".join(f"{prefix}*" for prefix in AGENT_REF_PREFIXES)
        raise GuardrailViolation(
            f"refusing to write to {ref!r}: agents may only write refs matching {allowed}"
        )
    return ref


def normalise_diff_path(raw: str) -> str:
    """Reduce a path as it appears in a diff header to a repo-relative path.

    Strips git's `a/`/`b/` prefixes and quoting, then normalises. Anything that
    escapes the repo (absolute, or `..` after normalisation) is refused here
    rather than handed to `assert_paths_allowed`, so a traversal cannot be used
    to reach a forbidden prefix from the side.
    """
    path = raw.strip()
    if path.startswith('"') and path.endswith('"') and len(path) > 1:
        path = path[1:-1]
    path = path.replace("\\", "/")
    if path.startswith(("a/", "b/")):
        path = path[2:]
    if path.startswith("/"):
        raise GuardrailViolation(f"refusing absolute path in diff: {raw!r}")
    normalised = posixpath.normpath(path)
    if normalised == ".." or normalised.startswith("../"):
        raise GuardrailViolation(f"refusing path outside the repo: {raw!r}")
    return normalised


def assert_paths_allowed(paths: list[str]) -> None:
    """Refuse the whole diff if any path it touches is off-limits.

    All-or-nothing on purpose: silently dropping the forbidden hunks would
    produce a PR that does not match the diagnosis it claims to implement.
    """
    if not paths:
        raise GuardrailViolation("refusing a diff that touches no files")
    for path in paths:
        for prefix, reason in FORBIDDEN_DIFF_PREFIXES:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                raise GuardrailViolation(f"refusing diff touching {path!r}: {reason}")
