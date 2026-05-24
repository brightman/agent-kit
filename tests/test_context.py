"""tests/test_context.py — Stage 1 context compaction contracts.

Covers:
- _assert_tool_pairs_intact validates tool_call_id pairing
- safe_split_messages ADK boundary rules
- TruncatingCompactor microcompact behavior
"""

from __future__ import annotations

import pytest

from agent_kit.context import (
    TruncatingCompactor,
    _assert_tool_pairs_intact,
    safe_split_messages,
)
from agent_kit.types import Message, ToolCall


# Helpers
def sys_msg(c: str = "system") -> Message:
    return Message(role="system", content=c)


def user(c: str) -> Message:
    return Message(role="user", content=c)


def asst_text(c: str) -> Message:
    return Message(role="assistant", content=c)


def asst_calls(ids: list[str]) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id=i, name="t", arguments={}) for i in ids],
    )


def tool(call_id: str, content: str = "result") -> Message:
    return Message(role="tool", content=content, tool_call_id=call_id)


# ---------- _assert_tool_pairs_intact ----------


def test_assert_passes_on_empty() -> None:
    _assert_tool_pairs_intact([])


def test_assert_passes_no_tools() -> None:
    _assert_tool_pairs_intact([sys_msg(), user("hi"), asst_text("ok")])


def test_assert_passes_well_formed_pair() -> None:
    msgs = [user("hi"), asst_calls(["x"]), tool("x")]
    _assert_tool_pairs_intact(msgs)


def test_assert_passes_multiple_tools_one_assistant() -> None:
    msgs = [user("hi"), asst_calls(["x", "y"]), tool("x"), tool("y")]
    _assert_tool_pairs_intact(msgs)


def test_assert_rejects_orphan_tool() -> None:
    """tool message with no matching assistant tool_call before it."""
    msgs = [user("hi"), tool("x")]
    with pytest.raises(ValueError, match="orphan tool message"):
        _assert_tool_pairs_intact(msgs)


def test_assert_rejects_out_of_order() -> None:
    """tool message before its assistant tool_call."""
    msgs = [tool("x"), asst_calls(["x"])]
    with pytest.raises(ValueError, match="orphan tool message"):
        _assert_tool_pairs_intact(msgs)


def test_assert_allows_pending_tool_call() -> None:
    """assistant.tool_calls without matching tool message is OK
    (LLM tolerates; only orphan tool messages 400)."""
    msgs = [user("hi"), asst_calls(["x", "y"]), tool("x")]
    _assert_tool_pairs_intact(msgs)   # no exception


# ---------- safe_split_messages ----------


def test_split_at_zero() -> None:
    msgs = [user("a"), asst_text("b")]
    assert safe_split_messages(msgs, 0) == 0


def test_split_at_end() -> None:
    msgs = [user("a"), asst_text("b")]
    assert safe_split_messages(msgs, 2) == 2


def test_split_at_safe_boundary() -> None:
    """split between two unrelated user/assistant messages — no adjustment."""
    msgs = [user("a"), asst_text("b"), user("c"), asst_text("d")]
    assert safe_split_messages(msgs, 2) == 2


def test_split_into_tool_message_pulls_back_to_before_assistant() -> None:
    """Splitting INTO a tool message must include its assistant call on the right."""
    msgs = [
        user("hi"),         # 0
        asst_calls(["x"]),  # 1
        tool("x"),          # 2
        user("next"),       # 3
    ]
    # asking to split at idx 2 (tool message) — must pull back to before idx 1
    assert safe_split_messages(msgs, 2) == 1


def test_split_after_assistant_with_pending_tool_calls() -> None:
    """split RIGHT after an assistant with tool_calls (idx == 2) means
    tool_calls in the assistant but tool message on the right — also unsafe."""
    msgs = [
        user("hi"),         # 0
        asst_calls(["x"]),  # 1 — has tool_calls
        tool("x"),          # 2
    ]
    # asking to split at idx 2: left = [user, asst_with_calls], right = [tool]
    # the tool message on right has tool_call_id=x matching asst, which is on left → orphan
    # safe_split must pull back to before asst
    assert safe_split_messages(msgs, 2) == 1


def test_split_falls_back_to_zero_when_inside_pair() -> None:
    """Splitting inside a tool pair pulls back; if the pair starts at idx 0,
    we fall all the way back to 0."""
    msgs = [asst_calls(["x"]), tool("x"), user("next")]
    # split_at=1: would leave tool("x") on right with no prior assistant → fall back
    assert safe_split_messages(msgs, 1) == 0
    # split_at=2: left side has the complete pair → safe
    assert safe_split_messages(msgs, 2) == 2


