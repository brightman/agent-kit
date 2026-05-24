"""Runner —— 使用方面向的门面类。

把 provider + skill registry + toolsets 组装好,统一返回 `AsyncIterator[Event]`。

**关于 toolsets 参数**:Runner **不**为你 new MCP toolset —— 这是有意为之。
SDK 不持有 toolset 实例 == SDK 不掌握 lifecycle == 使用方完全控制
"什么时候 new、什么时候 close"(详见 docs/tech-design.md § 7、§ 9)。

典型用法:

    from agent_kit import Runner, RunRequest
    from agent_kit.mcp import McpServerConfig, McpToolset
    from agent_kit.skill import SkillCatalogToolset
    from my_app import LiteLlmProvider, FileSystemSkillRegistry

    skill_registry = FileSystemSkillRegistry(root="./skills")
    runner = Runner(
        provider=LiteLlmProvider("minimax/MiniMax-M2.7"),
        toolsets=[
            SkillCatalogToolset(skill_registry, tenant_id="user_42"),
            McpToolset(McpServerConfig(name="github", transport="stdio",
                                       command=["mcp-github"])),
            McpToolset(McpServerConfig(name="WebSearch", transport="http",
                                       url="https://dashscope.aliyuncs.com/...")),
        ],
    )
    async for evt in runner.run(RunRequest(tenant_id="user_42", ...)):
        print(evt.kind, evt.payload)

便利函数 `agent_kit.mcp.toolsets_from_configs(configs)` 可批量构造
McpToolset 列表(可选,使用方可自己写 list comprehension)。
"""

from __future__ import annotations

from typing import AsyncIterator

from .context import ContextCompactor
from .hooks import Hook
from .loop import RunRequest
from .provider import LlmProvider
from .toolset import BaseToolset
from .types import Event


class Runner:
    """SDK 唯一推荐的入口。"""

    def __init__(
        self,
        provider: LlmProvider,
        toolsets: list[BaseToolset],
        *,
        default_max_rounds: int = 10,
        system_prelude: str = "",
        compactor: ContextCompactor | None = None,
        hooks: list[Hook] | None = None,
    ) -> None:
        self._provider = provider
        self._toolsets = list(toolsets)
        self._default_max_rounds = default_max_rounds
        self._prelude = system_prelude
        self._compactor = compactor
        self._hooks = list(hooks or ())

    async def run(self, request: RunRequest) -> AsyncIterator[Event]:
        """事件流形式。异常全部 catch,wrap 成 Event(kind="error") + return。
        stub —— 实现见 Stage 3。"""
        raise NotImplementedError
        yield  # type: ignore[unreachable]

    async def run_to_completion(self, request: RunRequest):  # -> RunResult
        """聚合形式。遇 error event raise RuntimeError。stub —— 实现见 Stage 3。"""
        raise NotImplementedError


__all__ = ["Runner"]
