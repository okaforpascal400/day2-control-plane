"""Read-only git history: log, show, blame.

**On "no shell execution", stated honestly.** This module runs `git` as a
subprocess. What it never does is hand a string to a shell. Those are different
claims and conflating them is how command injection gets shipped, so here is
the precise property this module enforces:

* `subprocess.run` is called with an **argv list** and `shell=False` (the
  default, set explicitly here so it is visible). No `/bin/sh` is involved, so
  `;`, `|`, `$(...)`, backticks and globs have no meaning — they are literal
  characters in one argument.
* The **subcommand is an allowlist**: `log`, `show`, `blame`. Not a denylist.
  `git push`, `git commit`, `git config`, `git gc` are unreachable because they
  are not in the tuple, not because they were thought of and blocked.
* **Every flag is chosen here, never by the caller.** The model supplies a ref,
  a path and a count; it cannot supply an option. This closes the flag-injection
  class where a "path" of `--output=/etc/cron.d/x` or `--upload-pack=...` turns
  a read into a write. Values that could start with `-` are additionally passed
  after `--`.
* **`GIT_CONFIG_GLOBAL=/dev/null` and a scrubbed environment.** Git honours
  config that can execute code — `core.pager`, `core.editor`, `diff.external`,
  `core.fsmonitor` — and an attacker-controlled repo config could turn a read
  into execution. Disabling external config removes that surface, and the
  environment is rebuilt from scratch rather than inherited so nothing carries
  a token into the subprocess.
* **A timeout**, so a pathological blame on a huge file cannot hang the server.

`git show` is the one that needs a second look, because it can print a blob at
an arbitrary path — `git show HEAD:.env` would read a file the `files.py` jail
refuses. So `show` here is restricted to **commit metadata and diffs**, and
`ref:path` blob syntax is rejected outright. Reading file *contents* is
`read_runbook`'s job, and it has the jail.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from day2_mcp.limits import MAX_COMMITS, Truncation, cap_list, enforce_result_bytes
from day2_mcp.provenance import Provenance

ALLOWED_SUBCOMMANDS: tuple[str, ...] = ("log", "show", "blame")

GIT_TIMEOUT_SECONDS = 15.0
MAX_DIFF_BYTES = 120_000
MAX_BLAME_LINES = 400

# A ref may name a branch, tag, sha, or a relative form like HEAD~3 or
# origin/main. It may not contain a colon (blob syntax), whitespace, or
# anything that would let it be read as an option.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@\-]{0,200}$")


class GitRefused(RuntimeError):
    """The requested git operation is outside what this tool may do."""


def _validate_ref(ref: str) -> str:
    candidate = (ref or "").strip()
    if not candidate:
        raise GitRefused("a git ref is required")
    if ":" in candidate:
        raise GitRefused(
            f"refusing ref {ref!r}: 'ref:path' blob syntax would read a file's "
            "contents, bypassing the path jail. Use read_runbook for file contents."
        )
    if not _REF_RE.match(candidate):
        raise GitRefused(
            f"refusing ref {ref!r}: refs must look like a branch, tag or sha"
        )
    return candidate


def _validate_repo_path(repo_root: Path, path: str) -> str:
    """A path argument must stay inside the repo, like the files jail."""
    candidate = (path or "").strip()
    if not candidate:
        raise GitRefused("a path is required")
    if Path(candidate).is_absolute():
        raise GitRefused(f"refusing absolute path {path!r}")
    root = repo_root.resolve()
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise GitRefused(f"refusing {path!r}: it resolves outside the repository")
    return str(target.relative_to(root))


def _run_git(repo_root: Path, argv: list[str]) -> str:
    """Run one git subcommand with no shell and a scrubbed environment."""
    if not argv or argv[0] not in ALLOWED_SUBCOMMANDS:
        raise GitRefused(
            f"refusing git {argv[0] if argv else '(none)'!r}; "
            f"this tool runs only: {', '.join(ALLOWED_SUBCOMMANDS)}"
        )

    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        raise GitRefused(f"{root} is not a git repository")

    # Rebuilt from nothing: no inherited GH_TOKEN, ANTHROPIC_API_KEY, AWS creds.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",  # never block waiting for credentials
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *argv],
            shell=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise GitRefused(
            f"git {argv[0]} timed out after {GIT_TIMEOUT_SECONDS}s"
        ) from None
    except FileNotFoundError:
        raise GitRefused("git is not installed on this host") from None

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip().splitlines()
        detail = stderr[0] if stderr else f"exit {completed.returncode}"
        raise GitRefused(f"git {argv[0]} failed: {detail}")
    return completed.stdout


_LOG_SEP = "\x1e"  # ASCII record separator: cannot appear in a commit message
_LOG_FORMAT = _LOG_SEP.join(["%H", "%an", "%aI", "%s", "%b"]) + "\x1d"


def git_history(
    repo_root: Path,
    mode: str = "log",
    ref: str | None = None,
    path: str | None = None,
    max_count: int | None = None,
) -> dict[str, Any]:
    """Read commit history, a commit's diff, or line-by-line blame.

    `mode` selects which of the three allowed subcommands runs. It is a closed
    set checked here as well as in `_run_git`, so a typo produces a clear error
    rather than an obscure git one.
    """
    trunc = Truncation()
    root = Path(repo_root)

    if mode == "log":
        count = max(1, min(int(max_count or 20), MAX_COMMITS))
        argv = ["log", f"--max-count={count}", f"--format={_LOG_FORMAT}", "--no-color"]
        if ref:
            argv.append(_validate_ref(ref))
        if path:
            # `--` ends option parsing: everything after it is a path, so a
            # path beginning with `-` cannot become a flag.
            argv.extend(["--", _validate_repo_path(root, path)])
        raw = _run_git(root, argv)

        commits = []
        for record in raw.split("\x1d"):
            if not record.strip():
                continue
            fields = record.strip("\n").split(_LOG_SEP)
            if len(fields) < 4:
                continue
            commits.append(
                {
                    "sha": fields[0],
                    "short_sha": fields[0][:7],
                    "author": fields[1],
                    "date": fields[2],
                    "subject": fields[3],
                    "body": fields[4].strip() if len(fields) > 4 else "",
                }
            )
        commits = cap_list(commits, MAX_COMMITS, "commits", trunc)

        query = f"git log --max-count={count}" + (f" {ref}" if ref else "")
        query += f" -- {path}" if path else ""
        prov = Provenance(source="git", query=query, endpoint=str(root.resolve()))
        payload = {
            "mode": "log",
            "commit_count": len(commits),
            "commits": commits,
            "provenance": prov.reference(),
            "citation_id": prov.citation_id(),
        }

    elif mode == "show":
        target = _validate_ref(ref or "HEAD")
        argv = [
            "show",
            "--no-color",
            "--stat",
            "--patch",
            f"--format={_LOG_FORMAT}",
            target,
        ]
        raw = _run_git(root, argv)
        header, _, diff = raw.partition("\x1d")
        fields = header.strip("\n").split(_LOG_SEP)
        if len(diff) > MAX_DIFF_BYTES:
            diff = diff[:MAX_DIFF_BYTES] + "\n…[diff truncated]"
            trunc.note(f"diff: truncated to {MAX_DIFF_BYTES} bytes")

        prov = Provenance(
            source="git", query=f"git show {target}", endpoint=str(root.resolve())
        )
        payload = {
            "mode": "show",
            "sha": fields[0] if fields else target,
            "author": fields[1] if len(fields) > 1 else None,
            "date": fields[2] if len(fields) > 2 else None,
            "subject": fields[3] if len(fields) > 3 else None,
            "body": fields[4].strip() if len(fields) > 4 else "",
            "diff": diff.lstrip("\n"),
            "provenance": prov.reference(),
            "citation_id": prov.citation_id(),
        }

    elif mode == "blame":
        if not path:
            raise GitRefused("blame requires a path")
        safe_path = _validate_repo_path(root, path)
        argv = ["blame", "--line-porcelain", "--no-color"]
        if ref:
            argv.append(_validate_ref(ref))
        argv.extend(["--", safe_path])
        raw = _run_git(root, argv)

        lines = _parse_blame_porcelain(raw)
        lines = cap_list(lines, MAX_BLAME_LINES, "blame lines", trunc)

        prov = Provenance(
            source="git",
            query=f"git blame{f' {ref}' if ref else ''} -- {safe_path}",
            endpoint=str(root.resolve()),
        )
        payload = {
            "mode": "blame",
            "path": safe_path,
            "line_count": len(lines),
            "lines": lines,
            "provenance": prov.reference(),
            "citation_id": prov.citation_id(),
        }

    else:
        raise GitRefused(f"unknown mode {mode!r}; git_history supports: log, show, blame")

    payload.update(trunc.as_payload())
    return enforce_result_bytes(payload, trunc)


def _parse_blame_porcelain(raw: str) -> list[dict[str, Any]]:
    """Parse `--line-porcelain` into one record per line.

    Porcelain is used rather than the human format because the human format is
    ambiguous: an author name containing a bracket or a date-like string makes
    it unparseable. Porcelain is designed for exactly this.
    """
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line:
            continue
        if line.startswith("\t"):
            current["line"] = line[1:]
            records.append(current)
            current = {}
            continue
        parts = line.split(" ", 1)
        head = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            current["sha"] = head
            current["short_sha"] = head[:7]
            fields = rest.split(" ")
            if len(fields) >= 2:
                current["line_number"] = int(fields[1])
        elif head == "author":
            current["author"] = rest
        elif head == "author-time":
            current["author_time"] = int(rest) if rest.isdigit() else rest
        elif head == "summary":
            current["summary"] = rest
    return records
