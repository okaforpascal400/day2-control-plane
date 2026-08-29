"""Turning a failed CI run into a bounded, relevant prompt.

Two jobs, both about *bounding*:

* **The log window.** A CI job log is tens of thousands of lines, almost all of
  it dependency resolution and setup noise. Sending the whole thing is slow,
  expensive, and — worse — buries the twenty lines that matter. So the window
  is anchored on the last `##[error]` marker GitHub writes, with a fixed number
  of lines either side and a hard character cap.

* **The file context.** A model cannot write a diff that applies against files
  it has not seen: it will invent line numbers and context, and `git apply`
  will reject it. So the paths named in the failure window are resolved against
  the repo's tracked files and their contents are included verbatim, up to a
  cap. Everything else is offered as a path-only inventory.

Both caps are constants here rather than parameters at the call site, so the
worst-case prompt size — and therefore the worst-case cost of a triage run — is
a property of this module that a reviewer can read off in one place.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# GitHub prefixes every log line with an RFC3339 timestamp. It carries no
# diagnostic value once the window is chosen and costs ~29 characters a line.
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?")
ERROR_MARKER = "##[error]"
GROUP_MARKER = re.compile(r"^##\[(group|endgroup|debug)\]")

LINES_BEFORE_ERROR = 160
LINES_AFTER_ERROR = 40
MAX_LOG_CHARS = 16_000

MAX_CONTEXT_FILES = 8
MAX_FILE_LINES = 400
MAX_CONTEXT_BYTES = 40_000
MAX_INVENTORY_FILES = 400
MAX_COMMIT_DIFF_CHARS = 8_000

# Extensions worth reading into the prompt. Binaries and lockfiles are noise a
# minimal fix should not be touching anyway.
CONTEXT_SUFFIXES = (
    ".py",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".json",
    ".sh",
    ".tpl",
)
PATH_TOKEN = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+")

Runner = Callable[[Sequence[str], str | None, str], subprocess.CompletedProcess]


def _default_runner(
    argv: Sequence[str], stdin: str | None, cwd: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), input=stdin, cwd=cwd, capture_output=True, text=True, check=False
    )


# ------------------------------------------------------------------ job/steps


def failing_job(jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first job that actually failed.

    `cancelled` is skipped deliberately: when one matrix leg fails, the others
    are cancelled, and diagnosing a cancellation would explain a symptom of the
    real failure rather than the failure.
    """
    for job in jobs:
        if job.get("conclusion") == "failure":
            return job
    return None


def failing_step(job: dict[str, Any]) -> dict[str, Any] | None:
    """The first failed step within a job — the thing to name in the PR title."""
    for step in job.get("steps") or []:
        if step.get("conclusion") == "failure":
            return step
    return None


# ------------------------------------------------------------------ log window


def clean_log_lines(raw: str) -> list[str]:
    """Strip per-line timestamps and Actions' own grouping chrome."""
    lines = []
    for line in raw.splitlines():
        stripped = TIMESTAMP.sub("", line)
        if GROUP_MARKER.match(stripped):
            continue
        lines.append(stripped.rstrip())
    return lines


def extract_failure_window(
    raw: str,
    before: int = LINES_BEFORE_ERROR,
    after: int = LINES_AFTER_ERROR,
    max_chars: int = MAX_LOG_CHARS,
) -> str:
    """A bounded slice of the log around the failure, never the whole log.

    Anchors on the *last* `##[error]`: when a step fails, Actions emits an
    error line at the point of failure and another when the job as a whole is
    marked failed, and the later one sits closest to the summary the runner
    prints. With no marker at all — a step that failed without one — the tail
    is the best available guess and is used instead.
    """
    lines = clean_log_lines(raw)
    if not lines:
        return ""

    anchors = [i for i, line in enumerate(lines) if ERROR_MARKER in line]
    end = min(len(lines), (anchors[-1] + 1 + after) if anchors else len(lines))
    start = max(0, end - after - before) if anchors else max(0, len(lines) - before)

    window = "\n".join(lines[start:end])
    if len(window) > max_chars:
        # Keep the tail: the failure and its traceback sit at the end of the
        # window, and truncating from the front loses only earlier setup noise.
        window = "…(earlier log truncated)…\n" + window[-max_chars:]
    return window


