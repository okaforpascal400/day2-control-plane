"""The Upgrade Agent: a Renovate PR in, a risk annotation out.

    Renovate opens a PR -> read it -> parse the pin that moved
        -> Renovate's own release notes -> grep our usage -> Claude
        -> one structured comment on that PR

This agent writes no code. It holds three scopes — call the model, read a pull
request, comment on one — and that is the entire grant. There is no branch, no
commit, no PR and no issue scope, so "it never pushes code" is not a rule it
follows; it is a capability it does not have. `agents/core` refuses the rest
before a subprocess is reached, and `test_scopes.py` asserts exactly that
shape.

Two design decisions worth reading:

* **It annotates on `opened` and `reopened` only.** Renovate force-pushes a PR
  when a newer version of the dependency appears, and re-annotating on every
  such push would need the agent to read its own prior comments to avoid
  duplicating them — a read it has no scope for. Rather than widen the grant
  for a convenience, a superseded annotation is left standing and a human can
  re-run the agent by hand (`gh workflow run upgrade-agent.yml -f pr=<n>`).
  The limitation is real and is written down rather than papered over.

* **It refuses to annotate anything but a dependency bot's PR.** A human's PR
  is not what this agent was reasoned about, and an agent-authored PR is worse
  than useless: an upgrade agent commenting on a CVE agent's PR is two models
  talking to each other at a reviewer's expense.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from day2_agents.audit import AuditLogger
from day2_agents.claude import ClaudeClient, ModelError, parse_json_object
from day2_agents.github import GitHubHelper
from day2_agents.scopes import Action, PermissionSet
from upgrade import evidence, prompts

AGENT = "upgrade"

# The whole grant. Every entry is exercised below; nothing is declared "just in
# case". What is absent is the point: no CREATE_BRANCH, no PUSH_COMMIT, no
# OPEN_PR, no OPEN_ISSUE. This agent cannot change the repository.
SCOPES = (
    Action.CALL_MODEL,  # assess the upgrade
    Action.READ_PR,  # read the Renovate PR and its diff
    Action.COMMENT_ON_PR,  # the annotation, and nothing else
)

# PR authors whose dependency bumps this agent will annotate.
DEPENDENCY_BOTS = frozenset({"renovate[bot]", "renovate-bot", "dependabot[bot]"})

# Branch prefixes this agent must never annotate: they are other agents' work.
AGENT_BRANCH_PREFIXES = ("agent/", "triage/")

VALID_RISK = ("high", "medium", "low", "unknown")
VALID_RECOMMENDATION = ("merge", "review", "test", "hold")
VALID_CONFIDENCE = ("high", "medium", "low")

COMMENT_MARKER = "Risk annotation by the upgrade agent — advisory only"

RISK_BADGE = {
    "high": "🔴 HIGH",
    "medium": "🟠 MEDIUM",
    "low": "🟢 LOW",
    "unknown": "⚪ UNKNOWN",
}


class UpgradeError(RuntimeError):
    """The pull request could not be annotated at all."""


@dataclass(frozen=True)
class Annotation:
    """The model's answer, after every field has been checked."""

    risk: str
    risk_reason: str
    upstream_changes: str
    our_usage: str
    affected_paths: list[str]
    breaking_changes: list[str]
    recommendation: str
    recommended_action: str
    confidence: str
    confidence_reason: str


def validate_annotation(payload: dict[str, Any]) -> Annotation:
    """Check the model's object before a byte of it reaches a reviewer.

    Governance pillar 6. This agent's only output is prose, so there is no
    `git apply --check` to catch a bad answer downstream — the field contract
    is the whole of the verification, which makes it stricter here than it
    would otherwise need to be.
    """

    def text(key: str, required: bool = True) -> str:
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise ModelError(
                f"field {key!r} must be a string, got {type(value).__name__}"
            )
        if required and not value.strip():
            raise ModelError(f"field {key!r} is required and was empty")
        return value.strip()

    def string_list(key: str) -> list[str]:
        value = payload.get(key) or []
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ModelError(f"field {key!r} must be a list of strings")
        return [v.strip() for v in value if v.strip()]

    def choice(key: str, allowed: tuple[str, ...]) -> str:
        value = text(key).lower()
        if value not in allowed:
            raise ModelError(f"{key} must be one of {allowed}, got {value!r}")
        return value

    return Annotation(
        risk=choice("risk", VALID_RISK),
        risk_reason=text("risk_reason"),
        upstream_changes=text("upstream_changes"),
        our_usage=text("our_usage"),
        affected_paths=string_list("affected_paths"),
        breaking_changes=string_list("breaking_changes"),
        recommendation=choice("recommendation", VALID_RECOMMENDATION),
        recommended_action=text("recommended_action"),
        confidence=choice("confidence", VALID_CONFIDENCE),
        confidence_reason=text("confidence_reason", required=False),
    )


