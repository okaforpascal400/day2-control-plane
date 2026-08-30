"""Receipt tests. The ones that matter are the tamper tests.

A signed receipt is worth exactly as much as the guarantee that altering it
breaks verification. So the central test here does not check that a valid
receipt passes — that is the easy half. It mutates **one character at a time,
in every field a liar would want to change**, and requires every single
mutation to FAIL. If someone later swaps the canonical encoder for
`json.dumps(...)` with default settings, or excludes a field from the digest,
these go red.
"""

from __future__ import annotations

import json

import pytest
from copilot.receipts import (
    AnswerRecord,
    CostRecord,
    EvidenceEntry,
    ReceiptChain,
    SigningKey,
    canonical_json,
    digest,
    export_public_key,
    load_or_create_key,
    payload_digest,
    sign_receipt,
    write_receipt,
)
from copilot.verify import (
    load_trusted_fingerprints,
    verify_chain,
    verify_receipt,
)
from copilot.verify import (
    main as verify_main,
)


@pytest.fixture
def key() -> SigningKey:
    from copilot.receipts import _key_from_private
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return _key_from_private(Ed25519PrivateKey.generate())


@pytest.fixture
def chain(key: SigningKey) -> ReceiptChain:
    return ReceiptChain("session-under-test", key, {"name": "copilot", "version": "1.0"})


def _evidence() -> list[EvidenceEntry]:
    return [
        EvidenceEntry(
            index=0,
            tool="query_prometheus",
            arguments={"query": "sum(up)", "start": None},
            result_digest=digest({"series_count": 1}),
            citation_id="prometheus:12345678",
            provenance={"source": "prometheus", "query": "sum(up)"},
            elapsed_ms=42,
        )
    ]


def _receipt(chain: ReceiptChain, question: str = "why did latency spike?") -> dict:
    return chain.append(
        model={"id": "claude-opus-5", "effort": "high", "max_tokens": 8000},
        question=question,
        evidence=_evidence(),
        answer=AnswerRecord(
            text="Latency rose because the worker queue backed up.",
            citations=["prometheus:12345678"],
            supported=True,
        ),
        cost=CostRecord(
            amount=0.0731, input_tokens=5000, output_tokens=400, model_calls=1
        ),
        extensions={"copilot": {"mode": "chat"}},
    )


# --- canonicalisation -------------------------------------------------------


