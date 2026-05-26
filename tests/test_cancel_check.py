"""tests/test_cancel_check.py — RunRequest.cancel_check contract (spec § 3.7.2).

Coverage:
- default None → never polled, normal completion
- True before first round → cancelled event, no provider call
- True between rounds → 1 LLM round + cancelled, reason="cancel_check"
- True before tool dispatch (mid-round) → cancelled mid_tool, reason
  "cancel_check_mid_tool", tool NOT executed
- False (always) → normal completion, polled but never triggers
- raises exception → swallowed + logged + treated as False, run continues
- ctx.cancel + cancel_check both fire → ctx.cancel wins (checked first,
  reason="external")
"""

from __future__ import annotations

import asyncio
import logging

from agent_kit.loop import AgentLoop
from agent_kit.types import Event, ToolCall

from tests._helpers import (
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


def _echo_toolset() -> RecordingToolset:
    return RecordingToolset(name="scripted_tools", handlers={"echo": "ok"})


# ---------------------------------------------------------------------------
# 1. Default / no-op
# ---------------------------------------------------------------------------


async def test_default_cancel_check_none_does_not_trigger_cancel() -> None:
    """Default cancel_check=None → 永不 cancel,normal completion。"""
    provider = ScriptedProvider([text_response()])
    loop = AgentLoop(provider, toolsets=[])
    events = await _drain(loop.run(make_request(), make_ctx()))
    assert "cancelled" not in _kinds(events)
    assert "final_text" in _kinds(events)


async def test_cancel_check_returning_false_does_not_trigger_cancel() -> None:
    """显式 cancel_check 永远返 False → 跟 None 等价的行为。"""
    poll_count = {"n": 0}

    def never_cancel() -> bool:
        poll_count["n"] += 1
        return False

    provider = ScriptedProvider([
        tool_call_response(ToolCall(id="c1", name="echo", arguments={})),
        text_response("done"),
    ])
    loop = AgentLoop(provider, toolsets=[_echo_toolset()])
    events = await _drain(
        loop.run(make_request(cancel_check=never_cancel), make_ctx())
    )
    assert "cancelled" not in _kinds(events)
    assert "final_text" in _kinds(events)
    # 至少 poll 过两次(2 round 顶部 + 1 tool dispatch 前 = 3 次起)
    assert poll_count["n"] >= 2


# ---------------------------------------------------------------------------
# 2. Cancel before first round
# ---------------------------------------------------------------------------


async def test_cancel_check_true_before_first_round_emits_cancelled_no_provider_call() -> None:
    provider = ScriptedProvider([text_response("never runs")])
    loop = AgentLoop(provider, toolsets=[])
    events = await _drain(
        loop.run(make_request(cancel_check=lambda: True), make_ctx())
    )
    assert _kinds(events) == ["cancelled"]
    assert events[0].payload["round"] == 0
    assert events[0].payload["reason"] == "cancel_check"
    # provider 完全没被调
    assert provider.calls == []


# ---------------------------------------------------------------------------
# 3. Cancel between rounds
# ---------------------------------------------------------------------------


async def test_cancel_check_true_after_first_round_completes_then_cancels() -> None:
    """Round 1 跑完(含 tool dispatch),Round 2 顶部 poll → True → cancel。"""
    poll_count = {"n": 0}

    def cancel_on_round_2() -> bool:
        poll_count["n"] += 1
        # Round 1 顶部 poll(n=1)= False;Round 1 tool dispatch poll(n=2)= False;
        # Round 2 顶部 poll(n=3)= True
        return poll_count["n"] >= 3

    provider = ScriptedProvider([
        tool_call_response(ToolCall(id="c1", name="echo", arguments={})),
        text_response("should not see"),  # Round 2 never reached
    ])
    tools = _echo_toolset()
    loop = AgentLoop(provider, toolsets=[tools])
    events = await _drain(
        loop.run(
            make_request(max_rounds=5, cancel_check=cancel_on_round_2),
            make_ctx(),
        )
    )
    kinds = _kinds(events)
    # 应该看到 Round 1 完整事件 + Round 2 顶部 cancelled
    assert "tool_call" in kinds  # Round 1 tool 跑了
    assert "tool_result" in kinds
    assert "cancelled" in kinds
    cancel_evt = next(e for e in events if e.kind == "cancelled")
    assert cancel_evt.payload["reason"] == "cancel_check"
    assert cancel_evt.payload["round"] == 1  # Round 2(0-indexed)被 cancel
    # provider 只被调 1 次(Round 1),Round 2 没到
    assert len(provider.calls) == 1
    assert tools.execute_calls == [ToolCall(id="c1", name="echo", arguments={})]


# ---------------------------------------------------------------------------
# 4. Cancel before tool dispatch (mid-round)
# ---------------------------------------------------------------------------


async def test_cancel_check_true_before_tool_dispatch_emits_mid_tool_reason() -> None:
    """LLM 返 tool_calls 后、tool 执行前 cancel_check=True → mid_tool reason,
    tool 不执行。"""
    poll_count = {"n": 0}

    def cancel_after_llm() -> bool:
        poll_count["n"] += 1
        # Round 1 顶部 poll(n=1)= False;tool dispatch 前 poll(n=2)= True
        return poll_count["n"] >= 2

    provider = ScriptedProvider([
        tool_call_response(ToolCall(id="c1", name="echo", arguments={})),
    ])
    tools = _echo_toolset()
    loop = AgentLoop(provider, toolsets=[tools])
    events = await _drain(
        loop.run(make_request(cancel_check=cancel_after_llm), make_ctx())
    )
    kinds = _kinds(events)
    # llm_response 出来了,但 tool 没执行(cancel 卡在 dispatch 前)
    assert "llm_response" in kinds
    assert "tool_call" not in kinds
    assert "tool_result" not in kinds
    cancel_evt = next(e for e in events if e.kind == "cancelled")
    assert cancel_evt.payload["reason"] == "cancel_check_mid_tool"
    assert tools.execute_calls == []  # tool 完全没跑


# ---------------------------------------------------------------------------
# 5. Exception handling
# ---------------------------------------------------------------------------


async def test_cancel_check_raising_exception_is_swallowed_run_continues(caplog) -> None:
    """cancel_check raises → 吞掉 + log,treat as False,run 正常 complete。"""
    def boom() -> bool:
        raise RuntimeError("cancel_check is buggy")

    provider = ScriptedProvider([text_response()])
    loop = AgentLoop(provider, toolsets=[])
    with caplog.at_level(logging.WARNING, logger="agent_kit.loop"):
        events = await _drain(
            loop.run(make_request(cancel_check=boom), make_ctx())
        )
    # Run 正常 complete,no cancelled event
    assert "cancelled" not in _kinds(events)
    assert "final_text" in _kinds(events)
    # log warning 至少打了一次
    assert any("cancel_check raised" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. Interaction with ctx.cancel
# ---------------------------------------------------------------------------


async def test_ctx_cancel_set_takes_precedence_over_cancel_check_in_reason() -> None:
    """ctx.cancel.is_set() + cancel_check True 同时触发 → emit reason="external"
    (ctx.cancel 优先 check)。"""
    provider = ScriptedProvider([text_response("never")])
    loop = AgentLoop(provider, toolsets=[])
    cancel_event = asyncio.Event()
    cancel_event.set()  # ctx.cancel 已经 set 了
    events = await _drain(
        loop.run(
            make_request(cancel_check=lambda: True),
            make_ctx(cancel=cancel_event),
        )
    )
    cancel_evt = next(e for e in events if e.kind == "cancelled")
    assert cancel_evt.payload["reason"] == "external"


async def test_ctx_cancel_alone_still_works_unchanged_from_before() -> None:
    """向后兼容:ctx.cancel 自己 set(没 cancel_check)→ 行为跟旧版一致,
    reason="external"。"""
    provider = ScriptedProvider([text_response("never")])
    loop = AgentLoop(provider, toolsets=[])
    cancel_event = asyncio.Event()
    cancel_event.set()
    events = await _drain(
        loop.run(make_request(), make_ctx(cancel=cancel_event))
    )
    cancel_evt = next(e for e in events if e.kind == "cancelled")
    assert cancel_evt.payload["reason"] == "external"


# ---------------------------------------------------------------------------
# 7. Reason vocab guard
# ---------------------------------------------------------------------------


async def test_cancel_event_reason_vocab_is_one_of_four_known_values() -> None:
    """spec lock:cancel reason 只 4 个 values:
    - "external" (ctx.cancel 顶部)
    - "external_mid_tool" (ctx.cancel mid-tool)
    - "cancel_check" (RunRequest.cancel_check 顶部)
    - "cancel_check_mid_tool" (RunRequest.cancel_check mid-tool)
    本测试 sanity check 顶部 2 个 reason,mid-tool 已被上面 case 各自覆盖。"""
    valid = {"external", "external_mid_tool", "cancel_check", "cancel_check_mid_tool"}
    cases = [
        # (cancel_check, ctx_cancel_set, expected_reason)
        (lambda: True, False, "cancel_check"),
        (None, True, "external"),
    ]
    for cc, ctx_set, expected in cases:
        provider = ScriptedProvider([text_response("x")])
        loop = AgentLoop(provider, toolsets=[])
        cancel_evt = asyncio.Event()
        if ctx_set:
            cancel_evt.set()
        events = await _drain(
            loop.run(make_request(cancel_check=cc), make_ctx(cancel=cancel_evt))
        )
        cancel_evts = [e for e in events if e.kind == "cancelled"]
        assert len(cancel_evts) == 1
        assert cancel_evts[0].payload["reason"] in valid
        assert cancel_evts[0].payload["reason"] == expected
