"""tests/contrib/test_providers_litellm.py — LiteLlm provider (spec § 17.3).

不打真 API,monkeypatch `litellm.acompletion` 喂 OpenAI 形态的 dict /
SimpleNamespace,验我们的:
- messages → OpenAI dict 翻译(role / tool_calls / tool_call_id)
- tools → OpenAI function 翻译
- response → LlmResponse 反向翻译(text / tool_calls / usage / finish_reason)
- tool_calls arguments JSON 解码(含非法 JSON 的兜底)
- usage 字段映射
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

# Skip the entire file if litellm extras not installed
litellm = pytest.importorskip("litellm")

from agent_kit.contrib.providers.litellm import LiteLlm
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.types import Message, ToolCall


# ---- helpers: fake litellm response shape ----


def _fake_response(
    *,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
):
    """LiteLLM ModelResponse 形态(OpenAI-compatible);用 SimpleNamespace 拼。"""
    message = SimpleNamespace(
        content=content or None,
        tool_calls=[
            SimpleNamespace(
                id=tc["id"],
                function=SimpleNamespace(
                    name=tc["name"],
                    arguments=tc["arguments"],   # JSON string
                ),
            )
            for tc in (tool_calls or [])
        ] or None,
    )
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage_obj = (
        SimpleNamespace(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
        if usage
        else None
    )
    raw = SimpleNamespace(choices=[choice], usage=usage_obj)
    # Mimic pydantic .model_dump
    raw.model_dump = lambda: {"choices": [{"message": {"content": content}}]}  # type: ignore[attr-defined]
    return raw


def _patch_acompletion(monkeypatch, returns):
    """Patch litellm.acompletion to record the kwargs + return the canned response."""
    seen: dict[str, Any] = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        return returns

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return seen


# ---- construction ----


def test_litellm_requires_non_empty_model() -> None:
    with pytest.raises(ValueError, match="non-empty model"):
        LiteLlm("")


def test_litellm_name_is_litellm_prefix() -> None:
    p = LiteLlm("anthropic/claude-haiku-4-5")
    assert p.name == "litellm:anthropic/claude-haiku-4-5"


# ---- request translation: messages ----


@pytest.mark.asyncio
async def test_simple_user_message_translation(monkeypatch) -> None:
    seen = _patch_acompletion(
        monkeypatch, _fake_response(content="hi back")
    )
    p = LiteLlm("openai/gpt-4o-mini")
    await p.chat([Message(role="user", content="hello")])
    assert seen["messages"] == [{"role": "user", "content": "hello"}]
    assert seen["model"] == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_assistant_with_tool_calls_translation(monkeypatch) -> None:
    """assistant.tool_calls → OpenAI tool_calls dict with JSON-string arguments."""
    seen = _patch_acompletion(monkeypatch, _fake_response(content="done"))
    p = LiteLlm("openai/gpt-4o-mini")
    await p.chat([
        Message(role="user", content="search foo"),
        Message(
            role="assistant", content="",
            tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "foo"})],
        ),
        Message(role="tool", content="results", tool_call_id="c1"),
    ])
    msgs = seen["messages"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "search"
    # arguments is JSON-stringified (OpenAI contract)
    assert json.loads(msgs[1]["tool_calls"][0]["function"]["arguments"]) == {"q": "foo"}
    assert msgs[2] == {
        "role": "tool", "tool_call_id": "c1", "content": "results",
    }


# ---- request translation: tools ----


@pytest.mark.asyncio
async def test_tools_translated_to_function_schemas(monkeypatch) -> None:
    seen = _patch_acompletion(monkeypatch, _fake_response(content=""))
    p = LiteLlm("openai/gpt-4o-mini")
    await p.chat(
        [Message(role="user", content="hi")],
        tools=[ToolSchema(
            name="search",
            description="Web search",
            parameters={"type": "object", "properties": {"q": {"type": "string"}},
                        "required": ["q"]},
        )],
    )
    assert seen["tools"][0]["type"] == "function"
    fn = seen["tools"][0]["function"]
    assert fn["name"] == "search"
    assert fn["description"] == "Web search"
    assert fn["parameters"]["required"] == ["q"]
    assert seen["tool_choice"] == "auto"


# ---- response translation ----


@pytest.mark.asyncio
async def test_text_only_response(monkeypatch) -> None:
    _patch_acompletion(
        monkeypatch,
        _fake_response(content="Hello there", finish_reason="stop",
                       usage={"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15}),
    )
    p = LiteLlm("openai/gpt-4o-mini")
    resp = await p.chat([Message(role="user", content="hi")])
    assert isinstance(resp, LlmResponse)
    assert resp.text == "Hello there"
    assert resp.tool_calls == []
    assert resp.finish_reason == "stop"
    assert resp.usage["prompt_tokens"] == 10
    assert resp.usage["completion_tokens"] == 5
    assert resp.usage["total_tokens"] == 15


@pytest.mark.asyncio
async def test_tool_calls_response_decodes_arguments(monkeypatch) -> None:
    _patch_acompletion(
        monkeypatch,
        _fake_response(
            content="",
            tool_calls=[
                {"id": "c1", "name": "search",
                 "arguments": json.dumps({"q": "Anthropic"})},
                {"id": "c2", "name": "list_skills",
                 "arguments": "{}"},
            ],
            finish_reason="tool_calls",
        ),
    )
    p = LiteLlm("openai/gpt-4o-mini")
    resp = await p.chat([Message(role="user", content="search and list")])
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0].id == "c1"
    assert resp.tool_calls[0].name == "search"
    assert resp.tool_calls[0].arguments == {"q": "Anthropic"}
    assert resp.tool_calls[1].arguments == {}


@pytest.mark.asyncio
async def test_invalid_json_arguments_recovered(monkeypatch) -> None:
    """LLM occasionally returns malformed JSON in tool_calls — we don't crash."""
    _patch_acompletion(
        monkeypatch,
        _fake_response(
            tool_calls=[{"id": "c1", "name": "search", "arguments": "{not valid"}],
            finish_reason="tool_calls",
        ),
    )
    p = LiteLlm("openai/gpt-4o-mini")
    resp = await p.chat([Message(role="user", content="hi")])
    assert resp.tool_calls[0].arguments == {"__raw_arguments__": "{not valid"}


