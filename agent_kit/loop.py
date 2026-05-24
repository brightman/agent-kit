"""AgentLoop —— SDK 的核心:bounded 多轮 LLM ↔ tool 循环。

设计要点(综合四家):
- pull 模型:`AsyncIterator[Event]`(ADK / OH 风格);callback 让使用方在外面包
- 轮数 cap 是硬约束(baizhi-agent / fam-runtime)
- 最后一轮屏蔽 tools 强制收尾(baizhi-agent 发明,值得保留)
- 终止条件简单:`response.tool_calls is None` 就退出。不引入 ADK 的 is_final_response 业务判断
- 取消用 asyncio.Event,在 round 边界 check
- Context compaction 在每次 provider.chat 前调用(若 compactor 注入),
  loop 兜底 _assert_tool_pairs_intact;详见 docs/tech-design.md § 8.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .context import ContextCompactor
from .provider import LlmProvider
from .toolset import BaseToolset, ToolCallContext, ToolsetRouter
from .types import Event


@dataclass
class RunRequest:
    """一次 run 的输入。"""

    tenant_id: str
    agent_id: str
    user_message: str
    enabled_skills: list[str] = field(default_factory=list)
    max_rounds: int = 10
    temperature: float = 0.7
    system_prelude: str = ""
    stream: bool = False                       # Q1 决议:opt-in stream
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentLoop:
    """无状态可重入的 loop。每次 run() 自带独立 cancel / messages。"""

    def __init__(
        self,
        provider: LlmProvider,
        toolsets: list[BaseToolset],
        *,
        default_max_rounds: int = 10,
        system_prelude: str = "",
        compactor: ContextCompactor | None = None,    # None == 不 compact
    ) -> None:
        self._provider = provider
        self._router = ToolsetRouter(toolsets)
        self._default_max_rounds = default_max_rounds
        self._prelude = system_prelude
        self._compactor = compactor

    async def run(
        self,
        request: RunRequest,
        ctx: ToolCallContext,
    ) -> AsyncIterator[Event]:
        """执行多轮 loop,yield 事件。"""
        # stub —— 真实现见 Stage 2
        raise NotImplementedError
        # 让类型检查认 yield
        yield  # type: ignore[unreachable]


__all__ = ["RunRequest", "AgentLoop"]