# ------------------------------------------------------------------- rendering


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty


def build_comment_body(
    annotation: Annotation,
    dependency_table: str,
    audit_url: str,
    cost_usd: float,
    scopes: PermissionSet,
    had_release_notes: bool,
    usage_hits: int,
) -> str:
    evidence_note = (
        "Release notes were read from this PR's own body, where Renovate recorded them."
        if had_release_notes
        else "**This PR's body carried no release-notes section**, so the "
        "assessment above is made without them — weigh it accordingly."
    )
    usage_note = (
        f"{usage_hits} file(s) in this repository reference the dependency."
        if usage_hits
        else "**No file in this repository references the dependency by name** "
        "beyond the pin itself."
    )

    return f"""\
### Upgrade risk — {RISK_BADGE[annotation.risk]}

{annotation.risk_reason}

{dependency_table}

**Recommendation: `{annotation.recommendation}`.** {annotation.recommended_action}

<details>
<summary>What changed upstream</summary>

{annotation.upstream_changes}

</details>

**Breaking changes in this jump**

{_bullets(annotation.breaking_changes, "- None identified in the notes provided.")}

**How we use it**

{annotation.our_usage}

**Our code paths this touches**

{_bullets(annotation.affected_paths, "- None identified.")}

---

**Confidence: {annotation.confidence}** — {annotation.confidence_reason}

{evidence_note} {usage_note}

<sub>{COMMENT_MARKER}. It proposes no code and holds no scope to push, open a \
PR, or merge — `{"`, `".join(scopes.as_list())}`. \
Audit trail: {audit_url} · Model cost: ${cost_usd:.4f}</sub>
"""


# ------------------------------------------------------------------- the agent


def annotate_pull_request(
    gh: GitHubHelper,
    claude: ClaudeClient,
    audit: AuditLogger,
    scopes: PermissionSet,
    number: str,
    repo: str,
    repo_root: str,
    audit_url: str,
    simulate: bool = False,
) -> int:
    pull = gh.get_pull_request(number)
    author = (pull.get("user") or {}).get("login", "")
    head_ref = (pull.get("head") or {}).get("ref", "")
    title = pull.get("title", "")
    url = pull.get("html_url", "")

    # Agents do not annotate agents. Cheapest check first, before any read that
    # costs anything.
    if head_ref.startswith(AGENT_BRANCH_PREFIXES):
        audit.record(
            action="skip",
            target=url or f"{repo}#{number}",
            decision_summary=(
                f"declined to annotate {head_ref!r}: it is another agent's "
                "branch, and agents do not annotate agents"
            ),
        )
        print(f"Skipping {head_ref}: agent-authored branch.")
        return 0

    if author not in DEPENDENCY_BOTS:
        if not simulate:
            audit.record(
                action="skip",
                target=url or f"{repo}#{number}",
                decision_summary=(
                    f"declined to annotate a PR by {author!r}: this agent "
                    f"annotates dependency bots only "
                    f"({', '.join(sorted(DEPENDENCY_BOTS))})"
                ),
            )
            print(f"Skipping #{number}: author {author!r} is not a dependency bot.")
            return 0
        # The seeded-verification path. Audited loudly, because "the author
        # check was bypassed" is exactly the kind of thing that must not be
        # inferable only from the absence of a skip entry.
        audit.record(
            action="simulate",
            target=url or f"{repo}#{number}",
            decision_summary=(
                f"UPGRADE_SIMULATE is set: annotating a PR by {author!r}, which "
                "is not a dependency bot. This is a seeded verification run, "
                "not a production annotation."
            ),
            metadata={"author": author, "simulated": True},
        )

    diff = gh.get_pull_request_diff(number)
    changes = evidence.parse_dependency_changes(diff)
    if not changes:
        audit.record(
            action="skip",
            target=url or f"{repo}#{number}",
            decision_summary=(
                "no dependency pin moved in this diff; there is nothing to "
                "assess and no model call was made"
            ),
        )
        print(f"Skipping #{number}: no recognisable dependency change.")
        return 0

    notes = evidence.extract_release_notes(pull.get("body") or "")
    tracked = evidence.tracked_files(repo_root)
    # The first change is the one the usage search is anchored on: Renovate
    # opens one PR per dependency by default, so a multi-change diff is the
    # same package pinned in several files rather than several packages.
    usage, usage_hits = evidence.find_usage(repo_root, changes[0], tracked)
    table = evidence.render_dependency_table(changes)

    audit.record(
        action="read_pr",
        target=url or f"{repo}#{number}",
        decision_summary=(
            f"{len(changes)} pin(s) moved, {len(notes)} chars of release notes, "
            f"{usage_hits} file(s) referencing {changes[0].name}"
        ),
        metadata={
            "pr": number,
            "author": author,
            "changes": [
                {"name": c.name, "kind": c.kind, "from": c.old, "to": c.new}
                for c in changes
            ],
            "release_notes_chars": len(notes),
            "usage_files": usage_hits,
        },
    )

    call = claude.complete(
        system=prompts.SYSTEM,
        user=prompts.build_user_prompt(
            repo=repo,
            number=str(number),
            title=title,
            head_ref=head_ref,
            author=author,
            url=url,
            dependency_table=table,
            release_notes=notes,
            pr_diff=evidence.bound_diff(diff),
            usage=usage,
            usage_hits=usage_hits,
        ),
        target=url or f"{repo}#{number}",
        decision_summary=(
            f"assessing {changes[0].name} {changes[0].old}->{changes[0].new}"
        ),
    )
    annotation = validate_annotation(parse_json_object(call.text))

    audit.record(
        action="assess",
        target=url or f"{repo}#{number}",
        decision_summary=(
            f"{annotation.risk} risk, recommendation={annotation.recommendation}: "
            f"{annotation.risk_reason}"
        ),
        metadata={
            "risk": annotation.risk,
            "recommendation": annotation.recommendation,
            "confidence": annotation.confidence,
            "breaking_changes": annotation.breaking_changes,
            "affected_paths": annotation.affected_paths,
        },
    )

    comment_url = gh.comment_on_pull_request(
        number,
        build_comment_body(
            annotation,
            table,
            audit_url,
            claude.total_cost_usd,
            scopes,
            had_release_notes=bool(notes),
            usage_hits=usage_hits,
        ),
    )
    print(f"Annotated {comment_url or url}")

    audit.record(
        action="finish",
        target=comment_url or url,
        decision_summary=(
            f"annotation posted: {annotation.risk} risk, "
            f"{claude.call_count} model call(s), ${claude.total_cost_usd:.4f}"
        ),
        metadata={
            "outcome": "annotated",
            "risk": annotation.risk,
            "model_calls": claude.call_count,
            "total_cost_usd": round(claude.total_cost_usd, 6),
        },
    )
    _write_step_summary(annotation, changes, comment_url, claude.total_cost_usd)
    return 0


