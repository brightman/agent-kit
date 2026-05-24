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
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from .tokens import estimate_messages_tokens
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
        ...

    async def compact(self, messages: list[Message]) -> list[Message]:
        """返回新 messages list。
        实现者 MUST 保证:
        1. 若 messages[0].role == "system",保留(MUST 在返回值的位置 0)
        2. 保留最近 N 条 verbatim(N 由实现自定)
        3. 任何 tool_call 与其对应的 tool_result MUST 同存或同删
        Loop 会在返回后跑 _assert_tool_pairs_intact 兜底;失败 raise。"""
        ...


@dataclass
class TruncatingCompactor:
    """microcompact 模式 —— 替换老 tool_result 内容为占位符,零 LLM 成本。

    适合 80% 场景:tool 输出(尤其是 file read / web fetch / mcp 大 payload)
    是 token bloat 的主因,且老 tool_result 极少被 LLM 反复回看。

    实现:扫所有 role="tool" 的 messages,距末尾超过 keep_recent_tool_results
    的,把 content 替换为 placeholder。**不删 message**,只替 content ——
    天然保持 tool_call_id 配对完整。
    """

    name: str = "truncate"
    token_budget: int = 100_000
    keep_recent_tool_results: int = 5
    placeholder: str = "[tool output omitted — older than retention window]"

    async def should_compact(
        self,
        messages: list[Message],
        last_usage: dict[str, Any] | None,
    ) -> bool:
        # 优先用 API 数(精确);否则用 estimate(保守)
        if last_usage and isinstance(last_usage.get("prompt_tokens"), int):
            return last_usage["prompt_tokens"] >= self.token_budget
        return estimate_messages_tokens(messages) >= self.token_budget

    async def compact(self, messages: list[Message]) -> list[Message]:
        # 找出所有 role="tool" 的 index,从老到新
        tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
        if len(tool_indices) <= self.keep_recent_tool_results:
            return list(messages)   # 无需压缩

        # 保留最近 K 个 tool_result 的 index;其余替换 content
        cutoff = len(tool_indices) - self.keep_recent_tool_results
        indices_to_truncate = set(tool_indices[:cutoff])

        out: list[Message] = []
        for i, m in enumerate(messages):
            if i in indices_to_truncate:
                # 已经替换过(content == placeholder)的不重复替换
                if m.content == self.placeholder:
                    out.append(m)
                else:
                    out.append(replace(m, content=self.placeholder))
            else:
                out.append(m)
        return out


def safe_split_messages(messages: list[Message], split_at: int) -> int:
    """返回**安全的** split index ≤ split_at,保证 tool_call 与 tool_result
    一定同存或同删(参考 ADK apps/compaction.py:388-421)。

    回退规则:
    - 若 split_at 落在 role="tool" message → 回退到那一对的 assistant 之前
    - 若 split_at-1 是 role="assistant" 且 tool_calls 非空 → 同样回退
    - 若回退到 0 仍不能满足 → 返回 0(切不出安全点)

    边界:
    - split_at <= 0 → 0
    - split_at >= len(messages) → len(messages)
    """
    n = len(messages)
    if split_at <= 0:
        return 0
    if split_at >= n:
        return n

    idx = split_at

    # 如果 split 点是 tool message,先回退到第一个 tool message 之前
    while idx > 0 and messages[idx].role == "tool":
        idx -= 1

    # idx 现在指向 tool messages 之前;若 messages[idx-1] 是带 tool_calls 的
    # assistant,意味着 tool_call 在左边、tool_result 在右边 → 还得再回退
    while idx > 0:
        prev = messages[idx - 1]
        if prev.role == "assistant" and prev.tool_calls:
            # 这个 assistant 的 tool_calls 至少有一部分还会被分到右边 → 把它也拉到左边以外
            idx -= 1
            # 然后再跳过它前面紧贴的 tool messages(虽然不太可能,但 defensive)
            while idx > 0 and messages[idx].role == "tool":
                idx -= 1
        else:
            break

    return idx


def _assert_tool_pairs_intact(messages: list[Message]) -> None:
    """SDK 兜底:验证 tool_call_id 配对完整。

    规则:
    - 每个 role="tool" 的 message,其 tool_call_id MUST 在前面某个
      role="assistant" message 的 tool_calls[*].id 集合中出现
    - assistant.tool_calls[*].id 不需要 100% 都跟 tool message(LLM 容忍)
      但 tool message 一定要有 prior assistant tool_call

    失败 raise ValueError。"""
    declared_ids: set[str] = set()
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                declared_ids.add(tc.id)
        elif m.role == "tool":
            if m.tool_call_id is None:
                raise ValueError(
                    "tool message missing tool_call_id (broke type invariant)"
                )
            if m.tool_call_id not in declared_ids:
                raise ValueError(
                    f"orphan tool message: tool_call_id={m.tool_call_id!r} has "
                    "no matching assistant tool_calls before it"
                )


__all__ = [
    "ContextCompactor",
    "TruncatingCompactor",
    "safe_split_messages",
    "_assert_tool_pairs_intact",
]
