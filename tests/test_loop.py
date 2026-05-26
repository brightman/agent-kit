"""tests/test_loop.py — Stage 2 AgentLoop integration tests.

Coverage:
- Simple final_text in one round
- Multi-round with tool calls then final_text
- Cancel before / between rounds / mid-tool
- Max rounds reached (last round tools masked)
- Compactor: should_compact True → context_compacted event;
             compactor breaks pairs → error event stage=compactor
- 4 hooks short-circuit each emits correct *_short_circuited event
- Hook exception → error event stage=hook with hook_class + method
- Provider exception → error event stage=provider
- Event tree integrity (parent_event_id pointers)
- after_model rewrites response (no short_circuited event for after_*)
- after_tool rewrites result
"""

from __future__ import annotations

import asyncio

import pytest

from agent_kit.context import TruncatingCompactor
from agent_kit.hooks import Hook
from agent_kit.loop import AgentLoop
from agent_kit.provider import LlmResponse
from agent_kit.types import Event, ToolCall, ToolResult

from tests._helpers import (
    RaisingProvider,
    RecordingToolset,
    ScriptedProvider,
    make_ctx,
    make_request,
    text_response,
    tool_call_response,
)


async def _drain(loop_run) -> list[Event]:
    return [evt async for evt in loop_run]


def _kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


# ---- basic flows ----


@pytest.mark.asyncio
async def test_single_round_final_text() -> None:
    """Provider returns text + no tool_calls → final_text + round_end + done."""
    provider = ScriptedProvider([text_response("hello world")])
    loop = AgentLoop(provider, toolsets=[])
    events = await _drain(loop.run(make_request(), make_ctx()))
    kinds = _kinds(events)
    assert kinds == [
        "round_start", "llm_request", "llm_response",
        "final_text", "round_end",
    ]
    final = next(e for e in events if e.kind == "final_text")
    assert final.payload["text"] == "hello world"


@pytest.mark.asyncio
async def test_tool_call_then_final_text() -> None:
    tool_calls = [ToolCall(id="c1", name="echo", arguments={"x": 1})]
    provider = ScriptedProvider([
        tool_call_response(*tool_calls),
        text_response("all done"),
    ])
    ts = RecordingToolset("test", {"echo": lambda args: f"echo:{args['x']}"})
    loop = AgentLoop(provider, toolsets=[ts])
    events = await _drain(loop.run(make_request(max_rounds=5), make_ctx()))
    kinds = _kinds(events)
    # round 0: tool dispatch round
    assert kinds.count("round_start") == 2
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("final_text") == 1
    assert ts.execute_calls[0].name == "echo"


@pytest.mark.asyncio
async def test_messages_thread_grows_with_tool_result() -> None:
    """After tool result, next chat call sees assistant + tool messages appended."""
    tc = ToolCall(id="c1", name="get_x", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("ok"),
    ])
    ts = RecordingToolset("test", {"get_x": "result_value"})
    loop = AgentLoop(provider, toolsets=[ts])
    await _drain(loop.run(make_request(max_rounds=5), make_ctx()))
    # second chat call's messages should include user + assistant(tool_calls) + tool
    msgs_round1 = provider.calls[1]["messages"]
    roles = [m.role for m in msgs_round1]
    assert roles == ["user", "assistant", "tool"]
    assert msgs_round1[-1].tool_call_id == "c1"
    assert msgs_round1[-1].content == "result_value"


@pytest.mark.asyncio
async def test_system_prelude_emitted() -> None:
    provider = ScriptedProvider([text_response()])
    loop = AgentLoop(provider, toolsets=[], system_prelude="GLOBAL")
    await _drain(loop.run(
        make_request(system_prelude="REQ", max_rounds=2),
        make_ctx(),
    ))
    msgs = provider.calls[0]["messages"]
    assert msgs[0].role == "system"
    assert "GLOBAL" in msgs[0].content
    assert "REQ" in msgs[0].content


# ---- cancel ----


@pytest.mark.asyncio
async def test_cancel_before_first_round() -> None:
    provider = ScriptedProvider([text_response("never")])
    loop = AgentLoop(provider, toolsets=[])
    cancel = asyncio.Event()
    cancel.set()
    events = await _drain(loop.run(make_request(), make_ctx(cancel=cancel)))
    assert _kinds(events) == ["cancelled"]
    assert len(provider.calls) == 0   # never called


@pytest.mark.asyncio
async def test_cancel_between_rounds() -> None:
    """Set cancel during first round so next round's pre-check sees it."""
    tc = ToolCall(id="c1", name="echo", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("should never get called"),
    ])
    cancel = asyncio.Event()

    def handler(args):
        cancel.set()    # fire during tool execution
        return "ran"

    ts = RecordingToolset("t", {"echo": handler})
    loop = AgentLoop(provider, toolsets=[ts])
    events = await _drain(
        loop.run(make_request(max_rounds=5), make_ctx(cancel=cancel))
    )
    kinds = _kinds(events)
    assert "cancelled" in kinds
    # only one chat call (round 0); cancel before round 1 chat
    assert len(provider.calls) == 1


