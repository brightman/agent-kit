"""核心数据类型 —— 纯数据,不依赖任何 IO / provider / runtime。

设计来源:
- ToolCall / ToolResult 形状收敛于 ADK / OpenHarness / baizhi-agent / fam-runtime 共识
- Event 的 event_id + parent_event_id 来自 baizhi-agent PR α(pr-trace-a)
- Message 同时容纳 OpenAI(role+content+tool_calls)和 Anthropic(content blocks)风格

NOTE: stage 0 只占位 + docstring;真实现等首次接进 baizhi-agent 时再补。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
EventKind = Literal[
    "round_start",
    "llm_request",
    "llm_response",
    "tool_call",
    "tool_result",
    "round_end",
    "final_text",
    "error",
    "cancelled",
]


@dataclass
class ToolCall:
    """LLM 发起的一次工具调用。id 必填(OpenAI / Anthropic 都需要回引)。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """单次工具调用的结果。call_id 回引 ToolCall.id。"""

    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """会话消息的统一表达。

    - role=assistant 时 tool_calls 可能非空
    - role=tool 时 tool_call_id 必填
    - content 用 str 是当前最小集;后续多模态再扩 list[ContentBlock]
    """

    role: Role
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


@dataclass
class Event:
    """Loop 对外的唯一输出形态。kind 决定 payload 结构。"""

    event_id: str
    parent_event_id: str | None
    kind: EventKind
    payload: dict[str, Any]
    ts: float


__all__ = ["Role", "EventKind", "ToolCall", "ToolResult", "Message", "Event"]
