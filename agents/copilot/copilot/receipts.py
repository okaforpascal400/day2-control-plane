"""Signed answer receipts — a portable, verifiable record of how an answer was reached.

This format is deliberately **agent-agnostic**. Nothing in the required fields
names this project, this copilot, or its tools: a receipt describes *an agent
answered a question using evidence, at a cost, and here is the cryptographic
proof that this record has not been altered since*. Anything specific to this
copilot lives in `extensions`, which a verifier ignores. The intent is that the
schema outlives the project.

## What a receipt proves, and what it does not

It proves **integrity and origin**: this exact question, this exact evidence
list, this exact answer and cost were recorded together by the holder of a
particular private key, and no byte has changed since. Chained to its
predecessor, it also proves **position**: receipt N in a session cannot be
removed, reordered, or swapped without breaking every receipt after it.

It does **not** prove the answer is correct, and it does not prove the tool
results were true — a signature over a lie is a signed lie. What it removes is
the ability to quietly revise history afterwards, which is the failure mode
that matters when an agent's output is used to make an operational decision and
someone asks, a week later, "what did it actually see?"

## The three-layer digest

Verification depends on the digest being reproducible by someone who has only
the JSON file, so canonicalisation is the whole game:

1. `canonical_json()` — sorted keys, no insignificant whitespace, UTF-8,
   `ensure_ascii=False`. Any two encoders that follow this produce identical
   bytes for identical data. This is why a verifier written in another language
   could check our receipts.
2. `payload_digest` — SHA-256 over the canonical bytes of everything except
   `signature`. Changing *one character anywhere* in the question, an argument,
   a result digest, the answer, or the cost changes this.
3. `signature.value` — Ed25519 over the payload digest. Only the private key
   holder can produce it; the committed public key lets anyone check it.

The chain link is *inside* the signed payload (`chain.previous_digest`), which
is what makes reordering detectable: a receipt's signature commits to its
predecessor's identity.

## A deliberate choice about what gets digested

`evidence[].result_digest` is the digest of the **redacted** tool result — the
bytes the model actually saw — not the pre-redaction original. Two reasons, and
the trade is worth being explicit about:

* The unredacted result never leaves `ToolRegistry.call`, so a digest of it
  would be unverifiable by anyone, forever. A digest nobody can reproduce is
  not evidence.
* What a reader wants to check is *what the model was reasoning over*, and that
  is the redacted form.

The receipt records `redactions` alongside each result digest, so the fact that
redaction occurred — and how much — is itself part of the signed record.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECEIPT_VERSION = "1.0"
SIGNATURE_ALGORITHM = "Ed25519"
DIGEST_ALGORITHM = "sha256"

# Required top-level keys. A verifier checks these exist before doing crypto,
# so a malformed file fails with a useful message rather than a stack trace.
REQUIRED_FIELDS: tuple[str, ...] = (
    "receipt_version",
    "receipt_id",
    "session_id",
    "sequence",
    "timestamp",
    "agent",
    "model",
    "question",
    "evidence",
    "answer",
    "cost",
    "chain",
    "signature",
)


class ReceiptError(RuntimeError):
    """A receipt could not be built or read."""


def canonical_json(value: Any) -> bytes:
    """The one serialisation both signer and verifier must agree on.

    `sort_keys` makes key order irrelevant; the tight separators remove
    whitespace ambiguity; `ensure_ascii=False` with an explicit UTF-8 encode
    means a non-ASCII character has exactly one byte representation rather than
    depending on whether the encoder chose to escape it.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def digest(value: Any) -> str:
    """`sha256:<hex>` over the canonical encoding of `value`."""
    return f"{DIGEST_ALGORITHM}:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def digest_bytes(raw: bytes) -> str:
    return f"{DIGEST_ALGORITHM}:{hashlib.sha256(raw).hexdigest()}"


