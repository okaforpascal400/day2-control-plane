"""Permission scopes: what an agent is allowed to do, declared at startup.

Governance pillar 1 (least-privilege). An agent names its capabilities once, in
one place, before it does anything; every helper in this package then asks the
declared set for permission before acting. An agent that forgets to declare
`OPEN_PR` cannot open a PR — it raises instead.

The declaration is the *ceiling*, not the floor. Some things are refused no
matter what an agent declares (merging, pushing to main, editing `.github/`);
those live in `guardrails.py` and are not expressible as scopes on purpose —
see the module docstring there.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    """Every action an agent may be granted. This enum is the whole vocabulary.

    Adding a member is a deliberate expansion of what agents can do and should
    be reviewed as such. Note what is *absent*: there is no merge, no release,
    no deploy, no delete. Those are not scopes an agent can be granted with a
    config change — they simply do not exist here.

    Note also what `COMMENT_ON_PR` is not. Commenting on a pull request is the
    weakest write GitHub offers: it adds a message to a thread and changes
    neither the code nor the PR's state. It is deliberately a separate member
    from `COMMENT_ON_RUN` rather than a reuse of it, so an agent granted one
    does not silently hold the other and the audit trail names the surface that
    was actually written to.
    """

    CALL_MODEL = "call_model"
    READ_CI_RUN = "read_ci_run"
    CREATE_BRANCH = "create_branch"
    PUSH_COMMIT = "push_commit"
    OPEN_PR = "open_pr"
    COMMENT_ON_RUN = "comment_on_run"

    # Added in Phase 5, for the CVE Response and Upgrade agents. Each one is a
    # read or a *proposal*; none of them is a new way to change the repository,
    # which is why the absences above still hold.
    READ_PR = "read_pr"  # read a pull request and its diff, and list open ones
    OPEN_ISSUE = "open_issue"  # file a diagnosis when there is no clean fix
    COMMENT_ON_PR = "comment_on_pr"  # annotate someone else's PR, in its thread


class PermissionDenied(RuntimeError):
    """Raised when an agent attempts an action it did not declare."""


@dataclass(frozen=True)
class PermissionSet:
    """An immutable declaration of one agent's allowed actions."""

    agent: str
    actions: frozenset[Action]

    @classmethod
    def declare(cls, agent: str, actions: Iterable[Action | str]) -> PermissionSet:
        """Declare an agent's scopes at startup.

        Accepts `Action` members or their string values; anything else is a
        typo or an invented capability and is rejected loudly rather than
        silently dropped, which would grant less than the author intended and
        fail much later.
        """
        if not agent:
            raise ValueError("an agent must name itself when declaring scopes")

        resolved: set[Action] = set()
        for action in actions:
            try:
                resolved.add(Action(action))
            except ValueError:
                known = ", ".join(sorted(a.value for a in Action))
                raise ValueError(
                    f"{agent!r} declared unknown action {action!r}; "
                    f"the grantable actions are: {known}"
                ) from None
        return cls(agent=agent, actions=frozenset(resolved))

    def allows(self, action: Action) -> bool:
        return action in self.actions

    def require(self, action: Action) -> None:
        """Gate an action. Raises `PermissionDenied` if it was not declared."""
        if action not in self.actions:
            granted = ", ".join(sorted(a.value for a in self.actions)) or "(none)"
            raise PermissionDenied(
                f"{self.agent!r} attempted {action.value!r} but declared only: {granted}"
            )

    def as_list(self) -> list[str]:
        """Sorted scope strings, for the audit trail and the PR body."""
        return sorted(a.value for a in self.actions)
