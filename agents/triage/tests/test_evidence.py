"""Evidence gathering: bounded, anchored on the failure, and honest about gaps."""

from __future__ import annotations

from triage import evidence

LOG = "\n".join(
    [
        *(
            f"2026-08-29T10:00:0{i % 10}.1234567Z setup noise line {i}"
            for i in range(400)
        ),
        "2026-08-29T10:01:00.1234567Z ##[group]Run pip install -r requirements-dev.txt",
        "2026-08-29T10:01:01.1234567Z ERROR: Could not find a version that satisfies "
        "the requirement httpx==0.99.99 (from -r requirements.txt (line 3))",
        "2026-08-29T10:01:02.1234567Z ##[error]Process completed with exit code 1.",
    ]
)


# The same three lines as `gh run view --log` renders them: every line carries
# a "<job>\t<step>\t" prefix, and the first one a UTF-8 BOM. The failure window
# has to survive both shapes, because the log has two transports and either may
# be the one that answered (see `GitHubHelper.get_job_log`).
BOM = "\ufeff"
RUN_VIEW_LOG = "\n".join(
    "pytest (api)\tInstall dependencies\t" + (BOM if i == 0 else "") + line
    for i, line in enumerate(LOG.splitlines())
)


def test_timestamps_and_group_chrome_are_stripped():
    lines = evidence.clean_log_lines(LOG)
    assert not any(line.startswith("2026-08-29T") for line in lines)
    assert not any(line.startswith("##[group]") for line in lines)


def test_the_run_view_line_prefix_and_bom_are_stripped_too():
    lines = evidence.clean_log_lines(RUN_VIEW_LOG)
    assert not any("\t" in line for line in lines)
    assert not any(line.startswith("\ufeff") for line in lines)
    assert not any(line.startswith("##[group]") for line in lines)
    # Identical output from both transports: the caller never has to know which
    # one answered.
    assert lines == evidence.clean_log_lines(LOG)


def test_the_window_survives_the_run_view_format():
    """A fallback log must cost nothing in prompt quality.

    Unstripped, the prefix repeats the job and step name on every one of the
    200 lines in the window — a third of the log budget spent restating two
    facts the prompt already states once, and the char cap then truncates real
    evidence to make room for it.
    """
    window = evidence.extract_failure_window(RUN_VIEW_LOG)
    assert "Could not find a version" in window
    assert "##[error]" in window
    assert "Install dependencies\t" not in window
    assert window == evidence.extract_failure_window(LOG)


def test_a_tab_inside_a_real_log_line_is_left_alone():
    """The prefix is only stripped when a timestamp follows it."""
    lines = evidence.clean_log_lines("make:\tTarget\tfoo is up to date")
    assert lines == ["make:\tTarget\tfoo is up to date"]


def test_window_is_anchored_on_the_error_and_keeps_the_cause():
    window = evidence.extract_failure_window(LOG)
    assert "Could not find a version" in window
    assert "##[error]" in window


def test_window_is_bounded_far_below_the_whole_log():
    window = evidence.extract_failure_window(LOG)
    assert len(window) < len(LOG) / 2
    assert len(window.splitlines()) <= (
        evidence.LINES_BEFORE_ERROR + evidence.LINES_AFTER_ERROR + 2
    )


def test_the_character_cap_is_hard():
    huge = "\n".join(["x" * 200 for _ in range(500)] + ["##[error]boom"])
    window = evidence.extract_failure_window(huge, max_chars=1000)
    assert len(window) <= 1000 + len("…(earlier log truncated)…\n")
    assert "boom" in window


def test_a_log_with_no_error_marker_falls_back_to_the_tail():
    plain = "\n".join(f"line {i}" for i in range(500))
    window = evidence.extract_failure_window(plain)
    assert "line 499" in window
    assert "line 0" not in window


def test_an_empty_log_yields_an_empty_window():
    assert evidence.extract_failure_window("") == ""


def test_failing_job_skips_successes_and_cancellations():
    jobs = [
        {"name": "ruff", "conclusion": "success"},
        {"name": "pytest (worker)", "conclusion": "cancelled"},
        {"name": "pytest (api)", "conclusion": "failure"},
    ]
    assert evidence.failing_job(jobs)["name"] == "pytest (api)"
    assert evidence.failing_job([{"conclusion": "success"}]) is None


def test_failing_step_names_the_step_not_the_job():
    job = {
        "steps": [
            {"name": "Checkout", "conclusion": "success"},
            {"name": "Install dependencies", "conclusion": "failure"},
            {"name": "Run tests", "conclusion": "skipped"},
        ]
    }
    assert evidence.failing_step(job)["name"] == "Install dependencies"
    assert evidence.failing_step({"steps": []}) is None


TRACKED = [
    "app/api/requirements.txt",
    "app/api/requirements-dev.txt",
    "app/worker/requirements.txt",
    "app/api/api/main.py",
    "deploy/helm/values.yaml",
    "README.md",
]


def test_an_exact_path_is_resolved():
    assert "app/api/api/main.py" in evidence.candidate_paths(
        'File "app/api/api/main.py", line 12', TRACKED
    )


def test_a_suffix_path_is_resolved():
    """pytest tracebacks are relative to the job's working directory."""
    assert "app/api/api/main.py" in evidence.candidate_paths("api/main.py:12", TRACKED)


def test_a_bare_basename_resolves_to_every_candidate():
    """`pip install -r requirements.txt` names no directory; offer both."""
    found = evidence.candidate_paths("(from -r requirements.txt (line 3))", TRACKED)
    assert "app/api/requirements.txt" in found
    assert "app/worker/requirements.txt" in found


def test_specific_matches_sort_ahead_of_ambiguous_ones():
    found = evidence.candidate_paths(
        "requirements.txt then app/worker/requirements.txt", TRACKED
    )
    assert found[0] == "app/worker/requirements.txt"


def test_candidates_are_capped():
    tracked = [f"pkg/mod{i}.py" for i in range(50)]
    text = " ".join(f"mod{i}.py" for i in range(50))
    assert len(evidence.candidate_paths(text, tracked)) == evidence.MAX_CONTEXT_FILES


def test_unreadable_and_binary_paths_are_skipped(tmp_path):
    assert evidence.read_excerpt(str(tmp_path), "does/not/exist.py") == ""


def test_file_excerpts_are_line_numbered(tmp_path):
    (tmp_path / "x.py").write_text("first\nsecond\n")
    excerpt = evidence.read_excerpt(str(tmp_path), "x.py")
    assert excerpt.splitlines()[0].strip().startswith("1")
    assert "first" in excerpt


def test_context_respects_the_byte_cap_and_reports_what_fitted(tmp_path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x = 1\n" * 200)
    block, included = evidence.build_file_context(
        str(tmp_path), ["a.py", "b.py"], max_bytes=500
    )
    assert included == ["a.py"]
    assert "b.py" not in block


def test_head_commit_diff_is_read_from_the_failing_commit(git_repo):
    import subprocess

    (git_repo / "app" / "api" / "requirements.txt").write_text("fastapi==9.9.9\n")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-aqm", "break"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    diff = evidence.head_commit_diff("HEAD", str(git_repo))
    assert "fastapi==9.9.9" in diff
    assert "requirements.txt" in diff


def test_an_unavailable_commit_yields_empty_rather_than_raising(tmp_path):
    assert evidence.head_commit_diff("deadbeef", str(tmp_path)) == ""


def test_the_commit_diff_is_capped(git_repo):
    assert len(evidence.head_commit_diff("HEAD", str(git_repo), max_chars=50)) <= 80
