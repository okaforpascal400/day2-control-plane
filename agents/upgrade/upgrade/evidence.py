"""Reading a Renovate PR, and finding out whether we actually use the thing.

Three extractions, each with a different failure mode worth naming:

* **The dependency change** comes from the PR's *diff*, not its title. A title
  is prose Renovate composes and its format is configurable; the diff is the
  change itself. A `-package==1.2.3` / `+package==1.2.4` pair, or a moved
  `ARG *_IMAGE=` pin, or a bumped `uses:` SHA — those are facts.

* **The release notes** come from the PR *body*, because Renovate has already
  fetched them. This is a deliberate boundary: the agent holds no scope to
  reach an arbitrary upstream repository, and giving it one so it could fetch a
  changelog itself would be a much larger grant than the job needs. Renovate
  did the fetch; the agent reads what Renovate wrote. When the body has no
  notes section the agent says so and the model is told to lower confidence —
  never to fall back on recollection.

* **Our usage** is a plain substring search across tracked text files for the
  package name and its import form. Deliberately dumb, because a clever search
  that silently missed a usage would produce a confident "we don't use this",
  which is the single most dangerous output this agent can produce. Over-
  matching costs prompt tokens; under-matching costs correctness.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_USAGE_FILES = 10
MAX_USAGE_LINES_PER_FILE = 12
MAX_USAGE_BYTES = 20_000
MAX_NOTES_CHARS = 12_000
MAX_DIFF_CHARS = 8_000

USAGE_SUFFIXES = (".py", ".txt", ".toml", ".cfg", ".yaml", ".yml", "Dockerfile")

# Renovate wraps release notes in a collapsible <details> block whose summary
# says "Release Notes". Matched leniently on the marker and then bounded, so a
# change to the surrounding markup degrades to "no notes" rather than to a
# silently truncated section presented as complete.
RELEASE_NOTES = re.compile(
    r"<details>\s*<summary>Release Notes(?P<body>.*?)</details>",
    re.DOTALL | re.IGNORECASE,
)

# `-fastapi==0.139.2` / `+fastapi==0.139.3` in a requirements diff.
PIP_CHANGE = re.compile(
    r"^-(?P<name>[A-Za-z0-9._-]+)(?P<extras>\[[^\]]*\])?==(?P<old>\S+)\s*$"
)
PIP_NEW = re.compile(
    r"^\+(?P<name>[A-Za-z0-9._-]+)(?P<extras>\[[^\]]*\])?==(?P<new>\S+)\s*$"
)

# `-ARG PYTHON_IMAGE=python:3.12-slim@sha256:...`
IMAGE_CHANGE = re.compile(
    r"^-\s*ARG\s+(?P<arg>[A-Z][A-Z0-9_]*_IMAGE)=(?P<name>[^\s:]+):(?P<old>\S+)\s*$"
)
IMAGE_NEW = re.compile(
    r"^\+\s*ARG\s+(?P<arg>[A-Z][A-Z0-9_]*_IMAGE)=(?P<name>[^\s:]+):(?P<new>\S+)\s*$"
)

# `-  uses: actions/checkout@<sha> # v5`
ACTION_CHANGE = re.compile(r"^-\s*uses:\s*(?P<name>[^@\s]+)@(?P<old>\S+)")
ACTION_NEW = re.compile(r"^\+\s*uses:\s*(?P<name>[^@\s]+)@(?P<new>\S+)")

Runner = Callable[[Sequence[str], str | None, str], subprocess.CompletedProcess]


def _default_runner(
    argv: Sequence[str], stdin: str | None, cwd: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), input=stdin, cwd=cwd, capture_output=True, text=True, check=False
    )


@dataclass(frozen=True)
class DependencyChange:
    """One pin that moved, as read out of the diff."""

    kind: str  # pip | image | action
    name: str
    old: str
    new: str
    path: str

    def describe(self) -> str:
        return (
            f"| `{self.name}` | {self.kind} | `{self.old}` | "
            f"`{self.new}` | `{self.path}` |"
        )


def parse_dependency_changes(diff: str) -> list[DependencyChange]:
    """Every pin the PR moves, paired old-to-new within each file.

    Renovate's diffs are small and one-sided by construction — a bump removes
    one line and adds one — so pairing by name within a file is exact rather
    than heuristic. A removal with no matching addition is dropped: that is a
    deletion, not an upgrade, and describing it as one would be wrong.
    """
    path = ""
    removed: dict[tuple[str, str], tuple[str, str]] = {}
    added: dict[tuple[str, str], str] = {}

    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0].removeprefix("b/").strip()
            continue
        if line.startswith(("--- ", "diff --git", "index ", "@@")):
            continue

        for kind, drop, add in (
            ("pip", PIP_CHANGE, PIP_NEW),
            ("image", IMAGE_CHANGE, IMAGE_NEW),
            ("action", ACTION_CHANGE, ACTION_NEW),
        ):
            hit = drop.match(line)
            if hit:
                removed[(kind, hit.group("name"))] = (hit.group("old"), path)
                break
            hit = add.match(line)
            if hit:
                added[(kind, hit.group("name"))] = hit.group("new")
                break

    changes: list[DependencyChange] = []
    for key, (old, where) in removed.items():
        new = added.get(key)
        if new is None or new == old:
            continue
        kind, name = key
        changes.append(DependencyChange(kind, name, old, new, where))
    return changes


def render_dependency_table(changes: Sequence[DependencyChange]) -> str:
    if not changes:
        return ""
    head = "| Dependency | Kind | From | To | Pinned in |\n|---|---|---|---|---|"
    return "\n".join([head, *(c.describe() for c in changes)])


def extract_release_notes(body: str, max_chars: int = MAX_NOTES_CHARS) -> str:
    """Renovate's own release-notes block, bounded. "" when there is none."""
    if not body:
        return ""
    match = RELEASE_NOTES.search(body)
    if not match:
        return ""
    notes = match.group("body").strip()
    if len(notes) > max_chars:
        # Keep the head: release notes lead with the newest version, which is
        # the end of the jump the reviewer is being asked about.
        notes = notes[:max_chars] + "\n…(release notes truncated)…"
    return notes


