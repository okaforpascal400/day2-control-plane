"""Reading a Renovate PR: the bump, the notes, and whether we use the thing."""

from __future__ import annotations

import subprocess

import pytest
from upgrade import evidence

from tests.conftest import PIP_DIFF, RENOVATE_BODY

IMAGE_DIFF = (
    "--- a/app/api/Dockerfile\n"
    "+++ b/app/api/Dockerfile\n"
    "@@ -3,1 +3,1 @@\n"
    "-ARG PYTHON_IMAGE=python:3.12-slim@sha256:" + "11" * 32 + "\n"
    "+ARG PYTHON_IMAGE=python:3.13-slim@sha256:" + "22" * 32 + "\n"
)

ACTION_DIFF = (
    "--- a/.github/workflows/ci.yml\n"
    "+++ b/.github/workflows/ci.yml\n"
    "@@ -37,1 +37,1 @@\n"
    "-      uses: actions/checkout@aaaaaaa # v5\n"
    "+      uses: actions/checkout@bbbbbbb # v6\n"
)


# ------------------------------------------------------- the dependency change


def test_a_pip_bump_is_read_out_of_the_diff_not_the_title():
    changes = evidence.parse_dependency_changes(PIP_DIFF)
    assert len(changes) == 1
    change = changes[0]
    assert (change.kind, change.name, change.old, change.new) == (
        "pip",
        "fastapi",
        "0.139.2",
        "0.140.0",
    )
    assert change.path == "app/api/requirements.txt"


def test_a_base_image_bump_is_recognised():
    change = evidence.parse_dependency_changes(IMAGE_DIFF)[0]
    assert change.kind == "image"
    assert change.old.startswith("3.12-slim") and change.new.startswith("3.13-slim")


def test_a_pinned_action_bump_is_recognised():
    change = evidence.parse_dependency_changes(ACTION_DIFF)[0]
    assert (change.kind, change.name) == ("action", "actions/checkout")
    assert change.path == ".github/workflows/ci.yml"


def test_a_removal_with_no_replacement_is_not_an_upgrade():
    """Dropping a dependency is a different change and must not be described as a bump."""
    removal = (
        "--- a/app/api/requirements.txt\n"
        "+++ b/app/api/requirements.txt\n"
        "@@ -1,2 +1,1 @@\n"
        "-fastapi==0.139.2\n"
        " uvicorn[standard]==0.51.0\n"
    )
    assert evidence.parse_dependency_changes(removal) == []


def test_a_diff_that_moves_no_pin_yields_nothing():
    plain = "--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    assert evidence.parse_dependency_changes(plain) == []


def test_the_dependency_table_renders_for_a_reviewer():
    table = evidence.render_dependency_table(evidence.parse_dependency_changes(PIP_DIFF))
    assert "`fastapi`" in table and "`0.139.2`" in table and "`0.140.0`" in table


# --------------------------------------------------------------- release notes


def test_renovates_release_notes_are_extracted():
    notes = evidence.extract_release_notes(RENOVATE_BODY)
    assert "Depends` no longer caches" in notes
    assert "Mend Renovate" not in notes  # the footer is not release notes


def test_a_body_with_no_notes_section_yields_empty_not_a_guess():
    """The agent must be able to say "I was given no notes" and mean it."""
    assert evidence.extract_release_notes("Just a bump, no details.") == ""
    assert evidence.extract_release_notes("") == ""


def test_release_notes_are_bounded():
    body = "<details><summary>Release Notes</summary>" + "x" * 50_000 + "</details>"
    notes = evidence.extract_release_notes(body, max_chars=100)
    assert len(notes) < 200
    assert notes.endswith("…(release notes truncated)…")


def test_the_diff_is_bounded():
    assert evidence.bound_diff("y" * 50, max_chars=100) == "y" * 50
    assert evidence.bound_diff("y" * 500, max_chars=100).endswith("…(diff truncated)…")


# ------------------------------------------------------------------- our usage


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app" / "api").mkdir(parents=True)
    (tmp_path / "agents" / "core" / "tests").mkdir(parents=True)
    (tmp_path / "app" / "api" / "requirements.txt").write_text("fastapi==0.139.2\n")
    (tmp_path / "app" / "api" / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n"
    )
    (tmp_path / "agents" / "core" / "tests" / "test_diffs.py").write_text(
        "FIXTURE = '-fastapi==0.139.2'\n"
    )
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
    ):
        subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def change(**kw):
    base = {
        "kind": "pip",
        "name": "fastapi",
        "old": "0.139.2",
        "new": "0.140.0",
        "path": "app/api/requirements.txt",
    }
    base.update(kw)
    return evidence.DependencyChange(**base)


def test_usage_finds_the_application_code(repo):
    tracked = evidence.tracked_files(str(repo))
    usage, hits = evidence.find_usage(str(repo), change(), tracked)
    assert hits == 3
    assert "app/api/main.py" in usage
    assert "from fastapi import FastAPI" in usage


def test_shipped_code_is_ranked_above_test_fixtures(repo):
    """A test fixture mentioning the package says nothing about our runtime risk."""
    tracked = evidence.tracked_files(str(repo))
    usage, _ = evidence.find_usage(str(repo), change(), tracked)
    assert usage.index("app/api/main.py") < usage.index("test_diffs.py")


def test_nothing_is_excluded_only_ordered(repo):
    """Ranking must never hide a match: that is how a search misses the one
    that mattered."""
    tracked = evidence.tracked_files(str(repo))
    usage, hits = evidence.find_usage(str(repo), change(), tracked)
    assert "test_diffs.py" in usage
    assert hits == 3


def test_an_unused_dependency_reports_zero_hits(repo):
    tracked = evidence.tracked_files(str(repo))
    usage, hits = evidence.find_usage(
        str(repo), change(name="nothing-uses-this"), tracked
    )
    assert hits == 0
    assert usage == ""


def test_a_hyphenated_package_is_searched_by_its_import_name():
    """`prometheus-fastapi-instrumentator` is imported with underscores."""
    terms = evidence.usage_terms(change(name="prometheus-fastapi-instrumentator"))
    assert "prometheus_fastapi_instrumentator" in terms


def test_an_action_is_searched_by_its_short_name():
    terms = evidence.usage_terms(change(kind="action", name="actions/checkout"))
    assert "checkout" in terms and "actions/checkout" in terms