@dataclass
class EvidenceEntry:
    """One tool call, as recorded in a receipt.

    `arguments` are kept in full because they are half of what makes a citation
    reproducible — the query without its time window is not the same query.
    """

    index: int
    tool: str
    arguments: dict[str, Any]
    result_digest: str
    citation_id: str | None = None
    provenance: dict[str, Any] | None = None
    is_error: bool = False
    error: str | None = None
    elapsed_ms: int | None = None
    redactions: int = 0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "arguments": self.arguments,
            "result_digest": self.result_digest,
            "citation_id": self.citation_id,
            "provenance": self.provenance,
            "is_error": self.is_error,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "redactions": self.redactions,
            "truncated": self.truncated,
        }

    @classmethod
    def from_tool_call(
        cls, index: int, call: dict[str, Any], result: Any
    ) -> EvidenceEntry:
        return cls(
            index=index,
            tool=call.get("tool", "unknown"),
            arguments=call.get("arguments", {}),
            result_digest=digest(result),
            citation_id=call.get("citation_id"),
            provenance=call.get("provenance"),
            is_error=bool(call.get("is_error")),
            error=call.get("error"),
            elapsed_ms=call.get("elapsed_ms"),
            redactions=int(call.get("redactions", 0) or 0),
            truncated=bool((result or {}).get("truncated"))
            if isinstance(result, dict)
            else False,
        )


@dataclass
class AnswerRecord:
    """The answer, and whether the runtime considered it supported.

    `supported` is the cited-or-flagged rule made durable. An uncited answer
    does not get quietly shown and then forgotten: it produces a receipt that
    says, in the signed payload, that it was unsupported and why.
    """

    text: str
    citations: list[str] = field(default_factory=list)
    supported: bool = True
    unsupported_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citations": list(self.citations),
            "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
        }


@dataclass
class CostRecord:
    """What the answer cost, from real token counts (CLAUDE.md rule 5)."""

    currency: str = "USD"
    amount: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "amount": round(self.amount, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "model_calls": self.model_calls,
        }


