"""Runner —— 使用方面向的门面类。

把 provider + toolsets 组装成可执行 run,统一返回 `AsyncIterator[Event]`
或聚合的 `RunResult`(Q4 双轨)。

设计要点(对齐 docs/tech-design.md § 9 / § 10):

- **不为你 new toolset**:使用方传入 `toolsets: list[BaseToolset]`,
  Runner 不掌控其 lifecycle。Runner 通过 `loop.aclose()` 在 finally 阶段
  触发 toolset 资源释放(详见 § 9.3)
- **skill catalog 来自 discovery**:Runner 不收 `skill_registry` 参数;
  而是在 toolsets 列表里 isinstance 找 `SkillCatalogToolset`,从中读
  registry 去构造 prelude 段(§ 10.1)
- **workspace 生命周期**:run_id 启动时分配,workspace = workspace_root / run_id;
  mkdir 在 run 开头,finally 删
- **错误传播 Q4 双轨**:`run` yield error event 后 return;
  `run_to_completion` 遇 error event raise RuntimeError

典型用法:

    from agent_kit import Runner, RunRequest
    from agent_kit.skill import SkillCatalogToolset

    runner = Runner(
        provider=LiteLlmProvider("minimax/MiniMax-M2.7"),
        toolsets=[
            SkillCatalogToolset(skill_registry),
            *toolsets_from_configs([...]),
        ],
    )
    # 流式:
    async for evt in runner.run(RunRequest(agent_id="researcher", user_message="...")):
        print(evt.kind, evt.payload)
    # 聚合:
    result = await runner.run_to_completion(RunRequest(...))
    print(result.final_text)
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from ._errors import unwrap_to_leaf
from .context import ContextCompactor
from .hooks import Hook
from .loop import AgentLoop, RunRequest
from .provider import LlmProvider
from .skill import SkillCatalogToolset, SkillFrontmatter, parse_skill_ref
from .toolset import BaseToolset, ToolCallContext
from .types import Event


@dataclass
class RunResult:
    """`run_to_completion` 的聚合返回(Q4 决议,spec § 3.8)。

    `cancelled` / `error` 互斥与 `final_text` 的关系:
    - 成功路径:`final_text` 非 None,`cancelled=False`,`error=None`
    - 取消:`cancelled=True`,`final_text` 可能为 None
    - error 路径:`run_to_completion` 抛 RuntimeError,不返回 RunResult
      (此 dataclass 的 `error` 字段保留给 future 不抛的变体)
    """

    final_text: str | None
    events: list[Event]
    rounds_used: int
    cancelled: bool
    error: dict[str, Any] | None


def _new_run_id() -> str:
    """ns 时间戳 + 8 字符 uuid 后缀;与 loop._new_event_id 同形态。"""
    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"


def _now_event(kind: str, payload: dict[str, Any]) -> Event:
    return Event(
        event_id=_new_run_id(),
        parent_event_id=None,
        kind=kind,  # type: ignore[arg-type]
        payload=payload,
        ts=time.time(),
    )


def _wrap_error_event(stage: str, exc: BaseException) -> Event:
    leaf = unwrap_to_leaf(exc)
    return _now_event(
        "error",
        {
            "stage": stage,
            "exc_type": leaf.__class__.__name__,
            "message": str(leaf),
            # 注意:traceback 用原 exc(可能是 ExceptionGroup),保留完整链,
            # 不丢上下文。`exc_type` / `message` 用 leaf,人读时根因清楚
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        },
    )


def _find_skill_catalog(
    toolsets: list[BaseToolset],
) -> SkillCatalogToolset | None:
    """spec § 10.1:多个时取第一个;没有时返回 None。"""
    for ts in toolsets:
        if isinstance(ts, SkillCatalogToolset):
            return ts
    return None


async def _build_skill_section(
    catalog: SkillCatalogToolset, enabled_refs: list[str]
) -> str:
    """spec § 10:渲染 "# Available Skills" 段。enabled_refs 为空返回 ""。"""
    if not enabled_refs:
        return ""
    wanted_names = {parse_skill_ref(ref)[0] for ref in enabled_refs}
    all_fms: list[SkillFrontmatter] = await catalog._registry.list()
    matched = [fm for fm in all_fms if fm.name in wanted_names]
    if not matched:
        return ""
    lines = [
        "# Available Skills",
        "",
        "You have access to the following skills. Use the `load_skill` tool "
        "to read a skill's full instructions before invoking it.",
        "",
    ]
    for fm in matched:
        lines.append(f"- {fm.name} (v{fm.version}): {fm.description}")
    return "\n".join(lines)


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
        workspace_root: Path = Path("/tmp/agent-kit-runs"),
        storage_root: Path = Path("./persistent"),
        workspace_provider: Callable[["RunRequest", str], Path] | None = None,
    ) -> None:
        """Args 见 spec § 9.1。

        `workspace_provider`:None(默认)= SDK 自建 `workspace_root / <run_id>`
        + finally rmtree(ephemeral)。传 callable = 使用方完全掌控 workspace
        路径与生命周期(provider 负责 mkdir,SDK 不删);ctx.workspace_ephemeral
        随之为 False,toolset 可在 workspace 跨 run 缓存(如 skill files 物化)。

        典型用法:把 baizhi-agent 的 tenant_agent 持久空间映射进 SDK
            def baizhi_workspace(req, run_id):
                # tenant 在 baizhi application 层通过 closure 或自家 metadata 取
                # SDK 自己不带 tenant 概念(spec § 1)
                p = Path(f"/data/baizhi/{this_tenant_id}/agents/{req.agent_id}")
                p.mkdir(parents=True, exist_ok=True)
                return p

            Runner(..., workspace_provider=baizhi_workspace)
        """
        self._provider = provider
        self._toolsets = list(toolsets)
        self._default_max_rounds = default_max_rounds
        self._prelude = system_prelude
        self._compactor = compactor
        self._hooks = list(hooks or ())
        self._workspace_root = Path(workspace_root)
        self._storage_root = Path(storage_root)
        self._workspace_provider = workspace_provider

    # ---- public ----

    async def run(self, request: RunRequest) -> AsyncIterator[Event]:
        """事件流形式。异常全部 catch,wrap 成 Event(kind="error") + return。

        SHOULD 用于服务端 / 持久化场景。
        """
        run_id = _new_run_id()
        loop: AgentLoop | None = None
        # workspace 与 ephemeral 由 provider 决定;ephemeral=True → SDK 建 + 删
        workspace: Path | None = None
        ephemeral = self._workspace_provider is None

        try:
            # --- setup ---
            try:
                if self._workspace_provider is None:
                    workspace = self._workspace_root / run_id
                    workspace.mkdir(parents=True, exist_ok=True)
                else:
                    workspace = self._workspace_provider(request, run_id)
                    # provider 全权负责 mkdir;SDK 不动它
                # spec § 7.5.1:对任何带 async `connect()` 的 toolset 做 pre-warm,
                # 让 ToolsetRouter 后续 sync build_schemas 拿到缓存
                await self._prewarm_toolsets()
                composed_prelude = await self._compose_prelude(request)
                loop = AgentLoop(
                    self._provider,
                    self._toolsets,
                    default_max_rounds=self._default_max_rounds,
                    system_prelude=composed_prelude,
                    compactor=self._compactor,
                    hooks=self._hooks,
                )
                ctx = ToolCallContext(
                    run_id=run_id,
                    skill_name=None,
                    cancel=asyncio.Event(),
                    workspace=workspace,
                    storage=self._storage_root,
                    emit=lambda evt: None,  # Stage 3: no-op(§ 10.2)
                    workspace_ephemeral=ephemeral,
                )
            except Exception as exc:  # noqa: BLE001
                yield _wrap_error_event("setup", exc)
                return

            # --- loop ---
            try:
                async for evt in loop.run(request, ctx):
                    yield evt
            except Exception as exc:  # noqa: BLE001
                # loop.run 应当 yield error event 而不是 raise —— 这是防御兜底
                yield _wrap_error_event("loop", exc)
                return
        finally:
            if loop is not None:
                try:
                    await loop.aclose()
                except Exception:  # noqa: BLE001
                    pass
            # 只清理 SDK 自建的 ephemeral workspace;provider 注入的归使用方
            if ephemeral and workspace is not None and workspace.exists():
                shutil.rmtree(workspace, ignore_errors=True)

    async def run_to_completion(self, request: RunRequest) -> RunResult:
        """聚合形式。遇 error event raise RuntimeError(包含原 exc_type / message)。

        SHOULD 用于脚本 / 测试 / 一次性 CLI 场景。
        """
        events: list[Event] = []
        final_text: str | None = None
        cancelled = False
        error: dict[str, Any] | None = None
        rounds_used = 0

        async for evt in self.run(request):
            events.append(evt)
            if evt.kind == "round_end":
                rounds_used = max(rounds_used, int(evt.payload.get("round", -1)) + 1)
            elif evt.kind == "final_text":
                final_text = evt.payload.get("text")
            elif evt.kind == "cancelled":
                cancelled = True
            elif evt.kind == "error":
                error = dict(evt.payload)

        if error is not None:
            raise RuntimeError(
                f"[{error.get('stage', '?')}] "
                f"{error.get('exc_type', 'Error')}: {error.get('message', '')}"
            )

        return RunResult(
            final_text=final_text,
            events=events,
            rounds_used=rounds_used,
            cancelled=cancelled,
            error=error,
        )

    def run_sync(self, request: RunRequest) -> RunResult:
        """同步 wrapper —— 内部就是 `asyncio.run(self.run_to_completion(request))`。

        spec § 9.2 (Stage 5 修订):给纯 sync 调用方(CLI / 命令行脚本 /
        Jupyter sync cell / sync codebase 渐进迁移如 baizhi-agent)用。

        **不能在已经跑着的 event loop 里调**(FastAPI handler / async test /
        Jupyter async cell)—— fail-fast 报友好错误,而不是让 Python 抛
        `RuntimeError: asyncio.run() cannot be called from a running event loop`。
        那种场景请直接 `await runner.run_to_completion(request)`。

        形态参考 openai-agents `Runner.run_sync`;**不返 Generator**
        (若需要 sync 实时事件流,见 spec § 14 Stage 7+ 候选)。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 没有跑着的 loop —— 可以用 asyncio.run
            return asyncio.run(self.run_to_completion(request))
        raise RuntimeError(
            "Runner.run_sync() cannot be called from a running event loop "
            "(FastAPI handler, async test, Jupyter async cell). "
            "Use `await runner.run_to_completion(request)` instead."
        )

    # ---- helpers ----

    async def _prewarm_toolsets(self) -> None:
        """spec § 7.5.1:对任何带 async `connect()` 的 toolset 调用一次。

        McpToolset 是首要场景(connect() 必须在 Router 的 build_schemas 之前
        完成,否则 RuntimeError);其他 toolset 自定义 connect 也会被识别。
        connect() 应当是 idempotent —— 已连接者不重连。
        """
        for ts in self._toolsets:
            connect = getattr(ts, "connect", None)
            if connect is None or not inspect.iscoroutinefunction(connect):
                continue
            await connect()

    async def _compose_prelude(self, request: RunRequest) -> str:
        """spec § 10:Runner.prelude + skill catalog + RunRequest.prelude。"""
        parts: list[str] = []
        if self._prelude:
            parts.append(self._prelude)

        catalog = _find_skill_catalog(self._toolsets)
        if catalog is not None and request.enabled_skills:
            section = await _build_skill_section(catalog, request.enabled_skills)
            if section:
                parts.append(section)

        if request.system_prelude:
            parts.append(request.system_prelude)
        return "\n\n".join(parts)


__all__ = ["Runner", "RunResult"]
