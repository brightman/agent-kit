"""核心数据类型 —— 纯数据,不依赖任何 IO / provider / runtime。

设计来源:
- ToolCall / ToolResult 形状收敛于 ADK / OpenHarness / baizhi-agent / fam-runtime 共识
- Event 的 event_id + parent_event_id 来自 baizhi-agent PR α(pr-trace-a)
- Message 同时容纳 OpenAI(role+content+tool_calls)和 Anthropic(content blocks)风格

每类都附 `to_dict()` 供 Event payload 序列化用,反向 `from_dict()` 用于
反序列化 trace。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
EventKind = Literal[
    "round_start",
    "llm_request",
    "llm_delta",                # Q1 stream 决议,仅 stream 模式下出现
    "llm_response",
    "llm_short_circuited",      # before_model hook 短路 → 替代 llm_response 之前的 LLM 实调
    "tool_call",
    "tool_result",
    "tool_short_circuited",     # before_tool hook 短路 → 替代 tool 实调
    "round_end",
    "final_text",
    "error",
    "cancelled",
    "context_compacted",        # 上下文 compact 触发后 emit;payload 见 tech-design § 3.5
]


@dataclass(frozen=True)
class ToolCall:
    """LLM 发起的一次工具调用。id 必填(OpenAI / Anthropic 都需要回引)。"""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": dict(self.arguments)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolCall":
        return cls(id=d["id"], name=d["name"], arguments=dict(d.get("arguments", {})))


@dataclass(frozen=True)
class ToolResult:
    """单次工具调用的结果。call_id 回引 ToolCall.id。"""

    call_id: str
    content: str
    is_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "content": self.content, "is_error": self.is_error}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolResult":
        return cls(
            call_id=d["call_id"],
            content=d["content"],
            is_error=bool(d.get("is_error", False)),
        )


@dataclass(frozen=True)
class Message:
    """会话消息的统一表达。

    - role=assistant 时 tool_calls 可能非空
    - role=tool 时 tool_call_id 必填
    - content 用 str 是当前最小集;后续多模态再扩 list[ContentBlock]

    Invariants(__post_init__ 校验):
    - tool_calls 非 None iff role == "assistant"
    - tool_call_id 非 None iff role == "tool"
    """

    role: Role
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.tool_calls is not None and self.role != "assistant":
            raise ValueError(
                f"tool_calls only valid for role='assistant', got role={self.role!r}"
            )
        if self.tool_call_id is not None and self.role != "tool":
            raise ValueError(
                f"tool_call_id only valid for role='tool', got role={self.role!r}"
            )
        if self.role == "tool" and self.tool_call_id is None:
            raise ValueError("role='tool' requires tool_call_id")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls is not None:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        tool_calls_raw = d.get("tool_calls")
        return cls(
            role=d["role"],
            content=d["content"],
            tool_calls=(
                [ToolCall.from_dict(tc) for tc in tool_calls_raw]
                if tool_calls_raw is not None
                else None
            ),
            tool_call_id=d.get("tool_call_id"),
        )


@dataclass(frozen=True)
class Event:
    """Loop 对外的唯一输出形态。kind 决定 payload 结构。"""

    event_id: str
    parent_event_id: str | None
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["Role", "EventKind", "ToolCall", "ToolResult", "Message", "Event"]