def head_commit_diff(
    sha: str,
    repo_root: str = ".",
    max_chars: int = MAX_COMMIT_DIFF_CHARS,
    runner: Runner = _default_runner,
) -> str:
    """What the failing commit actually changed.

    The single most useful piece of evidence after the error itself, and the
    one a log never contains: CI went from green to red, so the cause is
    usually *in here*. Without it the agent has to infer intent from the
    current file contents alone and cannot tell a deliberate change from a
    mistake — a flipped assertion looks exactly like a specification.

    Best-effort: a shallow clone or an expired ref yields "" rather than an
    error, and the agent falls back to the log.
    """
    result = runner(
        ["git", "show", "--no-color", "--stat", "--patch", sha], None, repo_root
    )
    if result.returncode != 0:
        return ""
    diff = (result.stdout or "").strip()
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n…(commit diff truncated)…"
    return diff


# --------------------------------------------------------------- file context


def tracked_files(repo_root: str = ".", runner: Runner = _default_runner) -> list[str]:
    result = runner(["git", "ls-files"], None, repo_root)
    if result.returncode != 0:
        return []
    return [line for line in (result.stdout or "").splitlines() if line]


def candidate_paths(text: str, tracked: Sequence[str]) -> list[str]:
    """Tracked files the failure window points at, most-specific first.

    A tool names a path the way *it* saw it: `app/api/api/main.py` from one
    root, `api/main.py` from a pytest traceback rooted elsewhere, and a bare
    `requirements.txt` from a `pip install -r` run inside `app/api`. So matches
    are collected in three tiers and concatenated in that order — exact path,
    then path-suffix, then basename-only. The tiers matter: a bare basename can
    match several real files, and the ambiguous candidates must not crowd out
    the one the log named precisely.
    """
    tracked_set = set(tracked)
    exact: list[str] = []
    suffix: list[str] = []
    basename: list[str] = []

    for token in dict.fromkeys(PATH_TOKEN.findall(text)):
        token = token.strip("./")
        if not token.endswith(CONTEXT_SUFFIXES):
            continue
        if token in tracked_set:
            exact.append(token)
        elif "/" in token:
            suffix.extend(p for p in tracked if p.endswith("/" + token))
        else:
            basename.extend(p for p in tracked if p.rsplit("/", 1)[-1] == token)

    ordered = list(dict.fromkeys([*exact, *suffix, *basename]))
    return ordered[:MAX_CONTEXT_FILES]


def read_excerpt(repo_root: str, path: str, max_lines: int = MAX_FILE_LINES) -> str:
    try:
        text = (Path(repo_root) / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], f"…(file truncated at {max_lines} lines)…"]
    # Numbered so the model can reason about line positions when writing hunks.
    return "\n".join(f"{n:>5}\t{line}" for n, line in enumerate(lines, 1))


def build_file_context(
    repo_root: str, paths: Sequence[str], max_bytes: int = MAX_CONTEXT_BYTES
) -> tuple[str, list[str]]:
    """Verbatim contents of the candidate files, under a total byte cap.

    Returns the rendered block and the paths that actually fitted, so the
    prompt can state plainly which files the model has and has not seen — a
    model that knows its context is partial writes a smaller, likelier diff.
    """
    blocks: list[str] = []
    included: list[str] = []
    used = 0
    for path in paths:
        excerpt = read_excerpt(repo_root, path)
        if not excerpt:
            continue
        block = f"----- {path} -----\n{excerpt}\n"
        if used + len(block) > max_bytes and included:
            break
        blocks.append(block)
        included.append(path)
        used += len(block)
    return "\n".join(blocks), included


def build_inventory(tracked: Sequence[str]) -> str:
    """Path-only listing, so the model knows what exists without reading it."""
    listed = [p for p in tracked if p.endswith(CONTEXT_SUFFIXES)][:MAX_INVENTORY_FILES]
    return "\n".join(listed)
