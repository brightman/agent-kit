"""Stage 0 smoke test:验证模块结构成立,可被 import。

真单元测试在 Stage 1+ 加。
"""

from __future__ import annotations


def test_package_imports() -> None:
    import agent_kit

    assert agent_kit.__version__ == "0.0.0"


def test_modules_importable() -> None:
    from agent_kit import loop, mcp, provider, runner, skill, toolset, types

    # 每个模块至少有 __all__
    for mod in [types, provider, toolset, skill, mcp, loop, runner]:
        assert hasattr(mod, "__all__"), f"{mod.__name__} missing __all__"


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
