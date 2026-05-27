"""tests/test_parallel_tools.py — P1: parallel tool dispatch when an LLM
turn returns multiple tool_calls.

Coverage:
- Default `parallel_tools=True` runs N tools concurrently (real wall-clock
  saving when each blocks)
- `parallel_tools=False` falls back to strict sequential
- Single tool call goes through sequential path either way (no parallel
  overhead when N=1)
- Original `tool_calls` order preserved in the conversation transcript
  (regardless of completion order)
- Event order: tool_call / tool_result events may interleave but each pair
  is parented correctly via parent_event_id
- Hook error in one parallel task → run aborts (consistent with sequential)
- ToolsetRouter.execute exceptions are caught + turned into ToolResult by
  the existing wrap layer (no special handling needed for parallel)
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

import pytest

from agent_kit import Agent, BaseToolset, Hook, ToolCall, ToolResult
from agent_kit.loop import AgentLoop, RunRequest
from agent_kit.provider import LlmResponse, ToolSchema

from tests._helpers import (
    ScriptedProvider,
    make_ctx,
    make_request,
    text_response,
    tool_call_response,
)


# ---- helpers ----


class _SlowToolset(BaseToolset):
    """Each tool sleeps for `delay` seconds then returns OK. Tracks order."""

    def __init__(self, name: str, *, delay: float, tool_names: list[str]) -> None:
        self.name = name
        self._delay = delay
        self._tools = tuple(tool_names)
        self.started: list[tuple[str, float]] = []
        self.finished: list[tuple[str, float]] = []

    def build_schemas(self) -> list[ToolSchema]:
        return [ToolSchema(name=n, description="", parameters={"type": "object"})
                for n in self._tools]

    async def execute(self, call: ToolCall, ctx) -> ToolResult:
        self.started.append((call.name, time.monotonic()))
        await asyncio.sleep(self._delay)
        self.finished.append((call.name, time.monotonic()))
        return ToolResult(call_id=call.id, content=f"ok:{call.name}")


def _three_tool_response_then_done():
    """ScriptedProvider that returns 3 tool_calls in round 1, final in round 2."""
    return ScriptedProvider([
        tool_call_response(
            ToolCall(id="c1", name="alpha", arguments={}),
            ToolCall(id="c2", name="beta", arguments={}),
            ToolCall(id="c3", name="gamma", arguments={}),
        ),
        text_response("all done"),
    ])


# ---- timing: parallel is meaningfully faster ----


async def test_parallel_is_faster_than_sequential() -> None:
    """3 tools × 100ms each: sequential ≥ 300ms, parallel ≤ ~150ms."""
    provider = _three_tool_response_then_done()
    ts = _SlowToolset("slow", delay=0.1, tool_names=["alpha", "beta", "gamma"])
    loop = AgentLoop(provider, toolsets=[ts])
    req = make_request(max_rounds=3, parallel_tools=True)

    t0 = time.monotonic()
    [evt async for evt in loop.run(req, make_ctx())]
    elapsed = time.monotonic() - t0

    # 3 tools at 0.1s each: parallel finishes in ~0.1s + overhead;
    # sequential would be ≥0.3s. Use 0.25s as a safe upper bound.
    assert elapsed < 0.25, f"parallel took {elapsed:.3f}s — too slow (sequential?)"
    # All three actually ran
    assert {n for n, _ in ts.started} == {"alpha", "beta", "gamma"}
    assert len(ts.finished) == 3


async def test_sequential_takes_at_least_sum_of_delays() -> None:
    """parallel_tools=False → strict serial = sum of delays."""
    provider = _three_tool_response_then_done()
    ts = _SlowToolset("slow", delay=0.05, tool_names=["alpha", "beta", "gamma"])
    loop = AgentLoop(provider, toolsets=[ts])
    req = make_request(max_rounds=3, parallel_tools=False)

    t0 = time.monotonic()
    [evt async for evt in loop.run(req, make_ctx())]
    elapsed = time.monotonic() - t0

    # 3 × 0.05s = 0.15s minimum
    assert elapsed >= 0.13, f"sequential too fast ({elapsed:.3f}s) — gather leaked?"


# ---- ordering invariants ----


async def test_message_order_matches_tool_calls_order_not_completion_order() -> None:
    """If gamma finishes first but came last in tool_calls, the tool message
    for gamma still appears LAST in the transcript. LLM sees deterministic
    ordering regardless of completion timing."""

    class _ReverseDelayToolset(BaseToolset):
        """alpha takes longest, gamma shortest → completion order is reversed."""
        name = "rev"
        def __init__(self):
            self.execute_calls: list = []
        def build_schemas(self):
            return [ToolSchema(name=n, description="", parameters={"type": "object"})
                    for n in ("alpha", "beta", "gamma")]
        async def execute(self, call, ctx):
            self.execute_calls.append(call.name)
            delays = {"alpha": 0.15, "beta": 0.10, "gamma": 0.05}
            await asyncio.sleep(delays[call.name])
            return ToolResult(call_id=call.id, content=f"done:{call.name}")

    provider = _three_tool_response_then_done()
    ts = _ReverseDelayToolset()
    loop = AgentLoop(provider, toolsets=[ts])
    req = make_request(max_rounds=3, parallel_tools=True)
    [evt async for evt in loop.run(req, make_ctx())]

    # Second chat call sees the tool messages in original tool_calls order
    round2_msgs = provider.calls[1]["messages"]
    tool_msgs = [m for m in round2_msgs if m.role == "tool"]
    assert [m.content for m in tool_msgs] == ["done:alpha", "done:beta", "done:gamma"]
    assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2", "c3"]


async def test_event_pairs_intact_under_parallel() -> None:
    """Each tool_call event has a tool_result event with matching parent.
    Order may interleave but counts match and parent_event_ids resolve."""
    provider = _three_tool_response_then_done()
    ts = _SlowToolset("slow", delay=0.02, tool_names=["alpha", "beta", "gamma"])
    loop = AgentLoop(provider, toolsets=[ts])
    events = [evt async for evt in loop.run(
        make_request(max_rounds=3, parallel_tools=True), make_ctx(),
    )]

    tool_calls = [e for e in events if e.kind == "tool_call"]
    tool_results = [e for e in events if e.kind == "tool_result"]
    assert len(tool_calls) == 3
    assert len(tool_results) == 3

    # Each tool_result's parent_event_id == some tool_call's event_id
    call_ids = {e.event_id for e in tool_calls}
    for r in tool_results:
        assert r.parent_event_id in call_ids


async def test_n1_uses_sequential_path_even_when_parallel_default() -> None:
    """Single tool call: no parallel machinery needed. Just verify it works."""
    provider = ScriptedProvider([
        tool_call_response(ToolCall(id="c1", name="alpha", arguments={})),
        text_response("one done"),
    ])
    ts = _SlowToolset("slow", delay=0.0, tool_names=["alpha"])
    loop = AgentLoop(provider, toolsets=[ts])
    events = [evt async for evt in loop.run(
        make_request(max_rounds=3, parallel_tools=True), make_ctx(),
    )]
    kinds = [e.kind for e in events]
    assert "final_text" in kinds
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1


# ---- error / hook paths under parallel ----


async def test_hook_error_in_one_parallel_task_aborts_run() -> None:
    """If before_tool raises on call #2, the run still emits an error event
    and aborts (we don't continue past the failed turn — consistent with
    sequential semantics)."""

    class _PoisonHook(Hook):
        async def before_tool(self, ctx, call):
            if call.id == "c2":
                raise ValueError(f"poisoned {call.id}")
            return None

    provider = _three_tool_response_then_done()
    ts = _SlowToolset("slow", delay=0.0, tool_names=["alpha", "beta", "gamma"])
    loop = AgentLoop(provider, toolsets=[ts], hooks=[_PoisonHook()])
    events = [evt async for evt in loop.run(
        make_request(max_rounds=3, parallel_tools=True), make_ctx(),
    )]

    # We got an error event from the hook failure
    err = [e for e in events if e.kind == "error"]
    assert any(e.payload["stage"] == "hook" for e in err)
    # The run aborts → no round 2 chat
    assert len(provider.calls) == 1


async def test_parallel_off_falls_back_strictly_sequential() -> None:
    """parallel_tools=False → tools start one at a time (started timestamps
    are non-overlapping)."""
    provider = _three_tool_response_then_done()
    ts = _SlowToolset("slow", delay=0.03, tool_names=["alpha", "beta", "gamma"])
    loop = AgentLoop(provider, toolsets=[ts])
    [evt async for evt in loop.run(
        make_request(max_rounds=3, parallel_tools=False), make_ctx(),
    )]

    # Each "started" timestamp must come AFTER the previous "finished".
    # In parallel mode they'd all start before any finish.
    starts = [t for _, t in ts.started]
    finishes = [t for _, t in ts.finished]
    for i in range(1, len(starts)):
        assert starts[i] >= finishes[i - 1] - 0.001, (
            f"tool {i} started before tool {i-1} finished — parallel leak"
        )


# ---- counts unchanged (back-compat with non-tool-batch tests) ----


async def test_parallel_default_doesnt_affect_single_round_no_tools() -> None:
    """Pure text-only response: no tools, no parallel path engaged."""
    provider = ScriptedProvider([text_response("plain")])
    loop = AgentLoop(provider, toolsets=[])
    events = [evt async for evt in loop.run(make_request(), make_ctx())]
    counts = Counter(e.kind for e in events)
    assert counts["final_text"] == 1
    assert counts["tool_call"] == 0