def bound_diff(diff: str, max_chars: int = MAX_DIFF_CHARS) -> str:
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + "\n…(diff truncated)…"


# ---------------------------------------------------------------- our usage


def tracked_files(repo_root: str = ".", runner: Runner = _default_runner) -> list[str]:
    result = runner(["git", "ls-files"], None, repo_root)
    if result.returncode != 0:
        return []
    return [line for line in (result.stdout or "").splitlines() if line]


def usage_terms(change: DependencyChange) -> list[str]:
    """The strings worth searching for, given what kind of pin moved.

    A pip package is imported under a normalised name (`prometheus-fastapi-
    instrumentator` becomes `prometheus_fastapi_instrumentator`), so both forms
    are searched. An image is referenced by its repository name, an action by
    its `owner/repo`.
    """
    terms = {change.name.lower()}
    if change.kind == "pip":
        terms.add(change.name.lower().replace("-", "_"))
    if "/" in change.name:
        terms.add(change.name.rsplit("/", 1)[-1].lower())
    return sorted(t for t in terms if len(t) > 2)


def _usage_rank(path: str) -> tuple[int, str]:
    """Order matches by how much they say about our runtime risk.

    Nothing is excluded — excluding is how a search silently misses the usage
    that mattered. But the cap has to fall somewhere, and it must not fall on
    the shipped code. A `fastapi` bump matches this repository's own agent test
    fixtures, which contain requirements-file *examples*; those are real
    matches and worthless as evidence of how we use the dependency. So the
    application and what deploys it sort first, tests and fixtures sort last,
    and the cap trims from the least informative end.
    """
    if path.startswith(("app/", "deploy/", "infra/")):
        area = 0
    elif path.startswith("scripts/"):
        area = 1
    elif path.startswith(".github/"):
        area = 2
    else:
        area = 3
    is_test = 1 if ("/tests/" in path or "/test_" in path or "conftest" in path) else 0
    return (is_test, area, path)


def find_usage(
    repo_root: str,
    change: DependencyChange,
    tracked: Sequence[str],
    max_files: int = MAX_USAGE_FILES,
    max_bytes: int = MAX_USAGE_BYTES,
) -> tuple[str, int]:
    """Every tracked file mentioning the dependency, with the matching lines.

    Returns the rendered block and the number of files that matched, so the
    prompt can state plainly whether "we do not use this" is a conclusion drawn
    from evidence or from an empty search. `matched` counts every hit, including
    the ones the cap kept out of the rendered block — the count and the block
    answer different questions and must not be conflated.
    """
    terms = usage_terms(change)
    blocks: list[str] = []
    matched = 0
    used = 0

    for path in sorted(tracked, key=_usage_rank):
        if not path.endswith(USAGE_SUFFIXES):
            continue
        try:
            text = (Path(repo_root) / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lowered = text.lower()
        if not any(term in lowered for term in terms):
            continue

        matched += 1
        if len(blocks) >= max_files or used >= max_bytes:
            continue
        hits = [
            f"{n:>5}\t{line.rstrip()}"
            for n, line in enumerate(text.splitlines(), 1)
            if any(term in line.lower() for term in terms)
        ][:MAX_USAGE_LINES_PER_FILE]
        block = f"----- {path} -----\n" + "\n".join(hits) + "\n"
        blocks.append(block)
        used += len(block)

    return "\n".join(blocks), matched
