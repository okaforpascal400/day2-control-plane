"""Runtime tests: the loop, the budget, and the cited-or-flagged rule.

Everything here runs against a fake Anthropic client. That is deliberate and
not a shortcut: these tests assert *governance* properties — that the budget
stops the loop, that an uncited answer is flagged, that every answer produces a
chained receipt — and those must hold regardless of what a model happens to say
on a given day. A test suite that needed a live API key would also cost money on
every CI run and go red on someone else's outage.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from copilot.receipts import load_or_create_key
from copilot.runtime import MAX_TURNS, BudgetExceeded, CopilotSession
from copilot.verify import verify_chain, verify_receipt
from day2_mcp.server import COPILOT_SCOPES, CopilotConfig, ToolRegistry

from day2_agents.audit import AuditLogger
from day2_agents.scopes import PermissionSet

# --- a fake client ----------------------------------------------------------


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Usage:
    def __init__(self, i: int = 1000, o: int = 200) -> None:
        self.input_tokens = i
        self.output_tokens = o
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _Response:
    def __init__(
        self, content: list[Any], stop_reason: str = "end_turn", usage=None
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _Usage()
        self.model = "claude-opus-5"


class FakeMessages:
    """Replays a scripted list of responses and records the requests."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        # Snapshot `messages`: the runtime appends to the same list between
        # calls, so storing the reference would make every recorded request
        # show the final state rather than what was actually sent.
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self._responses:
            return _Response([_Block(type="text", text="done")])
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = FakeMessages(responses)


def text(t: str) -> _Block:
    return _Block(type="text", text=t)


def tool_use(name: str, inp: dict, tid: str = "tu_1") -> _Block:
    return _Block(type="tool_use", name=name, input=inp, id=tid)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(
        "copilot-test", "pytest", path=tmp_path / "a.jsonl", stream=io.StringIO()
    )


@pytest.fixture
def registry(repo_root: Path, audit: AuditLogger) -> ToolRegistry:
    scopes = PermissionSet.declare("copilot-test", COPILOT_SCOPES)
    return ToolRegistry(CopilotConfig(repo_root=repo_root), scopes, audit)


def make_session(registry, audit, responses, tmp_path, budget=0.50) -> CopilotSession:
    return CopilotSession(
        registry=registry,
        audit=audit,
        scopes=PermissionSet.declare("copilot-test", COPILOT_SCOPES),
        client=FakeClient(responses),
        signing_key=load_or_create_key(tmp_path / "k.key"),
        budget_usd=budget,
        session_id="test-session",
    )


# --- the loop ---------------------------------------------------------------


def test_a_question_with_no_tool_calls_returns_the_answer(
    registry, audit, tmp_path
) -> None:
    session = make_session(
        registry, audit, [_Response([text("The data does not show this.")])], tmp_path
    )

    turn = session.ask("how many users signed up?")

    assert "does not show" in turn.answer
    assert turn.model_calls == 1
    assert turn.receipt is not None


