/**
 * Screenshot the copilot UI at 1440x900, from receipts that already exist.
 *
 * WHY THIS EXISTS
 * Reviewing a UI change should not cost an API call. Every view below is driven
 * by the signed receipts already committed in `agents/copilot/evidence/`, served
 * through the same `/api/*` contract the real server speaks, so the page under
 * test is byte-identical to the one `python -m copilot.web` serves and every
 * number on screen came from a real recorded run.
 *
 * It is a development tool. It is not imported by the copilot, adds no runtime
 * dependency, and touches no Python. `playwright-core` must be resolvable:
 *
 *     npm i playwright-core@1.63.0 && npx playwright@1.63.0 install chromium
 *     node agents/copilot/tools/screenshots.mjs
 *
 * Output lands in `docs/screenshots/`.
 */

import { createServer } from "node:http";
import { readFileSync, readdirSync, mkdirSync } from "node:fs";
import { dirname, resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const HERE = dirname(fileURLToPath(import.meta.url));
const COPILOT = resolve(HERE, "..");
const REPO = resolve(COPILOT, "..", "..");
const PAGE = join(COPILOT, "copilot", "static", "index.html");
const EVIDENCE = join(COPILOT, "evidence");
const OUT = join(REPO, "docs", "screenshots");

const VIEWPORT = { width: 1440, height: 900 };

/* -- receipts -> the payloads web.py's _turn_payload() would produce -------- */

const receipts = Object.fromEntries(
  readdirSync(EVIDENCE)
    .filter((f) => f.endsWith(".json"))
    .map((f) => [f.replace(/\.json$/, ""), JSON.parse(readFileSync(join(EVIDENCE, f), "utf8"))])
);

/** The budget these recorded sessions actually ran under, per receipt-003's
 *  refusal text ("spent $1.8287 of $2.00"). Not invented. */
const BUDGET_USD = 2.0;

function turn(receipt) {
  const ext = receipt.extensions.copilot;
  return {
    question: receipt.question,
    answer: receipt.answer.text,
    citations: receipt.answer.citations,
    supported: receipt.answer.supported,
    unsupported_reason: receipt.answer.unsupported_reason,
    cost_usd: receipt.cost.amount,
    elapsed_ms: ext.elapsed_ms,
    model_calls: receipt.cost.model_calls,
    sequence: receipt.sequence,
    receipt_id: receipt.receipt_id,
    evidence: receipt.evidence,
    session: {
      total_usd: ext.session_total_usd,
      budget_usd: BUDGET_USD,
      remaining_usd: BUDGET_USD - ext.session_total_usd,
      turns: receipt.sequence + 1,
    },
  };
}

function replayPayload(receipt) {
  const r = receipt.extensions.copilot.replay;
  return {
    window: r.window,
    summary: r.summary,
    conclusion: r.conclusion,
    timeline: r.timeline,
    dropped_uncited_entries: r.dropped_uncited_entries || [],
    turn: turn(receipt),
  };
}

const SUPPORTED = turn(receipts["receipt-000-d5b4e5da"]);
const REPO_ANSWER = turn(receipts["receipt-002-e1183202"]);
const UNSUPPORTED = turn(receipts["receipt-003-fc041b33"]);
const REPLAY = replayPayload(receipts["replay-supported-7-citations"]);

const STATE = {
  tools: receipts["receipt-000-d5b4e5da"].extensions.copilot.tools_available,
  scopes: receipts["receipt-000-d5b4e5da"].extensions.copilot.scopes,
  budget_usd: BUDGET_USD,
  total_usd: 0,
  turns: 0,
  session_id: receipts["receipt-000-d5b4e5da"].session_id,
  key_fingerprint: receipts["receipt-000-d5b4e5da"].signature.key_fingerprint,
};

/* -- a stand-in for web.py's two POST endpoints ---------------------------- */

/** Next turn each /api/ask should return, and how long to stall first. The
 *  stall is what makes the in-progress view screenshottable. */
let next = SUPPORTED;
let stallMs = 0;

const server = createServer((req, res) => {
  const send = (obj) => {
    const body = JSON.stringify(obj);
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    res.end(body);
  };
  if (req.method === "GET" && (req.url === "/" || req.url === "/index.html")) {
    const html = readFileSync(PAGE);
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    return res.end(html);
  }
  if (req.url === "/api/state") return send(STATE);
  if (req.url === "/api/ask") return void setTimeout(() => send(next), stallMs);
  if (req.url === "/api/replay") return void setTimeout(() => send(REPLAY), stallMs);
  res.writeHead(404).end("{}");
});

/* -- the views ------------------------------------------------------------- */

const shots = [];

async function shoot(page, name, note) {
  mkdirSync(OUT, { recursive: true });
  const file = join(OUT, `${name}.png`);
  await page.screenshot({ path: file });
  shots.push({ name, note });
  process.stdout.write(`  ${name}.png  ${note}\n`);
}

/** Animations (the sheen, the pulse, the spinner) make screenshots flaky.
 *  Freeze them, but only after the in-progress view has been captured. */
const FREEZE = `*,*::before,*::after{animation-play-state:paused!important}`;

async function main() {
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}`;
  process.stdout.write(`serving ${PAGE}\n  on ${url}\n\n`);

  const browser = await chromium.launch({ args: ["--force-color-profile=srgb"] });
  const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 });
  const settle = (ms = 350) => page.waitForTimeout(ms);

  await page.goto(url, { waitUntil: "networkidle" });
  await settle();
  await shoot(page, "copilot-01-chat-empty", "chat zero state + least-privilege disclosure");

  // In-progress. Stall the response long enough to capture the working card.
  stallMs = 6000;
  next = SUPPORTED;
  await page.click(".sample");
  await settle(1400);
  await shoot(page, "copilot-02-chat-in-progress", "working card, skeleton rail, header wire");

  await page.waitForSelector(".verdict", { timeout: 15000 });
  await settle(600);
  await page.addStyleTag({ content: FREEZE });
  await page.evaluate(() => { document.querySelector("#log").scrollTop = 0; });
  await settle();
  await shoot(page, "copilot-03-chat-supported", "supported answer, 9 citations, markdown + evidence rail");

  // An active citation, with its evidence card highlighted in the rail.
  await page.click('.cite[data-cite="prometheus:60576682"]');
  await settle(700);
  await shoot(page, "copilot-04-citation-active", "citation chip active, matching evidence card ringed");

  // A repository-sourced answer: git and repo hues, a different tool set.
  stallMs = 0;
  next = REPO_ANSWER;
  await page.reload({ waitUntil: "networkidle" });
  await page.click(".samples .sample:nth-child(3)");
  await page.waitForSelector(".verdict");
  await page.addStyleTag({ content: FREEZE });
  await page.evaluate(() => { document.querySelector("#log").scrollTop = 0; });
  await settle();
  await shoot(page, "copilot-05-chat-repo-answer", "git/repo sourced answer, commit citations");

  // Unsupported: the badge, the reason block, zero citations.
  next = UNSUPPORTED;
  await page.reload({ waitUntil: "networkidle" });
  await page.evaluate(() => {
    document.querySelector("#q").value = "How many unique end users hit the API during the load window today?";
  });
  await page.click("#send");
  await page.waitForSelector(".badge.no");
  await page.addStyleTag({ content: FREEZE });
  await settle();
  await shoot(page, "copilot-06-chat-unsupported", "unsupported badge + refusal reason, budget meter hot");

  // Replay.
  await page.reload({ waitUntil: "networkidle" });
  await page.click('.tab[data-tab="replay"]');
  await settle();
  await page.addStyleTag({ content: FREEZE });
  await shoot(page, "copilot-07-replay-empty", "replay zero state, window picker, presets");

  await page.reload({ waitUntil: "networkidle" });
  await page.click('.tab[data-tab="replay"]');
  await page.evaluate((w) => {
    document.querySelector("#wstart").value = w.start;
    document.querySelector("#wend").value = w.end;
  }, REPLAY.window);
  await page.click("#send");
  await page.waitForSelector(".tl");
  await page.addStyleTag({ content: FREEZE });
  await page.evaluate(() => { document.querySelector("#log").scrollTop = 0; });
  await settle();
  await shoot(page, "copilot-08-replay-timeline", "cited timeline, window header, supported footer");

  await page.evaluate(() => {
    const log = document.querySelector("#log");
    log.scrollTop = log.scrollHeight;
  });
  await settle(700);
  await shoot(page, "copilot-09-replay-conclusion", "timeline tail, conclusion block, verdict footer");

  // The same citation, active from the rail side: the answer column scrolls.
  await page.click(`.ev[data-cite="prometheus:25551386"]`);
  await settle(900);
  await shoot(page, "copilot-10-replay-citation-active", "evidence card clicked, timeline entry and chip lit");

  await browser.close();
  server.close();

  process.stdout.write(`\n${shots.length} screenshots in docs/screenshots/ at ${VIEWPORT.width}x${VIEWPORT.height}\n`);
}

main().catch((err) => {
  console.error(err);
  server.close();
  process.exit(1);
});
