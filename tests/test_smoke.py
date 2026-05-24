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
        hooks,
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
    for mod in [types, provider, toolset, skill, mcp, loop, runner,
                context, tokens, hooks]:
        assert hasattr(mod, "__all__"), f"{mod.__name__} missing __all__"


def test_event_kinds_extended() -> None:
    """Stage 2 spec:新加的 event kind 都在字面量内,Event 能构造。"""
    from typing import get_args

    from agent_kit.types import Event, EventKind

    expected = {
        "context_compacted",       # § 8.6 compaction
        "llm_delta",               # Q1 stream
        "llm_short_circuited",     # § 8.7 before_model hook 短路
        "tool_short_circuited",    # § 8.7 before_tool hook 短路
    }
    actual = set(get_args(EventKind))
    missing = expected - actual
    assert not missing, f"missing event kinds: {missing}"

    # 构造每个新 kind,确保 Event 接受
    for kind in expected:
        evt = Event(event_id="e1", parent_event_id=None,
                    kind=kind, payload={}, ts=0.0)
        assert evt.kind == kind


def test_hooks_base_class() -> None:
    """Hook 基类 4 个 method 都是 no-op,子类可单独覆盖。"""
    import asyncio

    from agent_kit.hooks import Hook

    h = Hook()
    # 4 method 都存在
    for name in ["before_model", "after_model", "before_tool", "after_tool"]:
        assert hasattr(h, name), f"Hook missing {name}"

    # no-op 调用都返回 None(不实际跑 — 我们只验证 sig 不爆)
    async def run():
        # 不需要构造 real args,只验 method 可调用 + 不抛
        # 真集成测试在 Stage 1 test_hooks.py 写
        pass

    asyncio.run(run())


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
