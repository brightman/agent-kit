"""tests/test_loop.py — AgentLoop semantics driven through Agent.

Phase 2 refactor: most tests now construct an `Agent` and verify behavior via
`result.events` + `provider.calls` rather than instantiating `AgentLoop`
directly. The few cases that need the event-streaming surface (errors becoming
events instead of raising) drop down to `agent.runner.run()`.

What's verified:
- Single-round / multi-round event sequences
- Tool dispatch + message threading (assistant→tool→assistant)
- system_prelude composition
- max_rounds last-round tools=None masking + exhaustion error
- Compactor invocation + pair-invariant guard
- 4 hooks: before_model / after_model / before_tool / after_tool
- First-non-None hook ordering
- Hook + provider exceptions become error events (not raises)
- Event tree integrity (parent_event_id, unique IDs, root round_start)
- Cancel before first round / between rounds (via cancel_check)
"""

from __future__ import annotations

import pytest

from agent_kit import Agent, Hook
from agent_kit.context import TruncatingCompactor
from agent_kit.provider import LlmResponse
from agent_kit.types import Event, ToolCall, ToolResult

from tests._helpers import (
    RaisingProvider,
    RecordingToolset,
    ScriptedProvider,
    make_request,
    text_response,
    tool_call_response,
)


def _kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


# ---- basic flows ----


def test_single_round_final_text() -> None:
    """Provider returns text + no tool_calls → expected event sequence."""
    a = Agent(name="x", model=ScriptedProvider([text_response("hello world")]))
    result = a.run_sync("hi")
    assert _kinds(result.events) == [
        "round_start", "llm_request", "llm_response",
        "final_text", "round_end",
    ]
    assert result.final_text == "hello world"


def test_tool_call_then_final_text() -> None:
    """Tool round + final text → two rounds, one tool dispatch."""
    tool_calls = [ToolCall(id="c1", name="echo", arguments={"x": 1})]
    provider = ScriptedProvider([
        tool_call_response(*tool_calls),
        text_response("all done"),
    ])
    ts = RecordingToolset("test", {"echo": lambda args: f"echo:{args['x']}"})
    a = Agent(name="x", model=provider, tools=[ts])
    result = a.run_sync("hi", max_rounds=5)
    kinds = _kinds(result.events)
    assert kinds.count("round_start") == 2
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("final_text") == 1
    assert ts.execute_calls[0].name == "echo"


def test_messages_thread_grows_with_tool_result() -> None:
    """After tool result, next chat call sees assistant + tool messages appended."""
    tc = ToolCall(id="c1", name="get_x", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("ok"),
    ])
    ts = RecordingToolset("test", {"get_x": "result_value"})
    a = Agent(name="x", model=provider, tools=[ts])
    a.run_sync("hi", max_rounds=5)
    msgs_round1 = provider.calls[1]["messages"]
    roles = [m.role for m in msgs_round1]
    assert roles == ["user", "assistant", "tool"]
    assert msgs_round1[-1].tool_call_id == "c1"
    assert msgs_round1[-1].content == "result_value"


def test_system_prelude_emitted() -> None:
    """instruction= becomes the system message prelude."""
    provider = ScriptedProvider([text_response()])
    a = Agent(name="x", model=provider, instruction="GLOBAL INSTRUCTION")
    a.run_sync("hi", max_rounds=2)
    msgs = provider.calls[0]["messages"]
    assert msgs[0].role == "system"
    assert "GLOBAL INSTRUCTION" in msgs[0].content


# ---- max_rounds + last round masks tools ----


def test_max_rounds_last_round_tools_masked() -> None:
    """Provider keeps asking for tool_calls; last round tools=None forces text."""
    tc = ToolCall(id="c1", name="loop_tool", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        tool_call_response(tc),
        text_response("finally done"),
    ])
    ts = RecordingToolset("t", {"loop_tool": "x"})
    a = Agent(name="x", model=provider, tools=[ts])
    result = a.run_sync("hi", max_rounds=3)
    assert provider.calls[0]["tools"] is not None
    assert provider.calls[1]["tools"] is not None
    assert provider.calls[2]["tools"] is None  # last round masked
    assert "final_text" in _kinds(result.events)


@pytest.mark.asyncio
async def test_exhausted_max_rounds_emits_error() -> None:
    """If provider violates spec (returns tool_calls on tools=None), error.

    Needs runner.run() — Agent.run_sync would raise; here we verify the
    error is reported as an EVENT in the stream.
    """
    tc = ToolCall(id="c1", name="loop_tool", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        tool_call_response(tc),  # last round, provider buggy
    ])
    ts = RecordingToolset("t", {"loop_tool": "x"})
    a = Agent(name="x", model=provider, tools=[ts])
    events = [e async for e in a.runner.run(make_request(max_rounds=2))]
    last = events[-1]
    assert last.kind == "error"
    assert last.payload["stage"] == "loop"


