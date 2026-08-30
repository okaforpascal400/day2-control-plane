"""Least-privilege: an agent gets exactly what it declared, and nothing else."""

from __future__ import annotations

import dataclasses

import pytest

from day2_agents.scopes import Action, PermissionDenied, PermissionSet


def test_declared_action_is_allowed():
    scopes = PermissionSet.declare("triage", [Action.OPEN_PR])
    scopes.require(Action.OPEN_PR)  # does not raise
    assert scopes.allows(Action.OPEN_PR)


def test_undeclared_action_is_refused():
    scopes = PermissionSet.declare("triage", [Action.OPEN_PR])
    with pytest.raises(PermissionDenied, match="push_commit"):
        scopes.require(Action.PUSH_COMMIT)


def test_empty_declaration_permits_nothing():
    scopes = PermissionSet.declare("triage", [])
    for action in Action:
        with pytest.raises(PermissionDenied):
            scopes.require(action)


def test_string_values_resolve_to_actions():
    scopes = PermissionSet.declare("triage", ["open_pr", "call_model"])
    assert scopes.as_list() == ["call_model", "open_pr"]


def test_unknown_action_is_rejected_not_ignored():
    # Silently dropping a typo would grant less than intended and fail later,
    # somewhere far from the declaration.
    with pytest.raises(ValueError, match="unknown action"):
        PermissionSet.declare("triage", ["merge_pr"])


def test_agent_must_name_itself():
    with pytest.raises(ValueError):
        PermissionSet.declare("", [Action.OPEN_PR])


def test_permission_set_is_immutable():
    scopes = PermissionSet.declare("triage", [Action.OPEN_PR])
    with pytest.raises(dataclasses.FrozenInstanceError):
        scopes.actions = frozenset(Action)  # type: ignore[misc]


def test_merge_is_not_a_grantable_action():
    """The vocabulary itself has no merge, deploy or release."""
    values = {a.value for a in Action}
    assert not values & {"merge_pr", "merge", "deploy", "release", "delete_branch"}


# ------------------------------------------------- the Phase 5 additions


@pytest.mark.parametrize(
    "action", [Action.READ_PR, Action.OPEN_ISSUE, Action.COMMENT_ON_PR]
)
def test_the_phase5_actions_are_grantable(action):
    scopes = PermissionSet.declare("cve-response", [action])
    scopes.require(action)


def test_commenting_on_a_pr_is_not_commenting_on_a_run():
    """Two surfaces, two scopes. Holding one must never imply the other."""
    pr_only = PermissionSet.declare("upgrade", [Action.COMMENT_ON_PR])
    with pytest.raises(PermissionDenied, match="comment_on_run"):
        pr_only.require(Action.COMMENT_ON_RUN)

    run_only = PermissionSet.declare("triage", [Action.COMMENT_ON_RUN])
    with pytest.raises(PermissionDenied, match="comment_on_pr"):
        run_only.require(Action.COMMENT_ON_PR)


def test_reading_a_pr_grants_no_write_at_all():
    """The Upgrade agent's real shape: it reads and comments, and that is all."""
    upgrade = PermissionSet.declare(
        "upgrade", [Action.CALL_MODEL, Action.READ_PR, Action.COMMENT_ON_PR]
    )
    for denied in (
        Action.CREATE_BRANCH,
        Action.PUSH_COMMIT,
        Action.OPEN_PR,
        Action.OPEN_ISSUE,
    ):
        with pytest.raises(PermissionDenied):
            upgrade.require(denied)


def test_the_vocabulary_still_has_no_merge_after_the_widening():
    """Phase 5 added three members. None of them is a way to change main."""
    values = {a.value for a in Action}
    assert not values & {
        "merge_pr",
        "merge",
        "approve_pr",
        "review_pr",
        "deploy",
        "release",
        "delete_branch",
        "close_pr",
    }
