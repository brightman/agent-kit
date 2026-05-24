"""tests/test_errors.py — `_errors.unwrap_to_leaf` + error event diagnostic."""

from __future__ import annotations

import pytest

from agent_kit._errors import unwrap_to_leaf
from agent_kit.runner import _wrap_error_event


def test_unwrap_plain_exception_returns_self() -> None:
    exc = RuntimeError("boom")
    assert unwrap_to_leaf(exc) is exc


def test_unwrap_single_level_group() -> None:
    leaf = ValueError("real cause")
    grp = ExceptionGroup("wrap", [leaf])
    assert unwrap_to_leaf(grp) is leaf


def test_unwrap_nested_groups() -> None:
    leaf = ConnectionRefusedError("[Errno 49] Can't assign requested address")
    inner = ExceptionGroup("inner", [leaf])
    outer = ExceptionGroup("outer", [inner])
    assert unwrap_to_leaf(outer) is leaf


# ---- error event integration ----


def test_wrap_error_event_exc_type_from_leaf() -> None:
    leaf = ConnectionRefusedError("EADDRNOTAVAIL")
    grp = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [leaf])
    evt = _wrap_error_event("setup", grp)
    assert evt.kind == "error"
    assert evt.payload["exc_type"] == "ConnectionRefusedError"
    assert evt.payload["message"] == "EADDRNOTAVAIL"
    # full traceback chain still in payload (contains the group AND the leaf)
    assert "ExceptionGroup" in evt.payload["traceback"]
    assert "ConnectionRefusedError" in evt.payload["traceback"]


def test_wrap_error_event_plain_exception_unchanged() -> None:
    evt = _wrap_error_event("provider", ValueError("nope"))
    assert evt.payload["exc_type"] == "ValueError"
    assert evt.payload["message"] == "nope"


@pytest.mark.asyncio
async def test_run_to_completion_raises_leaf_message_for_group() -> None:
    """End-to-end: TaskGroup-wrapped MCP failure surfaces leaf cause to caller."""
    import asyncio
    from contextlib import AsyncExitStack

    from agent_kit.loop import RunRequest
    from agent_kit.mcp import McpServerConfig, McpToolset
    from agent_kit.provider import LlmResponse
    from agent_kit.runner import Runner

    # Custom toolset whose connect() raises an ExceptionGroup wrapping a
    # ConnectionRefusedError — mimics what anyio TaskGroup does to MCP failures.
    class _FlakyMcpToolset(McpToolset):
        async def connect(self) -> None:
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup (1 sub-exception)",
                [ConnectionRefusedError("[Errno 49] Can't assign requested address")],
            )

    class _Dummy:
        name = "d"
        async def chat(self, *a, **k):
            return LlmResponse(text="x", tool_calls=[])
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    flaky = _FlakyMcpToolset(
        McpServerConfig(name="x", transport="stdio", command=["unused"])
    )
    runner = Runner(_Dummy(), toolsets=[flaky])
    with pytest.raises(RuntimeError, match=r"ConnectionRefusedError.*EADDRNOTAVAIL|\[Errno 49\]"):
        await runner.run_to_completion(
            RunRequest(tenant_id="t", agent_id="a", user_message="hi")
        )
