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

import asyncio
from pathlib import Path

import pytest

from agent_kit.loop import AgentLoop, RunRequest
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.toolset import ToolCallContext
from agent_kit.types import Message, ToolCall


# ---- helpers ----


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[Message], list[ToolSchema] | None]] = []

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append((list(messages), list(tools) if tools else None))
        if not self._responses:
            raise RuntimeError("scripted provider exhausted")
        return self._responses.pop(0)

    async def chat_stream(self, *_, **__):
        raise NotImplementedError


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        tenant_id="t1", run_id="r1", skill_name=None,
        cancel=asyncio.Event(),
        workspace=Path("/tmp"), storage=Path("/tmp"),
        emit=lambda evt: None,
    )


async def _drain(loop_run):
    return [evt async for evt in loop_run]


# ---------------------------------------------------------------------------
# 1. Default / shape
# ---------------------------------------------------------------------------


def test_run_request_default_prior_messages_is_empty_list() -> None:
    req = RunRequest(tenant_id="t", agent_id="a", user_message="hi")
    assert req.prior_messages == []
    # default_factory:每个 instance 独立 list,不共享
    req2 = RunRequest(tenant_id="t", agent_id="a", user_message="hi")
    req.prior_messages.append(Message(role="user", content="leak?"))
    assert req2.prior_messages == []


# ---------------------------------------------------------------------------
# 2. _compose_messages 拼接顺序
# ---------------------------------------------------------------------------


def test_compose_messages_places_prior_messages_between_system_and_user() -> None:
    loop = AgentLoop(_ScriptedProvider([]), toolsets=[], system_prelude="ROOT PRELUDE")
    prior = [
        Message(role="user", content="earlier user msg"),
        Message(role="assistant", content="earlier assistant reply"),
    ]
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="now",
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


def test_compose_messages_with_empty_prior_messages_unchanged_from_pre_prior_behavior() -> None:
    """No prior_messages → 旧行为 [system?, user];本测试 lock 住向后兼容性。"""
    loop = AgentLoop(_ScriptedProvider([]), toolsets=[], system_prelude="P")
    req = RunRequest(tenant_id="t", agent_id="a", user_message="hi")
    out = loop._compose_messages(req)
    assert len(out) == 2
    assert out[0].role == "system"
    assert out[1].role == "user"


def test_compose_messages_no_prelude_with_prior_messages_skips_system() -> None:
    """无 prelude(loop 和 request 都空)+ 有 prior_messages → [*prior, user],
    不发 system message。"""
    loop = AgentLoop(_ScriptedProvider([]), toolsets=[])
    prior = [Message(role="user", content="earlier")]
    req = RunRequest(tenant_id="t", agent_id="a", user_message="now", prior_messages=prior)
    out = loop._compose_messages(req)
    assert [m.role for m in out] == ["user", "user"]
    assert out[0].content == "earlier"
    assert out[1].content == "now"


# ---------------------------------------------------------------------------
# 3. Invariants — system role rejected
# ---------------------------------------------------------------------------


def test_run_request_rejects_system_role_in_prior_messages() -> None:
    with pytest.raises(ValueError, match="role='system'"):
        RunRequest(
            tenant_id="t", agent_id="a", user_message="hi",
            prior_messages=[
                Message(role="user", content="ok"),
                Message(role="system", content="sneaky system"),
            ],
        )


def test_run_request_error_message_points_to_alternative() -> None:
    """ValueError 文案告诉用户 system 内容该塞哪里 —— 避免重复踩。"""
    with pytest.raises(ValueError) as exc_info:
        RunRequest(
            tenant_id="t", agent_id="a", user_message="hi",
            prior_messages=[Message(role="system", content="x")],
        )
    msg = str(exc_info.value)
    assert "system_prelude" in msg


# ---------------------------------------------------------------------------
# 4. Invariants — tool-pair integrity
# ---------------------------------------------------------------------------


def test_run_request_rejects_orphan_tool_message_without_prior_assistant_call() -> None:
    with pytest.raises(ValueError, match="orphan tool message"):
        RunRequest(
            tenant_id="t", agent_id="a", user_message="hi",
            prior_messages=[
                Message(role="user", content="search"),
                Message(role="tool", content="result for nothing", tool_call_id="nope"),
            ],
        )


def test_run_request_accepts_complete_tool_call_pair_in_prior_messages() -> None:
    """assistant(tool_calls=[X]) + tool(call_id=X) 配对完整 → 接受。"""
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="now",
        prior_messages=[
            Message(role="user", content="search foo"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "foo"})],
            ),
            Message(role="tool", content="{result}", tool_call_id="c1"),
            Message(role="assistant", content="here it is"),
        ],
    )
    # 没 raise = 通过
    assert len(req.prior_messages) == 4


# ---------------------------------------------------------------------------
# 5. End-to-end:honesty re-run shape works through full loop
# ---------------------------------------------------------------------------


async def test_honesty_re_run_shape_loop_sees_prior_assistant_and_continues() -> None:
    """模拟 baizhi honesty re-run:上一 attempt 的 assistant text 进
    prior_messages,user_message 是 runtime correction;loop 正常跑一轮 → 出
    final_text。验证 provider 看到的 messages 顺序是
    [system?, prior_assistant, user(correction)]。"""
    provider = _ScriptedProvider([
        LlmResponse(text="correction applied", tool_calls=[]),
    ])
    loop = AgentLoop(provider, toolsets=[])
    req = RunRequest(
        tenant_id="t", agent_id="a",
        user_message="Runtime correction: actually use the tool",
        max_rounds=3,
        prior_messages=[
            Message(role="assistant", content="(premature) Already saved!"),
        ],
    )
    events = await _drain(loop.run(req, _ctx()))
    # final_text 出来
    kinds = [e.kind for e in events]
    assert "final_text" in kinds
    final = next(e for e in events if e.kind == "final_text")
    assert final.payload["text"] == "correction applied"
    # provider 看到的 messages 顺序对
    sent_messages = provider.calls[0][0]
    assert [m.role for m in sent_messages] == ["assistant", "user"]
    assert sent_messages[0].content == "(premature) Already saved!"
    assert sent_messages[1].content == "Runtime correction: actually use the tool"


async def test_multi_turn_history_replay_through_prior_messages() -> None:
    """更典型的多轮 chat:5 个 prior turns + 新 user turn。验证 loop 不动
    prior_messages,只 append user_message 然后给 provider。"""
    provider = _ScriptedProvider([
        LlmResponse(text="ok, that's turn 4 response", tool_calls=[]),
    ])
    loop = AgentLoop(provider, toolsets=[])
    prior = [
        Message(role="user", content="turn 1 user"),
        Message(role="assistant", content="turn 1 assistant"),
        Message(role="user", content="turn 2 user"),
        Message(role="assistant", content="turn 2 assistant"),
        Message(role="user", content="turn 3 user"),
        Message(role="assistant", content="turn 3 assistant"),
    ]
    req = RunRequest(
        tenant_id="t", agent_id="a",
        user_message="turn 4 user",
        max_rounds=3,
        prior_messages=prior,
    )
    await _drain(loop.run(req, _ctx()))
    sent = provider.calls[0][0]
    assert len(sent) == 7  # 6 prior + 1 new user
    assert [m.role for m in sent] == [
        "user", "assistant", "user", "assistant", "user", "assistant", "user"
    ]
    assert sent[-1].content == "turn 4 user"
