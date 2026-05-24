"""MCP 集成 —— wrap Anthropic 官方 `mcp` Python SDK。

不重写 MCP transport / JSON-RPC / initialize 握手 —— 直接用 Anthropic 的
`mcp` 包(参考 baizhi-agent PR f6 = pr-mcp-sdk-adopt 的迁移)。SDK 只在 mcp
之上加一层 baizhi 风格的:

- 命名 `mcp__<server>__<tool>`(与 OH / baizhi-agent / fam-runtime 共识)
- `${VAR}` 模板替换(SDK 没有这个约定)
- 显式 `async connect()` + idempotent `aclose()`(spec § 7.5.1)

**Lifecycle**:一个 McpToolset 实例 == 一个 MCP server == 一个 MCP session。
session 的生命周期等于 McpToolset 实例的生命周期。**使用方控制何时构造、
何时 aclose**(spec § 7.2 4 种用法 per-call / per-run / per-tenant / global)。

**Lazy connect**:`build_schemas` 是同步的(BaseToolset 契约),但 MCP
list_tools 是 async,所以:
- `await toolset.connect()` 必须在 Router init 之前显式调用一次
- Runner 在 setup 阶段自动遍历 toolsets 调它(`agent_kit/runner.py`),
  常路径无样板
- 直接用 AgentLoop 的使用方自己负责
- connect()/aclose() 均 idempotent —— 允许跨 run 复用 + 安全重入
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Literal

from mcp import ClientSession
from mcp import types as mcp_types

from .provider import ToolSchema
from .toolset import BaseToolset, ToolCallContext
from .types import ToolCall, ToolResult


# spec § 7.4:server.name 不能含 "__"(与命名前缀分隔符冲突)+ 只小写字母数字下划线
_SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class McpServerConfig:
    """单个 MCP server 的配置。"""

    name: str                                          # "github" / "WebSearch" / "filesystem"
    transport: Literal["stdio", "sse", "http"]
    command: list[str] | None = None                   # stdio
    url: str | None = None                             # sse / http
    headers: dict[str, str] = field(default_factory=dict)   # 支持 ${VAR}
    env: dict[str, str] = field(default_factory=dict)       # 支持 ${VAR}
    connect_timeout: float = 30.0                      # initialize + list_tools 总超时

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("McpServerConfig.name must be non-empty")
        if "__" in self.name:
            raise ValueError(
                f"McpServerConfig.name {self.name!r} must not contain '__' "
                f"(reserved for mcp__<server>__<tool> separator)"
            )
        if not _SERVER_NAME_RE.match(self.name):
            raise ValueError(
                f"McpServerConfig.name {self.name!r} must match "
                f"{_SERVER_NAME_RE.pattern}"
            )
        if self.transport == "stdio" and not self.command:
            raise ValueError(
                f"McpServerConfig({self.name!r}) transport='stdio' requires command"
            )
        if self.transport in ("sse", "http") and not self.url:
            raise ValueError(
                f"McpServerConfig({self.name!r}) transport={self.transport!r} requires url"
            )


def _substitute(text: str, lookup: dict[str, str]) -> str:
    """spec § 7.3:`${VAR}` 替换;缺失 → KeyError(fail-fast)。"""

    def repl(m: re.Match[str]) -> str:
        var = m.group(1)
        if var not in lookup:
            raise KeyError(f"missing env/secret for ${{{var}}}")
        return lookup[var]

    return _VAR_RE.sub(repl, text)


def _substitute_config(
    cfg: McpServerConfig, secrets: dict[str, str]
) -> McpServerConfig:
    """对 cfg 的 command / url / headers / env 做 `${VAR}` 替换。

    替换源:secrets > os.environ(secrets 覆盖 env)。spec § 7.3 优先级。
    """
    lookup = {**os.environ, **secrets}
    return replace(
        cfg,
        command=[_substitute(s, lookup) for s in cfg.command] if cfg.command else None,
        url=_substitute(cfg.url, lookup) if cfg.url else None,
        headers={k: _substitute(v, lookup) for k, v in cfg.headers.items()},
        env={k: _substitute(v, lookup) for k, v in cfg.env.items()},
    )


def _mcp_tool_to_schema(server_prefix: str, tool: mcp_types.Tool) -> ToolSchema:
    """remote tool → ToolSchema,名字加 `mcp__<server>__` 前缀。"""
    return ToolSchema(
        name=f"{server_prefix}__{tool.name}",
        description=tool.description or "",
        parameters=tool.inputSchema or {"type": "object", "properties": {}},
    )


def _serialize_call_result(result: mcp_types.CallToolResult) -> str:
    """MCP CallToolResult → str。

    优先 structuredContent(JSON);否则把 content blocks 拼接(只处理文本 +
    其他类型用 to-dict 兜底)。
    """
    if result.structuredContent is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False)
    parts: list[str] = []
    for block in result.content or []:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        else:
            # ImageContent / EmbeddedResource / etc.:dump JSON tag
            parts.append(json.dumps(block.model_dump(mode="json"), ensure_ascii=False))
    return "\n".join(parts)


class McpToolset(BaseToolset):
    """一个 McpToolset 实例 == 一个 MCP server == 一个 MCP session。

    工具命名:`mcp__<server.name>__<remote_tool_name>`。

    使用流程:

        ts = McpToolset(McpServerConfig(name="github", transport="stdio",
                                       command=["mcp-github"]))
        await ts.connect()              # 必须;Runner 会自动调
        schemas = ts.build_schemas()
        result = await ts.execute(call, ctx)
        await ts.aclose()               # idempotent;Runner 会自动调
    """

    def __init__(
        self,
        config: McpServerConfig,
        *,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self._config = _substitute_config(config, secrets or {})
        self.name = f"mcp__{self._config.name}"
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._schemas: list[ToolSchema] = []
        self._connected = False
        self._connect_lock = asyncio.Lock()

    # ---- BaseToolset overrides ----

    def build_schemas(self) -> list[ToolSchema]:
        if not self._connected:
            raise RuntimeError(
                f"{self.name}: not connected — call `await toolset.connect()` first "
                f"(Runner pre-warms automatically; see tech-design § 7.5.1)"
            )
        return list(self._schemas)

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        if not self._connected or self._session is None:
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: {self.name} not connected",
                is_error=True,
            )
        prefix = f"{self.name}__"
        if not call.name.startswith(prefix):
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: tool {call.name!r} not owned by {self.name}",
                is_error=True,
            )
        remote_name = call.name[len(prefix):]
        try:
            result = await self._session.call_tool(
                remote_name, call.arguments or {}
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: {type(exc).__name__}: {exc}",
                is_error=True,
            )
        content = _serialize_call_result(result)
        return ToolResult(
            call_id=call.id,
            content=content,
            is_error=bool(result.isError),
        )

    # ---- lifecycle ----

    async def connect(self) -> None:
        """启动 transport + ClientSession,initialize + list_tools,缓存 schemas。

        Idempotent:已 connected → 直接返回。多次并发调用串行化。
        """
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            stack = AsyncExitStack()
            try:
                async with asyncio.timeout(self._config.connect_timeout):
                    read, write = await self._open_streams(stack)
                    session = await stack.enter_async_context(
                        ClientSession(read, write)
                    )
                    await session.initialize()
                    list_result = await session.list_tools()
                self._schemas = [
                    _mcp_tool_to_schema(self.name, t) for t in list_result.tools
                ]
                self._session = session
                self._stack = stack
                self._connected = True
            except BaseException:
                await stack.aclose()
                raise

    async def aclose(self) -> None:
        """关闭 session + transport。Idempotent。"""
        if not self._connected:
            return
        stack = self._stack
        self._stack = None
        self._session = None
        self._connected = False
        self._schemas = []
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001
                # transport already torn down / process exited;不重要,记日志即可
                pass

    # ---- test seam ----

    async def _open_streams(
        self, stack: AsyncExitStack
    ) -> tuple[Any, Any]:
        """开 transport,返回 (read_stream, write_stream)。子类可 override
        做 in-memory 测试(见 tests/test_mcp.py 的 _InMemMcpToolset)。"""
        cfg = self._config
        if cfg.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            assert cfg.command is not None  # __post_init__ 已校验
            params = StdioServerParameters(
                command=cfg.command[0],
                args=list(cfg.command[1:]),
                env=cfg.env or None,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            return read, write

        if cfg.transport == "sse":
            from mcp.client.sse import sse_client

            assert cfg.url is not None
            read, write = await stack.enter_async_context(
                sse_client(cfg.url, headers=dict(cfg.headers) or None)
            )
            return read, write

        if cfg.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client

            assert cfg.url is not None
            opened = await stack.enter_async_context(
                streamablehttp_client(cfg.url, headers=dict(cfg.headers) or None)
            )
            # streamablehttp_client yields (read, write, session_id_callback)
            return opened[0], opened[1]

        raise ValueError(f"unknown transport {cfg.transport!r}")


def toolsets_from_configs(
    configs: list[McpServerConfig],
    *,
    secrets: dict[str, str] | None = None,
) -> list[McpToolset]:
    """批量构造 McpToolset。spec § 7.6 —— 等价于 list comprehension,语义化糖。"""
    return [McpToolset(c, secrets=secrets) for c in configs]


__all__ = [
    "McpServerConfig",
    "McpToolset",
    "toolsets_from_configs",
]
