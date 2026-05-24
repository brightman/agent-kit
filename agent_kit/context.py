"""Context compaction —— 防止 agent loop 多轮后 context window 爆炸。

## 设计要点

- **ContextCompactor 是 Protocol,不是策略**(类比 LlmProvider)。SDK 不绑
  策略,使用方可注入"LLM 摘要 / RAG 拉回 / 直接丢老的"等任意实现。
- **SDK 提供 1 个内置实现 `TruncatingCompactor`**(microcompact):零 LLM
  成本,替换老 tool_result 的 content 为占位字符串。覆盖 80% 场景。
- **safe_split_messages 是 SDK 强制兜底**:任何 compactor 返回的 messages
  会被 loop 校验"tool_call 与 tool_result 配对"不破坏。失败 raise,不让
  下次 API 调用 400。
- **policy 留给使用方**:LLM 摘要(选模型 / 写 prompt / 控成本)、滑动窗口、
  RAG 拉回老对话等高级策略 = 使用方实现 ContextCompactor。

## 设计依据

- OpenHarness services/compact/__init__.py:808-856 `microcompact_messages`
  —— TruncatingCompactor 默认实现的来源
- ADK apps/compaction.py:388-421 `_safe_token_compaction_split_index`
  —— safe_split_messages 的来源(保护 function_call/response 配对)
- ADK apps/compaction.py:156-173 —— 优先用 API 返回 prompt_tokens 的做法

## Stage 1 实现范围

- ContextCompactor Protocol(本文件,签名 final)
- safe_split_messages(本文件,Stage 1 实现 + 单测)
- _assert_tool_pairs_intact(本文件,Stage 1 实现 + 单测)
- TruncatingCompactor(本文件,Stage 1 实现 + 单测)

Loop 集成(`AgentLoop.__init__(compactor=...)`)留 Stage 2 做。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .types import Message


class ContextCompactor(Protocol):
    """Loop 在每次 provider.chat 前调用。"""

    async def should_compact(
        self,
        messages: list[Message],
        last_usage: dict[str, Any] | None,
    ) -> bool:
        """决定是否要 compact。
        - last_usage 是上一次 LlmResponse.usage(可能含 prompt_tokens / total_tokens)
        - 返回 False 跳过本轮 compact"""

    async def compact(self, messages: list[Message]) -> list[Message]:
        """返回新 messages list。
        实现者 MUST 保证:
        1. 若 messages[0].role == "system",保留(MUST 在返回值的位置 0)
        2. 保留最近 N 条 verbatim(N 由实现自定)
        3. 任何 tool_call 与其对应的 tool_result MUST 同存或同删
        Loop 会在返回后跑 _assert_tool_pairs_intact 兜底;失败 raise。"""


@dataclass
class TruncatingCompactor:
    """microcompact 模式 —— 替换老 tool_result 内容为占位符,零 LLM 成本。

    适合 80% 场景:tool 输出(尤其是 file read / web fetch / mcp 大 payload)
    是 token bloat 的主因,且老 tool_result 极少被 LLM 反复回看。

    对应 OpenHarness microcompact_messages 的极简版(无 keep_recent 复杂规则,
    只按"tool_result message index 距末尾的距离"判定)。
    """

    token_budget: int = 100_000
    keep_recent_tool_results: int = 5
    placeholder: str = "[tool output omitted — older than retention window]"

    async def should_compact(
        self,
        messages: list[Message],
        last_usage: dict[str, Any] | None,
    ) -> bool:
        """优先用 API 返回 prompt_tokens;否则用 estimate_messages_tokens。"""
        raise NotImplementedError   # Stage 1 实现

    async def compact(self, messages: list[Message]) -> list[Message]:
        """从老到新扫 role="tool" 的 messages,超出保留窗口的替换 content。
        不删 message —— 替换 content 即可,保持 tool_call_id 配对。"""
        raise NotImplementedError   # Stage 1 实现


def safe_split_messages(messages: list[Message], split_at: int) -> int:
    """返回**安全的** split index ≤ split_at,不会把 tool_call 和它的
    tool_result 拆到两边。

    具体规则(参考 ADK apps/compaction.py:388-421):
    - 若 messages[split_at] 是 role="tool",回退到包含 assistant.tool_calls
      的那个 message 之前
    - 若 messages[split_at-1] 是 role="assistant" 且 tool_calls 非空,
      继续往前找,直到所有 tool 对都在右侧

    Stage 1 实现 + 单测覆盖所有边界。"""
    raise NotImplementedError   # Stage 1 实现


def _assert_tool_pairs_intact(messages: list[Message]) -> None:
    """SDK 兜底:验证 tool_call_id 配对完整。

    - 每个 role="tool" 的 message 的 tool_call_id MUST 在前面某个
      role="assistant" message 的 tool_calls[*].id 中出现
    - 每个 assistant.tool_calls[*].id 之后 SHOULD 跟着对应 tool_call_id 的
      tool message(若不跟,LLM 也能容忍,但 SDK warn)

    失败 raise ValueError。"""
    raise NotImplementedError   # Stage 1 实现


__all__ = [
    "ContextCompactor",
    "TruncatingCompactor",
    "safe_split_messages",
    "_assert_tool_pairs_intact",
]
