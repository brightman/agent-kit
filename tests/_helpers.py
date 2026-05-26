"""共享测试 helper —— 之前每个测试文件自己造一套 _ScriptedProvider /
_RecordingToolset / _ctx() / _basic_req()。集中放这里,降重 + 行为一致。

不导出到 agent_kit;`tests/conftest.py` 不需要(被 import 即用)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_kit import (
    BaseToolset,
    LlmResponse,
    Message,
    RunRequest,
    ToolCall,
    ToolCallContext,
    ToolResult,
    ToolSchema,
)


# ---- Provider -------------------------------------------------------------


def _default_response() -> LlmResponse:
    return LlmResponse(
        text="ok", tool_calls=[], usage={}, raw={}, finish_reason="stop"
    )


class ScriptedProvider:
    """LlmProvider 实现 —— 按顺序返回 queued responses。

    三种模式:
    - 单个 / 多个 responses:按顺序弹;耗尽时根据 `exhaust` 处理
    - `chat_fn=async_callable(messages, tools, round_index)` —— round-aware
      自定义响应(用于多轮 scripted 流程)
    - `exhaust="repeat-last"`(默认)/ `"raise"`(老 _ScriptedProvider 行为)
      / `"default"`(返回 default reply)

    `calls` 记录每次 chat 的 messages / tools / temperature / max_tokens。
    """

    name = "scripted"

    def __init__(
        self,
        responses: list[LlmResponse] | None = None,
        *,
        chat_fn: Callable[..., Awaitable[LlmResponse]] | None = None,
        exhaust: str = "repeat-last",  # "repeat-last" | "raise" | "default"
    ) -> None:
        self._responses = list(responses or [])
        self._chat_fn = chat_fn
        self._exhaust = exhaust
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LlmResponse:
        self.calls.append({
            "messages": list(messages),
            "tools": list(tools) if tools else None,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if self._chat_fn is not None:
            return await self._chat_fn(messages, tools, len(self.calls) - 1)
        if not self._responses:
            if self._exhaust == "raise":
                raise RuntimeError("scripted provider exhausted")
            return _default_response()
        if len(self._responses) == 1:
            return self._responses[0]  # repeat last forever
        return self._responses.pop(0)

    async def chat_stream(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


class RaisingProvider:
    """每次 chat 都抛 `error_cls(message)`。用于测 provider 异常 →
    error event stage=provider。"""

    name = "raising"

    def __init__(self, error_cls: type = RuntimeError, message: str = "boom") -> None:
        self._error_cls = error_cls
        self._message = message
        self.calls = 0

    async def chat(self, *a: Any, **k: Any) -> LlmResponse:
        self.calls += 1
        raise self._error_cls(self._message)

    async def chat_stream(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


# ---- Toolset --------------------------------------------------------------


class RecordingToolset(BaseToolset):
    """通用 stub toolset —— 接 `{tool_name: handler|str}` dict,handler 可以是
    `lambda args: result`(callable)或静态值。execute 调用全记录到
    `self.execute_calls`,aclose 计数到 `self.closed`。"""

    def __init__(
        self, name: str = "test", handlers: dict[str, Any] | None = None
    ) -> None:
        self.name = name
        self._handlers = dict(handlers or {})
        self.execute_calls: list[ToolCall] = []
        self.closed: int = 0

    def build_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name=n,
                description=f"stub {n}",
                parameters={"type": "object", "properties": {}},
            )
            for n in self._handlers
        ]

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        self.execute_calls.append(call)
        h = self._handlers.get(call.name)
        if h is None:
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: unknown {call.name}",
                is_error=True,
            )
        r = h(call.arguments) if callable(h) else h
        if isinstance(r, ToolResult):
            return r
        return ToolResult(call_id=call.id, content=str(r))

    async def aclose(self) -> None:
        self.closed += 1


# ---- RunRequest / ToolCallContext factories -------------------------------


def make_request(**overrides: Any) -> RunRequest:
    """RunRequest with sane defaults; `overrides` 覆盖任何字段。"""
    base: dict[str, Any] = dict(
        agent_id="a",
        user_message="hi",
        max_rounds=3,
    )
    base.update(overrides)
    return RunRequest(**base)


def make_ctx(**overrides: Any) -> ToolCallContext:
    """ToolCallContext with sane defaults;tests 一般不关心 ctx 细节。"""
    base: dict[str, Any] = dict(
        run_id="r1",
        cancel=asyncio.Event(),
        workspace=Path("/tmp"),
        emit=lambda evt: None,
    )
    base.update(overrides)
    return ToolCallContext(**base)


# ---- Convenience constructors ---------------------------------------------


def text_response(text: str = "ok", **kw: Any) -> LlmResponse:
    """单条 final-text response。"""
    return LlmResponse(
        text=text, tool_calls=[],
        usage=kw.get("usage", {}),
        raw=kw.get("raw", {}),
        finish_reason=kw.get("finish_reason", "stop"),
    )


def tool_call_response(*calls: ToolCall, text: str = "", **kw: Any) -> LlmResponse:
    """LLM response 含 tool_calls(常配多轮 scripted 流程的前 N 轮)。"""
    return LlmResponse(
        text=text, tool_calls=list(calls),
        usage=kw.get("usage", {}),
        raw=kw.get("raw", {}),
        finish_reason=kw.get("finish_reason", "tool_calls"),
    )


__all__ = [
    "ScriptedProvider",
    "RaisingProvider",
    "RecordingToolset",
    "make_request",
    "make_ctx",
    "text_response",
    "tool_call_response",
]
