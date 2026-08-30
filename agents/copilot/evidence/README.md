# Recorded receipts from the Phase 6 live runs

Committed as evidence, not as fixtures. Every file here was produced by a real
run against the live Kind cluster and is verifiable by anyone:

```bash
cd agents/copilot
python -m copilot.verify evidence/receipt-*.json
```

That checks the signature and the hash chain against the public key in
`../keys/`, and prints `PASS (attested)` — no cluster, no API key, no access to
the chat required. Change one character in any file and it prints `FAIL`.

| File | What it records |
|---|---|
| `receipt-000-*.json` | "why did latency spike at 21:08" — **supported**, 9 citations, $0.4432. The copilot rejected the question's false premise with evidence. |
| `receipt-001-*.json` | "which pods restarted recently" — **unsupported**, hit the 12-tool-call ceiling, $0.8841. Kept deliberately: a receipt that records a failure is the point. |
| `receipt-002-*.json` | "why FOR UPDATE SKIP LOCKED" — **supported**, 5 citations from git history, $0.4632. |
| `receipt-003-*.json` | "how many unique end users" — **unsupported**, the budget refused the next call, $0.0382. |
| `receipt-004-replay.json` | Replay of 21:05-21:15, in the same chain. |
| `replay-supported-7-citations.json` | A **supported** replay from an earlier session — 7 cited timeline entries, $0.0943. Chains to that session, so it verifies alone but not as part of the five. |

The first five form one intact chain: `sequence` 0-4, each linking to its
predecessor's `payload_digest`. Removing or reordering any of them breaks
verification, which is the property that makes the set an audit trail rather
than five separate claims.
