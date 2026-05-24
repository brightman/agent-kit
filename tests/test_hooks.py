"""tests/test_hooks.py — Stage 1 Hook base class contracts.

Loop integration tests(short-circuit 顺序、event emit)在 Stage 2 加。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from agent_kit.hooks import Hook
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.toolset import ToolCallContext
from agent_kit.types import Message, ToolCall, ToolResult


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        tenant_id="t1", run_id="r1", skill_name=None,
        cancel=asyncio.Event(), workspace=Path("/tmp"), storage=Path("/tmp"),
        emit=lambda evt: None,
    )


# ---- 4 methods present ----


def test_hook_has_four_methods() -> None:
    h = Hook()
    for name in ("before_model", "after_model", "before_tool", "after_tool"):
        assert hasattr(h, name), f"missing {name}"
        assert inspect.iscoroutinefunction(getattr(h, name)), f"{name} must be async"


# ---- defaults are no-op (return None) ----


@pytest.mark.asyncio
async def test_before_model_default_returns_none() -> None:
    h = Hook()
    r = await h.before_model(_ctx(), [Message(role="user", content="hi")], None)
    assert r is None


@pytest.mark.asyncio
async def test_after_model_default_returns_none() -> None:
    h = Hook()
    resp = LlmResponse(text="hi", tool_calls=[], usage={}, raw={})
    r = await h.after_model(_ctx(), resp)
    assert r is None


@pytest.mark.asyncio
async def test_before_tool_default_returns_none() -> None:
    h = Hook()
    r = await h.before_tool(_ctx(), ToolCall(id="x", name="t", arguments={}))
    assert r is None


@pytest.mark.asyncio
async def test_after_tool_default_returns_none() -> None:
    h = Hook()
    r = await h.after_tool(
        _ctx(), ToolCall(id="x", name="t", arguments={}),
        ToolResult(call_id="x", content="ok"),
    )
    assert r is None


# ---- subclass override works ----


class _RecordingHook(Hook):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def before_model(self, ctx, messages, tools):
        self.calls.append("before_model")
        return None

    async def after_tool(self, ctx, call, result):
        self.calls.append("after_tool")
        return ToolResult(call_id=call.id, content="replaced", is_error=False)


@pytest.mark.asyncio
async def test_subclass_records_and_returns_replacement() -> None:
    h = _RecordingHook()
    # before_model recorded but no-op (return None)
    r1 = await h.before_model(_ctx(), [], None)
    assert r1 is None
    assert h.calls == ["before_model"]

    # after_tool returns replacement ToolResult
    r2 = await h.after_tool(
        _ctx(), ToolCall(id="x", name="t", arguments={}),
        ToolResult(call_id="x", content="orig"),
    )
    assert r2 is not None
    assert r2.content == "replaced"
    assert h.calls == ["before_model", "after_tool"]


# ---- multiple hooks can coexist without interfering ----


@pytest.mark.asyncio
async def test_multiple_hook_instances_independent_state() -> None:
    h1 = _RecordingHook()
    h2 = _RecordingHook()
    await h1.before_model(_ctx(), [], None)
    assert h1.calls == ["before_model"]
    assert h2.calls == []   # h2's state unaffected


# ---- ToolSchema dataclass works (sanity) ----


def test_tool_schema_constructs() -> None:
    s = ToolSchema(name="fetch", description="d", parameters={"type": "object"})
    assert s.name == "fetch"
