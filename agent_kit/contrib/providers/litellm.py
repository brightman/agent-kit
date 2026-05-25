"""LiteLLM-backed `LlmProvider` —— spec § 17.3。

`LiteLlm(model)` 包成 `agent_kit.LlmProvider` Protocol 实现。LiteLLM 内部把
所有 provider 规范化成 OpenAI chat completions 形态,我们只做一层数据形态
翻译(`agent_kit.Message` ↔ OpenAI dict)。

Examples:

    from agent_kit.contrib.providers.litellm import LiteLlm

    # Gemini(LiteLLM 自家路由)
    provider = LiteLlm("gemini/gemini-flash-latest")

    # Anthropic
    provider = LiteLlm("anthropic/claude-haiku-4-5", api_key="sk-...")

    # OpenAI
    provider = LiteLlm("openai/gpt-4o-mini")

    # 自家 OpenAI-compatible(MiniMax / DeepSeek / Together / ...)
    provider = LiteLlm(
        "openai/MiniMax-M2.7",
        api_base="https://api.minimaxi.com/v1",
        api_key="...",
    )

**`**litellm_kwargs` 透传给 `litellm.acompletion`**(api_key / api_base /
custom_llm_provider / drop_params / 等)。我们不重新发明 LiteLLM 的 API surface
—— LiteLLM 加 / 改参数,这里**不用同步改**。

需要 `pip install "agent-kit[litellm]"`。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from ...provider import LlmDelta, LlmResponse, ToolSchema
from ...types import Message, ToolCall

try:
    import litellm  # noqa: F401
except ImportError as exc:
    raise ImportError(
        "agent_kit.contrib.providers.litellm requires `litellm`. Install with:\n\n"
        "    pip install \"agent-kit[litellm]\"\n"
    ) from exc


class LiteLlm:
    """LiteLLM-backed `LlmProvider` 实现。"""

    def __init__(self, model: str, **litellm_kwargs: Any) -> None:
        if not model:
            raise ValueError("LiteLlm requires a non-empty model string")
        self.model = model
        self._kwargs = dict(litellm_kwargs)
        self.name = f"litellm:{model}"

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LlmResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_encode_message(m) for m in messages],
            "temperature": temperature,
            **self._kwargs,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [_encode_tool_schema(t) for t in tools]
            payload["tool_choice"] = payload.get("tool_choice", "auto")

        # 调 LiteLLM 异步路径;LiteLLM 内部统一规范化成 OpenAI ChatCompletion 形态
        import litellm
        raw = await litellm.acompletion(**payload)
        return _decode_response(raw)

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LlmDelta]:
        # spec § 14 修订:stream 推迟到 Stage 7+。
        # LlmProvider Protocol 要求不支持 stream 时 raise NotImplementedError。
        raise NotImplementedError(
            "LiteLlm stream support is deferred — see spec § 14 (Stage 7+ candidate)"
        )
        # unreachable but type-checker wants it
        yield  # type: ignore[unreachable]


# ============================================================================
# 翻译:agent_kit ↔ OpenAI / LiteLLM 形态
# ============================================================================


def _encode_message(message: Message) -> dict[str, Any]:
    """`agent_kit.Message` → OpenAI chat.completions message dict。"""
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False
                        ),
                    },
                }
                for call in message.tool_calls
            ],
        }
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    return {"role": message.role, "content": message.content}


def _encode_tool_schema(tool: ToolSchema) -> dict[str, Any]:
    """`agent_kit.ToolSchema` → OpenAI `tools[*]` entry。"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _decode_response(raw: Any) -> LlmResponse:
    """LiteLLM `ModelResponse` → `agent_kit.LlmResponse`。

    LiteLLM 已经规范化成 OpenAI 形态,所以这里只需要 attribute / dict 访问
    +  tool_call.arguments JSON 解码。
    """
    # LiteLLM 返回 pydantic 模型,也支持 `.dict()` / `.model_dump()`;
    # 走 attribute 风格保持兼容
    choice = raw.choices[0]
    message = choice.message

    # text(可能是 None 当全是 tool_call)
    content = getattr(message, "content", None) or ""

    # tool_calls
    tool_calls: list[ToolCall] = []
    raw_tool_calls = getattr(message, "tool_calls", None) or []
    for index, call in enumerate(raw_tool_calls):
        fn = getattr(call, "function", None) or {}
        # LiteLLM 把 .function 返回成 pydantic 模型或 dict;统一处理
        fn_name = (
            getattr(fn, "name", None)
            if hasattr(fn, "name")
            else fn.get("name", "")
        ) or ""
        fn_args_raw = (
            getattr(fn, "arguments", None)
            if hasattr(fn, "arguments")
            else fn.get("arguments")
        )
        if isinstance(fn_args_raw, str):
            try:
                args = json.loads(fn_args_raw) if fn_args_raw else {}
            except json.JSONDecodeError:
                # LLM 偶尔吐非法 JSON;把原文塞进 __raw_arguments__ 给上层观察
                args = {"__raw_arguments__": fn_args_raw}
        elif isinstance(fn_args_raw, dict):
            args = dict(fn_args_raw)
        else:
            args = {}
        call_id = getattr(call, "id", None) or f"call_{index}"
        tool_calls.append(ToolCall(id=call_id, name=fn_name, arguments=args))

    # usage(LiteLLM 规范化成 OpenAI 形态)
    usage_obj = getattr(raw, "usage", None)
    usage: dict[str, Any] = {}
    if usage_obj is not None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = getattr(usage_obj, key, None)
            if v is not None:
                usage[key] = int(v)
        # provider-specific 计费字段(cost 等)如果 LiteLLM 提供,带上
        cost = getattr(usage_obj, "cost", None) or getattr(raw, "response_cost", None)
        if cost is not None:
            usage["cost"] = cost

    finish_reason = getattr(choice, "finish_reason", None)

    # raw:LiteLLM 的 ModelResponse 全量(可用 .model_dump() / 也接受非 dict)
    raw_dump: dict[str, Any]
    if hasattr(raw, "model_dump"):
        try:
            raw_dump = raw.model_dump()
        except Exception:  # noqa: BLE001 —— pydantic 偶尔挑食,fallback to dict
            raw_dump = {}
    else:
        raw_dump = {}

    return LlmResponse(
        text=content,
        tool_calls=tool_calls,
        usage=usage,
        raw=raw_dump,
        finish_reason=finish_reason,
    )


__all__ = ["LiteLlm"]
