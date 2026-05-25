"""tests/test_max_tokens.py — RunRequest.max_tokens contract (spec § 3.7.3).

Coverage:
- default max_tokens=None → provider sees None
- custom max_tokens=512 → provider sees 512
- non-None value carried across multiple rounds (per-call passes it each
  time, not just first round)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_kit.loop import AgentLoop, RunRequest
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.toolset import BaseToolset, ToolCallContext
from agent_kit.types import Message, ToolCall, ToolResult


class _RecordingProvider:
    """Provider that records every chat() kwargs for assertion."""

    name = "recording"

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append({"max_tokens": max_tokens, "temperature": temperature})
        if not self._responses:
            raise RuntimeError("recording provider exhausted")
        return self._responses.pop(0)

    async def chat_stream(self, *_, **__):
        raise NotImplementedError


class _OneTool(BaseToolset):
    name = "tools"

    def build_schemas(self):
        return [
            ToolSchema(name="t", description="t", parameters={"type": "object"})
        ]

    async def execute(self, call, ctx):
        return ToolResult(call_id=call.id, content="ok")


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        tenant_id="t", run_id="r", skill_name=None,
        cancel=asyncio.Event(),
        workspace=Path("/tmp"), storage=Path("/tmp"),
        emit=lambda evt: None,
    )


async def _drain(loop_run):
    return [evt async for evt in loop_run]


async def test_default_max_tokens_none_passed_through_as_none() -> None:
    provider = _RecordingProvider([LlmResponse(text="ok", tool_calls=[])])
    loop = AgentLoop(provider, toolsets=[])
    req = RunRequest(tenant_id="t", agent_id="a", user_message="hi", max_rounds=3)
    await _drain(loop.run(req, _ctx()))
    assert len(provider.calls) == 1
    assert provider.calls[0]["max_tokens"] is None


async def test_explicit_max_tokens_512_passed_through_to_provider() -> None:
    provider = _RecordingProvider([LlmResponse(text="ok", tool_calls=[])])
    loop = AgentLoop(provider, toolsets=[])
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi",
        max_rounds=3, max_tokens=512,
    )
    await _drain(loop.run(req, _ctx()))
    assert provider.calls[0]["max_tokens"] == 512


async def test_max_tokens_passed_every_round_not_just_first() -> None:
    """multi-round 场景:max_tokens 在每个 provider.chat 调用都透传,不是
    只第一次。"""
    provider = _RecordingProvider([
        LlmResponse(text="", tool_calls=[ToolCall(id="c1", name="t", arguments={})]),
        LlmResponse(text="done", tool_calls=[]),
    ])
    loop = AgentLoop(provider, toolsets=[_OneTool()])
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi",
        max_rounds=3, max_tokens=200,
    )
    await _drain(loop.run(req, _ctx()))
    assert len(provider.calls) == 2
    assert provider.calls[0]["max_tokens"] == 200
    assert provider.calls[1]["max_tokens"] == 200


async def test_max_tokens_zero_is_valid_int_passed_through() -> None:
    """edge:0 是合法 int(provider 决定怎么处理 —— 通常是 vendor default
    或 error)。SDK 不替 caller 决策,如实传。"""
    provider = _RecordingProvider([LlmResponse(text="ok", tool_calls=[])])
    loop = AgentLoop(provider, toolsets=[])
    req = RunRequest(
        tenant_id="t", agent_id="a", user_message="hi",
        max_rounds=3, max_tokens=0,
    )
    await _drain(loop.run(req, _ctx()))
    assert provider.calls[0]["max_tokens"] == 0
