"""The copilot runtime: a question in, a cited answer and a signed receipt out.

## Why a manual tool loop rather than the SDK's tool runner

The SDK's `client.beta.messages.tool_runner` would drive this loop in a few
lines, and for most applications that is the right call. Three requirements
here point the other way, and they are the requirements that make this a
*governed* agent rather than a chatbot with tools:

1. **A hard spend cap that stops mid-loop.** The cap has to be checked between
   turns and abort the conversation, returning the partial evidence gathered so
   far. A wrapper that runs to completion and reports the bill afterwards is a
   different guarantee — it tells you what you already spent.
2. **An audit entry per tool call, written as it happens.** If the process dies
   halfway, the trail must still show what was already read (the same reason
   `AuditLogger` flushes both sinks on every write).
3. **No beta dependency.** Everything about this project is pinned and
   reviewed; taking a beta API surface for loop convenience trades a governance
   property for a small amount of code.

The loop itself is the documented manual pattern: call, check `stop_reason`,
execute every `tool_use` block, return **all** `tool_result` blocks in a single
user message. That last detail matters — splitting results across messages
teaches the model to stop making parallel calls.

## The cited-or-flagged rule

An answer that cites no evidence is not shown as though it were grounded. The
runtime extracts `citation_id`s from the answer text, checks them against the
citations actually produced by this question's tool calls, and marks the answer
`supported=False` with a reason when they do not line up. That flag is written
into the signed receipt, so an uncited answer produces durable evidence that it
was uncited — it cannot be quietly forgotten.

Note what this does *not* claim. Checking that an answer cites real evidence is
not checking that the evidence supports the claim; a model can cite a real query
next to a wrong conclusion. This catches fabrication-from-nothing, which is the
common failure, and it is deliberately described that way rather than as
"verified".
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from day2_mcp.server import ToolRegistry

from copilot.receipts import (
    AnswerRecord,
    CostRecord,
    EvidenceEntry,
    ReceiptChain,
    SigningKey,
    load_or_create_key,
)
from day2_agents.audit import AuditLogger
from day2_agents.claude import (
    EFFORT,
    MAX_TOKENS_CEILING,
    MODEL,
    ModelError,
    compute_cost_usd,
)
from day2_agents.scopes import Action, PermissionSet

AGENT_NAME = "day2-observability-copilot"
AGENT_VERSION = "1.0"

# Hard ceiling on one session's model spend, approved at the Phase 6 gate.
#
# CORRECTED after the first live run overspent it 2.4x. The original check ran
# *between* turns and compared spend-so-far against the cap. That is not a cap,
# it is a report: a single turn's cost is unbounded, and on the run that found
# this the seventh turn cost $0.73 on its own from a standing balance of $0.45.
# See "The overspend, and what it cost to learn" in agents/README.md.
#
# A cap has to refuse a call *before* it is made, so `_preflight` estimates what
# the next call will cost and stops if that estimate would breach the budget.
DEFAULT_SESSION_BUDGET_USD = 0.50

# A runaway loop is the other way to spend money. Ten turns is generous for the
# questions this answers (the live runs use three to six) and bounded.
MAX_TURNS = 10
MAX_TOKENS_PER_TURN = 2_000

# The real cause of the overspend was not any single tool result — those are
# capped in `limits.py` — but their *accumulation*. Every tool result stays in
# the conversation and is re-sent on every subsequent turn, so 14 calls of
# capped-but-large results reached 211,833 input tokens. Bounding one message
# and leaving the transcript unbounded is the same bug one level up.
#
# So the transcript is bounded too: past a ceiling, older tool results are
# replaced by a compact stub that keeps what the model actually needs later —
# which tool ran, with what arguments, and the citation id it can still cite —
# and drops the payload it has already read.
MAX_CONTEXT_TOKENS = 40_000
KEEP_FULL_RESULTS = 3

# Over-investigation is a cost driver in its own right: the first live run made
# 14 tool calls for one question, several of them redundant.
MAX_TOOL_CALLS_PER_QUESTION = 12

# Characters per token, used only for the pre-flight estimate. Deliberately low
# (real English is ~4) so the estimate runs *high*: for a spend cap, erring
# toward refusing a call is the safe direction. This is a local heuristic rather
# than `client.messages.count_tokens` on purpose — the cap must work without an
# extra network round-trip on every turn, and must be testable offline.
CHARS_PER_TOKEN = 3.0

_CITATION_RE = re.compile(r"\b(prometheus|loki|repo|git):(\d{8})\b")

SYSTEM_PROMPT = """You are the Observability Copilot for the day2-control-plane \
project. You answer questions about a running Kubernetes system by reading it \
with the tools you have been given, and you cite what you read.