def test_the_loop_executes_tools_and_feeds_results_back(
    registry, audit, tmp_path
) -> None:
    session = make_session(
        registry,
        audit,
        [
            _Response([tool_use("read_runbook", {"path": None})], stop_reason="tool_use"),
            _Response([text("Three documents exist [repo:11111111].")]),
        ],
        tmp_path,
    )

    turn = session.ask("what runbooks exist?")

    assert len(turn.evidence) == 1
    assert turn.evidence[0].tool == "read_runbook"
    # The tool result must be fed back as a user message containing tool_result.
    second_request = session._client.messages.requests[1]
    last = second_request["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"


def test_parallel_tool_calls_return_in_one_user_message(
    registry, audit, tmp_path
) -> None:
    """Splitting them across messages trains the model out of parallel calls."""
    session = make_session(
        registry,
        audit,
        [
            _Response(
                [
                    tool_use("read_runbook", {"path": None}, "tu_a"),
                    tool_use("get_dashboard", {"name": None}, "tu_b"),
                ],
                stop_reason="tool_use",
            ),
            _Response([text("Both read [repo:11111111].")]),
        ],
        tmp_path,
    )

    session.ask("what do we have?")

    last = session._client.messages.requests[1]["messages"][-1]
    assert last["role"] == "user"
    assert len(last["content"]) == 2, "both results must be in ONE user message"
    assert {b["tool_use_id"] for b in last["content"]} == {"tu_a", "tu_b"}


def test_a_tool_error_is_returned_to_the_model_rather_than_raising(
    registry, audit, tmp_path
) -> None:
    session = make_session(
        registry,
        audit,
        [
            _Response(
                [tool_use("search_logs", {"query": "no selector"})],
                stop_reason="tool_use",
            ),
            _Response([text("The query was invalid; the data does not show this.")]),
        ],
        tmp_path,
    )

    turn = session.ask("any errors?")

    assert turn.evidence[0].is_error
    assert "stream selector" in turn.evidence[0].error
    last = session._client.messages.requests[1]["messages"][-1]
    assert last["content"][0]["is_error"] is True


def test_a_refusal_is_handled_without_pretending_to_answer(
    registry, audit, tmp_path
) -> None:
    session = make_session(
        registry, audit, [_Response([], stop_reason="refusal")], tmp_path
    )

    turn = session.ask("do something forbidden")

    assert not turn.supported
    assert "refusal" in turn.unsupported_reason


def test_the_loop_stops_after_max_turns(registry, audit, tmp_path) -> None:
    """A model that never concludes must not spin forever."""
    forever = [
        _Response(
            [tool_use("read_runbook", {"path": None}, f"t{i}")], stop_reason="tool_use"
        )
        for i in range(MAX_TURNS + 5)
    ]
    session = make_session(registry, audit, forever, tmp_path)

    turn = session.ask("loop please")

    assert turn.model_calls == MAX_TURNS
    assert not turn.supported
    assert "without concluding" in turn.unsupported_reason


# --- the budget -------------------------------------------------------------


def test_the_budget_stops_the_loop_mid_investigation(registry, audit, tmp_path) -> None:
    """The cap must abort between turns, not report the bill afterwards."""
    many = [
        _Response(
            [tool_use("read_runbook", {"path": None}, f"t{i}")], stop_reason="tool_use"
        )
        for i in range(MAX_TURNS)
    ]
    # 1000 in + 200 out on opus-5 = $0.005 + $0.005 = $0.010 per call.
    session = make_session(registry, audit, many, tmp_path, budget=0.025)

    turn = session.ask("expensive question")

    assert turn.model_calls < MAX_TURNS, "should have stopped early"
    assert session.total_cost_usd >= 0.025
    assert not turn.supported
    assert "spend cap" in turn.unsupported_reason


def test_a_second_question_past_the_cap_is_refused_before_any_call(
    registry, audit, tmp_path
) -> None:
    session = make_session(
        registry,
        audit,
        [_Response([text("first [repo:11111111]")])],
        tmp_path,
        budget=0.005,
    )
    session.ask("first question")
    calls_before = len(session._client.messages.requests)

    with pytest.raises(BudgetExceeded):
        session.ask("second question")

    assert len(session._client.messages.requests) == calls_before, (
        "no model call was made"
    )


def test_cost_is_computed_from_real_token_counts(registry, audit, tmp_path) -> None:
    session = make_session(
        registry,
        audit,
        [_Response([text("answer [repo:11111111]")], usage=_Usage(i=10_000, o=1_000))],
        tmp_path,
    )

    turn = session.ask("q")

    # opus-5: $5/Mtok in, $25/Mtok out -> 0.05 + 0.025
    assert turn.cost_usd == pytest.approx(0.075, abs=1e-6)
    assert turn.receipt["cost"]["input_tokens"] == 10_000


# --- the cited-or-flagged rule ----------------------------------------------


def test_an_answer_citing_real_evidence_is_supported(registry, audit, tmp_path) -> None:
    session = make_session(
        registry,
        audit,
        [
            _Response([tool_use("read_runbook", {"path": None})], stop_reason="tool_use"),
            _Response([text("PLACEHOLDER")]),
        ],
        tmp_path,
    )
    # Patch the final text to cite the citation_id the tool actually produced.
    original_create = session._client.messages.create

    def create(**kw):
        response = original_create(**kw)
        if response.content and getattr(response.content[0], "text", "") == "PLACEHOLDER":
            cid = session.registry.calls[-1]["citation_id"]
            response.content = [text(f"Three documents exist [{cid}].")]
        return response

    session._client.messages.create = create
    turn = session.ask("what runbooks exist?")

    assert turn.supported, turn.unsupported_reason
    assert len(turn.citations) == 1


def test_an_answer_citing_evidence_that_does_not_exist_is_flagged(
    registry, audit, tmp_path
) -> None:
    """The fabricated-citation case."""
    session = make_session(
        registry,
        audit,
        [
            _Response([tool_use("read_runbook", {"path": None})], stop_reason="tool_use"),
            _Response([text("Latency was fine [prometheus:99999999].")]),
        ],
        tmp_path,
    )

    turn = session.ask("how is latency?")

    assert not turn.supported
    assert "did not produce" in turn.unsupported_reason
    assert "prometheus:99999999" in turn.unsupported_reason


def test_an_uncited_factual_answer_is_flagged(registry, audit, tmp_path) -> None:
    session = make_session(
        registry,
        audit,
        [
            _Response([tool_use("read_runbook", {"path": None})], stop_reason="tool_use"),
            _Response([text("Everything is running normally and latency is low.")]),
        ],
        tmp_path,
    )

    turn = session.ask("how is the system?")

    assert not turn.supported
    assert "cites no tool result" in turn.unsupported_reason


def test_an_honest_decline_is_supported_even_with_no_citations(
    registry, audit, tmp_path
) -> None:
    """Refusing to answer is the behaviour we want; flagging it would punish it."""
    session = make_session(
        registry,
        audit,
        [
            _Response(
                [text("The data does not show this — no metric tracks user signups.")]
            )
        ],
        tmp_path,
    )

    turn = session.ask("how many users signed up last week?")

    assert turn.supported, turn.unsupported_reason
    assert turn.citations == []


# --- receipts ---------------------------------------------------------------


def test_every_answer_produces_a_verifiable_receipt(registry, audit, tmp_path) -> None:
    session = make_session(
        registry, audit, [_Response([text("The data does not show this.")])], tmp_path
    )

    turn = session.ask("q")

    assert verify_receipt(turn.receipt, "t", set()).passed
    assert turn.receipt["question"] == "q"
    assert turn.receipt["extensions"]["copilot"]["mode"] == "chat"


def test_receipts_across_a_session_form_an_intact_chain(
    registry, audit, tmp_path
) -> None:
    session = make_session(
        registry,
        audit,
        [
            _Response([text("The data does not show this.")]),
            _Response([text("Nor does it show that.")]),
            _Response([text("No data for that either.")]),
        ],
        tmp_path,
    )

    for q in ("q1", "q2", "q3"):
        session.ask(q)

    checks = verify_chain([(f"r{i}", r) for i, r in enumerate(session.chain.receipts)])
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]


