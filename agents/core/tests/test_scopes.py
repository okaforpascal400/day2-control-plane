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