# ---- compactor ----


def test_compactor_triggers_emits_context_compacted_event() -> None:
    tc = ToolCall(id="c1", name="big", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("ok", usage={"prompt_tokens": 999}),
    ])
    big_content = "x" * 100_000
    ts = RecordingToolset("t", {"big": big_content})
    compactor = TruncatingCompactor(token_budget=10, keep_recent_tool_results=0,
                                     placeholder="[OMITTED]")
    a = Agent(name="x", model=provider, tools=[ts], compactor=compactor)
    result = a.run_sync("hi")
    kinds = _kinds(result.events)
    assert "context_compacted" in kinds
    cc = next(e for e in result.events if e.kind == "context_compacted")
    assert cc.payload["strategy"] == "truncate"
    assert cc.payload["before_count"] == cc.payload["after_count"]
    assert cc.payload["after_tokens"] < cc.payload["before_tokens"]


class _BadCompactor:
    """Compactor that breaks tool pair invariant — SDK should catch + emit error."""
    name = "bad"

    async def should_compact(self, messages, last_usage):
        return True

    async def compact(self, messages):
        # Drop the assistant.tool_calls but keep its tool message → orphan
        return [m for m in messages if not (m.role == "assistant" and m.tool_calls)]


@pytest.mark.asyncio
async def test_compactor_breaks_pairs_emits_error_event() -> None:
    """Pair-invariant guard catches a buggy compactor → error event (not raise)."""
    tc = ToolCall(id="c1", name="t", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("never"),
    ])
    ts = RecordingToolset("t", {"t": "result"})
    a = Agent(name="x", model=provider, tools=[ts], compactor=_BadCompactor())
    events = [e async for e in a.runner.run(make_request())]
    err = events[-1]
    assert err.kind == "error"
    assert err.payload["stage"] == "compactor"
    assert err.payload["method"] == "compact"


# ---- hooks: short-circuit ----


class _BeforeModelShortCircuits(Hook):
    async def before_model(self, ctx, messages, tools):
        return LlmResponse(text="fake from hook", tool_calls=[])


def test_before_model_short_circuit_skips_provider() -> None:
    provider = ScriptedProvider([], exhaust="raise")   # should not be called
    a = Agent(name="x", model=provider, hooks=[_BeforeModelShortCircuits()])
    result = a.run_sync("hi")
    assert len(provider.calls) == 0
    kinds = _kinds(result.events)
    assert "llm_short_circuited" in kinds
    assert "llm_response" not in kinds
    sc = next(e for e in result.events if e.kind == "llm_short_circuited")
    assert sc.payload["by_hook"] == "_BeforeModelShortCircuits"
    final = next(e for e in result.events if e.kind == "final_text")
    assert final.payload["text"] == "fake from hook"


class _AfterModelRewrites(Hook):
    async def after_model(self, ctx, response):
        return LlmResponse(text="REWRITTEN", tool_calls=response.tool_calls)


def test_after_model_rewrites_response() -> None:
    provider = ScriptedProvider([text_response("orig")])
    a = Agent(name="x", model=provider, hooks=[_AfterModelRewrites()])
    result = a.run_sync("hi", max_rounds=2)
    final = next(e for e in result.events if e.kind == "final_text")
    assert final.payload["text"] == "REWRITTEN"
    assert "llm_response" in _kinds(result.events)


class _BeforeToolShortCircuits(Hook):
    async def before_tool(self, ctx, call):
        return ToolResult(call_id=call.id, content="hook-mock", is_error=False)


def test_before_tool_short_circuit_skips_real_execute() -> None:
    tc = ToolCall(id="c1", name="real_tool", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("done"),
    ])
    ts = RecordingToolset("t", {"real_tool": "should NOT run"})
    a = Agent(name="x", model=provider, tools=[ts], hooks=[_BeforeToolShortCircuits()])
    result = a.run_sync("hi")
    assert len(ts.execute_calls) == 0
    kinds = _kinds(result.events)
    assert "tool_short_circuited" in kinds
    assert "tool_result" in kinds
    msgs = provider.calls[1]["messages"]
    assert msgs[-1].role == "tool"
    assert msgs[-1].content == "hook-mock"


class _AfterToolRewrites(Hook):
    async def after_tool(self, ctx, call, result):
        return ToolResult(call_id=call.id, content="REDACTED", is_error=False)


def test_after_tool_rewrites_result() -> None:
    tc = ToolCall(id="c1", name="echo", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("done"),
    ])
    ts = RecordingToolset("t", {"echo": "real_data"})
    a = Agent(name="x", model=provider, tools=[ts], hooks=[_AfterToolRewrites()])
    a.run_sync("hi")
    assert len(ts.execute_calls) == 1   # real toolset ran
    msgs = provider.calls[1]["messages"]
    assert msgs[-1].content == "REDACTED"


# ---- hook first-non-None semantics ----


