"""AgentLoop —— SDK 的核心:bounded 多轮 LLM ↔ tool 循环。

设计要点(综合四家):
- pull 模型:`AsyncIterator[Event]`(ADK / OH 风格);callback 让使用方在外面包
- 轮数 cap 是硬约束(baizhi-agent / fam-runtime)
- 最后一轮屏蔽 tools 强制收尾(baizhi-agent 发明,值得保留)
- 终止条件简单:`response.tool_calls is None` 就退出。不引入 ADK 的 is_final_response 业务判断
- 取消用 asyncio.Event,在 round 边界 check
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from .provider import LlmProvider
from .toolset import BaseToolset, ToolCallContext, ToolsetRouter
from .types import Event, Message


@dataclass
class RunRequest:
    """一次 run 的输入。"""

    tenant_id: str
    agent_id: str
    user_message: str
    enabled_skills: list[str]
    max_rounds: int = 10
    temperature: float = 0.7
    system_prelude: str = ""


class AgentLoop:
    """无状态可重入的 loop。每次 run() 自带独立 cancel / messages。"""

    def __init__(
        self,
        provider: LlmProvider,
        toolsets: list[BaseToolset],
        *,
        default_max_rounds: int = 10,
        system_prelude: str = "",
    ) -> None:
        self._provider = provider
        self._router = ToolsetRouter(toolsets)
        self._default_max_rounds = default_max_rounds
        self._prelude = system_prelude

    async def run(
        self,
        request: RunRequest,
        ctx: ToolCallContext,
    ) -> AsyncIterator[Event]:
        """执行多轮 loop,yield 事件。"""
        # stub —— 真实现见首次接进 baizhi-agent 的 PR
        raise NotImplementedError
        # 让类型检查认 yield
        yield  # type: ignore[unreachable]


__all__ = ["RunRequest", "AgentLoop"]
