"""tests/test_tokens.py — Stage 1 token estimation contracts."""

from __future__ import annotations

from agent_kit.tokens import (
    TOKEN_ESTIMATION_PADDING,
    estimate_messages_tokens,
    estimate_text_tokens,
)
from agent_kit.types import Message, ToolCall


def test_padding_constant() -> None:
    assert TOKEN_ESTIMATION_PADDING == 4 / 3


def test_empty_text() -> None:
    assert estimate_text_tokens("") == 0


def test_short_text() -> None:
    # "hi" → len=2 → (2+3)//4 = 1 → 1 * 4/3 = 1 (int)
    assert estimate_text_tokens("hi") == 1


def test_longer_text() -> None:
    # 40 chars → (40+3)//4 = 10 → 10 * 4/3 = 13 (int floor)
    s = "x" * 40
    assert estimate_text_tokens(s) == 13


def test_text_scales_roughly_linear() -> None:
    short = estimate_text_tokens("a" * 100)
    long = estimate_text_tokens("a" * 10000)
    # ratio should be ~100x give or take a few
    assert 90 < long / short < 110


def test_empty_messages() -> None:
    assert estimate_messages_tokens([]) == 0


def test_single_user_message() -> None:
    m = Message(role="user", content="hello")
    n = estimate_messages_tokens([m])
    # 4 (overhead) + role(4 char)~tokens + content(5 char)~tokens > 4
    assert n > 4


def test_assistant_with_tool_calls_costs_more() -> None:
    """Assistant with tool_calls JSON payload ≥ assistant without."""
    plain = Message(role="assistant", content="ok")
    with_tc = Message(
        role="assistant",
        content="ok",
        tool_calls=[ToolCall(id="x", name="fetch_url",
                             arguments={"url": "https://example.com"})],
    )
    assert estimate_messages_tokens([with_tc]) > estimate_messages_tokens([plain])


def test_tool_message_includes_call_id() -> None:
    """Tool message token count includes the tool_call_id field."""
    short = Message(role="tool", content="ok", tool_call_id="x")
    long = Message(role="tool", content="ok", tool_call_id="x" * 100)
    assert estimate_messages_tokens([long]) > estimate_messages_tokens([short])


def test_messages_additive() -> None:
    m1 = Message(role="user", content="abc")
    m2 = Message(role="assistant", content="def")
    sum_individual = estimate_messages_tokens([m1]) + estimate_messages_tokens([m2])
    sum_together = estimate_messages_tokens([m1, m2])
    assert sum_individual == sum_together


def test_large_content_dominates_overhead() -> None:
    """Big content should make per-message overhead negligible."""
    m = Message(role="user", content="x" * 10000)
    n = estimate_messages_tokens([m])
    assert n > 3000   # ~ 10000 chars * 4/3 / 4