def test_canonical_json_is_key_order_independent() -> None:
    """Two encoders following the rule must produce identical bytes."""
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_has_no_insignificant_whitespace() -> None:
    assert canonical_json({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'


def test_canonical_json_encodes_non_ascii_as_utf8_not_escapes() -> None:
    """An escaped and an unescaped encoder would otherwise disagree."""
    assert canonical_json({"k": "café"}) == '{"k":"café"}'.encode()


def test_pretty_printing_a_receipt_on_disk_does_not_break_it(chain, tmp_path) -> None:
    """Verification re-canonicalises, so formatting is irrelevant."""
    receipt = _receipt(chain)
    path = write_receipt(receipt, tmp_path / "r.json")

    assert "\n  " in path.read_text(), "written indented for humans"
    reloaded = json.loads(path.read_text())
    assert verify_receipt(reloaded, str(path), set()).passed


# --- a valid receipt --------------------------------------------------------


def test_a_freshly_signed_receipt_verifies(chain) -> None:
    result = verify_receipt(_receipt(chain), "memory", set())

    assert result.passed
    names = {c.name for c in result.checks}
    assert {"structure", "payload_digest", "signature"} <= names


def test_the_required_fields_carry_nothing_copilot_specific(chain) -> None:
    """The schema is meant to outlive this project.

    Anything naming this copilot, its tools or its cluster belongs in
    `extensions`, which a general verifier ignores.
    """
    receipt = _receipt(chain)
    generic = {k: v for k, v in receipt.items() if k != "extensions"}
    blob = json.dumps(generic).lower()

    for word in ("copilot", "day2", "prometheus", "loki", "grafana"):
        # `agent.name` and tool names are *values* supplied by the caller, not
        # schema. What must not appear is a required *key* naming them.
        assert word not in json.dumps(list(generic.keys())).lower(), (
            f"required field names must be agent-agnostic; found {word!r}"
        )
    assert "extensions" in receipt
    assert blob  # keeps the value-level inspection meaningful


def test_an_unsupported_answer_is_recorded_as_such_in_the_signed_payload(chain) -> None:
    """The cited-or-flagged rule, made durable."""
    receipt = chain.append(
        model={"id": "claude-opus-5"},
        question="how many users signed up last week?",
        evidence=[],
        answer=AnswerRecord(
            text="The data does not show this.",
            citations=[],
            supported=False,
            unsupported_reason="no tool calls produced evidence for this claim",
        ),
        cost=CostRecord(amount=0.01, model_calls=1),
    )

    assert receipt["answer"]["supported"] is False
    assert "no tool calls" in receipt["answer"]["unsupported_reason"]
    # And it is inside the signature, so the flag cannot be stripped silently.
    tampered = json.loads(json.dumps(receipt))
    tampered["answer"]["supported"] = True
    assert not verify_receipt(tampered, "t", set()).passed


# --- THE TAMPER TESTS -------------------------------------------------------

TAMPER_CASES = [
    ("question", lambda r: r.__setitem__("question", r["question"] + ".")),
    ("answer.text", lambda r: r["answer"].__setitem__("text", r["answer"]["text"] + " ")),
    ("answer.supported", lambda r: r["answer"].__setitem__("supported", False)),
    (
        "answer.citations",
        lambda r: r["answer"]["citations"].append("prometheus:00000000"),
    ),
    ("cost.amount", lambda r: r["cost"].__setitem__("amount", 0.0001)),
    ("cost.input_tokens", lambda r: r["cost"].__setitem__("input_tokens", 1)),
    ("model.id", lambda r: r["model"].__setitem__("id", "claude-haiku-4-5")),
    ("evidence.tool", lambda r: r["evidence"][0].__setitem__("tool", "search_logs")),
    (
        "evidence.arguments",
        lambda r: r["evidence"][0]["arguments"].__setitem__("query", "sum(down)"),
    ),
    (
        "evidence.result_digest",
        lambda r: r["evidence"][0].__setitem__("result_digest", "sha256:" + "0" * 64),
    ),
    ("evidence.removed", lambda r: r["evidence"].clear()),
    ("timestamp", lambda r: r.__setitem__("timestamp", "2020-01-01T00:00:00+00:00")),
    ("session_id", lambda r: r.__setitem__("session_id", "someone-elses-session")),
    ("sequence", lambda r: r.__setitem__("sequence", 99)),
    ("chain.previous_digest", lambda r: r["chain"].__setitem__("previous_digest", None)),
    ("agent.name", lambda r: r["agent"].__setitem__("name", "a-different-agent")),
    ("extensions", lambda r: r["extensions"].__setitem__("copilot", {"mode": "replay"})),
]


@pytest.mark.parametrize(
    ("field", "mutate"), TAMPER_CASES, ids=[c[0] for c in TAMPER_CASES]
)
def test_tampering_with_any_field_fails_verification(chain, field, mutate) -> None:
    # The *second* receipt in the session, deliberately: the first one's
    # `chain.previous_digest` is legitimately null, so mutating it to null
    # would be a no-op and the case would pass for the wrong reason.
    _receipt(chain, "an earlier question")
    receipt = json.loads(json.dumps(_receipt(chain)))
    assert verify_receipt(receipt, "before", set()).passed, (
        "control: unmodified must pass"
    )
    assert receipt["chain"]["previous_digest"] is not None, "control: chained"

    mutate(receipt)
    result = verify_receipt(receipt, "after", set())

    assert not result.passed, f"tampering with {field} was not detected"
    digest_check = next(c for c in result.checks if c.name == "payload_digest")
    assert not digest_check.passed, (
        f"{field}: the digest check should be the one that fires"
    )


def test_a_single_altered_character_anywhere_fails(chain, tmp_path) -> None:
    """The literal requirement: one character, anywhere in the file."""
    path = write_receipt(_receipt(chain), tmp_path / "r.json")
    original = path.read_text()

    # Flip one character of the answer text, leaving the JSON structurally valid.
    tampered_text = original.replace("queue backed up", "queue backed uq")
    assert tampered_text != original
    (tmp_path / "tampered.json").write_text(tampered_text)

    result = verify_receipt(
        json.loads(tampered_text), str(tmp_path / "tampered.json"), set()
    )
    assert not result.passed


def test_re_signing_a_tampered_receipt_with_a_different_key_is_not_attested(
    chain, key, tmp_path
) -> None:
    """The trust subtlety the verifier is careful about.

    An attacker can rewrite a receipt and re-sign it with their own key. Every
    internal check then passes — which is exactly why an unattested PASS is
    reported differently from an attested one.
    """
    from copilot.receipts import _key_from_private
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    receipt = json.loads(json.dumps(_receipt(chain)))
    receipt["answer"]["text"] = "Everything was fine."
    attacker = _key_from_private(Ed25519PrivateKey.generate())
    forged = sign_receipt(
        {k: v for k, v in receipt.items() if k != "signature"}, attacker
    )

    # Internally consistent...
    assert verify_receipt(forged, "forged", set()).passed
    # ...but not signed by the trusted key.
    trusted = {key.fingerprint}
    result = verify_receipt(forged, "forged", trusted)
    assert not result.passed
    assert not result.attested


def test_a_corrupt_signature_fails(chain) -> None:
    receipt = json.loads(json.dumps(_receipt(chain)))
    value = receipt["signature"]["value"]
    receipt["signature"]["value"] = ("A" if value[0] != "A" else "B") + value[1:]

    result = verify_receipt(receipt, "corrupt", set())

    assert not result.passed
    assert not next(c for c in result.checks if c.name == "signature").passed


def test_a_missing_field_fails_before_any_crypto(chain) -> None:
    receipt = json.loads(json.dumps(_receipt(chain)))
    del receipt["cost"]

    result = verify_receipt(receipt, "incomplete", set())

    assert not result.passed
    assert result.checks[0].name == "structure"


# --- the hash chain ---------------------------------------------------------


def test_receipts_link_to_their_predecessor(chain) -> None:
    first = _receipt(chain, "first question")
    second = _receipt(chain, "second question")

    assert first["chain"]["previous_digest"] is None
    assert second["chain"]["previous_digest"] == first["signature"]["payload_digest"]
    assert second["sequence"] == 1


def test_an_intact_chain_verifies(chain) -> None:
    receipts = [
        ("a", _receipt(chain, "q1")),
        ("b", _receipt(chain, "q2")),
        ("c", _receipt(chain, "q3")),
    ]

    checks = verify_chain(receipts)

    assert all(c.passed for c in checks), [c for c in checks if not c.passed]


def test_removing_a_receipt_from_the_middle_breaks_the_chain(chain) -> None:
    """The property that makes history un-revisable."""
    r0, r1, r2 = _receipt(chain, "q1"), _receipt(chain, "q2"), _receipt(chain, "q3")

    checks = verify_chain([("a", r0), ("c", r2)])

    assert not all(c.passed for c in checks)
    assert not next(c for c in checks if c.name == "chain_sequence").passed
    assert r1  # the removed one


def test_reordering_receipts_breaks_the_chain(chain) -> None:
    r0, r1 = _receipt(chain, "q1"), _receipt(chain, "q2")
    r0["sequence"], r1["sequence"] = 1, 0

    checks = verify_chain([("a", r0), ("b", r1)])

    assert not next(c for c in checks if c.name == "chain_linkage").passed


def test_splicing_in_a_receipt_from_another_session_is_detected(chain, key) -> None:
    other = ReceiptChain(
        "a-different-session", key, {"name": "copilot", "version": "1.0"}
    )
    mine = _receipt(chain, "q1")
    theirs = _receipt(other, "q2")

    checks = verify_chain([("a", mine), ("b", theirs)])

    assert not next(c for c in checks if c.name == "chain_session").passed


# --- keys and the CLI -------------------------------------------------------


def test_the_private_key_is_written_owner_only(tmp_path, monkeypatch) -> None:
    """Governance pillar 5: a secret must never be briefly world-readable."""
    monkeypatch.delenv("DAY2_COPILOT_SIGNING_KEY", raising=False)
    target = tmp_path / "sub" / "copilot.key"

    created = load_or_create_key(target)

    assert target.exists()
    assert oct(target.stat().st_mode)[-3:] == "600"
    # Loading again returns the same key rather than rotating it.
    assert load_or_create_key(target).fingerprint == created.fingerprint


def test_the_signing_key_can_come_from_the_environment(tmp_path, monkeypatch) -> None:
    """So CI or a container can supply it without a file on disk."""
    import base64

    key = load_or_create_key(tmp_path / "k.key")
    monkeypatch.setenv(
        "DAY2_COPILOT_SIGNING_KEY", base64.b64encode(key.private_bytes).decode()
    )

    assert (
        load_or_create_key(tmp_path / "does-not-exist.key").fingerprint == key.fingerprint
    )


def test_the_exported_public_key_is_safe_to_commit(tmp_path, key) -> None:
    import base64

    path = export_public_key(key, tmp_path)
    data = json.loads(path.read_text())

    assert data["fingerprint"] == key.fingerprint
    assert base64.b64decode(data["public_key"]) == key.public_bytes
    # The private half must not appear anywhere in the file.
    assert base64.b64encode(key.private_bytes).decode() not in path.read_text()


def test_trusted_fingerprints_load_from_a_directory(tmp_path, key) -> None:
    export_public_key(key, tmp_path)

    assert load_trusted_fingerprints(tmp_path) == {key.fingerprint}


def test_the_cli_exits_zero_on_pass_and_one_on_fail(chain, tmp_path, capsys) -> None:
    """End-to-end, the way the demo will run it."""
    good = write_receipt(_receipt(chain), tmp_path / "good.json")
    assert verify_main([str(good), "--trusted-key", str(tmp_path / "empty")]) == 0
    assert "PASS" in capsys.readouterr().out

    tampered = json.loads(good.read_text())
    tampered["answer"]["text"] = "something else entirely"
    bad = write_receipt(tampered, tmp_path / "bad.json")

    assert verify_main([str(bad), "--trusted-key", str(tmp_path / "empty")]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "payload_digest" in out


def test_the_cli_distinguishes_attested_from_unattested(
    chain, key, tmp_path, capsys
) -> None:
    receipt = write_receipt(_receipt(chain), tmp_path / "r.json")

    verify_main([str(receipt), "--trusted-key", str(tmp_path / "nokeys")])
    assert "PASS (unattested key)" in capsys.readouterr().out

    keydir = tmp_path / "keys"
    export_public_key(key, keydir)
    verify_main([str(receipt), "--trusted-key", str(keydir)])
    assert "PASS (attested)" in capsys.readouterr().out


def test_payload_digest_excludes_only_the_signature(chain) -> None:
    receipt = _receipt(chain)
    without = {k: v for k, v in receipt.items() if k != "signature"}

    assert payload_digest(receipt) == digest(without)
    assert receipt["signature"]["payload_digest"] == payload_digest(receipt)
