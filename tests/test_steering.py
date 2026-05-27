"""tests/test_steering.py — P0: mid-run user-message injection via
`Agent.send_steering()` / `RunRequest.steering_drain`.

Coverage:
- send_steering enqueues; loop drains at next round_start
- Multiple steering messages drained in FIFO order, one round each call
- Empty / non-string steering values are dropped silently
- Drain happens BEFORE the LLM is called (so the model sees it next round)
- `user_message_added` event emitted with round / text / source payload
- pending_steering() reflects queue size between drains
- steering_drain that raises is swallowed (run continues, like cancel_check)
"""

from __future__ import annotations

import pytest

from agent_kit import Agent, ToolCall
from agent_kit.loop import AgentLoop, RunRequest
from agent_kit.provider import LlmResponse

from tests._helpers import (
    ScriptedProvider,
    make_ctx,
    make_request,
    text_response,
    tool_call_response,
)


# ---- queue mechanics ----


def test_send_steering_enqueues_text() -> None:
    a = Agent(name="x", model=ScriptedProvider())
    assert a.pending_steering() == 0
    a.send_steering("hello")
    a.send_steering("world")
    assert a.pending_steering() == 2


def test_send_steering_drops_empty_and_non_string() -> None:
    a = Agent(name="x", model=ScriptedProvider())
    a.send_steering("")
    a.send_steering(None)  # type: ignore[arg-type]
    a.send_steering(123)   # type: ignore[arg-type]
    assert a.pending_steering() == 0


def test_drain_clears_queue() -> None:
    a = Agent(name="x", model=ScriptedProvider())
    a.send_steering("foo")
    a.send_steering("bar")
    out = a._drain_steering()
    assert out == ["foo", "bar"]
    assert a.pending_steering() == 0


# ---- loop integration: drain at round_start ----


class _TwoRoundScripter:
    """Round 1 → tool_call; round 2 → final_text."""

    name = "scripter"

    def __init__(self) -> None:
        self.calls: list[list] = []
        self._n = 0

    async def chat(self, messages, tools=None, **kw):
        self.calls.append([m for m in messages])
        self._n += 1
        if self._n == 1:
            return tool_call_response(ToolCall(id="c1", name="noop", arguments={}))
        return LlmResponse(text="done", tool_calls=[], usage={}, raw={}, finish_reason="stop")

    async def chat_stream(self, *a, **k):
        raise NotImplementedError


async def test_steering_drained_at_round_start_appended_to_context() -> None:
    """A pre-enqueued steering message lands in the provider's first chat call
    (because round 1's round_start runs before the LLM call)."""
    provider = ScriptedProvider([text_response("hi")])
    drain_calls = {"n": 0}
    def drain() -> list[str]:
        drain_calls["n"] += 1
        return ["INJECTED"] if drain_calls["n"] == 1 else []

    loop = AgentLoop(provider, toolsets=[])
    req = make_request(user_message="original", steering_drain=drain)
    events = [evt async for evt in loop.run(req, make_ctx())]

    # The first (and only) provider chat call saw BOTH original + injected
    sent = [m.content for m in provider.calls[0]["messages"]]
    assert "original" in sent
    assert "INJECTED" in sent

    # And we emitted a user_message_added event with correct payload
    added = [e for e in events if e.kind == "user_message_added"]
    assert len(added) == 1
    assert added[0].payload == {"round": 0, "text": "INJECTED", "source": "steering"}


