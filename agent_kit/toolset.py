"""BaseToolset + ToolCallContext —— 工具集统一接口。

任何"一组可被 LLM 调的工具"都实现 BaseToolset:
- 内置 SkillCatalogToolset
- 每个 MCP server 一份 McpToolset
- 用户自定义的 Python 函数集

ToolCallContext 是 SDK 和 toolset 的契约 —— 所有 execute() 都拿到一样的上下文。

设计来源:
- ADK BaseTool / Toolset
- baizhi-agent toolsets.py(BaseToolset、SkillStorageToolset、SkillCatalogToolset)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .provider import ToolSchema
from .types import Event, ToolCall, ToolResult

log = logging.getLogger(__name__)


@dataclass
class ToolCallContext:
    """每次工具调用统一拿到的上下文。"""

    tenant_id: str
    run_id: str
    skill_name: str | None
    cancel: asyncio.Event
    workspace: Path
    storage: Path
    emit: Callable[[Event], None]
    run_state: dict[str, Any] = field(default_factory=dict)


class BaseToolset(ABC):
    """SDK 内所有工具集的基类。

    Invariants:
    - `name` 在 Router 持有的所有 toolsets 内必须唯一(Router init 校验)
    - `execute` SHOULD 内部 catch 异常并返回 `ToolResult(is_error=True)`,
      若抛出 Router 会兜底 catch 并转 ToolResult(防御)。
    """

    name: str

    @abstractmethod
    def build_schemas(self) -> list[ToolSchema]:
        """暴露给 LLM 的工具 schema 列表。"""

    @abstractmethod
    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        """执行一次工具调用。"""

    async def aclose(self) -> None:
        """释放资源(MCP stdio 关进程、临时文件清理 等)。默认 no-op。"""
        return None


class ToolsetRouter:
    """合并多个 toolset,按 ToolCall.name 路由到拥有该 schema 的 toolset。

    启动期检测:
    1. toolset.name 唯一(否则 raise ValueError)
    2. ToolSchema.name 跨 toolset 无冲突(否则 raise ValueError)
    """

    def __init__(self, toolsets: list[BaseToolset]) -> None:
        self._toolsets = list(toolsets)
        self._owner: dict[str, BaseToolset] = {}
        # 1. toolset.name 唯一性
        seen_toolset_names: set[str] = set()
        for ts in self._toolsets:
            if ts.name in seen_toolset_names:
                raise ValueError(f"toolset name collision: {ts.name!r}")
            seen_toolset_names.add(ts.name)
        # 2. ToolSchema.name 跨 toolset 唯一性
        for ts in self._toolsets:
            for schema in ts.build_schemas():
                if schema.name in self._owner:
                    raise ValueError(
                        f"tool name collision: {schema.name!r} provided by both "
                        f"{self._owner[schema.name].name!r} and {ts.name!r}"
                    )
                self._owner[schema.name] = ts

    def all_schemas(self) -> list[ToolSchema]:
        return [s for ts in self._toolsets for s in ts.build_schemas()]

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        ts = self._owner.get(call.name)
        if ts is None:
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: unknown tool {call.name!r}",
                is_error=True,
            )
        try:
            return await ts.execute(call, ctx)
        except Exception as exc:
            # 兜底:toolset.execute 不该抛,但若抛了 wrap 成 error ToolResult
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: {type(exc).__name__}: {exc}",
                is_error=True,
            )

    async def aclose(self) -> None:
        """按注册的 reverse 顺序 close;每个 toolset 的异常 swallow + log。"""
        for ts in reversed(self._toolsets):
            try:
                await ts.aclose()
            except Exception:  # noqa: BLE001
                log.warning("toolset aclose failed for %r", ts.name, exc_info=True)


__all__ = ["ToolCallContext", "BaseToolset", "ToolsetRouter"]
