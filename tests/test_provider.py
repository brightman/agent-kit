"""tests/test_provider.py — Stage 1 provider type contracts.

Real provider implementations (LiteLlm / MiniMax) 是 Stage 5/6 的事;
Stage 1 只覆盖数据类型 + Protocol 形状。
"""

from __future__ import annotations

import inspect

import pytest

from agent_kit.provider import LlmDelta, LlmProvider, LlmResponse, ToolSchema
from agent_kit.types import ToolCall


# ---- ToolSchema ----


def test_tool_schema_basic() -> None:
    s = ToolSchema(name="fetch", description="get a url", parameters={"type": "object"})
    assert s.name == "fetch"


def test_tool_schema_frozen() -> None:
    s = ToolSchema(name="fetch", description="d", parameters={})
    with pytest.raises(Exception):
        s.name = "other"  # type: ignore[misc]


def test_tool_schema_to_dict() -> None:
    s = ToolSchema(name="f", description="d", parameters={"type": "object"})
    d = s.to_dict()
    assert d == {"name": "f", "description": "d", "parameters": {"type": "object"}}


def test_tool_schema_to_dict_copies_parameters() -> None:
    params = {"type": "object", "properties": {}}
    s = ToolSchema(name="f", description="d", parameters=params)
    d = s.to_dict()
    d["parameters"]["mutated"] = True
    assert "mutated" not in s.parameters


# ---- LlmResponse ----


def test_response_minimal() -> None:
    r = LlmResponse(text="hi")
    assert r.tool_calls == []
    assert r.usage == {}
    assert r.finish_reason is None


def test_response_with_tool_calls() -> None:
    tc = ToolCall(id="x", name="t", arguments={})
    r = LlmResponse(text="", tool_calls=[tc], finish_reason="tool_calls")
    assert r.tool_calls == [tc]
    assert r.finish_reason == "tool_calls"


def test_response_to_dict_omits_raw() -> None:
    """to_dict() omits 'raw' to keep trace payloads small."""
    r = LlmResponse(text="hi", usage={"prompt_tokens": 50},
                    raw={"provider_specific": "big_blob"})
    d = r.to_dict()
    assert "raw" not in d
    assert d["text"] == "hi"
    assert d["usage"] == {"prompt_tokens": 50}


def test_response_to_dict_has_tool_call_dicts() -> None:
    tc = ToolCall(id="x", name="t", arguments={"a": 1})
    r = LlmResponse(text="", tool_calls=[tc])
    d = r.to_dict()
    assert d["tool_calls"] == [{"id": "x", "name": "t", "arguments": {"a": 1}}]


# ---- LlmDelta ----


def test_delta_text_only() -> None:
    d = LlmDelta(text_delta="hi")
    assert d.text_delta == "hi"
    assert d.tool_call_delta is None


def test_delta_to_dict_handles_none_fields() -> None:
    d = LlmDelta(text_delta="x")
    out = d.to_dict()
    assert out["text_delta"] == "x"
    assert out["tool_call_delta"] is None
    assert out["finish_reason"] is None
    assert out["usage"] is None


def test_delta_to_dict_with_tool_call() -> None:
    tc = ToolCall(id="x", name="t", arguments={})
    d = LlmDelta(tool_call_delta=tc, finish_reason="tool_calls",
                 usage={"prompt_tokens": 5})
    out = d.to_dict()
    assert out["tool_call_delta"]["id"] == "x"
    assert out["finish_reason"] == "tool_calls"
    assert out["usage"] == {"prompt_tokens": 5}


# ---- LlmProvider Protocol shape ----


def test_provider_protocol_has_chat_methods() -> None:
    # Protocol can't be instantiated; verify the methods are declared
    assert "chat" in LlmProvider.__dict__
    assert "chat_stream" in LlmProvider.__dict__


def test_fake_provider_satisfies_protocol() -> None:
    """A class with chat/chat_stream methods structurally satisfies LlmProvider."""

    class Fake:
        name = "fake"

        async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
            return LlmResponse(text="fake")

        async def chat_stream(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
            yield LlmDelta(text_delta="fake")

    # Structural check: methods exist + are async-callable
    f = Fake()
    assert f.name == "fake"
    assert inspect.iscoroutinefunction(f.chat)
    # chat_stream returns an async generator function; check via inspect
    assert inspect.isasyncgenfunction(f.chat_stream)
