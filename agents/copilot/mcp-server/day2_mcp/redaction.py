"""Secret redaction — the egress boundary every tool result crosses.

Governance pillar 5 (secrets via env/SSM only) has a second half that is easy
to miss: keeping secrets *out of the environment* is not enough if a read-only
tool can then read one back out of a log line, a Grafana panel, a commit diff or
a Kubernetes annotation and hand it to a model — and, from there, to a chat
transcript and an audit artifact that is uploaded and kept for 90 days.

So redaction is not a helper that tools may call. It is a **chokepoint they
cannot avoid**: `day2_mcp.server.ToolRegistry` pipes every result through
`redact()` before it leaves the process, and `tests/test_redaction.py` asserts
that property by introspection, so a tool added later that tries to return raw
data fails the suite rather than leaking quietly.

Three design choices worth stating, because each one is a deliberate trade:

1. **Structural, not string-level.** `redact()` walks dicts, lists and tuples
   recursively and rewrites only the string leaves. A secret nested six levels
   into a dashboard's JSON is caught exactly like one in a log line, and the
   caller still gets a usable structure back rather than a stringified blob.

2. **Fails closed.** If a pattern raises — a pathological regex, a surprising
   type — the caller gets `RedactionError` and the tool result is dropped. A
   redactor that errors must never degrade to "return the original"; that is
   precisely the moment a secret escapes.

3. **Counts what it did.** Every call reports how many substitutions it made
   and which rules fired, and the server writes that into the audit entry.
   Silent redaction is unfalsifiable — you cannot tell "found nothing" from
   "never ran". A visible zero is evidence; an absent number is not.

The patterns cover the credential shapes this repository actually handles
(`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, AWS keys for the OIDC role and the state
backend, the Postgres DSN, the Grafana admin secret, the k3s kubeconfig's
client cert) plus the generic shapes any cluster accumulates. It is a denylist,
and a denylist is never complete — which is why the path jail in `files.py` and
the argv allowlist in `git.py` exist alongside it rather than trusting this
module to be the only thing standing between a secret and the model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PLACEHOLDER = "[REDACTED:{rule}]"

# Keys whose *value* is secret whatever it looks like. Matched case-insensitively
# against dict keys and against `KEY=value` / `KEY: value` text. This catches the
# large class of secrets with no distinctive shape — a database password is just
# a string — which no value-shaped pattern can find.
SECRET_KEY_HINTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "secret_key",
    "private_key",
    "credential",
    "authorization",
    "auth_token",
    "session_key",
    "client_secret",
    "connection_string",
    "dsn",
)

# Keys that *contain* a hint substring but are not secrets. Without this,
# `token_count` and `max_tokens` from our own usage metadata would be redacted
# into uselessness — and an audit trail whose cost figures are "[REDACTED]"
# fails CLAUDE.md rule 5 as surely as a fabricated one would.
SECRET_KEY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "token_count",
        "tokens",
        "max_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "token_limit",
        "secret_scanning",  # a trivy/gh feature name, not a secret
        "tokenizer",
    }
)


@dataclass(frozen=True)
class Rule:
    """One named redaction pattern."""

    name: str
    pattern: re.Pattern[str]
    # When set, only this capture group is replaced, so surrounding context
    # (the key name, the URI scheme and host) survives and the result stays
    # readable and therefore reviewable.
    group: int | None = None


def _c(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


VALUE_RULES: tuple[Rule, ...] = (
    # --- Provider credentials, distinctive prefixes -------------------------
    Rule("anthropic_api_key", _c(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    Rule("openai_api_key", _c(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    Rule("github_token", _c(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    Rule("github_pat", _c(r"github_pat_[A-Za-z0-9_]{20,}")),
    Rule("slack_token", _c(r"xox[abprs]-[A-Za-z0-9\-]{10,}")),
    # --- AWS ----------------------------------------------------------------
    # Access key IDs have a fixed shape. Secret access keys do not, so they are
    # only reachable through the key-hint path above — stated here so the gap
    # is documented rather than assumed covered.
    Rule("aws_access_key_id", _c(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    Rule("aws_session_token", _c(r"\bFwoG[A-Za-z0-9/+=]{50,}")),
    # --- Structured credentials ---------------------------------------------
    Rule(
        "jwt", _c(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
    ),
    Rule(
        "private_key_block",
        _c(
            r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    Rule(
        "certificate_block",
        _c(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL),
    ),
    # --- Credentials embedded in URIs ---------------------------------------
    # Replace only the userinfo, so "postgres://[REDACTED]@day2-postgres:5432/day2"
    # still tells a reader which host and database the line was about.
    Rule(
        "uri_credentials",
        _c(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<cred>[^/\s:@]+:[^/\s@]+)@"),
        group=2,
    ),
    # --- Headers ------------------------------------------------------------
    Rule(
        "authorization_header",
        _c(r"(?i)\bauthorization\s*[:=]\s*(?P<val>(?:bearer|basic|token)\s+\S+)"),
        group=1,
    ),
    # --- Generic KEY=value / KEY: value -------------------------------------
    # Deliberately last: the specific rules above produce better-labelled
    # placeholders, and a match here means nothing more precise fired.
    Rule(
        "secret_assignment",
        _c(
            r"(?i)\b(?P<key>[A-Z0-9_]*(?:"
            + "|".join(h.upper() for h in SECRET_KEY_HINTS)
            + r")[A-Z0-9_]*)\s*[:=]\s*(?P<val>\"[^\"]{4,}\"|'[^']{4,}'|\S{4,})"
        ),
        group=2,
    ),
)


class RedactionError(RuntimeError):
    """Redaction could not complete. The caller must drop the value."""


@dataclass
class RedactionReport:
    """What redaction did to one value — written into the audit trail."""

    substitutions: int = 0
    rules_fired: dict[str, int] = field(default_factory=dict)

    def note(self, rule: str, count: int) -> None:
        if count <= 0:
            return
        self.substitutions += count
        self.rules_fired[rule] = self.rules_fired.get(rule, 0) + count

    def as_metadata(self) -> dict[str, Any]:
        return {
            "redactions": self.substitutions,
            "redaction_rules": dict(sorted(self.rules_fired.items())),
        }

    @property
    def clean(self) -> bool:
        return self.substitutions == 0


def _key_is_secret(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered in SECRET_KEY_ALLOWLIST:
        return False
    return any(hint in lowered for hint in SECRET_KEY_HINTS)


def redact_text(text: str, report: RedactionReport) -> str:
    """Apply every value rule to one string."""
    result = text
    for rule in VALUE_RULES:
        count = 0

        def _sub(match: re.Match[str], _rule: Rule = rule) -> str:
            nonlocal count
            count += 1
            placeholder = PLACEHOLDER.format(rule=_rule.name)
            if _rule.group is None:
                return placeholder
            # Preserve everything outside the captured group so the line stays
            # readable: the key name, the URI scheme, the header name.
            whole, span = match.group(0), match.span(_rule.group)
            start = span[0] - match.start()
            end = span[1] - match.start()
            return whole[:start] + placeholder + whole[end:]

        try:
            result = rule.pattern.sub(_sub, result)
        except (re.error, RecursionError) as exc:  # pragma: no cover - defensive
            raise RedactionError(f"rule {rule.name!r} failed: {exc}") from exc
        report.note(rule.name, count)
    return result


def redact(
    value: Any, report: RedactionReport | None = None
) -> tuple[Any, RedactionReport]:
    """Recursively redact secrets from any JSON-shaped value.

    Returns the cleaned value and a report. Raises `RedactionError` rather than
    returning anything it could not fully process — see the module docstring on
    failing closed.
    """
    rep = report if report is not None else RedactionReport()
    try:
        cleaned = _walk(value, rep, depth=0)
    except RedactionError:
        raise
    except Exception as exc:
        raise RedactionError(f"redaction aborted: {type(exc).__name__}: {exc}") from exc
    return cleaned, rep


_MAX_DEPTH = 40


def _walk(value: Any, report: RedactionReport, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        # Deeply nested structures are far more likely to be a cycle or an
        # attack than a real dashboard, and recursing forever is its own
        # failure. Refuse rather than truncate silently.
        raise RedactionError(f"value nested deeper than {_MAX_DEPTH} levels")

    if isinstance(value, str):
        return redact_text(value, report)

    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _key_is_secret(key):
                # The key itself declares the value secret. Do not look at the
                # value at all — a password that happens to look like a normal
                # word must still be removed.
                report.note("secret_key", 1)
                out[key] = PLACEHOLDER.format(rule="secret_key")
                continue
            out[key] = _walk(item, report, depth + 1)
        return out

    # str is a Sequence, and bytes are not JSON-shaped; both are handled above
    # or excluded here so they do not get iterated character by character.
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_walk(item, report, depth + 1) for item in value]

    if isinstance(value, bytes | bytearray):
        raise RedactionError(
            "refusing to redact raw bytes; decode to str before returning a tool result"
        )

    # int, float, bool, None — nothing to redact, and nothing that can hide a
    # secret without having been a string first.
    return value