# ---- max_rounds + last round masks tools ----


@pytest.mark.asyncio
async def test_max_rounds_last_round_tools_masked() -> None:
    """Provider keeps asking for tool_calls; last round tools=None forces text."""
    tc = ToolCall(id="c1", name="loop_tool", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        tool_call_response(tc),
        text_response("finally done"),  # round 2 (last), no tools
    ])
    ts = RecordingToolset("t", {"loop_tool": "x"})
    loop = AgentLoop(provider, toolsets=[ts])
    events = await _drain(loop.run(make_request(), make_ctx()))
    # round 0, round 1 had tools; round 2 should have had tools=None
    assert provider.calls[0]["tools"] is not None  # tools given
    assert provider.calls[1]["tools"] is not None
    assert provider.calls[2]["tools"] is None      # last round masked
    assert "final_text" in _kinds(events)


@pytest.mark.asyncio
async def test_exhausted_max_rounds_emits_error() -> None:
    """If provider violates spec (returns tool_calls even on tools=None), error."""
    tc = ToolCall(id="c1", name="loop_tool", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        tool_call_response(tc),  # last round, provider buggy
    ])
    ts = RecordingToolset("t", {"loop_tool": "x"})
    loop = AgentLoop(provider, toolsets=[ts])
    events = await _drain(loop.run(make_request(max_rounds=2), make_ctx()))
    last = events[-1]
    assert last.kind == "error"
    assert last.payload["stage"] == "loop"


# ---- compactor ----


@pytest.mark.asyncio
async def test_compactor_triggers_emits_context_compacted_event() -> None:
    tc = ToolCall(id="c1", name="big", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("ok", usage={"prompt_tokens": 999}),  # last_usage of round 0 chat
    ])
    # tool returns a huge result; round 1 compactor sees > budget
    big_content = "x" * 100_000
    ts = RecordingToolset("t", {"big": big_content})
    compactor = TruncatingCompactor(token_budget=10, keep_recent_tool_results=0,
                                     placeholder="[OMITTED]")
    loop = AgentLoop(provider, toolsets=[ts], compactor=compactor)
    events = await _drain(loop.run(make_request(), make_ctx()))
    kinds = _kinds(events)
    assert "context_compacted" in kinds
    cc = next(e for e in events if e.kind == "context_compacted")
    assert cc.payload["strategy"] == "truncate"
    assert cc.payload["before_count"] == cc.payload["after_count"]   # microcompact preserves count
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
    tc = ToolCall(id="c1", name="t", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("never"),
    ])
    ts = RecordingToolset("t", {"t": "result"})
    loop = AgentLoop(provider, toolsets=[ts], compactor=_BadCompactor())
    events = await _drain(loop.run(make_request(), make_ctx()))
    err = events[-1]
    assert err.kind == "error"
    assert err.payload["stage"] == "compactor"
    assert err.payload["method"] == "compact"


# ---- hooks: short-circuit ----


class _BeforeModelShortCircuits(Hook):
    async def before_model(self, ctx, messages, tools):
        return LlmResponse(text="fake from hook", tool_calls=[])


@pytest.mark.asyncio
async def test_before_model_short_circuit_skips_provider() -> None:
    provider = ScriptedProvider([], exhaust="raise")   # exhausted; should not be called
    loop = AgentLoop(provider, toolsets=[], hooks=[_BeforeModelShortCircuits()])
    events = await _drain(loop.run(make_request(), make_ctx()))
    assert len(provider.calls) == 0
    kinds = _kinds(events)
    assert "llm_short_circuited" in kinds
    assert "llm_response" not in kinds
    sc = next(e for e in events if e.kind == "llm_short_circuited")
    assert sc.payload["by_hook"] == "_BeforeModelShortCircuits"
    final = next(e for e in events if e.kind == "final_text")
    assert final.payload["text"] == "fake from hook"


class _AfterModelRewrites(Hook):
    async def after_model(self, ctx, response):
        return LlmResponse(text="REWRITTEN", tool_calls=response.tool_calls)


@pytest.mark.asyncio
async def test_after_model_rewrites_response() -> None:
    provider = ScriptedProvider([text_response("orig")])
    loop = AgentLoop(provider, toolsets=[], hooks=[_AfterModelRewrites()])
    events = await _drain(loop.run(make_request(max_rounds=2), make_ctx()))
    final = next(e for e in events if e.kind == "final_text")
    assert final.payload["text"] == "REWRITTEN"
    # llm_response event still emitted (with original); only final_text reflects rewrite
    assert "llm_response" in _kinds(events)


class _BeforeToolShortCircuits(Hook):
    async def before_tool(self, ctx, call):
        return ToolResult(call_id=call.id, content="hook-mock", is_error=False)