async def test_send_steering_during_run_picked_up_next_round() -> None:
    """End-to-end through Agent: ScriptedProvider returns a tool call in
    round 1; while we're between rounds we shove a steering message in;
    round 2 chat sees it before the LLM continues."""
    from tests._helpers import RecordingToolset

    toolset = RecordingToolset("t", {"noop": "ok"})
    provider = _TwoRoundScripter()

    class _PostToolHook:
        """after_tool fires AT the end of round 1, between rounds. That's
        when a real TUI would call send_steering()."""
        def __init__(self, agent: "Agent") -> None:
            self.agent = agent
            self.fired = False
        async def before_model(self, ctx, messages, tools): return None
        async def after_model(self, ctx, response): return None
        async def before_tool(self, ctx, call): return None
        async def after_tool(self, ctx, call, result):
            if not self.fired:
                self.agent.send_steering("MID-RUN STEERING")
                self.fired = True
            return None

    # Two-pass: first build the agent, then attach the hook that needs the
    # agent reference. Easiest: bind after construction.
    from agent_kit.hooks import Hook

    class _BoundHook(Hook):
        def __init__(self, agent_ref):
            self._a = agent_ref
            self.fired = False
        async def after_tool(self, ctx, call, result):
            if not self.fired:
                self._a.send_steering("MID-RUN STEERING")
                self.fired = True
            return None

    agent = Agent(name="x", model=provider, tools=[toolset])
    # Attach hook with cycle reference
    hook = _BoundHook(agent)
    agent.runner._hooks.append(hook)

    result = await agent.run("first turn")

    # Round 2's chat saw the steering message
    round2_msgs = [m.content for m in provider.calls[1]]
    assert "MID-RUN STEERING" in round2_msgs

    # And event stream emitted user_message_added in round 1 (wait, no -
    # we sent the steering at end of round 0's after_tool; it gets drained
    # at the TOP of round 1).
    added = [e for e in result.events if e.kind == "user_message_added"]
    assert len(added) == 1
    assert added[0].payload["round"] == 1
    assert added[0].payload["text"] == "MID-RUN STEERING"

    # Queue is empty after the run
    assert agent.pending_steering() == 0


async def test_multiple_steering_drained_in_fifo_at_one_round_start() -> None:
    """All pending messages flush at the same round_start (we don't trickle)."""
    provider = ScriptedProvider([text_response("done")])
    drain_calls = {"n": 0}
    def drain() -> list[str]:
        drain_calls["n"] += 1
        if drain_calls["n"] == 1:
            return ["first", "second", "third"]
        return []

    loop = AgentLoop(provider, toolsets=[])
    req = make_request(steering_drain=drain)
    events = [evt async for evt in loop.run(req, make_ctx())]

    added = [e for e in events if e.kind == "user_message_added"]
    assert [e.payload["text"] for e in added] == ["first", "second", "third"]
    # All in round 0
    assert {e.payload["round"] for e in added} == {0}

    # Provider saw all of them
    sent = [m.content for m in provider.calls[0]["messages"]]
    for t in ["first", "second", "third"]:
        assert t in sent


async def test_steering_drain_returning_empty_no_event() -> None:
    """When drain returns [] no user_message_added event is emitted."""
    provider = ScriptedProvider([text_response("ok")])
    loop = AgentLoop(provider, toolsets=[])
    req = make_request(steering_drain=lambda: [])
    events = [evt async for evt in loop.run(req, make_ctx())]
    assert not any(e.kind == "user_message_added" for e in events)


async def test_steering_drain_raising_is_swallowed(caplog) -> None:
    """A buggy steering_drain shouldn't crash the run; warn + continue."""
    import logging

    def boom() -> list[str]:
        raise RuntimeError("buggy drain")

    provider = ScriptedProvider([text_response("survived")])
    loop = AgentLoop(provider, toolsets=[])
    req = make_request(steering_drain=boom)
    with caplog.at_level(logging.WARNING, logger="agent_kit.loop"):
        events = [evt async for evt in loop.run(req, make_ctx())]
    assert any(e.kind == "final_text" for e in events)
    assert any("steering_drain raised" in r.message for r in caplog.records)


# ---- default behavior unchanged ----


async def test_no_steering_drain_no_event_no_extra_messages() -> None:
    """Default RunRequest (drain=None) emits zero user_message_added events
    and doesn't touch the message thread."""
    provider = ScriptedProvider([text_response("plain")])
    loop = AgentLoop(provider, toolsets=[])
    events = [evt async for evt in loop.run(make_request(), make_ctx())]
    assert not any(e.kind == "user_message_added" for e in events)
