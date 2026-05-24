"""MCP 集成 —— wrap Anthropic 官方 `mcp` Python SDK。

不重写 MCP transport / JSON-RPC / initialize 握手 —— 直接用 Anthropic 的
`mcp` 包(参考 baizhi-agent PR f6 = pr-mcp-sdk-adopt 的迁移)。SDK 只在 mcp
之上加一层 baizhi 风格的:
- 命名 `mcp__<server>__<tool>`(与 OH / baizhi-agent / fam-runtime 共识)
- secret 注入 `${VAR}` 模板替换(SDK 没有这个约定)

**Lifecycle**:一个 McpToolset 实例 == 一个 MCP session。session 的生命周期
等于 McpToolset 实例的生命周期。**使用方控制何时构造、何时 aclose** ——
- per-call 强隔离:每次 execute 前构造,完后 aclose
- per-run 默认:构造一次,run 结束 aclose
- per-tenant / global:在使用方维护实例池,SDK 不感知

(这是 ADK 的模式;不在 SDK 里枚举 lifecycle。详见 docs/tech-design.md § 7。)

参考实现:
- ADK McpToolset:lifecycle = instance lifetime,使用方控制
- OpenHarness McpClientManager:进程单例(使用方层 hardcode)
- baizhi-agent mcp_session.py:per-call(使用方层 hardcode)
- fam-runtime fam_runtime/mcp/manager.py:per-family 缓存(使用方层 hardcode)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .provider import ToolSchema
from .toolset import BaseToolset, ToolCallContext
from .types import ToolCall, ToolResult


@dataclass
class McpServerConfig:
    """单个 MCP server 的配置。"""

    name: str                                          # "github" / "filesystem" / "WebSearch"
    transport: Literal["stdio", "sse", "http"]
    command: list[str] | None = None                   # stdio
    url: str | None = None                             # sse / http
    headers: dict[str, str] = field(default_factory=dict)   # 支持 ${VAR} 模板
    env: dict[str, str] = field(default_factory=dict)       # 同上


class McpToolset(BaseToolset):
    """一个 McpToolset 实例 == 一个 MCP server == 一个 MCP session。

    工具命名:`mcp__<server.name>__<remote_tool_name>`。

    Session lazy-connect:首次 build_schemas / execute 触发 connect。
    使用方负责 aclose(Runner 会对它持有的 toolsets 自动 aclose;若使用方
    在 Runner 之外持有 McpToolset,需要自己负责 aclose)。
    """

    def __init__(
        self,
        config: McpServerConfig,
        *,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self._config = config
        self._secrets = secrets or {}
        self.name = f"mcp__{config.name}"

    def build_schemas(self) -> list[ToolSchema]:
        raise NotImplementedError

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


__all__ = ["McpServerConfig", "McpToolset"]
