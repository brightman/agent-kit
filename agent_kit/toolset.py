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
from typing import TYPE_CHECKING, Any, Callable

from .provider import ToolSchema
from .types import Event, ToolCall, ToolResult

if TYPE_CHECKING:
    # 避免 toolset ↔ loop 循环 import:RunRequest 仅类型注解用
    from .loop import RunRequest

log = logging.getLogger(__name__)


@dataclass
class ToolCallContext:
    """每次工具调用统一拿到的上下文。

    `workspace_ephemeral`:Runner 是否会在 run 结束后 rmtree workspace。
    - True(默认)= Runner 自建 + 自删,toolset **不应**在 workspace 里
      跨 run 缓存(数据下次 run 没了)
    - False = 使用方通过 `Runner.workspace_provider` 注入持久目录,
      Runner 不动它,toolset 可以放心物化 + 缓存(例如 skill files)
    """

    run_id: str
    skill_name: str | None
    cancel: asyncio.Event
    workspace: Path
    storage: Path
    emit: Callable[[Event], None]
    workspace_ephemeral: bool = True
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
        """静态 schema 列表。无 request 上下文时被调(也是
        `build_schemas_for_request` 的默认 fallback)。"""

    def build_schemas_for_request(
        self, request: "RunRequest"
    ) -> list[ToolSchema]:
        """**per-run 动态 schema**(spec § 5.4)。

        默认 = `self.build_schemas()`(静态 toolset 无需 override)。

        想 per-run 过滤 / 动态生成的 toolset(例如 baizhi `SkillToolsetCatalog`
        按 `request.enabled_skills` 暴露 `skill_*` 工具)override 这个方法。
        AgentLoop 每个 `run()` 入口调它,所以同一个 toolset 实例跨多 run
        可以 advertise 不同 schemas。
        """
        return self.build_schemas()

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

    `request` 参数(spec § 5.4):
    - None(默认)= 用 `toolset.build_schemas()` 静态构建。caller 自己
      创建 Router 而不经过 AgentLoop 时走这条
    - RunRequest = 用 `toolset.build_schemas_for_request(request)`,toolset
      可按 request 动态过滤 / 生成 schemas。AgentLoop 每个 run 走这条
    """

    def __init__(
        self,
        toolsets: list[BaseToolset],
        *,
        request: "RunRequest | None" = None,
    ) -> None:
        self._toolsets = list(toolsets)
        self._owner: dict[str, BaseToolset] = {}
        self._schemas_by_owner: dict[BaseToolset, list[ToolSchema]] = {}
        # 1. toolset.name 唯一性
        seen_toolset_names: set[str] = set()
        for ts in self._toolsets:
            if ts.name in seen_toolset_names:
                raise ValueError(f"toolset name collision: {ts.name!r}")
            seen_toolset_names.add(ts.name)
        # 2. ToolSchema.name 跨 toolset 唯一性 —— 用 request-aware 路径(默认 fallback 静态)
        for ts in self._toolsets:
            schemas = (
                ts.build_schemas_for_request(request)
                if request is not None
                else ts.build_schemas()
            )
            self._schemas_by_owner[ts] = list(schemas)
            for schema in schemas:
                if schema.name in self._owner:
                    raise ValueError(
                        f"tool name collision: {schema.name!r} provided by both "
                        f"{self._owner[schema.name].name!r} and {ts.name!r}"
                    )
                self._owner[schema.name] = ts

    def all_schemas(self) -> list[ToolSchema]:
        """Router init 时已经定下的 schema 列表(顺序 = registration order)。"""
        return [s for ts in self._toolsets for s in self._schemas_by_owner[ts]]

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