def build_receipt(
    *,
    session_id: str,
    sequence: int,
    previous_digest: str | None,
    agent: dict[str, str],
    model: dict[str, Any],
    question: str,
    evidence: list[EvidenceEntry],
    answer: AnswerRecord,
    cost: CostRecord,
    extensions: dict[str, Any] | None = None,
    receipt_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Assemble the unsigned payload. `sign_receipt` completes it."""
    payload: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": receipt_id or str(uuid.uuid4()),
        "session_id": session_id,
        "sequence": sequence,
        "timestamp": timestamp or datetime.now(UTC).isoformat(timespec="seconds"),
        "agent": agent,
        "model": model,
        "question": question,
        "evidence": [e.as_dict() for e in evidence],
        "answer": answer.as_dict(),
        "cost": cost.as_dict(),
        "chain": {
            "sequence": sequence,
            "previous_digest": previous_digest,
        },
        "extensions": extensions or {},
    }
    return payload


def payload_digest(receipt: dict[str, Any]) -> str:
    """Digest of everything except the signature block.

    The signature cannot cover itself, so it is excluded here and the
    *signature* then covers this digest. A verifier recomputes exactly this.
    """
    unsigned = {k: v for k, v in receipt.items() if k != "signature"}
    return digest(unsigned)


# --------------------------------------------------------------------------
# Key handling
# --------------------------------------------------------------------------
#
# The private key is a secret and is treated like every other secret in this
# project (governance pillar 5): it lives outside the repository, is read from
# a path or an environment variable, is created with owner-only permissions,
# and is in `.gitignore` so an accidental `git add -A` cannot commit it.
#
# The public key is committed on purpose. That is the whole point — a receipt
# is only useful if someone with no access to this machine can check it.

DEFAULT_KEY_DIR = Path.home() / ".config" / "day2-copilot"
DEFAULT_PRIVATE_KEY = DEFAULT_KEY_DIR / "copilot_ed25519.key"
PUBLIC_KEYRING = Path(__file__).resolve().parents[1] / "keys"


@dataclass(frozen=True)
class SigningKey:
    """An Ed25519 keypair for one copilot instance."""

    private_bytes: bytes
    public_bytes: bytes

    @property
    def fingerprint(self) -> str:
        """Short, stable identifier for the public key."""
        return hashlib.sha256(self.public_bytes).hexdigest()[:16]

    def public_b64(self) -> str:
        import base64

        return base64.b64encode(self.public_bytes).decode("ascii")


def load_or_create_key(path: Path | None = None) -> SigningKey:
    """Load the signing key, generating one on first use.

    `DAY2_COPILOT_SIGNING_KEY` (base64 raw private bytes) takes precedence so a
    CI run or a container can supply the key from a secret store without a file
    ever touching disk.
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    env_key = os.environ.get("DAY2_COPILOT_SIGNING_KEY")
    if env_key:
        try:
            raw = base64.b64decode(env_key)
        except Exception as exc:
            raise ReceiptError("DAY2_COPILOT_SIGNING_KEY is not valid base64") from exc
        private = Ed25519PrivateKey.from_private_bytes(raw)
        return _key_from_private(private)

    target = Path(path or os.environ.get("DAY2_COPILOT_KEY_PATH") or DEFAULT_PRIVATE_KEY)
    if target.exists():
        raw = base64.b64decode(target.read_text().strip())
        private = Ed25519PrivateKey.from_private_bytes(raw)
        return _key_from_private(private)

    private = Ed25519PrivateKey.generate()
    key = _key_from_private(private)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written 0600 *before* any content, so the key is never briefly world-readable.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(base64.b64encode(key.private_bytes).decode("ascii"))
    return key


def _key_from_private(private: Any) -> SigningKey:
    from cryptography.hazmat.primitives import serialization

    return SigningKey(
        private_bytes=private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        public_bytes=private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
    )


def export_public_key(key: SigningKey, directory: Path | None = None) -> Path:
    """Write the public key where a verifier can find it. Safe to commit."""
    target_dir = Path(directory or PUBLIC_KEYRING)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"copilot-{key.fingerprint}.pub"
    target.write_text(
        json.dumps(
            {
                "algorithm": SIGNATURE_ALGORITHM,
                "fingerprint": key.fingerprint,
                "public_key": key.public_b64(),
                "note": "Public verification key for day2 copilot answer receipts.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def sign_receipt(receipt: dict[str, Any], key: SigningKey) -> dict[str, Any]:
    """Attach the signature block. Returns a new dict; does not mutate."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signed = dict(receipt)
    signed.pop("signature", None)
    computed = payload_digest(signed)

    private = Ed25519PrivateKey.from_private_bytes(key.private_bytes)
    signature = private.sign(computed.encode("ascii"))

    signed["signature"] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "public_key": key.public_b64(),
        "key_fingerprint": key.fingerprint,
        "payload_digest": computed,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def write_receipt(receipt: dict[str, Any], path: Path) -> Path:
    """Export one receipt as a standalone JSON file.

    Indented rather than canonical, because a human should be able to read it —
    verification re-canonicalises, so formatting on disk is irrelevant. That
    property is worth having explicitly: a receipt someone has pretty-printed,
    or moved between systems, still verifies.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target


class ReceiptChain:
    """Builds and signs the receipts of one session, in order."""

    def __init__(
        self, session_id: str | None, key: SigningKey, agent: dict[str, str]
    ) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.key = key
        self.agent = agent
        self.receipts: list[dict[str, Any]] = []

    @property
    def head_digest(self) -> str | None:
        if not self.receipts:
            return None
        return self.receipts[-1]["signature"]["payload_digest"]

    def append(
        self,
        *,
        model: dict[str, Any],
        question: str,
        evidence: list[EvidenceEntry],
        answer: AnswerRecord,
        cost: CostRecord,
        extensions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = build_receipt(
            session_id=self.session_id,
            sequence=len(self.receipts),
            previous_digest=self.head_digest,
            agent=self.agent,
            model=model,
            question=question,
            evidence=evidence,
            answer=answer,
            cost=cost,
            extensions=extensions,
        )
        signed = sign_receipt(payload, self.key)
        self.receipts.append(signed)
        return signed
