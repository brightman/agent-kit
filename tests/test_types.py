"""tests/test_types.py — Stage 1 type contracts."""

from __future__ import annotations

import pytest

from agent_kit.types import Event, Message, ToolCall, ToolResult


# --- ToolCall ---


def test_toolcall_basic() -> None:
    tc = ToolCall(id="x1", name="fetch", arguments={"url": "https://example.com"})
    assert tc.id == "x1"
    assert tc.arguments == {"url": "https://example.com"}


def test_toolcall_frozen() -> None:
    tc = ToolCall(id="x", name="t", arguments={})
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        tc.id = "y"  # type: ignore[misc]


def test_toolcall_roundtrip() -> None:
    tc = ToolCall(id="x", name="t", arguments={"a": 1, "b": [1, 2]})
    d = tc.to_dict()
    assert d == {"id": "x", "name": "t", "arguments": {"a": 1, "b": [1, 2]}}
    assert ToolCall.from_dict(d) == tc


def test_toolcall_arguments_isolated() -> None:
    args = {"a": 1}
    tc = ToolCall(id="x", name="t", arguments=args)
    d = tc.to_dict()
    d["arguments"]["a"] = 999
    assert tc.arguments["a"] == 1  # to_dict should make a copy


# --- ToolResult ---


def test_toolresult_default_no_error() -> None:
    r = ToolResult(call_id="x", content="ok")
    assert r.is_error is False


def test_toolresult_error_flag() -> None:
    r = ToolResult(call_id="x", content="bad", is_error=True)
    assert r.is_error is True


def test_toolresult_roundtrip() -> None:
    r = ToolResult(call_id="x", content="hello", is_error=True)
    d = r.to_dict()
    assert d == {"call_id": "x", "content": "hello", "is_error": True}
    assert ToolResult.from_dict(d) == r


def test_toolresult_from_dict_default_is_error() -> None:
    r = ToolResult.from_dict({"call_id": "x", "content": "ok"})
    assert r.is_error is False


# --- Message invariants ---


def test_message_user_simple() -> None:
    m = Message(role="user", content="hi")
    assert m.role == "user"


def test_message_assistant_with_tool_calls() -> None:
    tc = ToolCall(id="x", name="t", arguments={})
    m = Message(role="assistant", content="", tool_calls=[tc])
    assert m.tool_calls == [tc]


def test_message_tool_with_id() -> None:
    m = Message(role="tool", content="result", tool_call_id="x")
    assert m.tool_call_id == "x"


def test_message_invariant_tool_calls_only_on_assistant() -> None:
    tc = ToolCall(id="x", name="t", arguments={})
    with pytest.raises(ValueError, match="tool_calls only valid"):
        Message(role="user", content="", tool_calls=[tc])


def test_message_invariant_tool_call_id_only_on_tool() -> None:
    with pytest.raises(ValueError, match="tool_call_id only valid"):
        Message(role="assistant", content="", tool_call_id="x")


def test_message_invariant_tool_requires_call_id() -> None:
    with pytest.raises(ValueError, match="role='tool' requires tool_call_id"):
        Message(role="tool", content="result")


def test_message_roundtrip_user() -> None:
    m = Message(role="user", content="hi")
    d = m.to_dict()
    assert d == {"role": "user", "content": "hi"}
    assert Message.from_dict(d) == m


def test_message_roundtrip_assistant_with_tools() -> None:
    tc = ToolCall(id="x", name="t", arguments={"k": "v"})
    m = Message(role="assistant", content="ok", tool_calls=[tc])
    d = m.to_dict()
    assert "tool_calls" in d
    assert Message.from_dict(d) == m


def test_message_roundtrip_tool() -> None:
    m = Message(role="tool", content="result", tool_call_id="x")
    d = m.to_dict()
    assert d["tool_call_id"] == "x"
    assert Message.from_dict(d) == m


# --- Event ---


def test_event_basic() -> None:
    e = Event(event_id="e1", parent_event_id=None, kind="round_start",
              payload={"round": 0}, ts=1.5)
    assert e.kind == "round_start"
    assert e.payload == {"round": 0}


def test_event_frozen() -> None:
    e = Event(event_id="e1", parent_event_id=None, kind="round_start")
    with pytest.raises(Exception):
        e.event_id = "e2"  # type: ignore[misc]


def test_event_to_dict() -> None:
    e = Event(event_id="e1", parent_event_id="parent", kind="tool_call",
              payload={"x": 1}, ts=42.0)
    d = e.to_dict()
    assert d["event_id"] == "e1"
    assert d["parent_event_id"] == "parent"
    assert d["kind"] == "tool_call"
    assert d["payload"] == {"x": 1}
    assert d["ts"] == 42.0


def test_event_default_payload_independent() -> None:
    """Two events with default payload don't share dict."""
    e1 = Event(event_id="a", parent_event_id=None, kind="round_start")
    e2 = Event(event_id="b", parent_event_id=None, kind="round_end")
    # frozen, can't mutate, but verify they're separate identities anyway
    assert e1.payload is not e2.payload