@pytest.mark.asyncio
async def test_before_tool_short_circuit_skips_real_execute() -> None:
    tc = ToolCall(id="c1", name="real_tool", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("done"),
    ])
    ts = RecordingToolset("t", {"real_tool": "should NOT run"})
    loop = AgentLoop(provider, toolsets=[ts], hooks=[_BeforeToolShortCircuits()])
    events = await _drain(loop.run(make_request(), make_ctx()))
    # toolset NOT called
    assert len(ts.execute_calls) == 0
    kinds = _kinds(events)
    assert "tool_short_circuited" in kinds
    # tool_result still emitted (with mocked result)
    assert "tool_result" in kinds
    # next chat's tool message has hook-mock content
    msgs = provider.calls[1]["messages"]
    assert msgs[-1].role == "tool"
    assert msgs[-1].content == "hook-mock"


class _AfterToolRewrites(Hook):
    async def after_tool(self, ctx, call, result):
        return ToolResult(call_id=call.id, content="REDACTED", is_error=False)


@pytest.mark.asyncio
async def test_after_tool_rewrites_result() -> None:
    tc = ToolCall(id="c1", name="echo", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("done"),
    ])
    ts = RecordingToolset("t", {"echo": "real_data"})
    loop = AgentLoop(provider, toolsets=[ts], hooks=[_AfterToolRewrites()])
    await _drain(loop.run(make_request(), make_ctx()))
    # toolset called (after_tool only rewrites the result)
    assert len(ts.execute_calls) == 1
    # next chat's tool message has redacted content
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


@pytest.mark.asyncio
async def test_first_non_none_wins_skips_later_hooks() -> None:
    h1 = _AlwaysNone()
    h2 = _ShortCircuit("first")
    h3 = _ShortCircuit("second")
    provider = ScriptedProvider([], exhaust="raise")
    loop = AgentLoop(provider, toolsets=[], hooks=[h1, h2, h3])
    events = await _drain(loop.run(make_request(max_rounds=2), make_ctx()))
    final = next(e for e in events if e.kind == "final_text")
    assert final.payload["text"] == "first"
    assert h2.called is True
    assert h3.called is False   # short-circuited away


# ---- hook exceptions ----


class _BeforeToolRaises(Hook):
    async def before_tool(self, ctx, call):
        raise ValueError("hook says no")


@pytest.mark.asyncio
async def test_hook_exception_emits_error_event() -> None:
    tc = ToolCall(id="c1", name="t", arguments={})
    provider = ScriptedProvider([tool_call_response(tc)])
    ts = RecordingToolset("t", {"t": "x"})
    loop = AgentLoop(provider, toolsets=[ts], hooks=[_BeforeToolRaises()])
    events = await _drain(loop.run(make_request(), make_ctx()))
    err = events[-1]
    assert err.kind == "error"
    assert err.payload["stage"] == "hook"
    assert err.payload["method"] == "before_tool"
    assert "ValueError" in err.payload["exc_type"]
    assert "hook says no" in err.payload["message"]


# ---- provider exception ----


@pytest.mark.asyncio
async def test_provider_exception_emits_error_event() -> None:
    loop = AgentLoop(RaisingProvider(message="provider exploded"), toolsets=[])
    events = await _drain(loop.run(make_request(max_rounds=2), make_ctx()))
    err = events[-1]
    assert err.kind == "error"
    assert err.payload["stage"] == "provider"
    assert "provider exploded" in err.payload["message"]


# ---- event tree integrity ----


@pytest.mark.asyncio
async def test_event_tree_parent_pointers() -> None:
    """Each non-round_start event's parent should reference a real prior event."""
    tc = ToolCall(id="c1", name="echo", arguments={})
    provider = ScriptedProvider([
        tool_call_response(tc),
        text_response("done"),
    ])
    ts = RecordingToolset("t", {"echo": "y"})
    loop = AgentLoop(provider, toolsets=[ts])
    events = await _drain(loop.run(make_request(), make_ctx()))
    ids_seen: set[str] = set()
    for e in events:
        if e.parent_event_id is not None:
            assert e.parent_event_id in ids_seen, (
                f"event {e.kind} has parent {e.parent_event_id} not seen yet"
            )
        ids_seen.add(e.event_id)


@pytest.mark.asyncio
async def test_event_ids_unique() -> None:
    provider = ScriptedProvider([text_response()])
    loop = AgentLoop(provider, toolsets=[])
    events = await _drain(loop.run(make_request(max_rounds=2), make_ctx()))
    ids = [e.event_id for e in events]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_round_start_parent_is_none() -> None:
    provider = ScriptedProvider([text_response()])
    loop = AgentLoop(provider, toolsets=[])
    events = await _drain(loop.run(make_request(max_rounds=2), make_ctx()))
    rs = next(e for e in events if e.kind == "round_start")
    assert rs.parent_event_id is None
