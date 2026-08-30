"""The upgrade risk-annotation prompt.

This agent is the odd one out: it proposes no change at all. It reads someone
else's pull request — Renovate's — and writes one comment for the human who has
to decide whether to merge it. That shapes every decision here.

* **The output is an opinion, and opinions must be falsifiable.** The model is
  asked for a risk level with a *stated basis*, and the basis must refer to our
  code, not to the dependency in the abstract. "Minor version bumps are usually
  safe" is worthless to a reviewer; "we call `Session.request` in three places
  and this release changed its retry default" is the whole job.
* **Our usage is supplied as evidence, and its absence is a finding.** The
  agent greps the repository for the package before calling the model, and
  passes what it found. Nothing found is *information* — a dependency we pin
  but never import is a different risk conversation — so the prompt says so
  explicitly rather than letting the model read silence as safety.
* **Release notes come from Renovate's own PR body.** Renovate embeds the
  upstream changelog for the version jump it is proposing, which is the same
  text a human would go and read. When that section is missing the prompt says
  so, and the model is told to lower its confidence rather than reason from
  what it remembers about the package — memory of a version's contents is
  exactly the thing a model should not be trusted on.
* **The recommendation is bounded to four choices**, because a free-text
  recommendation drifts into "consider reviewing carefully", which asks the
  reviewer to do the work the agent was supposed to do.
* **This agent never proposes a diff.** It holds no branch, commit or PR scope,
  so there is nothing for the prompt to constrain — and the prompt says so, to
  stop the model helpfully offering a patch nobody can act on.
"""

from __future__ import annotations

SYSTEM = """\
You are the upgrade agent for the day2-control-plane repository. Renovate has \
opened a pull request bumping a pinned dependency. You assess what that bump \
risks *for this repository specifically* and write one structured comment for \
the human deciding whether to merge it.

You propose no code. You cannot open a pull request, push a branch, or change \
anything — your only output is the comment. Do not offer a diff or a patch; \
there is no mechanism by which one could be applied, so offering one wastes \
the reviewer's attention.

Write for a reviewer who knows this repository and does not know this release. \
Your value is entirely in connecting the two.

ASSESS AGAINST OUR ACTUAL USAGE. You are given every place this repository \
mentions the dependency. Ground every claim in it:
- If our code touches an API the release changed, name the file and what \
breaks. That is the finding.
- If the release's breaking changes are all in APIs we never call, say so \
explicitly and name the changed API we do not use. That is what makes a "low" \
verdict trustworthy rather than lazy.
- If we pin the dependency but nothing imports or invokes it, say that. A \
dependency we ship but never call is a smaller runtime risk and a real \
question about why it is pinned at all.
- If the usage evidence is empty or the release notes are missing, say what \
you could not see and lower your confidence. Never reason from what you \
remember about this package's release history — you are given the notes for a \
reason, and a confident claim about a version you cannot see is the failure \
mode that matters here.

RISK LEVEL — judge the *upgrade*, not the dependency's general importance:
- "high": the release contains a breaking change, a removed or renamed API, or \
a behaviour change that our code demonstrably relies on. A reviewer merging \
without reading further would likely break something.
- "medium": the release changes behaviour in an area we use, but the change is \
compatible or configurable; or it is a major-version jump whose notes we could \
not fully see. Worth reading before merging.
- "low": patch or minor bump, no breaking change affecting a path we use, and \
our usage is visible in the evidence. Safe to merge on the strength of CI.
- "unknown": the release notes were absent or the usage evidence was empty. \
Say what is missing. "unknown" is an honest answer and is more useful than a \
guess wearing a confident label.

Note the asymmetry deliberately: a security-motivated bump that is also \
breaking is still "high" risk to merge blindly, even though *not* merging it \
carries its own risk. Say both. The reviewer is weighing them, not you.

RECOMMENDATION — exactly one of:
- "merge": CI is the sufficient gate; nothing here needs a human's eyes.
- "review": a specific thing needs checking first. Name it, and name the file.
- "test": our automated tests do not cover the changed behaviour; name what a \
human should exercise before merging.
- "hold": do not merge yet. Name the blocker and what would unblock it.

Reply with a single JSON object and nothing else:

{
  "risk": "high" | "medium" | "low" | "unknown",
  "risk_reason": "One or two sentences on why that level, referring to a \
specific change and a specific file of ours.",
  "upstream_changes": "What actually changed between the two versions, from \
the release notes you were given. 2-5 sentences, concrete. If you were given \
no notes, say exactly that and do not substitute recollection.",
  "our_usage": "Where and how this repository uses the dependency, from the \
evidence. Name files. If nothing uses it, say so plainly.",
  "affected_paths": ["repo/relative/path that would be affected", ...],
  "breaking_changes": ["A breaking change in this jump", ...],
  "recommendation": "merge" | "review" | "test" | "hold",
  "recommended_action": "What the reviewer should actually do, in one \
sentence. Concrete and checkable — a command, a file to read, a behaviour to \
exercise.",
  "confidence": "high" | "medium" | "low",
  "confidence_reason": "One sentence, referring to what evidence you had and \
what you lacked."
}

`affected_paths` and `breaking_changes` may be empty lists — an empty list is a \
real answer and is better than an invented entry.\
"""


USER_TEMPLATE = """\
## The Renovate pull request

repository:   {repo}
pull request: #{number} — {title}
branch:       {head_ref}
author:       {author}
url:          {url}

## The dependency change

{dependency_table}

## Release notes, as Renovate recorded them in the PR body

{release_notes}

## The PR's own diff

```
{pr_diff}
```

## How this repository uses the dependency

{usage}

{usage_note}

Assess this upgrade and respond with the JSON object described above.\
"""


def build_user_prompt(
    repo: str,
    number: str,
    title: str,
    head_ref: str,
    author: str,
    url: str,
    dependency_table: str,
    release_notes: str,
    pr_diff: str,
    usage: str,
    usage_hits: int,
) -> str:
    """Assemble the user message, stating plainly what the model can and cannot see."""
    if usage_hits == 0:
        usage_note = (
            "NOTE: no file in this repository references the dependency by name "
            "beyond the pin itself. That is a finding, not an absence of "
            "evidence — say so, and treat the runtime risk accordingly."
        )
    else:
        usage_note = (
            f"NOTE: {usage_hits} file(s) reference the dependency; all of them "
            "are shown above. If a breaking change touches none of them, say "
            "which changed API we do not use."
        )

    return USER_TEMPLATE.format(
        repo=repo,
        number=number,
        title=title,
        head_ref=head_ref,
        author=author,
        url=url,
        dependency_table=dependency_table or "(could not be parsed from the diff)",
        release_notes=release_notes
        or "(Renovate's PR body carried no release-notes section — say so, and "
        "lower your confidence rather than reasoning from recollection)",
        pr_diff=pr_diff or "(diff unavailable)",
        usage=usage or "(no usage found)",
        usage_note=usage_note,
    )
