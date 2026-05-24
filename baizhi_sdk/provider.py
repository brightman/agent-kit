"""LlmProvider Protocol —— 所有 LLM 接入的最小接口。

设计来源:
- baizhi-agent llm/base.py(MiniMaxProvider / LiteLlmProvider 已实现这一接口形状)
- ADK BaseLlm.generate_content_async 简化版

只规定两个方法:chat(non-stream)+ chat_stream(stream)。模型 id / API key /
base_url / 重试 / 速率限制 全部在 provider 的构造函数里吃掉,loop 层不感知。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from .types import Message, ToolCall


@dataclass
class ToolSchema:
    """暴露给 LLM 的工具 schema。对应 OpenAI tools / Anthropic tools 的统一中间格式。"""

    name: str
    description: str
    parameters: dict[str, Any]   # JSON Schema


@dataclass
class LlmResponse:
    """provider.chat 的返回。"""

    text: str
    tool_calls: list[ToolCall]
    raw: dict[str, Any]          # provider 原始响应,留作 trace / debug


@dataclass
class LlmDelta:
    """provider.chat_stream 的增量。"""

    text_delta: str | None = None
    tool_call_delta: ToolCall | None = None
    finish_reason: str | None = None


class LlmProvider(Protocol):
    """SDK 接入新模型只需实现这个 Protocol。"""

    name: str

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LlmResponse: ...

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LlmDelta]: ...


__all__ = ["ToolSchema", "LlmResponse", "LlmDelta", "LlmProvider"]