def _write_step_summary(
    annotation: Annotation,
    changes: list[evidence.DependencyChange],
    comment_url: str,
    cost_usd: float,
) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    bumped = ", ".join(f"`{c.name}` {c.old}→{c.new}" for c in changes)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            f"## Upgrade risk\n\n{bumped}\n\n"
            f"- Risk: **{RISK_BADGE[annotation.risk]}**\n"
            f"- Recommendation: **{annotation.recommendation}**\n"
            f"- Confidence: **{annotation.confidence}**\n"
            f"- Model cost: **${cost_usd:.4f}**\n"
            f"- Comment: {comment_url}\n\n{annotation.risk_reason}\n"
        )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    number = os.environ.get("UPGRADE_PR_NUMBER") or (argv[0] if argv else "")
    if not repo or not number:
        raise UpgradeError("GITHUB_REPOSITORY and UPGRADE_PR_NUMBER must both be set")

    repo_root = os.environ.get("GITHUB_WORKSPACE", ".")
    audit_url = os.environ.get("UPGRADE_AUDIT_URL", "(artifact on this run)")
    simulate = os.environ.get("UPGRADE_SIMULATE", "") == "1"

    audit = AuditLogger(agent=AGENT, trigger=f"pull_request:{repo}#{number}")
    scopes = PermissionSet.declare(AGENT, SCOPES)
    audit.record(
        action="declare_scopes",
        target=repo,
        decision_summary=f"declared scopes: {', '.join(scopes.as_list())}",
        metadata={"scopes": scopes.as_list()},
    )

    gh = GitHubHelper(scopes, audit, repo=repo, repo_root=repo_root)
    claude = ClaudeClient(scopes, audit)

    try:
        return annotate_pull_request(
            gh, claude, audit, scopes, number, repo, repo_root, audit_url, simulate
        )
    # Broad on purpose: whatever went wrong, the trail must record it before
    # the run dies, and the exception is re-raised so the job still goes red.
    except Exception as exc:
        audit.record(
            action="error",
            target=f"{repo}#{number}",
            decision_summary=f"{type(exc).__name__}: {exc}",
        )
        print(json.dumps({"upgrade_error": str(exc)}), file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