def test_the_unsupported_flag_reaches_the_receipt(registry, audit, tmp_path) -> None:
    session = make_session(
        registry, audit, [_Response([text("Everything is fine.")])], tmp_path
    )

    turn = session.ask("how is it?")

    assert turn.receipt["answer"]["supported"] is False
    assert turn.receipt["answer"]["unsupported_reason"]


def test_the_receipt_records_the_evidence_digests(registry, audit, tmp_path) -> None:
    session = make_session(
        registry,
        audit,
        [
            _Response([tool_use("read_runbook", {"path": None})], stop_reason="tool_use"),
            _Response([text("The data does not show more.")]),
        ],
        tmp_path,
    )

    turn = session.ask("q")
    evidence = turn.receipt["evidence"][0]

    assert evidence["tool"] == "read_runbook"
    assert evidence["result_digest"].startswith("sha256:")
    assert evidence["provenance"]["source"] == "repo"


def test_the_audit_trail_records_every_call_with_approved_by_null(
    registry, audit, tmp_path
) -> None:
    session = make_session(
        registry,
        audit,
        [
            _Response([tool_use("read_runbook", {"path": None})], stop_reason="tool_use"),
            _Response([text("The data does not show more.")]),
        ],
        tmp_path,
    )

    session.ask("q")

    actions = [e.action for e in audit.entries]
    assert "mcp_tool:read_runbook" in actions
    assert actions.count("call_model") == 2
    assert "answer" in actions
    assert all(e.approved_by is None for e in audit.entries)


def test_tool_results_are_serialised_as_json_for_the_model(
    registry, audit, tmp_path
) -> None:
    """Not a prose summary — a second place for a bug to change what it saw."""
    session = make_session(
        registry,
        audit,
        [
            _Response([tool_use("read_runbook", {"path": None})], stop_reason="tool_use"),
            _Response([text("The data does not show more.")]),
        ],
        tmp_path,
    )

    session.ask("q")

    content = session._client.messages.requests[1]["messages"][-1]["content"][0][
        "content"
    ]
    assert json.loads(content)["index"] is True