class _AlwaysNone(Hook):
    async def before_model(self, ctx, messages, tools):
        return None


class _ShortCircuit(Hook):
    def __init__(self, text: str) -> None:
        self.text = text
        self.called = False

    async def before_model(self, ctx, messages, tools):
        self.called = True
        return LlmResponse(text=self.text, tool_calls=[])


def test_first_non_none_wins_skips_later_hooks() -> None:
    h1 = _AlwaysNone()
    h2 = _ShortCircuit("first")
    h3 = _ShortCircuit("second")
    a = Agent(
        name="x", model=ScriptedProvider([], exhaust="raise"),
        hooks=[h1, h2, h3],
    )
    result = a.run_sync("hi", max_rounds=2)
    final = next(e for e in result.events if e.kind == "final_text")
    assert final.payload["text"] == "first"
    assert h2.called is True
    assert h3.called is False   # short-circuited away


# ---- hook & provider exceptions become error events ----


class _BeforeToolRaises(Hook):
    async def before_tool(self, ctx, call):
        raise ValueError("hook says no")


@pytest.mark.asyncio
async def test_hook_exception_emits_error_event() -> None:
    """Loop wraps hook exceptions into error events. Streaming surface used to
    capture the event without it being re-raised."""
    tc = ToolCall(id="c1", name="t", arguments={})
    provider = ScriptedProvider([tool_call_response(tc)])
    ts = RecordingToolset("t", {"t": "x"})
    a = Agent(name="x", model=provider, tools=[ts], hooks=[_BeforeToolRaises()])
    events = [e async for e in a.runner.run(make_request())]
    err = events[-1]
    assert err.kind == "error"
    assert err.payload["stage"] == "hook"
    assert err.payload["method"] == "before_tool"
    assert "ValueError" in err.payload["exc_type"]
    assert "hook says no" in err.payload["message"]


@pytest.mark.asyncio
async def test_provider_exception_emits_error_event() -> None:
    """Provider exception → error event with stage=provider (in event stream)."""
    a = Agent(name="x", model=RaisingProvider(message="provider exploded"))
    events = [e async for e in a.runner.run(make_request(max_rounds=2))]
    err = events[-1]
    assert err.kind == "error"
    assert err.payload["stage"] == "provider"
    assert "provider exploded" in err.payload["message"]


# ---- event tree integrity ----


def test_event_tree_parent_pointers() -> None:
    """Each non-root event's parent_event_id must reference a real prior event."""
    tc = ToolCall(id="c1", name="echo", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("done"),
    ])
    ts = RecordingToolset("t", {"echo": "y"})
    a = Agent(name="x", model=provider, tools=[ts])
    result = a.run_sync("hi")
    ids_seen: set[str] = set()
    for e in result.events:
        if e.parent_event_id is not None:
            assert e.parent_event_id in ids_seen, (
                f"event {e.kind} has parent {e.parent_event_id} not seen yet"
            )
        ids_seen.add(e.event_id)


def test_event_ids_unique() -> None:
    a = Agent(name="x", model=ScriptedProvider([text_response()]))
    result = a.run_sync("hi", max_rounds=2)
    ids = [e.event_id for e in result.events]
    assert len(ids) == len(set(ids))


def test_round_start_parent_is_none() -> None:
    a = Agent(name="x", model=ScriptedProvider([text_response()]))
    result = a.run_sync("hi", max_rounds=2)
    rs = next(e for e in result.events if e.kind == "round_start")
    assert rs.parent_event_id is None


# ---- cancel (via cancel_check; ctx.cancel external path covered in test_cancel_check.py) ----


def test_cancel_before_first_round_via_cancel_check() -> None:
    """cancel_check=True before any provider call → cancelled event, no chat."""
    provider = ScriptedProvider([text_response("never")])
    a = Agent(name="x", model=provider)
    result = a.run_sync("hi", cancel_check=lambda: True)
    assert result.cancelled is True
    assert len(provider.calls) == 0


def test_cancel_between_rounds_via_cancel_check() -> None:
    """cancel_check flips True after first round → Round 2 sees it on top-of-loop."""
    tc = ToolCall(id="c1", name="echo", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("should never get called"),
    ])
    ts = RecordingToolset("t", {"echo": "ran"})

    poll_count = {"n": 0}

    def cancel_after_first_round() -> bool:
        poll_count["n"] += 1
        # Round 1 top-poll (n=1) False; Round 1 mid-tool poll (n=2) False;
        # Round 2 top-poll (n=3) True
        return poll_count["n"] >= 3

    a = Agent(name="x", model=provider, tools=[ts])
    result = a.run_sync("hi", cancel_check=cancel_after_first_round, max_rounds=5)
    assert result.cancelled is True
    assert len(provider.calls) == 1   # Round 2 never reached
    assert ts.execute_calls[0].name == "echo"  # Round 1 tool did run
