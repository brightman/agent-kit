"""MCP 集成 —— wrap Anthropic 官方 `mcp` Python SDK。

不重写 MCP transport / JSON-RPC / initialize 握手 —— 直接用 Anthropic 的
`mcp` 包(参考 baizhi-agent PR f6 = pr-mcp-sdk-adopt 的迁移)。SDK 只在 mcp
之上加一层 baizhi 风格的:
- 命名 `mcp__<server>__<tool>`(与 OH / baizhi-agent / fam-runtime 共识)
- secret 注入 `${VAR}` 模板替换(SDK 没有这个约定)
- lifecycle 配置(per_call / per_run / per_tenant / global)

参考实现:
- ADK McpToolset
- OpenHarness McpClientManager(全局)
- baizhi-agent mcp_session.py + toolsets.py:McpHttpToolset
- fam-runtime fam_runtime/mcp/manager.py(per-family 缓存)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from .provider import ToolSchema
from .toolset import BaseToolset, ToolCallContext
from .types import ToolCall, ToolResult


class McpLifecycle(Enum):
    """MCP server 进程/连接的生命周期范围。"""

    PER_CALL = "per_call"        # baizhi-agent 当前默认:每次调用拉起 + 关闭
    PER_RUN = "per_run"          # 单次 agent run 内复用
    PER_TENANT = "per_tenant"    # Fam 风格:按 family_id / tenant_id 缓存
    GLOBAL = "global"            # OH 风格:进程全局


@dataclass
class McpServerConfig:
    """单个 MCP server 的配置。"""

    name: str                                          # "github" / "filesystem" / "WebSearch"
    transport: Literal["stdio", "sse", "http"]
    command: list[str] | None = None                   # stdio
    url: str | None = None                             # sse / http
    headers: dict[str, str] = field(default_factory=dict)   # 支持 ${VAR} 模板
    env: dict[str, str] = field(default_factory=dict)       # 同上
    lifecycle: McpLifecycle = McpLifecycle.PER_CALL


class McpToolset(BaseToolset):
    """一个 MCP server == 一个 toolset。

    工具命名:`mcp__<server.name>__<remote_tool_name>`。
    """

    def __init__(self, config: McpServerConfig) -> None:
        self._config = config
        self.name = f"mcp__{config.name}"

    def build_schemas(self) -> list[ToolSchema]:
        raise NotImplementedError

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


__all__ = ["McpLifecycle", "McpServerConfig", "McpToolset"]
