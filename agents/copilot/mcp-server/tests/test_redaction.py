"""Redaction tests.

The important ones here are not "does the regex match" — they are:

* every credential shape this repository actually handles is caught;
* redaction is *structural*, so a secret nested in a dashboard or a log line is
  caught exactly like one at the top level;
* it fails **closed**, returning nothing rather than the original, when it
  cannot complete;
* and the chokepoint property holds — `ToolRegistry.call` is the only way out,
  and it always redacts. That last test is the one that keeps this module
  honest as tools are added later.
"""

from __future__ import annotations

import pytest
from day2_mcp.redaction import (
    PLACEHOLDER,
    RedactionError,
    RedactionReport,
    redact,
    redact_text,
)

# Real *shapes*, invented values. Each is a credential this project handles.
SECRET_CORPUS: list[tuple[str, str]] = [
    ("anthropic_api_key", "sk-" + "ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
    ("github_token", "gh" + "p_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
    ("github_token", "gh" + "s_ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210"),
    ("github_pat", "github" + "_pat_11ABCDEFG0abcdefghijklmnop"),
    ("aws_access_key_id", "AKIA" + "IOSFODNN7EXAMPLE"),
    ("aws_access_key_id", "ASIA" + "Y34FZKBOKMSEXAMP"),
    ("slack_token", "xox" + "b-123456789012-abcdefghijklmnop"),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ),
]


@pytest.mark.parametrize(("rule", "secret"), SECRET_CORPUS)
def test_every_known_credential_shape_is_redacted(rule: str, secret: str) -> None:
    line = f"2026-08-30T10:00:00Z INFO starting with credential {secret} ok"
    cleaned, report = redact(line)

    assert secret not in cleaned, f"{rule} survived redaction"
    assert report.substitutions >= 1
    assert not report.clean


def test_uri_credentials_are_removed_but_the_host_survives() -> None:
    line = "connecting to postgres://day2:hunter2@day2-postgres:5432/day2"
    cleaned, report = redact(line)

    assert "hunter2" not in cleaned
    assert "day2-postgres:5432/day2" in cleaned, "host and database should survive"
    assert report.substitutions == 1


def test_private_key_block_is_removed_whole() -> None:
    blob = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxyz\nabcdefgh\n"
        "-----END RSA PRIVATE KEY-----"
    )
    cleaned, _ = redact(f"kubeconfig contains {blob} here")

    assert "MIIEowIBAAKCAQEAxyz" not in cleaned
    assert "BEGIN RSA PRIVATE KEY" not in cleaned


def test_a_secret_key_redacts_its_value_whatever_the_value_looks_like() -> None:
    """The hard case: a password with no distinctive shape."""
    payload = {"database": {"host": "db", "password": "correct horse battery"}}
    cleaned, report = redact(payload)

    assert cleaned["database"]["password"] == PLACEHOLDER.format(rule="secret_key")
    assert cleaned["database"]["host"] == "db", "non-secret siblings must survive"
    assert report.rules_fired["secret_key"] == 1


def test_redaction_is_structural_and_reaches_nested_leaves() -> None:
    payload = {
        "panels": [
            {"title": "ok", "targets": [{"expr": "up"}]},
            {"title": "leaky", "note": "use ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"},
        ]
    }
    cleaned, report = redact(payload)

    assert "gh" + "p_" not in str(cleaned)
    assert cleaned["panels"][0]["targets"][0]["expr"] == "up"
    assert report.substitutions == 1


def test_our_own_cost_metadata_is_not_destroyed() -> None:
    """A trail whose token counts read '[REDACTED]' fails CLAUDE.md rule 5."""
    payload = {"input_tokens": 1234, "output_tokens": 567, "max_tokens": 8000}
    cleaned, report = redact(payload)

    assert cleaned == payload
    assert report.clean, "allowlisted keys must not trip the key-hint rule"


def test_bytes_are_refused_rather_than_passed_through() -> None:
    with pytest.raises(RedactionError, match="raw bytes"):
        redact({"blob": b"sk-ant-secret"})


def test_deep_nesting_fails_closed_instead_of_recursing() -> None:
    payload: dict = {}
    node = payload
    for _ in range(60):
        node["next"] = {}
        node = node["next"]

    with pytest.raises(RedactionError, match="nested deeper"):
        redact(payload)


def test_report_is_evidence_that_redaction_ran() -> None:
    """A visible zero distinguishes 'found nothing' from 'never ran'."""
    _, report = redact({"msg": "nothing secret here"})

    assert report.clean
    assert report.as_metadata() == {"redactions": 0, "redaction_rules": {}}


def test_redact_text_is_idempotent() -> None:
    once = redact_text(
        "token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", RedactionReport()
    )
    twice = redact_text(once, RedactionReport())

    assert once == twice, "re-redacting a placeholder must not corrupt it"


# --- the chokepoint property ------------------------------------------------


def test_every_tool_result_passes_through_redaction(registry, monkeypatch) -> None:
    """Fail-red guard: adding a tool that bypasses the redactor breaks this.

    Rather than trusting that each handler was wired correctly, this replaces
    the registry's redactor with a spy and asserts it fires for *every*
    registered tool. A future tool that returns directly to the model — never
    reaching `ToolRegistry.call`'s redaction step — cannot pass this.
    """
    seen: list[str] = []
    real_redact = redact

    def spy(value, report=None):
        seen.append("called")
        return real_redact(value, report)

    monkeypatch.setattr("day2_mcp.server.redact", spy)

    for name in registry.tool_names():
        seen.clear()
        # Arguments are deliberately empty: handlers that need one will fail,
        # and a *failed* call must not reach redaction (there is nothing to
        # redact). What matters is that no successful call skips it.
        result = registry.call(name, {})
        if not result.get("is_error"):
            assert seen, f"{name} returned a result without passing through redact()"


def test_a_leaky_tool_result_is_cleaned_at_the_chokepoint(registry, monkeypatch) -> None:
    """End-to-end: a handler that returns a secret cannot leak it."""
    leaked = "gh" + "p_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"

    def leaky_handler(**_kwargs):
        return {"line": f"token={leaked}", "citation_id": "test:1"}

    spec = registry._specs["read_runbook"]
    monkeypatch.setattr(
        registry,
        "_specs",
        {**registry._specs, "read_runbook": _replace_handler(spec, leaky_handler)},
    )

    result = registry.call("read_runbook", {})

    assert leaked not in str(result)
    assert "[REDACTED" in str(result)


def _replace_handler(spec, handler):
    from dataclasses import replace

    return replace(spec, handler=handler)
