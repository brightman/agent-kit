"""tests/test_max_tokens.py — RunRequest.max_tokens contract (spec § 3.7.3).

Coverage:
- default max_tokens=None → provider sees None
- custom max_tokens=512 → provider sees 512
- non-None value carried across multiple rounds (per-call passes it each
  time, not just first round)
"""

from __future__ import annotations

from agent_kit.loop import AgentLoop
from agent_kit.types import ToolCall

from tests._helpers import (
    RecordingToolset,
    ScriptedProvider,
    make_ctx,
    make_request,
    text_response,
    tool_call_response,
)


async def _drain(loop_run):
    return [evt async for evt in loop_run]


async def test_default_max_tokens_none_passed_through_as_none() -> None:
    provider = ScriptedProvider([text_response()])
    loop = AgentLoop(provider, toolsets=[])
    await _drain(loop.run(make_request(), make_ctx()))
    assert provider.calls[0]["max_tokens"] is None


async def test_explicit_max_tokens_512_passed_through_to_provider() -> None:
    provider = ScriptedProvider([text_response()])
    loop = AgentLoop(provider, toolsets=[])
    await _drain(loop.run(make_request(max_tokens=512), make_ctx()))
    assert provider.calls[0]["max_tokens"] == 512


async def test_max_tokens_passed_every_round_not_just_first() -> None:
    """multi-round 场景:max_tokens 每次 provider.chat 都透传。"""
    provider = ScriptedProvider([
        tool_call_response(ToolCall(id="c1", name="t", arguments={})),
        text_response("done"),
    ])
    loop = AgentLoop(provider, toolsets=[RecordingToolset("tools", {"t": "ok"})])
    await _drain(loop.run(make_request(max_tokens=200), make_ctx()))
    assert len(provider.calls) == 2
    assert provider.calls[0]["max_tokens"] == 200
    assert provider.calls[1]["max_tokens"] == 200


async def test_max_tokens_zero_is_valid_int_passed_through() -> None:
    """edge:0 是合法 int,如实传给 provider。SDK 不替 caller 决策合法范围。"""
    provider = ScriptedProvider([text_response()])
    loop = AgentLoop(provider, toolsets=[])
    await _drain(loop.run(make_request(max_tokens=0), make_ctx()))
    assert provider.calls[0]["max_tokens"] == 0
