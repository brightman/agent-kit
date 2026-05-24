"""Runner —— 使用方面向的门面类。

把 provider + toolsets 组装成可执行 run,统一返回 `AsyncIterator[Event]`
或聚合的 `RunResult`(Q4 双轨)。

设计要点(对齐 docs/tech-design.md § 9 / § 10):

- **不为你 new toolset**:使用方传入 `toolsets: list[BaseToolset]`,
  Runner 不掌控其 lifecycle。Runner 通过 `loop.aclose()` 在 finally 阶段
  触发 toolset 资源释放(详见 § 9.3)
- **skill catalog 来自 discovery**:Runner 不收 `skill_registry` 参数;
  而是在 toolsets 列表里 isinstance 找 `SkillCatalogToolset`,从中读
  registry+tenant_id 去构造 prelude 段(§ 10.1)
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
            SkillCatalogToolset(skill_registry, tenant_id="user_42"),
            *toolsets_from_configs([...]),
        ],
    )
    # 流式:
    async for evt in runner.run(RunRequest(tenant_id="user_42", ...)):
        print(evt.kind, evt.payload)
    # 聚合:
    result = await runner.run_to_completion(RunRequest(...))
    print(result.final_text)
"""

from __future__ import annotations

import asyncio
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

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
    return _now_event(
        "error",
        {
            "stage": stage,
            "exc_type": exc.__class__.__name__,
            "message": str(exc),
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
    all_fms: list[SkillFrontmatter] = await catalog._registry.list(catalog._tenant_id)
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
    ) -> None:
        self._provider = provider
        self._toolsets = list(toolsets)
        self._default_max_rounds = default_max_rounds
        self._prelude = system_prelude
        self._compactor = compactor
        self._hooks = list(hooks or ())
        self._workspace_root = Path(workspace_root)
        self._storage_root = Path(storage_root)

    # ---- public ----

    async def run(self, request: RunRequest) -> AsyncIterator[Event]:
        """事件流形式。异常全部 catch,wrap 成 Event(kind="error") + return。

        SHOULD 用于服务端 / 持久化场景。
        """
        run_id = _new_run_id()
        workspace = self._workspace_root / run_id
        loop: AgentLoop | None = None

        try:
            # --- setup ---
            try:
                workspace.mkdir(parents=True, exist_ok=True)
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
                    tenant_id=request.tenant_id,
                    run_id=run_id,
                    skill_name=None,
                    cancel=asyncio.Event(),
                    workspace=workspace,
                    storage=self._storage_root,
                    emit=lambda evt: None,  # Stage 3: no-op(§ 10.2)
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
            if workspace.exists():
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

    # ---- helpers ----

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
