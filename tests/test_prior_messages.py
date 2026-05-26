"""tests/test_prior_messages.py — RunRequest.prior_messages contract (spec § 3.x).

Coverage:
- default empty list
- _compose_messages places prior_messages between system and user
- __post_init__ rejects role="system" in prior_messages
- __post_init__ rejects orphan tool message (no prior assistant tool_call)
- __post_init__ accepts valid assistant(tool_calls=[X]) + tool(call_id=X) pair
- end-to-end honesty re-run shape: assistant text + user correction works as
  a complete prior_messages + user_message tuple, loop continues normally
"""

from __future__ import annotations

import pytest

from agent_kit.loop import AgentLoop, RunRequest
from agent_kit.types import Message, ToolCall

from tests._helpers import (
    ScriptedProvider,
    make_ctx,
    make_request,
    text_response,
)


async def _drain(loop_run):
    return [evt async for evt in loop_run]


# ---- 1. Default / shape ---------------------------------------------------


def test_run_request_default_prior_messages_is_empty_list() -> None:
    req = make_request()
    assert req.prior_messages == []
    # default_factory:每个 instance 独立 list,不共享
    req2 = make_request()
    req.prior_messages.append(Message(role="user", content="leak?"))
    assert req2.prior_messages == []


# ---- 2. _compose_messages 拼接顺序 ----------------------------------------


def test_compose_messages_places_prior_messages_between_system_and_user() -> None:
    loop = AgentLoop(ScriptedProvider(), toolsets=[], system_prelude="ROOT PRELUDE")
    prior = [
        Message(role="user", content="earlier user msg"),
        Message(role="assistant", content="earlier assistant reply"),
    ]
    req = make_request(
        user_message="now",
        system_prelude="REQUEST PRELUDE",
        prior_messages=prior,
    )
    out = loop._compose_messages(req)
    # 顺序:[system(loop+request 合并), prior_user, prior_assistant, user(now)]
    assert len(out) == 4
    assert out[0].role == "system"
    assert "ROOT PRELUDE" in out[0].content
    assert "REQUEST PRELUDE" in out[0].content
    assert out[1] == prior[0]
    assert out[2] == prior[1]
    assert out[3].role == "user"
    assert out[3].content == "now"


def test_compose_messages_with_empty_prior_messages_unchanged() -> None:
    """No prior_messages → 旧行为 [system?, user]。"""
    loop = AgentLoop(ScriptedProvider(), toolsets=[], system_prelude="P")
    out = loop._compose_messages(make_request())
    assert [m.role for m in out] == ["system", "user"]


def test_compose_messages_no_prelude_with_prior_messages_skips_system() -> None:
    """无 prelude + 有 prior_messages → [*prior, user],不发 system message。"""
    loop = AgentLoop(ScriptedProvider(), toolsets=[])
    prior = [Message(role="user", content="earlier")]
    req = make_request(user_message="now", prior_messages=prior)
    out = loop._compose_messages(req)
    assert [m.role for m in out] == ["user", "user"]
    assert [m.content for m in out] == ["earlier", "now"]


# ---- 3. Invariants — system role rejected --------------------------------


def test_run_request_rejects_system_role_in_prior_messages() -> None:
    with pytest.raises(ValueError, match="role='system'"):
        make_request(prior_messages=[
            Message(role="user", content="ok"),
            Message(role="system", content="sneaky system"),
        ])


def test_run_request_error_message_points_to_alternative() -> None:
    """ValueError 文案告诉用户 system 内容该塞哪里 —— 避免重复踩。"""
    with pytest.raises(ValueError) as exc_info:
        make_request(prior_messages=[Message(role="system", content="x")])
    assert "system_prelude" in str(exc_info.value)


# ---- 4. Invariants — tool-pair integrity ---------------------------------


def test_run_request_rejects_orphan_tool_message_without_prior_assistant_call() -> None:
    with pytest.raises(ValueError, match="orphan tool message"):
        make_request(prior_messages=[
            Message(role="user", content="search"),
            Message(role="tool", content="result for nothing", tool_call_id="nope"),
        ])


def test_run_request_accepts_complete_tool_call_pair_in_prior_messages() -> None:
    """assistant(tool_calls=[X]) + tool(call_id=X) 配对完整 → 接受。"""
    req = make_request(prior_messages=[
        Message(role="user", content="search foo"),
        Message(
            role="assistant", content="",
            tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "foo"})],
        ),
        Message(role="tool", content="{result}", tool_call_id="c1"),
        Message(role="assistant", content="here it is"),
    ])
    assert len(req.prior_messages) == 4   # 没 raise = 通过


# ---- 5. End-to-end ------------------------------------------------------


async def test_honesty_re_run_shape_loop_sees_prior_assistant_and_continues() -> None:
    """模拟 baizhi honesty re-run:上一 attempt 的 assistant text 进
    prior_messages,user_message 是 runtime correction;loop 正常跑一轮 → 出
    final_text。验证 provider 看到的 messages 顺序对。"""
    provider = ScriptedProvider([text_response("correction applied")])
    loop = AgentLoop(provider, toolsets=[])
    req = make_request(
        user_message="Runtime correction: actually use the tool",
        prior_messages=[
            Message(role="assistant", content="(premature) Already saved!"),
        ],
    )
    events = await _drain(loop.run(req, make_ctx()))
    final = next(e for e in events if e.kind == "final_text")
    assert final.payload["text"] == "correction applied"
    sent = provider.calls[0]["messages"]
    assert [m.role for m in sent] == ["assistant", "user"]
    assert sent[0].content == "(premature) Already saved!"
    assert sent[1].content == "Runtime correction: actually use the tool"


async def test_multi_turn_history_replay_through_prior_messages() -> None:
    """6 prior turns + 新 user turn。验证 loop 不动 prior_messages,
    只 append user_message 然后给 provider。"""
    provider = ScriptedProvider([text_response("turn 4 response")])
    loop = AgentLoop(provider, toolsets=[])
    prior = [
        Message(role=r, content=f"turn {i} {r}")
        for i in range(1, 4) for r in ("user", "assistant")
    ]
    req = make_request(user_message="turn 4 user", prior_messages=prior)
    await _drain(loop.run(req, make_ctx()))
    sent = provider.calls[0]["messages"]
    assert len(sent) == 7  # 6 prior + 1 new user
    assert [m.role for m in sent] == [
        "user", "assistant", "user", "assistant", "user", "assistant", "user",
    ]
    assert sent[-1].content == "turn 4 user"
