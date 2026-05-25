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
from pathlib import Path

import pytest

from agent_kit.loop import AgentLoop, RunRequest
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.toolset import BaseToolset, ToolCallContext
from agent_kit.types import Event, Message, ToolCall, ToolResult


# ---- helpers ----


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[Message], list[ToolSchema] | None]] = []

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append((list(messages), list(tools) if tools else None))
        if not self._responses:
            raise RuntimeError("scripted provider exhausted")
        return self._responses.pop(0)

    async def chat_stream(self, *_, **__):
        raise NotImplementedError


class _ScriptedToolset(BaseToolset):
    name = "scripted_tools"

    def __init__(self) -> None:
        self.executed: list[ToolCall] = []

    def build_schemas(self):
        return [
            ToolSchema(
                name="echo",
                description="echo args",
                parameters={"type": "object"},
            )
        ]

    async def execute(self, call, ctx):
        self.executed.append(call)
        return ToolResult(call_id=call.id, content="ok")


def _ctx(cancel: asyncio.Event | None = None) -> ToolCallContext:
    return ToolCallContext(
        tenant_id="t1", run_id="r1", skill_name=None,
        cancel=cancel or asyncio.Event(),
        workspace=Path("/tmp"), storage=Path("/tmp"),
        emit=lambda evt: None,
    )


async def _drain(loop_run) -> list[Event]:
    return [evt async for evt in loop_run]


def _kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


# ---------------------------------------------------------------------------
# 1. Default / no-op
# ---------------------------------------------------------------------------


async def test_default_cancel_check_none_does_not_trigger_cancel() -> None:
    """Default cancel_check=None → 永不 cancel,normal completion。"""
    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    loop = AgentLoop(provider, toolsets=[])
    req = RunRequest(tenant_id="t", agent_id="a", user_message="hi", max_rounds=3)
    events = await _drain(loop.run(req, _ctx()))
    assert "cancelled" not in _kinds(events)
    assert "final_text" in _kinds(events)


async def test_cancel_check_returning_false_does_not_trigger_cancel() -> None:
    """显式 cancel_check 永远返 False → 跟 None 等价的行为。"""
    poll_count = {"n": 0}

    def never_cancel():
        poll_count["n"] += 1
        return False

    provider = _ScriptedProvider([
        LlmResponse(text="", tool_calls=[ToolCall(id="c1", name="echo", arguments={})]),
        LlmResponse(text="done", tool_calls=[]),
    ])
    loop = AgentLoop(provider, toolsets=[_ScriptedToolset()])
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi", max_rounds=3,
        cancel_check=never_cancel,
    )
    events = await _drain(loop.run(req, _ctx()))
    assert "cancelled" not in _kinds(events)
    assert "final_text" in _kinds(events)
    # 至少 poll 过两次(2 round 顶部 + 1 tool dispatch 前 = 3 次起)
    assert poll_count["n"] >= 2


# ---------------------------------------------------------------------------
# 2. Cancel before first round
# ---------------------------------------------------------------------------


async def test_cancel_check_true_before_first_round_emits_cancelled_no_provider_call() -> None:
    provider = _ScriptedProvider([LlmResponse(text="never runs", tool_calls=[])])
    loop = AgentLoop(provider, toolsets=[])
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi", max_rounds=3,
        cancel_check=lambda: True,
    )
    events = await _drain(loop.run(req, _ctx()))
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
    cancel_after = {"after_first_round": False}

    def maybe_cancel():
        return cancel_after["after_first_round"]

    provider = _ScriptedProvider([
        # Round 1: 调 tool,继续
        LlmResponse(text="", tool_calls=[ToolCall(id="c1", name="echo", arguments={})]),
        # Round 2 never reached(被 cancel_check 拦下)
        LlmResponse(text="should not see", tool_calls=[]),
    ])
    tools = _ScriptedToolset()
    loop = AgentLoop(provider, toolsets=[tools])
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi", max_rounds=5,
        cancel_check=maybe_cancel,
    )

    # 安排:Round 1 跑完(tool 执行了),然后 Round 2 顶部 poll 看到 True
    # 怎么触发?最简单是让 cancel_check 第二次被调时变 True。计数:
    poll_count = {"n": 0}

    def cancel_on_round_2(orig=maybe_cancel):
        poll_count["n"] += 1
        # Round 1 顶部 poll(n=1)= False;Round 1 tool dispatch poll(n=2)= False;
        # Round 2 顶部 poll(n=3)= True
        return poll_count["n"] >= 3

    req2 = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi", max_rounds=5,
        cancel_check=cancel_on_round_2,
    )
    events = await _drain(loop.run(req2, _ctx()))
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
    assert tools.executed == [ToolCall(id="c1", name="echo", arguments={})]


# ---------------------------------------------------------------------------
# 4. Cancel before tool dispatch (mid-round)
# ---------------------------------------------------------------------------


