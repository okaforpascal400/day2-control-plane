# tools/

Development tooling for the copilot's web surface. Nothing here is imported by
the copilot, ships in an image, or appears in `requirements.txt` — the runtime
dependency count is unchanged.

## `screenshots.mjs`

Captures every view of the UI at 1440×900 into `docs/screenshots/`.

Reviewing a UI change should not cost an API call, so the script does not run
the copilot. It serves `copilot/static/index.html` — the same file
`python -m copilot.web` serves, byte for byte — behind a stand-in for the two
POST endpoints, and answers them from the signed receipts already committed in
`agents/copilot/evidence/`. Every number on screen (cost, elapsed, token
counts, citation ids, key fingerprint, session id, budget) therefore comes from
a real recorded run, not from a fixture someone typed.

The views, and the receipt behind each:

| shot | receipt | shows |
| --- | --- | --- |
| `01-chat-empty` | — | zero state, tool list, granted scopes |
| `02-chat-in-progress` | — | working card, skeleton rail, header progress |
| `03-chat-supported` | `receipt-000-d5b4e5da` | supported answer, 9 citations, markdown |
| `04-citation-active` | `receipt-000-d5b4e5da` | chip clicked → evidence card ringed |
| `05-chat-repo-answer` | `receipt-002-e1183202` | git/repo sourced answer |
| `06-chat-unsupported` | `receipt-003-fc041b33` | unsupported badge, budget meter hot |
| `07-replay-empty` | — | window picker and presets |
| `08-replay-timeline` | `replay-supported-7-citations` | cited timeline |
| `09-replay-conclusion` | `replay-supported-7-citations` | conclusion and verdict footer |
| `10-replay-citation-active` | `replay-supported-7-citations` | card clicked → timeline entry lit |

### Running it

`playwright-core` must be resolvable from this directory, and its matching
chromium build must be installed. Both are pinned:

```sh
npm i playwright-core@1.63.0
npx playwright@1.63.0 install chromium
node agents/copilot/tools/screenshots.mjs
```

On a distribution playwright does not recognise, prefix the install with
`PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64`. `node_modules/` here is
gitignored.
