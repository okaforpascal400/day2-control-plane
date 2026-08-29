"""Audit trail: the schema is fixed, and every action lands in both sinks."""

from __future__ import annotations

import io
import json

from day2_agents.audit import SCHEMA_FIELDS, AuditLogger


def test_entry_carries_exactly_the_claude_md_schema(tmp_path):
    log = AuditLogger("triage", "workflow_run", tmp_path / "a.jsonl", io.StringIO())
    entry = log.record("open_pr", "repo#1", "proposed a fix")
    assert tuple(entry.to_dict()) == SCHEMA_FIELDS


def test_metadata_is_appended_not_substituted(tmp_path):
    log = AuditLogger("triage", "workflow_run", tmp_path / "a.jsonl", io.StringIO())
    entry = log.record("call_model", "run/42", "diagnosed", metadata={"cost_usd": 0.01})
    payload = entry.to_dict()
    assert tuple(payload)[: len(SCHEMA_FIELDS)] == SCHEMA_FIELDS
    assert payload["metadata"] == {"cost_usd": 0.01}


def test_approved_by_defaults_to_null(tmp_path):
    """An agent cannot approve its own work; null is the unapproved marker."""
    log = AuditLogger("triage", "workflow_run", tmp_path / "a.jsonl", io.StringIO())
    assert log.record("open_pr", "repo#1", "proposed").to_dict()["approved_by"] is None


def test_written_to_both_the_workflow_log_and_the_artifact_file(tmp_path):
    stream = io.StringIO()
    path = tmp_path / "audit.jsonl"
    log = AuditLogger("triage", "workflow_run", path, stream)
    log.record("create_branch", "repo@triage/1-x", "branched")
    log.record("open_pr", "repo#7", "proposed")

    from_stream = [json.loads(line) for line in stream.getvalue().splitlines()]
    from_file = [json.loads(line) for line in path.read_text().splitlines()]
    assert from_stream == from_file
    assert [e["action"] for e in from_file] == ["create_branch", "open_pr"]


def test_provenance_cannot_be_overridden_per_entry(tmp_path):
    """agent/trigger describe who ran and why — fixed for the whole run."""
    log = AuditLogger("triage", "workflow_run:ci#99", tmp_path / "a.jsonl", io.StringIO())
    entry = log.record("open_pr", "repo#1", "proposed").to_dict()
    assert entry["agent"] == "triage"
    assert entry["trigger"] == "workflow_run:ci#99"


def test_timestamp_is_utc_iso8601(tmp_path):
    from datetime import datetime

    log = AuditLogger("triage", "t", tmp_path / "a.jsonl", io.StringIO())
    stamp = log.record("open_pr", "x", "y").to_dict()["timestamp"]
    assert datetime.fromisoformat(stamp).tzinfo is not None


def test_every_line_is_standalone_json(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLogger("triage", "t", path, io.StringIO())
    log.record("a", "t1", "multi\nline\nsummary")
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["decision_summary"] == "multi\nline\nsummary"
