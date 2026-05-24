"""Token 估算 —— 零依赖,跟 OpenHarness `services/token_estimation.py` 同公式。

公式:`(len(text) + 3) // 4`,再乘 4/3 padding(`TOKEN_ESTIMATION_PADDING`)
作保守估计。这是 OH 验证过的实用近似;Stage 1 不引 tiktoken。

API-returned `usage.prompt_tokens`(若 provider 给)永远优先于本估算 ——
loop 在 `compactor.should_compact` 调用时把 `last_usage` 也传过去,Compactor
实现 SHOULD 优先用 API 数。

参考:
- OpenHarness services/token_estimation.py: chars/4
- OpenHarness services/compact/__init__.py:75 TOKEN_ESTIMATION_PADDING = 4/3
- ADK apps/compaction.py:125-153 同样用 chars/4 fallback
"""

from __future__ import annotations

from .types import Message

TOKEN_ESTIMATION_PADDING = 4 / 3   # 与 OpenHarness 一致;保守估计


def estimate_text_tokens(text: str) -> int:
    """单段文本估算。零依赖。"""
    raise NotImplementedError   # Stage 1 实现


def estimate_messages_tokens(messages: list[Message]) -> int:
    """整段对话估算。包含 role / content / tool_calls / tool_call_id 的字符开销。"""
    raise NotImplementedError   # Stage 1 实现


__all__ = ["TOKEN_ESTIMATION_PADDING", "estimate_text_tokens", "estimate_messages_tokens"]