def test_split_at_user_message_is_safe() -> None:
    """User messages are always safe split points."""
    msgs = [user("a"), asst_calls(["x"]), tool("x"), user("b"), asst_text("c")]
    assert safe_split_messages(msgs, 3) == 3   # right at user("b")


def test_split_with_multi_tool_round() -> None:
    msgs = [
        user("hi"),               # 0
        asst_calls(["x", "y"]),   # 1
        tool("x"),                # 2
        tool("y"),                # 3
        user("next"),             # 4
    ]
    # asking to split at 3 (middle of tool pair) — must pull back to 1
    assert safe_split_messages(msgs, 3) == 1
    # split at 4 is fine (right at user)
    assert safe_split_messages(msgs, 4) == 4


def test_split_at_negative() -> None:
    msgs = [user("a")]
    assert safe_split_messages(msgs, -5) == 0


def test_split_beyond_end() -> None:
    msgs = [user("a")]
    assert safe_split_messages(msgs, 999) == 1


# ---------- TruncatingCompactor ----------


@pytest.mark.asyncio
async def test_should_compact_uses_api_usage_first() -> None:
    c = TruncatingCompactor(token_budget=100)
    msgs = [user("hi")]
    # API says 150 → over budget
    assert await c.should_compact(msgs, {"prompt_tokens": 150}) is True
    # API says 50 → under
    assert await c.should_compact(msgs, {"prompt_tokens": 50}) is False


@pytest.mark.asyncio
async def test_should_compact_falls_back_to_estimate() -> None:
    c = TruncatingCompactor(token_budget=10)
    msgs = [user("x" * 10000)]   # big enough to exceed 10
    assert await c.should_compact(msgs, None) is True


@pytest.mark.asyncio
async def test_should_compact_ignores_missing_prompt_tokens() -> None:
    """last_usage without prompt_tokens key → fall back to estimate."""
    c = TruncatingCompactor(token_budget=10)
    msgs = [user("x" * 10000)]
    assert await c.should_compact(msgs, {"completion_tokens": 5}) is True


@pytest.mark.asyncio
async def test_compact_keeps_recent_n() -> None:
    c = TruncatingCompactor(keep_recent_tool_results=2)
    msgs = [
        user("u"),
        asst_calls(["a"]), tool("a", "old1"),
        asst_calls(["b"]), tool("b", "old2"),
        asst_calls(["c"]), tool("c", "old3"),
        asst_calls(["d"]), tool("d", "recent1"),
        asst_calls(["e"]), tool("e", "recent2"),
    ]
    result = await c.compact(msgs)
    # 5 tool messages total; keep_recent=2 → 3 oldest truncated
    truncated = [m for m in result if m.role == "tool" and m.content == c.placeholder]
    assert len(truncated) == 3
    # recent two should be intact
    recent = [m for m in result if m.role == "tool" and m.content != c.placeholder]
    assert {m.content for m in recent} == {"recent1", "recent2"}


@pytest.mark.asyncio
async def test_compact_preserves_message_count_and_order() -> None:
    """Compact replaces content, not message position."""
    c = TruncatingCompactor(keep_recent_tool_results=1)
    msgs = [
        user("u"),
        asst_calls(["a"]), tool("a", "x"),
        asst_calls(["b"]), tool("b", "y"),
        asst_calls(["c"]), tool("c", "z"),
    ]
    result = await c.compact(msgs)
    assert len(result) == len(msgs)
    # roles unchanged
    assert [m.role for m in result] == [m.role for m in msgs]
    # tool_call_id preserved
    tools = [m for m in result if m.role == "tool"]
    assert [m.tool_call_id for m in tools] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_compact_no_op_when_few_tools() -> None:
    c = TruncatingCompactor(keep_recent_tool_results=5)
    msgs = [user("u"), asst_calls(["x"]), tool("x", "ok")]
    result = await c.compact(msgs)
    assert result == msgs   # no truncation


@pytest.mark.asyncio
async def test_compact_output_passes_pair_assertion() -> None:
    """SDK guarantee: TruncatingCompactor output passes _assert_tool_pairs_intact."""
    c = TruncatingCompactor(keep_recent_tool_results=1)
    msgs = [
        user("u"),
        asst_calls(["a"]), tool("a", "old"),
        asst_calls(["b"]), tool("b", "recent"),
    ]
    result = await c.compact(msgs)
    _assert_tool_pairs_intact(result)


@pytest.mark.asyncio
async def test_compact_idempotent_on_already_truncated() -> None:
    """Re-compacting already-truncated messages doesn't keep replacing."""
    c = TruncatingCompactor(keep_recent_tool_results=1)
    msgs = [
        user("u"),
        asst_calls(["a"]), tool("a", "old"),
        asst_calls(["b"]), tool("b", "recent"),
    ]
    r1 = await c.compact(msgs)
    r2 = await c.compact(r1)
    assert r1 == r2
