"""Standalone receipt verifier.

    python -m copilot.verify <receipt.json> [more.json ...] [--trusted-key FILE|DIR]

Runnable by someone with **no access to the chat, the cluster, or an API key**.
It needs the receipt file and nothing else; `cryptography` is its only import
beyond the standard library, and it never opens a socket.

## What "PASS" means here, precisely

Four checks, reported individually so a partial failure is legible rather than
a single opaque red:

1. **Structure** — the required fields exist and the version is understood.
2. **Payload digest** — recomputing SHA-256 over the canonical encoding of
   everything-but-the-signature reproduces `signature.payload_digest`. This is
   the check that catches a single altered character anywhere in the receipt.
3. **Signature** — Ed25519 verification of that digest against the public key.
4. **Chain** — across multiple receipts, each `chain.previous_digest` equals
   the preceding receipt's `signature.payload_digest`, and sequence numbers run
   0,1,2,… with no gaps.

## The trust subtlety, stated rather than glossed

A receipt carries its own public key. Verifying a signature against the key
embedded in the same file proves *internal consistency*, not authenticity — an
attacker who rewrites a receipt can also generate a fresh keypair, re-sign, and
swap in their own public key. Every check would pass.

So this verifier separates the two questions and says which one it answered:

* Without `--trusted-key`, it reports `PASS (unattested key)` and prints the
  key fingerprint. That is an honest statement: the file is internally
  consistent and self-signed.
* With `--trusted-key` pointing at a committed public key (or the repository's
  `keys/` directory, which is found automatically when present), it also
  asserts the fingerprint matches, and reports `PASS (attested)`.

A tool that printed a bare "PASS" for the first case would be teaching its
users something false, which is worse than not having the tool.
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from copilot.receipts import (
        REQUIRED_FIELDS,
        SIGNATURE_ALGORITHM,
        payload_digest,
    )
except ImportError:  # pragma: no cover - allows running the file directly
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from copilot.receipts import (  # type: ignore[no-redef]
        REQUIRED_FIELDS,
        SIGNATURE_ALGORITHM,
        payload_digest,
    )

SUPPORTED_VERSIONS = {"1.0"}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Result:
    path: str
    checks: list[Check] = field(default_factory=list)
    attested: bool = False
    fingerprint: str | None = None

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, passed, detail))
        return passed

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks) and bool(self.checks)


def _load(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def load_trusted_fingerprints(source: Path | None) -> set[str]:
    """Read trusted public keys from a file or a directory of `.pub` files."""
    if source is None:
        return set()
    target = Path(source)
    files = (
        sorted(target.glob("*.pub"))
        if target.is_dir()
        else ([target] if target.exists() else [])
    )
    fingerprints: set[str] = set()
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            if fp := data.get("fingerprint"):
                fingerprints.add(str(fp))
        except (json.JSONDecodeError, OSError):
            continue
    return fingerprints


def verify_receipt(receipt: dict[str, Any], path: str, trusted: set[str]) -> Result:
    """Run every check against one receipt."""
    result = Result(path=path)

    missing = [f for f in REQUIRED_FIELDS if f not in receipt]
    if not result.add(
        "structure",
        not missing,
        f"missing: {', '.join(missing)}"
        if missing
        else f"{len(REQUIRED_FIELDS)} required fields present",
    ):
        return result

    version = receipt.get("receipt_version")
    if not result.add(
        "version",
        version in SUPPORTED_VERSIONS,
        f"receipt_version={version!r}"
        + (
            ""
            if version in SUPPORTED_VERSIONS
            else f"; this verifier understands {sorted(SUPPORTED_VERSIONS)}"
        ),
    ):
        return result

    signature = receipt.get("signature") or {}
    algorithm = signature.get("algorithm")
    if not result.add(
        "algorithm",
        algorithm == SIGNATURE_ALGORITHM,
        f"{algorithm!r}"
        + (
            ""
            if algorithm == SIGNATURE_ALGORITHM
            else f"; expected {SIGNATURE_ALGORITHM}"
        ),
    ):
        return result

    # --- the tamper check -------------------------------------------------
    claimed = signature.get("payload_digest")
    recomputed = payload_digest(receipt)
    result.add(
        "payload_digest",
        claimed == recomputed,
        "recomputed digest matches the signed one"
        if claimed == recomputed
        else f"MISMATCH\n      claimed:    {claimed}\n      recomputed: {recomputed}",
    )

    # --- the signature ----------------------------------------------------
    fingerprint = signature.get("key_fingerprint")
    result.fingerprint = fingerprint
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(signature.get("public_key", ""))
        )
        public.verify(
            base64.b64decode(signature.get("value", "")),
            str(recomputed).encode("ascii"),
        )
        result.add("signature", True, f"Ed25519 verified against key {fingerprint}")
    except InvalidSignature:
        result.add("signature", False, "Ed25519 signature does not verify")
    except Exception as exc:
        result.add("signature", False, f"could not verify: {type(exc).__name__}: {exc}")

    # --- attestation ------------------------------------------------------
    if trusted:
        result.attested = fingerprint in trusted
        result.add(
            "trusted_key",
            result.attested,
            f"{fingerprint} is in the trusted keyring"
            if result.attested
            else f"{fingerprint} is NOT in the trusted keyring",
        )
    return result


def verify_chain(receipts: list[tuple[str, dict[str, Any]]]) -> list[Check]:
    """Check chain linkage across receipts, ordered by sequence."""
    checks: list[Check] = []
    if len(receipts) < 2:
        return checks

    ordered = sorted(receipts, key=lambda r: r[1].get("sequence", 0))
    sequences = [r[1].get("sequence") for r in ordered]
    expected = list(range(sequences[0], sequences[0] + len(sequences)))
    checks.append(
        Check(
            "chain_sequence",
            sequences == expected,
            f"sequences {sequences}"
            + (
                ""
                if sequences == expected
                else f"; expected {expected} — a receipt is missing or duplicated"
            ),
        )
    )

    sessions = {r[1].get("session_id") for r in ordered}
    checks.append(
        Check("chain_session", len(sessions) == 1, f"{len(sessions)} session id(s)")
    )

    linked = True
    detail = "every receipt links to its predecessor"
    for previous, current in itertools.pairwise(ordered):
        expected_link = (previous[1].get("signature") or {}).get("payload_digest")
        actual_link = (current[1].get("chain") or {}).get("previous_digest")
        if expected_link != actual_link:
            linked = False
            detail = (
                f"break between sequence {previous[1].get('sequence')} and "
                f"{current[1].get('sequence')}\n      expected: {expected_link}\n"
                f"      found:    {actual_link}"
            )
            break
    checks.append(Check("chain_linkage", linked, detail))
    return checks


def _default_keyring() -> Path | None:
    candidate = Path(__file__).resolve().parents[1] / "keys"
    return candidate if candidate.is_dir() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.verify",
        description="Verify signed agent answer receipts (signature + hash chain).",
    )
    parser.add_argument("receipts", nargs="+", help="receipt JSON file(s)")
    parser.add_argument(
        "--trusted-key",
        type=Path,
        default=None,
        help="public key file or directory of .pub files to attest against "
        "(defaults to the repository's keys/ directory when present)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="print only the final verdict line"
    )
    args = parser.parse_args(argv)

    keyring = args.trusted_key if args.trusted_key is not None else _default_keyring()
    trusted = load_trusted_fingerprints(keyring)

    loaded: list[tuple[str, dict[str, Any]]] = []
    results: list[Result] = []

    for raw_path in args.receipts:
        path = Path(raw_path)
        try:
            receipt = _load(path)
        except (OSError, ValueError) as exc:
            result = Result(path=str(path))
            result.add("readable", False, str(exc))
            results.append(result)
            continue
        loaded.append((str(path), receipt))
        results.append(verify_receipt(receipt, str(path), trusted))

    chain_checks = verify_chain(loaded)

    if not args.quiet:
        for result in results:
            print(f"\n{result.path}")
            for check in result.checks:
                mark = "  ok  " if check.passed else " FAIL "
                print(f"  [{mark}] {check.name:<16} {check.detail}")
        if chain_checks:
            print("\nchain across receipts")
            for check in chain_checks:
                mark = "  ok  " if check.passed else " FAIL "
                print(f"  [{mark}] {check.name:<16} {check.detail}")

    all_passed = all(r.passed for r in results) and all(c.passed for c in chain_checks)
    attested = bool(trusted) and all(r.attested for r in results)

    print()
    if not all_passed:
        failed = [c.name for r in results for c in r.checks if not c.passed]
        failed += [c.name for c in chain_checks if not c.passed]
        print(f"FAIL — {len(results)} receipt(s); failed checks: {', '.join(failed)}")
        return 1

    n = len(results)
    what = f"{n} receipt{'s' if n != 1 else ''}"
    chain_note = f", chain of {n} intact" if chain_checks else ""
    if attested:
        print(
            f"PASS (attested) — {what} verified{chain_note}; "
            f"signed by trusted key {results[0].fingerprint}"
        )
    else:
        print(
            f"PASS (unattested key) — {what} internally consistent and "
            f"self-signed by {results[0].fingerprint}{chain_note}."
        )
        print("      The receipt has not been altered since signing, but the key is not")
        print("      in a trusted keyring — pass --trusted-key to attest authorship.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
