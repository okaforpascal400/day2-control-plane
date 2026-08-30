"""The refusals no scope can grant. These are the tests that matter most."""

from __future__ import annotations

import pytest

from day2_agents.guardrails import (
    AGENT_REF_PREFIXES,
    PROTECTED_REFS,
    GuardrailViolation,
    assert_paths_allowed,
    assert_writable_ref,
    normalise_diff_path,
)


@pytest.mark.parametrize(
    "ref",
    [
        "triage/12345-bad-dep",
        "triage/1",
        "triage/a.b_c-d",
        "agent/cve-2026-14456",
        "agent/1",
        "agent/a.b_c-d",
    ],
)
def test_agent_namespace_refs_are_writable(ref):
    assert assert_writable_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "master",
        "HEAD",
        "refs/heads/main",
        "phase4/triage-agent",
        "feature/x",
        "triage",
        "Triage/1",
        "../triage/1",
        "triage//1",
        "",
    ],
)
def test_everything_else_is_refused(ref):
    with pytest.raises(GuardrailViolation):
        assert_writable_ref(ref)


# ------------------------------------------- the Phase 5 widening, in detail
#
# `agent/*` was added alongside `triage/*` when the CVE agent arrived. The
# tests below exist because *widening* a guardrail is the change most likely to
# do more than it says: a regex that means to add one path segment very easily
# adds a prefix match instead, and `agents/` — a real directory in this repo,
# and one an agent may never write — is one character away from `agent/`.
#
# So the widening is pinned from both sides: exactly two namespaces are
# writable, and every near-miss is refused by name.


@pytest.mark.parametrize(
    "ref",
    [
        # The near-misses that a prefix match — rather than a segment match —
        # would wrongly admit. `agents/` is the dangerous one: it is a real
        # path in this repository.
        "agents/core",
        "agents/x",
        "agent-x/y",
        "agentfoo/z",
        "agentic/x",
        "triages/x",
        "triage-x/y",
        # Right namespace, wrong shape.
        "agent",
        "agent/",
        "agent//x",
        "agent/../x",
        "agent/.hidden",
        "Agent/x",
        "AGENT/x",
        # Anchoring: the namespace must start the ref, not appear in it.
        "x/agent/y",
        "refs/heads/agent/x",
        " agent/x",
        "agent/x ",
        "agent/x\n",
    ],
)
def test_the_widening_is_exact_not_loose(ref):
    """A namespace is a whole path segment. Near-misses stay refused."""
    with pytest.raises(GuardrailViolation):
        assert_writable_ref(ref)


def test_exactly_two_namespaces_are_writable():
    """A third prefix must be a deliberate change to this assertion, not a regex tweak."""
    assert AGENT_REF_PREFIXES == ("triage/", "agent/")


def test_no_protected_ref_can_be_reached_through_either_namespace():
    """Widening must not open a second route to the refs that are never writable."""
    for protected in PROTECTED_REFS:
        for prefix in AGENT_REF_PREFIXES:
            with pytest.raises(GuardrailViolation):
                assert_writable_ref(f"{prefix}../{protected}")


def test_main_is_refused_by_name_not_only_by_pattern():
    """Two independent reasons, so a pattern change cannot unblock main."""
    with pytest.raises(GuardrailViolation, match="never push main"):
        assert_writable_ref("main")


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/triage-agent.yml",
        ".github/scripts/cost_sentinel.py",
        ".github",
        "agents/core/day2_agents/guardrails.py",
    ],
)
def test_self_modifying_paths_are_refused(path):
    with pytest.raises(GuardrailViolation):
        assert_paths_allowed([path])


def test_one_forbidden_path_rejects_the_whole_diff():
    """All-or-nothing: dropping hunks would decouple the PR from its diagnosis."""
    with pytest.raises(GuardrailViolation, match=r"\.github"):
        assert_paths_allowed(["app/api/requirements.txt", ".github/workflows/ci.yml"])


def test_ordinary_source_paths_are_allowed():
    assert_paths_allowed(["app/api/requirements.txt", "deploy/helm/values.yaml"])


def test_a_diff_touching_nothing_is_refused():
    with pytest.raises(GuardrailViolation):
        assert_paths_allowed([])


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a/app/api/main.py", "app/api/main.py"),
        ("b/app/api/main.py", "app/api/main.py"),
        ('"a/app/with space.py"', "app/with space.py"),
        ("a/app/./api/main.py", "app/api/main.py"),
    ],
)
def test_diff_paths_are_normalised(raw, expected):
    assert normalise_diff_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["/etc/passwd", "a/../../etc/passwd", "../outside", "a/../.github/workflows/ci.yml"],
)
def test_traversal_and_absolute_paths_are_refused(raw):
    with pytest.raises(GuardrailViolation):
        path = normalise_diff_path(raw)
        # If normalisation let it through, the prefix check must still catch it.
        assert_paths_allowed([path])