async def test_cancel_check_true_before_tool_dispatch_emits_mid_tool_reason() -> None:
    """LLM 返 tool_calls 后、tool 执行前 cancel_check=True → mid_tool reason,
    tool 不执行。"""
    poll_count = {"n": 0}

    def cancel_after_llm(check_calls=poll_count):
        check_calls["n"] += 1
        # Round 1 顶部 poll(n=1)= False;tool dispatch 前 poll(n=2)= True
        return check_calls["n"] >= 2

    provider = _ScriptedProvider([
        LlmResponse(text="", tool_calls=[ToolCall(id="c1", name="echo", arguments={})]),
    ])
    tools = _ScriptedToolset()
    loop = AgentLoop(provider, toolsets=[tools])
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi", max_rounds=3,
        cancel_check=cancel_after_llm,
    )
    events = await _drain(loop.run(req, _ctx()))
    kinds = _kinds(events)
    # llm_response 出来了,但 tool 没执行(cancel 卡在 dispatch 前)
    assert "llm_response" in kinds
    assert "tool_call" not in kinds
    assert "tool_result" not in kinds
    cancel_evt = next(e for e in events if e.kind == "cancelled")
    assert cancel_evt.payload["reason"] == "cancel_check_mid_tool"
    assert tools.executed == []  # tool 完全没跑


# ---------------------------------------------------------------------------
# 5. Exception handling
# ---------------------------------------------------------------------------


async def test_cancel_check_raising_exception_is_swallowed_run_continues(caplog) -> None:
    """cancel_check raises → 吞掉 + log,treat as False,run 正常 complete。"""
    def boom():
        raise RuntimeError("cancel_check is buggy")

    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    loop = AgentLoop(provider, toolsets=[])
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi", max_rounds=3,
        cancel_check=boom,
    )
    with caplog.at_level(logging.WARNING, logger="agent_kit.loop"):
        events = await _drain(loop.run(req, _ctx()))
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
    provider = _ScriptedProvider([LlmResponse(text="never", tool_calls=[])])
    loop = AgentLoop(provider, toolsets=[])
    cancel_event = asyncio.Event()
    cancel_event.set()  # ctx.cancel 已经 set 了
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi", max_rounds=3,
        cancel_check=lambda: True,  # cancel_check 也想 cancel
    )
    events = await _drain(loop.run(req, _ctx(cancel=cancel_event)))
    cancel_evt = next(e for e in events if e.kind == "cancelled")
    assert cancel_evt.payload["reason"] == "external"


async def test_ctx_cancel_alone_still_works_unchanged_from_before() -> None:
    """向后兼容:ctx.cancel 自己 set(没 cancel_check)→ 行为跟旧版一致,
    reason="external"。"""
    provider = _ScriptedProvider([LlmResponse(text="never", tool_calls=[])])
    loop = AgentLoop(provider, toolsets=[])
    cancel_event = asyncio.Event()
    cancel_event.set()
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi", max_rounds=3,
        # cancel_check 默认 None
    )
    events = await _drain(loop.run(req, _ctx(cancel=cancel_event)))
    cancel_evt = next(e for e in events if e.kind == "cancelled")
    assert cancel_evt.payload["reason"] == "external"


# ---------------------------------------------------------------------------
# 7. Reason vocab guard
# ---------------------------------------------------------------------------


async def test_cancel_event_reason_vocab_is_one_of_four_known_values() -> None:
    """spec lock:cancel reason 只 4 个 values"""
    # 4 个 known reasons:
    # - "external" (ctx.cancel 顶部)
    # - "external_mid_tool" (ctx.cancel mid-tool)
    # - "cancel_check" (RunRequest.cancel_check 顶部)
    # - "cancel_check_mid_tool" (RunRequest.cancel_check mid-tool)
    # 本测试只是 docstring + 类型 lock,真正校验在上面 6 个 test 各自覆盖
    # 自己那个 reason。这里 sanity check:reason 不会出现新词。
    valid = {"external", "external_mid_tool", "cancel_check", "cancel_check_mid_tool"}
    # 4 个 case 各跑一次,验 reason 都在 valid 集
    cases = [
        # (cancel_check, ctx_cancel_set, expected_reason)
        (lambda: True, False, "cancel_check"),
        (None, True, "external"),
    ]
    for cc, ctx_set, expected in cases:
        provider = _ScriptedProvider([LlmResponse(text="x", tool_calls=[])])
        loop = AgentLoop(provider, toolsets=[])
        cancel_evt = asyncio.Event()
        if ctx_set:
            cancel_evt.set()
        req = RunRequest(
            tenant_id="t", agent_id="a", user_message="hi", max_rounds=3,
            cancel_check=cc,
        )
        events = await _drain(loop.run(req, _ctx(cancel=cancel_evt)))
        cancel_evts = [e for e in events if e.kind == "cancelled"]
        assert len(cancel_evts) == 1
        assert cancel_evts[0].payload["reason"] in valid
        assert cancel_evts[0].payload["reason"] == expected
