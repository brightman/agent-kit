"""Runner —— 使用方面向的门面类。

把 provider + skill registry + MCP servers + 其他 toolset 组装好,统一返回
`AsyncIterator[Event]`。

典型用法:

    runner = Runner(
        provider=LiteLlmProvider("minimax/MiniMax-M2.7"),
        skill_registry=FileSystemSkillRegistry(root="./skills"),
        mcp_servers=[
            McpServerConfig(name="github", transport="stdio", command=["mcp-github"]),
        ],
    )
    async for evt in runner.run(RunRequest(tenant_id="user_42", ...)):
        print(evt.kind, evt.payload)
"""

from __future__ import annotations

from typing import AsyncIterator

from .loop import AgentLoop, RunRequest
from .mcp import McpServerConfig
from .provider import LlmProvider
from .skill import SkillRegistry
from .toolset import BaseToolset
from .types import Event


class Runner:
    """SDK 唯一推荐的入口。"""

    def __init__(
        self,
        provider: LlmProvider,
        skill_registry: SkillRegistry,
        mcp_servers: list[McpServerConfig] | None = None,
        extra_toolsets: list[BaseToolset] | None = None,
        *,
        default_max_rounds: int = 10,
        system_prelude: str = "",
    ) -> None:
        self._provider = provider
        self._skill_registry = skill_registry
        self._mcp_servers = list(mcp_servers or ())
        self._extra_toolsets = list(extra_toolsets or ())
        self._default_max_rounds = default_max_rounds
        self._prelude = system_prelude

    async def run(self, request: RunRequest) -> AsyncIterator[Event]:
        """组装 toolsets + 起 loop。stub —— 实现见首次接进 baizhi-agent 的 PR。"""
        raise NotImplementedError
        yield  # type: ignore[unreachable]


__all__ = ["Runner"]
