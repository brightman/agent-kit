"""Stage 0 smoke test:验证模块结构成立,可被 import。

真单元测试在 Stage 1+ 加。
"""

from __future__ import annotations


def test_package_imports() -> None:
    import agent_kit

    assert agent_kit.__version__ == "0.0.0"


def test_modules_importable() -> None:
    from agent_kit import (
        context,
        loop,
        mcp,
        provider,
        runner,
        skill,
        tokens,
        toolset,
        types,
    )

    # 每个模块至少有 __all__
    for mod in [types, provider, toolset, skill, mcp, loop, runner, context, tokens]:
        assert hasattr(mod, "__all__"), f"{mod.__name__} missing __all__"


def test_context_compacted_event_kind() -> None:
    """Stage 2 spec:context_compacted 在 EventKind 字面量内。"""
    from typing import get_args

    from agent_kit.types import Event, EventKind

    assert "context_compacted" in get_args(EventKind)
    assert "llm_delta" in get_args(EventKind)   # Q1 stream 决议
    # 构造一下,确保 Event 接受新 kind
    evt = Event(event_id="e1", parent_event_id=None,
                kind="context_compacted", payload={}, ts=0.0)
    assert evt.kind == "context_compacted"


def test_types_dataclasses() -> None:
    from agent_kit.types import Event, Message, ToolCall, ToolResult

    tc = ToolCall(id="x", name="t", arguments={})
    tr = ToolResult(call_id="x", content="ok")
    msg = Message(role="user", content="hi")
    evt = Event(event_id="e1", parent_event_id=None, kind="round_start",
                payload={}, ts=0.0)
    assert tc.id == "x"
    assert tr.is_error is False
    assert msg.role == "user"
    assert evt.kind == "round_start"
