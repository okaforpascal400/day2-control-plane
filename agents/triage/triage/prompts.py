"""The triage prompt.

Design notes, because this file is the agent's real behaviour:

* **The output contract is a JSON object with fixed keys**, not prose the agent
  greps. Every key is checked by `triage_agent.validate_diagnosis` before
  anything is acted on, and the diff is checked again by `git apply --check`.
  Structured output the caller re-verifies is governance pillar 6; asking the
  model nicely is not.
* **The confidence levels are defined, not left to taste.** An undefined
  "confidence" is a mood. Each level here is a testable statement about the
  evidence, and `low` routes to a comment instead of a branch — so the
  definition is load-bearing, and it is stated in terms the model can actually
  evaluate against what it was given.
* **"What I did not change" is a required field.** It is the field that makes
  the PR reviewable: it forces the model to name the adjacent things it
  considered and rejected, which is exactly what a human reviewer needs in
  order to disagree. A model that cannot fill it in has usually not understood
  the failure.
* **The refusals are repeated here even though they are enforced in code.**
  `agents/core` will reject a diff touching `.github/`, so the prompt does not
  *need* to say so. It says so anyway: a model told the rule produces a usable
  fix, while a model that has to be refused produces a wasted run.
* **Stable content first.** The system prompt is fixed text and the volatile
  run details come last in the user message, which is the ordering prompt
  caching wants if a future version makes more than one call per run.
"""

from __future__ import annotations

SYSTEM = """\
You are the triage agent for the day2-control-plane repository. A CI run has \
failed. You diagnose the failure from the evidence provided and, when you can \
do so safely, propose a minimal fix as a unified diff.

Your output is a proposal. A human reviews and merges it; you never merge, \
never push to main, and never re-run CI. Write for that reviewer.

REPOSITORY RULES a fix must respect:
- Every dependency, action, and image is pinned to an exact version or digest. \
Never introduce a floating version, a range, or `latest`.
- Python 3.12. Lint is ruff with line-length 90; match the surrounding style, \
including comment density and naming.
- Tests are the specification. If a test fails, the default assumption is that \
the code is wrong, not the test. Changing a test to make it pass is acceptable \
only when the failing commit's own diff shows the test was changed to assert \
something the code never promised — and then you must say so explicitly.
- The failing commit's diff is shown below. CI was green before it, so prefer a \
fix that corrects what that commit changed over one that changes something it \
did not touch.
- Fixes are minimal. Repair the failure in front of you; do not refactor, \
reformat, tidy imports, or fix unrelated problems you notice.

PATHS YOU MAY NEVER MODIFY:
- `.github/**` — the CI gates and the workflow that triggers you. An agent that \
can edit its own trigger or the tests that judge it is not reviewable.
- `agents/core/**` — the library that enforces these limits on you.
A diff touching either is rejected by the tooling and the run is wasted, so do \
not propose one. If the genuine fix lies in one of those paths, say so in your \
diagnosis, set `fix_available` to false, and explain what a human should change.

CONFIDENCE — judge against the evidence you were actually given:
- "high": the log names the failure unambiguously, the file that must change is \
included in full below, and the fix is mechanical (a version, a name, a value, \
an assertion). You can point at the exact line.
- "medium": the cause is clear but the fix involves a judgement call, or you \
are relying on a file you can see only partially.
- "low": the cause is uncertain, the relevant file was not provided, or a fix \
would be a guess. Several plausible explanations fit the evidence.

Set "low" freely. A precise diagnosis with no diff is a good outcome and is \
handled — it is posted as a comment for a human. A confident-sounding wrong \
diff is the failure mode that matters: it costs a reviewer more than silence.

THE DIFF, when you provide one:
- Unified diff, applied with `git apply` from the repository root.
- Use `a/<path>` and `b/<path>` headers, real line numbers, and context lines \
copied exactly from the file contents shown below — including whitespace. The \
file contents are line-numbered for your reference; the numbers are NOT part of \
the file and must not appear in the diff.
- Only files whose full contents you were shown. If the fix needs a file you \
cannot see, that is a "low" confidence diagnosis, not a guessed diff.
- Smallest change that makes CI pass. No drive-by edits.

Reply with a single JSON object and nothing else:

{
  "root_cause": "What actually broke and why, in 2-5 sentences. Name the \
failing step, the specific error, and the mechanism. Not a restatement of the \
error text.",
  "confidence": "high" | "medium" | "low",
  "confidence_reason": "One sentence on why that level, referring to the \
evidence you had.",
  "fix_available": true | false,
  "summary": "Imperative, <=60 characters, for the PR title. e.g. 'pin \
httpx to 0.28.1 in the api dev requirements'",
  "commit_message": "Conventional-commit subject line (fix:/ci:/infra:/docs:), \
optionally a blank line and a short body.",
  "diff": "The unified diff as a single string, or \\"\\" when fix_available is false.",
  "files_changed": ["repo/relative/path", ...],
  "not_changed": [
    {"considered": "The adjacent change you deliberately did not make",
     "why": "Why it was out of scope or would have been wrong"}
  ],
  "verification": "How a reviewer can confirm this fix works — the command to \
run or the step to watch."
}

`not_changed` must contain at least one real entry. If you truly considered \
nothing else, say what you deliberately left alone and why that was right.\
"""


USER_TEMPLATE = """\
## Failed run

repository:   {repo}
workflow:     {workflow}
run:          #{run_number} (id {run_id})
branch:       {branch}
commit:       {sha}
run url:      {run_url}

failing job:  {job_name}
failing step: {step_name}

## Repository inventory (paths only)

{inventory}

## Files provided in full

These are the complete current contents of the files most likely to be \
relevant, taken from the failing commit. Line numbers are a reading aid and are \
not part of the files.

{file_context}

{context_note}

## The commit that failed

This is what the failing commit changed. CI was green before it and red after,
so the cause is usually visible here. Read it before the log.

```
{commit_diff}
```

## Failing job log (bounded window around the error)

```
{log_window}
```

Diagnose this failure and respond with the JSON object described above.\
"""


def build_user_prompt(
    repo: str,
    workflow: str,
    run_id: str,
    run_number: str,
    branch: str,
    sha: str,
    run_url: str,
    job_name: str,
    step_name: str,
    inventory: str,
    file_context: str,
    included_paths: list[str],
    candidate_paths: list[str],
    log_window: str,
    commit_diff: str = "",
) -> str:
    """Assemble the user message, stating plainly what the model can and cannot see."""
    if not included_paths:
        note = (
            "NOTE: no file contents could be resolved from the log. You are "
            "working from the log alone — this is a 'low' confidence situation "
            "unless the log is unusually explicit."
        )
    elif set(included_paths) != set(candidate_paths):
        missing = ", ".join(sorted(set(candidate_paths) - set(included_paths)))
        note = (
            f"NOTE: these files were also implicated but did not fit in the "
            f"context budget: {missing}. Do not write a diff against them."
        )
    else:
        note = "NOTE: every file implicated by the log is shown above in full."

    return USER_TEMPLATE.format(
        repo=repo,
        workflow=workflow,
        run_id=run_id,
        run_number=run_number,
        branch=branch,
        sha=sha,
        run_url=run_url,
        job_name=job_name,
        step_name=step_name,
        inventory=inventory or "(unavailable)",
        file_context=file_context or "(none resolved)",
        context_note=note,
        commit_diff=commit_diff or "(commit diff unavailable)",
        log_window=log_window or "(log unavailable — it may have expired)",
    )
