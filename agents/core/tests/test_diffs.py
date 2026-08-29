"""Output verification: parse, permit, then prove the patch actually applies."""

from __future__ import annotations

import pytest

from day2_agents.diffs import DiffRejected, diff_paths, validate_diff
from tests.conftest import fail, ok

GOOD = """diff --git a/app/api/requirements.txt b/app/api/requirements.txt
--- a/app/api/requirements.txt
+++ b/app/api/requirements.txt
@@ -1,2 +1,2 @@
-fastapi==0.139.2
+fastapi==0.139.3
 uvicorn[standard]==0.51.0
"""

NEW_FILE = """diff --git a/app/api/tests/test_new.py b/app/api/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/app/api/tests/test_new.py
@@ -0,0 +1 @@
+assert True
"""

WORKFLOW = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-name: ci
+name: ci-disabled
"""

NO_DIFF_GIT_LINE = """--- a/app/worker/config.py
+++ b/app/worker/config.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def test_paths_are_extracted_from_headers():
    assert diff_paths(GOOD) == ["app/api/requirements.txt"]


def test_dev_null_is_not_treated_as_a_path():
    assert diff_paths(NEW_FILE) == ["app/api/tests/test_new.py"]


def test_paths_are_found_without_a_diff_git_line():
    """Models often emit bare ---/+++ diffs; those must still be accounted for."""
    assert diff_paths(NO_DIFF_GIT_LINE) == ["app/worker/config.py"]


@pytest.mark.parametrize("text", ["", "   \n", "not a diff at all"])
def test_unparseable_input_fails_closed(text):
    with pytest.raises(DiffRejected):
        diff_paths(text)


def test_a_clean_diff_passes_all_three_checks(runner):
    runner.results["git apply --check"] = ok()
    assert validate_diff(GOOD, runner=runner) == ["app/api/requirements.txt"]
    assert runner.ran("git", "apply", "--check")


def test_a_workflow_diff_is_refused_before_git_is_ever_called(runner):
    with pytest.raises(DiffRejected, match="not agent-editable"):
        validate_diff(WORKFLOW, runner=runner)
    assert runner.calls == []


def test_a_hallucinated_diff_is_refused_by_git_apply(runner):
    """Plausible text, wrong context — this is the check that catches it."""
    runner.results["git apply"] = fail("error: patch failed: app/api/requirements.txt:1")
    with pytest.raises(DiffRejected, match="does not apply cleanly"):
        validate_diff(GOOD, runner=runner)


def test_check_never_writes_to_the_tree(runner):
    runner.results["git apply --check"] = ok()
    validate_diff(GOOD, runner=runner)
    for call in runner.calls:
        assert "--check" in call, f"{call} would have modified the tree"


def test_a_diff_missing_its_trailing_newline_still_applies(runner):
    runner.results["git apply --check"] = ok()
    validate_diff(GOOD.rstrip("\n"), runner=runner)
    assert runner.stdins[0].endswith("\n")
