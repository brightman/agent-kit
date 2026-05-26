"""Agent —— 便利层,降低 hello-world boilerplate(spec § 17)。

`Agent` 是 `Runner` 的 thin wrapper。它把"构造 Runner + 写 RunRequest"两步
合成一个 dataclass + 一个 `.run(message)`,语法接近 ADK / openai-agents。

设计要点(对照 spec § 17):

- **Thin convenience layer**:0 新概念,所有字段都对应到 Runner ctor 或
  RunRequest 字段。底层 Runner 通过 `agent.runner` 暴露,advanced 用户
  可以直接 `agent.runner.run_to_completion(RunRequest(...))` 走全自由路径
- **Long-lived Runner**:`Agent` 内部持有一个 Runner 实例,跨多次 `.run()`
  调用复用 —— toolsets 的 MCP session 不会每次重建
- **没有 tenant_id 概念**(spec § 1,2026-05-25 修订):多租户应用层每个
  tenant new 一份 Agent + per-tenant `SkillRegistry` / `workspace_provider`
  closure。SDK 自身完全 tenant-agnostic
- **`model=string` 走 LiteLlm extras**:`"gemini/..."` / `"anthropic/..."` /
  `"openai/..."` 等 LiteLLM 路由格式 → 自动包成 `LiteLlm` provider
  (需 `pip install "agent-kit[litellm]"`);否则给清晰 ImportError

故意不做的(对照 spec § 17.2):
- 函数当工具(用户写 BaseToolset 显式)
- sub-agent / handoff
- 内置 google_search 等具体工具
- multimodal / sessions / memory
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .context import ContextCompactor
from .hooks import Hook
from .loop import RunRequest
from .provider import LlmProvider
from .runner import Runner, RunResult
from .toolset import BaseToolset
from .types import Message


@dataclass
class Agent:
    """ADK / openai-agents 形态的便利 wrapper。

    Examples:

        # 最短形态(需 `pip install "agent-kit[litellm]"`)
        agent = Agent(
            name="researcher",
            model="gemini/gemini-flash-latest",
            instruction="You help users research topics thoroughly.",
            tools=[search_mcp_toolset],
        )
        result = agent.run_sync("What are the latest LLM benchmarks?")
        print(result.final_text)

        # 自带 provider 实例(不依赖 LiteLLM)
        agent = Agent(
            name="researcher",
            model=MyCustomProvider(...),
            instruction="...",
            tools=[...],
        )

        # 多租户使用方:per-tenant Agent(SDK 自身不带 tenant 概念)
        agent_for_42 = Agent(name="r", model=..., skills=registry_for_42, ...)
        result = await agent_for_42.run("query")

        # advanced:拿底层 Runner
        result = await agent.runner.run_to_completion(RunRequest(...))
    """

    name: str
    model: LlmProvider | str
    instruction: str = ""
    tools: list[BaseToolset] = field(default_factory=list)

    # advanced —— Runner ctor 的其他参数,默认值跟 Runner 一致
    hooks: list[Hook] = field(default_factory=list)
    compactor: ContextCompactor | None = None
    workspace_provider: Callable[[RunRequest, str], Path] | None = None
    workspace_root: Path = Path("/tmp/agent-kit-runs")
    storage_root: Path = Path("./persistent")

    # 默认的 per-run 参数(`.run()` 不传就用这个;传了就 override)
    default_max_rounds: int = 10
    default_temperature: float = 0.7
    default_max_tokens: int | None = None

    # 注意:**没有任何 tenant_id 字段**(spec § 1 修订 2026-05-25)。
    # 多租户应用层每个 tenant new 一份 Agent;SDK 自身完全 tenant-agnostic

    def __post_init__(self) -> None:
        # str → LiteLlm wrapper(需 extras)
        if isinstance(self.model, str):
            self.model = self._resolve_string_model(self.model)
        # 一次性构 Runner,跨多 `.run()` 复用(toolsets / provider 跨 run 状态保留)
        self._runner = Runner(
            provider=self.model,
            toolsets=self.tools,
            default_max_rounds=self.default_max_rounds,
            system_prelude=self.instruction,
            compactor=self.compactor,
            hooks=self.hooks,
            workspace_root=self.workspace_root,
            storage_root=self.storage_root,
            workspace_provider=self.workspace_provider,
        )

    # ---- public ----

    @property
    def runner(self) -> Runner:
        """底层 Runner 实例。advanced 用户可以 `agent.runner.run_to_completion
        (RunRequest(...))` 拿全自由(stream / metadata / 自定义 run_id / 等)。"""
        return self._runner

    async def run(
        self,
        user_message: str,
        *,
        enabled_skills: list[str] | None = None,
        max_rounds: int | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        prior_messages: list[Message] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunResult:
        """Async 一次性 run,返回 RunResult。遇 error event raise RuntimeError
        (跟 Runner.run_to_completion 一致)。"""
        return await self._runner.run_to_completion(
            self._build_request(
                user_message,
                enabled_skills=enabled_skills,
                max_rounds=max_rounds,
                temperature=temperature,
                max_tokens=max_tokens,
                prior_messages=prior_messages,
                cancel_check=cancel_check,
                metadata=metadata,
            )
        )

    def run_sync(
        self,
        user_message: str,
        *,
        enabled_skills: list[str] | None = None,
        max_rounds: int | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        prior_messages: list[Message] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunResult:
        """Sync wrapper —— 内部走 `Runner.run_sync()`(`asyncio.run`)。

        **不能在已经跑着的 event loop 里调**(FastAPI handler / Jupyter async
        cell / async test)—— 调了会 raise 友好错误。那种场景直接 `await
        agent.run(...)`。
        """
        return self._runner.run_sync(
            self._build_request(
                user_message,
                enabled_skills=enabled_skills,
                max_rounds=max_rounds,
                temperature=temperature,
                max_tokens=max_tokens,
                prior_messages=prior_messages,
                cancel_check=cancel_check,
                metadata=metadata,
            )
        )

    # ---- helpers ----

    def _build_request(
        self,
        user_message: str,
        *,
        enabled_skills: list[str] | None,
        max_rounds: int | None,
        temperature: float | None,
        max_tokens: int | None,
        prior_messages: list[Message] | None,
        cancel_check: Callable[[], bool] | None,
        metadata: dict[str, Any] | None,
    ) -> RunRequest:
        return RunRequest(
            agent_id=self.name,
            user_message=user_message,
            enabled_skills=list(enabled_skills) if enabled_skills else [],
            max_rounds=max_rounds if max_rounds is not None else self.default_max_rounds,
            temperature=(
                temperature if temperature is not None else self.default_temperature
            ),
            max_tokens=max_tokens if max_tokens is not None else self.default_max_tokens,
            prior_messages=list(prior_messages) if prior_messages else [],
            cancel_check=cancel_check,
            metadata=dict(metadata) if metadata else {},
        )

    @staticmethod
    def _resolve_string_model(model: str) -> LlmProvider:
        """str → `agent_kit.contrib.providers.litellm.LiteLlm(model)`。

        需要 `pip install "agent-kit[litellm]"`(litellm 是 optional extras)。
        没装 → 抛清晰 ImportError,告诉 caller 怎么办。
        """
        try:
            from .contrib.providers.litellm import LiteLlm
        except ImportError as exc:
            raise ImportError(
                f"Agent(model={model!r}) is a string, which requires the "
                f"LiteLlm provider. Install it with:\n\n"
                f"    pip install \"agent-kit[litellm]\"\n\n"
                f"Or pass an LlmProvider instance directly:\n\n"
                f"    Agent(model=MyProvider(...), ...)"
            ) from exc
        return LiteLlm(model)


__all__ = ["Agent"]
