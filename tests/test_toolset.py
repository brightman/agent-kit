"""tests/test_toolset.py — Stage 1 toolset router contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_kit.provider import ToolSchema
from agent_kit.toolset import BaseToolset, ToolCallContext, ToolsetRouter
from agent_kit.types import ToolCall, ToolResult


# ---- helpers ----


class _StubToolset(BaseToolset):
    def __init__(
        self,
        name: str,
        tools: list[str],
        *,
        raise_on_execute: bool = False,
        close_raises: bool = False,
    ) -> None:
        self.name = name
        self._tools = tools
        self.execute_calls: list[ToolCall] = []
        self.closed = 0
        self._raise_on_execute = raise_on_execute
        self._close_raises = close_raises

    def build_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(name=n, description=f"stub {n}", parameters={"type": "object"})
            for n in self._tools
        ]

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        self.execute_calls.append(call)
        if self._raise_on_execute:
            raise RuntimeError("boom")
        return ToolResult(call_id=call.id, content=f"ran {call.name}")

    async def aclose(self) -> None:
        self.closed += 1
        if self._close_raises:
            raise RuntimeError("close-bang")


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        run_id="r1",
        skill_name=None,
        cancel=asyncio.Event(),
        workspace=Path("/tmp"),
        storage=Path("/tmp"),
        emit=lambda evt: None,
    )


# ---- conflict detection ----


def test_router_rejects_duplicate_toolset_name() -> None:
    a = _StubToolset("dup", ["a"])
    b = _StubToolset("dup", ["b"])
    with pytest.raises(ValueError, match="toolset name collision"):
        ToolsetRouter([a, b])


def test_router_rejects_duplicate_tool_schema_name() -> None:
    a = _StubToolset("a", ["fetch"])
    b = _StubToolset("b", ["fetch"])   # same tool name across toolsets
    with pytest.raises(ValueError, match="tool name collision"):
        ToolsetRouter([a, b])


def test_router_accepts_distinct_toolsets() -> None:
    a = _StubToolset("a", ["fetch"])
    b = _StubToolset("b", ["write"])
    r = ToolsetRouter([a, b])
    names = [s.name for s in r.all_schemas()]
    assert names == ["fetch", "write"]


def test_router_preserves_registration_order() -> None:
    a = _StubToolset("a", ["x", "y"])
    b = _StubToolset("b", ["z"])
    r = ToolsetRouter([a, b])
    assert [s.name for s in r.all_schemas()] == ["x", "y", "z"]


# ---- dispatch ----


@pytest.mark.asyncio
async def test_router_dispatches_to_owner() -> None:
    a = _StubToolset("a", ["fetch"])
    b = _StubToolset("b", ["write"])
    r = ToolsetRouter([a, b])
    result = await r.execute(ToolCall(id="1", name="write", arguments={}), _ctx())
    assert result.content == "ran write"
    assert len(b.execute_calls) == 1
    assert len(a.execute_calls) == 0


@pytest.mark.asyncio
async def test_router_unknown_tool_returns_error_result() -> None:
    a = _StubToolset("a", ["fetch"])
    r = ToolsetRouter([a])
    result = await r.execute(ToolCall(id="1", name="nope", arguments={}), _ctx())
    assert result.is_error
    assert "unknown tool" in result.content


@pytest.mark.asyncio
async def test_router_catches_toolset_execute_exception() -> None:
    a = _StubToolset("a", ["fetch"], raise_on_execute=True)
    r = ToolsetRouter([a])
    result = await r.execute(ToolCall(id="1", name="fetch", arguments={}), _ctx())
    assert result.is_error
    assert "RuntimeError" in result.content
    assert "boom" in result.content


# ---- aclose ----


@pytest.mark.asyncio
async def test_router_aclose_reverse_order() -> None:
    a = _StubToolset("a", ["x"])
    b = _StubToolset("b", ["y"])
    c = _StubToolset("c", ["z"])
    r = ToolsetRouter([a, b, c])
    await r.aclose()
    assert a.closed == 1 and b.closed == 1 and c.closed == 1


@pytest.mark.asyncio
async def test_router_aclose_swallows_exceptions() -> None:
    """one toolset raises in aclose; others still close."""
    a = _StubToolset("a", ["x"])
    b = _StubToolset("b", ["y"], close_raises=True)
    c = _StubToolset("c", ["z"])
    r = ToolsetRouter([a, b, c])
    await r.aclose()   # must not raise
    assert a.closed == 1
    assert b.closed == 1
    assert c.closed == 1


# ---- ToolCallContext ----


def test_context_runstate_default_empty() -> None:
    ctx = _ctx()
    assert ctx.run_state == {}


def test_context_runstate_independent_per_instance() -> None:
    ctx1 = _ctx()
    ctx2 = _ctx()
    ctx1.run_state["k"] = 1
    assert "k" not in ctx2.run_state