THE RULES THAT MATTER

1. Never state a fact about the running system that you did not read from a \
tool in this conversation. You have no reliable memory of this cluster.

2. Cite every such fact inline with the `citation_id` of the tool result it \
came from, in square brackets: [prometheus:12345678]. A reader must be able to \
tell which tool call supports which claim.

3. If the tools do not show what was asked, say so plainly and stop. "The data \
does not show this" is a correct and useful answer. Do not reason from what \
would usually be true of Kubernetes clusters, and do not present a plausible \
mechanism as if you had observed it. An honest gap is worth more than a \
confident guess, and a guess here is worse than useless because it looks the \
same as an answer.

4. Check `truncated` on every result. If it is true you are looking at part of \
the data, and any conclusion has to be qualified accordingly.

5. Prefer a range query over an instant one when the question is about change \
over time — the shape is usually the answer.

WORKING METHOD

Investigate before concluding. A single query rarely answers a real question: \
look at the metric, then the logs around the same window, then whether an alert \
covers it, then whether anything was deployed. When a tool returns an error, \
read it and adjust — a rejected LogQL query usually means the stream selector \
was wrong, not that there are no logs.

Be concise. Lead with the answer, then the evidence for it. Do not narrate \
which tools you are about to call."""


class BudgetExceeded(RuntimeError):
    """The session spend cap was reached."""


@dataclass
class Turn:
    """One question and everything that came of it."""

    question: str
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    supported: bool = True
    unsupported_reason: str | None = None
    evidence: list[EvidenceEntry] = field(default_factory=list)
    tool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    elapsed_ms: int = 0
    receipt: dict[str, Any] | None = None
    stopped_early: str | None = None


class CopilotSession:
    """One chat session: shared history, a running bill, and a receipt chain."""

    def __init__(
        self,
        registry: ToolRegistry,
        audit: AuditLogger,
        scopes: PermissionSet,
        client: Any | None = None,
        signing_key: SigningKey | None = None,
        budget_usd: float = DEFAULT_SESSION_BUDGET_USD,
        session_id: str | None = None,
    ) -> None:
        self.registry = registry
        self.audit = audit
        self.scopes = scopes
        self._client = client
        self.budget_usd = budget_usd
        self.total_cost_usd = 0.0
        self.trimmed_results = 0
        self.turns: list[Turn] = []
        self.chain = ReceiptChain(
            session_id,
            signing_key or load_or_create_key(),
            {"name": AGENT_NAME, "version": AGENT_VERSION},
        )
        # Conversation history persists across questions so follow-ups work
        # ("and what about the worker?"), which is the point of a chat surface.
        self._messages: list[dict[str, Any]] = []

    # -- model plumbing --------------------------------------------------
    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic

            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise ModelError(
                    "ANTHROPIC_API_KEY is not set; secrets come from the "
                    "environment only (governance pillar 5)"
                )
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    @property
    def budget_remaining(self) -> float:
        return max(0.0, self.budget_usd - self.total_cost_usd)

    def _check_budget(self) -> None:
        if self.total_cost_usd >= self.budget_usd:
            raise BudgetExceeded(
                f"session spend cap reached: ${self.total_cost_usd:.4f} of "
                f"${self.budget_usd:.2f}. Start a new session to continue."
            )

    def estimate_input_tokens(self, system: str, messages: list[dict[str, Any]]) -> int:
        """Rough, deliberately high, estimate of a request's input size.

        Counts the characters that will actually be serialised — system prompt,
        every message, and the tool schemas, which are re-sent on every call and
        are not negligible. Divided by a low chars-per-token so the number errs
        upward.
        """
        payload = json.dumps(messages, default=str, ensure_ascii=False)
        schemas = json.dumps(self.registry.schemas(), default=str)
        characters = len(payload) + len(system) + len(schemas)
        return int(characters / CHARS_PER_TOKEN)

    def _preflight(self, system: str, messages: list[dict[str, Any]]) -> None:
        """Refuse a call whose worst case would breach the cap.

        The worst case is knowable before sending: estimated input tokens at the
        input rate, plus `MAX_TOKENS_PER_TURN` at the output rate, since the API
        cannot return more output than that. If spend-so-far plus that worst
        case exceeds the budget, the call is not made at all.

        This is the difference between a cap and a report, and the reason the
        first live run cost 2.4x its cap: checking afterwards cannot prevent
        anything.
        """
        estimated_input = self.estimate_input_tokens(system, messages)
        worst_case = compute_cost_usd(MODEL, estimated_input, MAX_TOKENS_PER_TURN)
        if self.total_cost_usd + worst_case > self.budget_usd:
            raise BudgetExceeded(
                f"refusing the next call: spent ${self.total_cost_usd:.4f}, and "
                f"this call could cost up to ${worst_case:.4f} "
                f"(~{estimated_input:,} input tokens), which would exceed the "
                f"${self.budget_usd:.2f} cap. Nothing was sent."
            )

    def _trim_transcript(self) -> None:
        """Stub out old tool results once the transcript grows past the ceiling.

        Only the *content* of a `tool_result` block is replaced, never the block
        itself: the API requires every `tool_use` to be answered by a matching
        `tool_result`, so removing one would make the request invalid. What
        remains is enough for the model to keep working — the tool that ran and
        the citation id it may still cite — without re-sending a payload it has
        already read.
        """
        if (
            self.estimate_input_tokens(SYSTEM_PROMPT, self._messages)
            <= MAX_CONTEXT_TOKENS
        ):
            return

        # Find tool_result blocks, oldest first, keeping the most recent intact.
        positions: list[tuple[int, int]] = []
        for m_index, message in enumerate(self._messages):
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, list):
                continue
            for b_index, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    positions.append((m_index, b_index))

        trimmable = (
            positions[:-KEEP_FULL_RESULTS] if len(positions) > KEEP_FULL_RESULTS else []
        )
        for m_index, b_index in trimmable:
            block = self._messages[m_index]["content"][b_index]
            if block.get("_trimmed"):
                continue
            stub = self._stub_for(block)
            block["content"] = stub
            block["_trimmed"] = True
            self.trimmed_results += 1
            if (
                self.estimate_input_tokens(SYSTEM_PROMPT, self._messages)
                <= MAX_CONTEXT_TOKENS
            ):
                break

    @staticmethod
    def _stub_for(block: dict[str, Any]) -> str:
        """A one-line replacement for a tool result the model has already read."""
        try:
            original = json.loads(block.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            original = {}
        citation = original.get("citation_id")
        keys = [k for k in original if k not in ("provenance", "citation_id")][:6]
        return json.dumps(
            {
                "elided": True,
                "note": (
                    "This result was read earlier in the conversation and its "
                    "payload has been dropped to stay within the session's "
                    "context budget. You may still cite it."
                ),
                "citation_id": citation,
                "fields_it_contained": keys,
            }
        )

    # -- the loop --------------------------------------------------------
    def ask(self, question: str, extensions: dict[str, Any] | None = None) -> Turn:
        """Answer one question. Always returns a Turn with a signed receipt."""
        self.scopes.require(Action.CALL_MODEL)
        started = time.monotonic()
        turn = Turn(question=question)
        self._check_budget()

        self._messages.append({"role": "user", "content": question})
        client = self._ensure_client()
        tools = self.registry.schemas()
        evidence_index = 0

        # A question that cannot even be *started* raises, rather than returning
        # an empty turn: there is no partial result to hand back, and the caller
        # needs to know the session is finished rather than that this question
        # happened to fail. Once at least one call has been made the loop below
        # degrades gracefully instead, so whatever evidence was gathered is kept.
        self._preflight(SYSTEM_PROMPT, self._messages)

        for _turn_number in range(MAX_TURNS):
            try:
                self._check_budget()
                self._preflight(SYSTEM_PROMPT, self._messages)
            except BudgetExceeded as exc:
                turn.stopped_early = str(exc)
                break

            response = client.messages.create(
                model=MODEL,
                max_tokens=min(MAX_TOKENS_PER_TURN, MAX_TOKENS_CEILING),
                output_config={"effort": EFFORT},
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=self._messages,
            )

            usage = response.usage
            call_cost = compute_cost_usd(
                getattr(response, "model", MODEL),
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
                getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
            self.total_cost_usd += call_cost
            turn.cost_usd += call_cost
            turn.input_tokens += getattr(usage, "input_tokens", 0) or 0
            turn.output_tokens += getattr(usage, "output_tokens", 0) or 0
            turn.model_calls += 1

            self.audit.record(
                action="call_model",
                target=question[:80],
                decision_summary=(
                    f"copilot turn [{MODEL}, effort={EFFORT}, "
                    f"stop={getattr(response, 'stop_reason', None)}, ${call_cost:.4f}]"
                ),
                metadata={
                    "model": getattr(response, "model", MODEL),
                    "input_tokens": turn.input_tokens,
                    "output_tokens": turn.output_tokens,
                    "cost_usd": round(call_cost, 6),
                    "session_total_usd": round(self.total_cost_usd, 6),
                },
            )

            if getattr(response, "stop_reason", None) == "refusal":
                turn.answer = "The model declined to answer this question."
                turn.supported = False
                turn.unsupported_reason = "model returned stop_reason=refusal"
                break

            self._messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                turn.answer = "".join(
                    b.text for b in response.content if b.type == "text"
                ).strip()
                break

            # Execute every requested tool, then return ALL results in ONE user
            # message — splitting them trains the model out of parallel calls.
            if evidence_index >= MAX_TOOL_CALLS_PER_QUESTION:
                turn.stopped_early = (
                    f"reached the {MAX_TOOL_CALLS_PER_QUESTION}-tool-call ceiling "
                    "for one question without concluding"
                )
                break

            results_block: list[dict[str, Any]] = []
            for block in tool_uses:
                result = self.registry.call(block.name, dict(block.input))
                call_record = self.registry.calls[-1]
                entry = EvidenceEntry.from_tool_call(evidence_index, call_record, result)
                turn.evidence.append(entry)
                evidence_index += 1
                if entry.citation_id:
                    turn.tool_results[entry.citation_id] = result

                results_block.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _as_tool_content(result),
                        "is_error": bool(result.get("is_error")),
                    }
                )
            self._messages.append({"role": "user", "content": results_block})
            # Bound the transcript before the next turn re-sends all of it.
            self._trim_transcript()
        else:
            turn.stopped_early = f"stopped after {MAX_TURNS} turns without concluding"

        if not turn.answer and turn.stopped_early:
            turn.answer = "I stopped before reaching an answer: " + turn.stopped_early

        self._grade(turn)
        turn.elapsed_ms = int((time.monotonic() - started) * 1000)
        turn.receipt = self.chain.append(
            model={
                "id": MODEL,
                "effort": EFFORT,
                "max_tokens": MAX_TOKENS_PER_TURN,
            },
            question=question,
            evidence=turn.evidence,
            answer=AnswerRecord(
                text=turn.answer,
                citations=turn.citations,
                supported=turn.supported,
                unsupported_reason=turn.unsupported_reason,
            ),
            cost=CostRecord(
                amount=turn.cost_usd,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                model_calls=turn.model_calls,
            ),
            extensions={
                "copilot": {
                    "mode": (extensions or {}).get("mode", "chat"),
                    "elapsed_ms": turn.elapsed_ms,
                    "tools_available": self.registry.tool_names(),
                    "scopes": self.registry.declared_scopes(),
                    "session_total_usd": round(self.total_cost_usd, 6),
                    "stopped_early": turn.stopped_early,
                    "trimmed_results": self.trimmed_results,
                    **{k: v for k, v in (extensions or {}).items() if k != "mode"},
                }
            },
        )
        self.turns.append(turn)

        self.audit.record(
            action="answer",
            target=turn.receipt["receipt_id"],
            decision_summary=(
                f"{'supported' if turn.supported else 'UNSUPPORTED'} answer, "
                f"{len(turn.evidence)} tool calls, ${turn.cost_usd:.4f}"
            ),
            metadata={
                "receipt_id": turn.receipt["receipt_id"],
                "sequence": turn.receipt["sequence"],
                "supported": turn.supported,
                "citations": turn.citations,
                "cost_usd": round(turn.cost_usd, 6),
            },
        )
        return turn

    def ask_raw(
        self,
        *,
        question: str,
        system: str,
        user: str,
        evidence: list[EvidenceEntry],
        mode: str = "chat",
        extensions: dict[str, Any] | None = None,
    ) -> Turn:
        """One model call over evidence that was already gathered.

        The replay path uses this: its evidence is collected by code rather
        than chosen by the model (see `replay.py` on why), so there is no loop
        to run — just one narration call. It still goes through the same
        budget check, the same audit entries and the same receipt chain, so a
        replay is accounted for exactly like a chat answer.
        """
        self.scopes.require(Action.CALL_MODEL)
        started = time.monotonic()
        turn = Turn(question=question, evidence=list(evidence))
        self._check_budget()
        # Replay sends one large gathered-evidence prompt; it needs the same
        # pre-flight refusal as the chat loop, for the same reason.
        self._preflight(system, [{"role": "user", "content": user}])

        response = self._ensure_client().messages.create(
            model=MODEL,
            max_tokens=min(MAX_TOKENS_PER_TURN, MAX_TOKENS_CEILING),
            output_config={"effort": EFFORT},
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        usage = response.usage
        cost = compute_cost_usd(
            getattr(response, "model", MODEL),
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
        self.total_cost_usd += cost
        turn.cost_usd = cost
        turn.input_tokens = getattr(usage, "input_tokens", 0) or 0
        turn.output_tokens = getattr(usage, "output_tokens", 0) or 0
        turn.model_calls = 1

        self.audit.record(
            action="call_model",
            target=question[:80],
            decision_summary=(
                f"copilot {mode} [{MODEL}, effort={EFFORT}, "
                f"stop={getattr(response, 'stop_reason', None)}, ${cost:.4f}]"
            ),
            metadata={
                "mode": mode,
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "cost_usd": round(cost, 6),
                "session_total_usd": round(self.total_cost_usd, 6),
            },
        )

        if getattr(response, "stop_reason", None) == "refusal":
            turn.answer = "The model declined this request."
            turn.supported = False
            turn.unsupported_reason = "model returned stop_reason=refusal"
        else:
            turn.answer = "".join(
                b.text for b in response.content if b.type == "text"
            ).strip()

        turn.elapsed_ms = int((time.monotonic() - started) * 1000)
        turn.receipt = self.chain.append(
            model={"id": MODEL, "effort": EFFORT, "max_tokens": MAX_TOKENS_PER_TURN},
            question=question,
            evidence=turn.evidence,
            answer=AnswerRecord(
                text=turn.answer,
                citations=turn.citations,
                supported=turn.supported,
                unsupported_reason=turn.unsupported_reason,
            ),
            cost=CostRecord(
                amount=turn.cost_usd,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                model_calls=turn.model_calls,
            ),
            extensions={"copilot": {"mode": mode, **(extensions or {})}},
        )
        self.turns.append(turn)
        return turn

    # -- the cited-or-flagged rule ---------------------------------------
    def _grade(self, turn: Turn) -> None:
        """Decide whether the answer is supported by the evidence it cites."""
        cited = {f"{a}:{b}" for a, b in _CITATION_RE.findall(turn.answer or "")}
        available = {e.citation_id for e in turn.evidence if e.citation_id}
        turn.citations = sorted(cited)

        unknown = cited - available
        successful_reads = [e for e in turn.evidence if not e.is_error]

        # An honest "the data does not show this" is a *supported* answer even
        # with no citations — refusing to answer is the behaviour we want, and
        # flagging it as unsupported would punish exactly the right response.
        declines = _looks_like_a_decline(turn.answer)

        if turn.unsupported_reason and not turn.supported:
            # Already graded by the loop — a refusal, say — and that reason is
            # more specific than anything this function would infer.
            return
        if turn.stopped_early:
            turn.supported = False
            turn.unsupported_reason = turn.stopped_early
        elif unknown:
            turn.supported = False
            turn.unsupported_reason = (
                "answer cites evidence that this session did not produce: "
                + ", ".join(sorted(unknown))
            )
        elif not cited and not declines:
            turn.supported = False
            turn.unsupported_reason = (
                "answer states facts about the system but cites no tool result"
                if successful_reads
                else "no tool call produced evidence, and the answer does not "
                "say the data is unavailable"
            )
        else:
            turn.supported = True
            turn.unsupported_reason = None


_DECLINE_MARKERS = (
    "does not show",
    "doesn't show",
    "do not show",
    "don't show",
    "no data",
    "not available",
    "cannot determine",
    "can't determine",
    "cannot answer",
    "no evidence",
    "not collected",
    "not instrumented",
    "no metric",
    "nothing in the",
    "unable to answer",
)


def _looks_like_a_decline(answer: str) -> bool:
    lowered = (answer or "").lower()
    return any(marker in lowered for marker in _DECLINE_MARKERS)


def _as_tool_content(result: dict[str, Any]) -> str:
    """Serialise a tool result for the model.

    JSON rather than a prose summary: the model is better at reading structure
    than at trusting our summary of it, and a summary here would be a second
    place for a bug to change what the model sees relative to what the receipt
    digests.
    """
    import json

    return json.dumps(result, default=str, ensure_ascii=False)