@pytest.mark.asyncio
async def test_mixed_text_and_tool_calls(monkeypatch) -> None:
    """LLM can produce text AND tool calls in the same response."""
    _patch_acompletion(
        monkeypatch,
        _fake_response(
            content="Searching...",
            tool_calls=[{"id": "c1", "name": "search",
                          "arguments": json.dumps({"q": "x"})}],
            finish_reason="tool_calls",
        ),
    )
    p = LiteLlm("openai/gpt-4o-mini")
    resp = await p.chat([Message(role="user", content="hi")])
    assert resp.text == "Searching..."
    assert len(resp.tool_calls) == 1


# ---- per-call params ----


@pytest.mark.asyncio
async def test_temperature_and_max_tokens_passed(monkeypatch) -> None:
    seen = _patch_acompletion(monkeypatch, _fake_response(content="ok"))
    p = LiteLlm("openai/gpt-4o-mini")
    await p.chat(
        [Message(role="user", content="hi")],
        temperature=0.1,
        max_tokens=200,
    )
    assert seen["temperature"] == 0.1
    assert seen["max_tokens"] == 200


@pytest.mark.asyncio
async def test_ctor_kwargs_passthrough(monkeypatch) -> None:
    """**litellm_kwargs go through to acompletion (api_key / api_base / 等)."""
    seen = _patch_acompletion(monkeypatch, _fake_response(content="ok"))
    p = LiteLlm(
        "openai/MiniMax-M2.7",
        api_base="https://api.minimaxi.com/v1",
        api_key="sk-test",
    )
    await p.chat([Message(role="user", content="hi")])
    assert seen["api_base"] == "https://api.minimaxi.com/v1"
    assert seen["api_key"] == "sk-test"


# ---- stream not supported yet (spec § 14 deferred) ----


@pytest.mark.asyncio
async def test_chat_stream_raises_not_implemented() -> None:
    p = LiteLlm("openai/gpt-4o-mini")
    with pytest.raises(NotImplementedError, match="Stage 7"):
        async for _ in p.chat_stream([Message(role="user", content="hi")]):
            pass


# ---- end-to-end Agent + LiteLlm wiring ----


@pytest.mark.asyncio
async def test_agent_with_litellm_string_model(monkeypatch) -> None:
    """Agent(model='openai/...') → LiteLlm wrap → fake acompletion → RunResult."""
    _patch_acompletion(
        monkeypatch,
        _fake_response(content="agent reply", finish_reason="stop",
                       usage={"prompt_tokens": 3, "completion_tokens": 2,
                              "total_tokens": 5}),
    )
    from agent_kit import Agent
    agent = Agent(name="x", model="openai/gpt-4o-mini")
    result = await agent.run("hi")
    assert result.final_text == "agent reply"
