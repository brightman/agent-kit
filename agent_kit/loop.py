"""AgentLoop —— SDK 的核心:bounded 多轮 LLM ↔ tool 循环。

设计要点(综合四家):
- pull 模型:`AsyncIterator[Event]`(ADK / OH 风格)
- 轮数 cap 是硬约束(baizhi-agent / fam-runtime)
- 最后一轮屏蔽 tools 强制收尾(baizhi-agent 发明)
- 终止条件:`response.tool_calls is []` 退出
- 取消用 asyncio.Event,在 round/chat/tool 边界 check
- Compactor 在每次 chat 前调用,SDK 兜底 _assert_tool_pairs_intact
- 4 个 Hook(before/after × model/tool),first-non-None 短路

完整契约见 docs/tech-design.md § 8。
"""

from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ._errors import unwrap_to_leaf
from .context import ContextCompactor, _assert_tool_pairs_intact
from .hooks import Hook
from .provider import LlmProvider, LlmResponse, ToolSchema
from .tokens import estimate_messages_tokens
from .toolset import BaseToolset, ToolCallContext, ToolsetRouter
from .types import Event, EventKind, Message, ToolCall, ToolResult


@dataclass
class RunRequest:
    """一次 run 的输入。"""

    tenant_id: str
    agent_id: str
    user_message: str
    enabled_skills: list[str] = field(default_factory=list)
    max_rounds: int = 10
    temperature: float = 0.7
    system_prelude: str = ""
    stream: bool = False                       # Q1 决议:opt-in stream(Stage 5)
    metadata: dict[str, Any] = field(default_factory=dict)


def _new_event_id() -> str:
    """单调可排序的 event id:ns 时间戳 + 8 字符 uuid 后缀。"""
    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"


class AgentLoop:
    """无状态可重入的 loop。每次 run() 自带独立 cancel / messages。"""

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
        # spec § 5.4(Stage 5 修订):Router per-run 重建,见 run()。
        # 这里不再持有 Router 实例

    # ---- public ----

    async def aclose(self) -> None:
        """关闭所有 toolset(按 registration order 反序;每个的异常 swallow + log)。

        spec § 5.4 修订:不再委托给 Router(Router 是 per-run 的,
        生命周期不匹配)。直接遍历 toolsets。
        """
        import logging
        log = logging.getLogger(__name__)
        for ts in reversed(self._toolsets):
            try:
                await ts.aclose()
            except Exception:  # noqa: BLE001
                log.warning("toolset aclose failed for %r", ts.name, exc_info=True)

    async def run(
        self,
        request: RunRequest,
        ctx: ToolCallContext,
    ) -> AsyncIterator[Event]:
        """执行多轮 loop,yield 事件。"""
        if request.stream:
            # Stream 路径见 Stage 5(tech-design § 8.5);本 Stage 2 fallback 报错
            yield self._mk_event(
                None, "error",
                {"stage": "loop", "message": "stream mode is Stage 5, not yet implemented"},
            )
            return

        messages = self._compose_messages(request)
        # spec § 5.4:per-request Router 构建,toolset 可按 request 过滤 / 动态生成 schemas
        try:
            router = ToolsetRouter(self._toolsets, request=request)
        except Exception as exc:  # noqa: BLE001
            yield self._mk_error("setup", exc, None)
            return
        all_schemas = router.all_schemas()
        last_usage: dict[str, Any] | None = None
        last_round_start_id: str | None = None
        max_rounds = request.max_rounds or self._default_max_rounds

        for round_idx in range(max_rounds):
            if ctx.cancel.is_set():
                yield self._mk_event(
                    last_round_start_id, "cancelled",
                    {"round": round_idx, "reason": "external"},
                )
                return

            # --- compactor pre-chat ---
            if self._compactor is not None:
                try:
                    should = await self._compactor.should_compact(messages, last_usage)
                except Exception as exc:
                    yield self._mk_error("compactor", exc, last_round_start_id,
                                          extra={"method": "should_compact"})
                    return
                if should:
                    before_count = len(messages)
                    before_tokens = estimate_messages_tokens(messages)
                    try:
                        new_messages = await self._compactor.compact(messages)
                        _assert_tool_pairs_intact(new_messages)
                    except Exception as exc:
                        yield self._mk_error("compactor", exc, last_round_start_id,
                                              extra={"method": "compact"})
                        return
                    after_tokens = estimate_messages_tokens(new_messages)
                    messages = new_messages
                    yield self._mk_event(
                        last_round_start_id, "context_compacted",
                        {
                            "before_count": before_count,
                            "after_count": len(messages),
                            "before_tokens": before_tokens,
                            "after_tokens": after_tokens,
                            "strategy": getattr(self._compactor, "name", "<unknown>"),
                        },
                    )

            # --- round_start ---
            round_start_id = _new_event_id()
            last_round_start_id = round_start_id
            yield self._mk_event_with_id(
                round_start_id, None, "round_start", {"round": round_idx},
            )

            # 最后一轮屏蔽 tools(spec § 8.3)
            is_last_round = round_idx == max_rounds - 1
            tools_this_round: list[ToolSchema] | None = None if is_last_round else (all_schemas or None)

            # --- before_model hooks ---
            try:
                sc_response, sc_hook = await self._run_before_model(
                    ctx, messages, tools_this_round
                )
            except Exception as exc:
                yield self._mk_error("hook", exc, round_start_id,
                                      extra={"method": "before_model",
                                             "hook_class": exc.__class__.__name__})
                return

            yield self._mk_event(
                round_start_id, "llm_request",
                {
                    "messages_count": len(messages),
                    "tools_count": len(tools_this_round) if tools_this_round else 0,
                },
            )

            if sc_response is not None:
                response = sc_response
                yield self._mk_event(
                    round_start_id, "llm_short_circuited",
                    {"by_hook": sc_hook, "response": response.to_dict()},
                )
            else:
                try:
                    response = await self._provider.chat(
                        messages, tools_this_round,
                        temperature=request.temperature,
                    )
                except Exception as exc:
                    yield self._mk_error("provider", exc, round_start_id)
                    return
                llm_response_id = _new_event_id()
                yield self._mk_event_with_id(
                    llm_response_id, round_start_id, "llm_response",
                    response.to_dict(),
                )

            last_usage = response.usage

            # --- after_model hooks ---
            try:
                rewritten, _ = await self._run_after_model(ctx, response)
            except Exception as exc:
                yield self._mk_error("hook", exc, round_start_id,
                                      extra={"method": "after_model",
                                             "hook_class": exc.__class__.__name__})
                return
            if rewritten is not None:
                response = rewritten

            # --- final_text or tool dispatch ---
            if not response.tool_calls:
                yield self._mk_event(
                    round_start_id, "final_text", {"text": response.text},
                )
                yield self._mk_event(
                    round_start_id, "round_end", {"round": round_idx},
                )
                return

            # 把 assistant.tool_calls 加进 messages
            messages.append(
                Message(
                    role="assistant",
                    content=response.text or "",
                    tool_calls=list(response.tool_calls),
                )
            )

            for call in response.tool_calls:
                if ctx.cancel.is_set():
                    yield self._mk_event(
                        round_start_id, "cancelled",
                        {"round": round_idx, "reason": "external_mid_tool"},
                    )
                    return

                tool_call_id = _new_event_id()
                yield self._mk_event_with_id(
                    tool_call_id, round_start_id, "tool_call", call.to_dict(),
                )

                # --- before_tool hooks ---
                try:
                    sc_result, sc_hook = await self._run_before_tool(ctx, call)
                except Exception as exc:
                    yield self._mk_error("hook", exc, round_start_id,
                                          extra={"method": "before_tool",
                                                 "hook_class": exc.__class__.__name__,
                                                 "call_id": call.id})
                    return

                if sc_result is not None:
                    result = sc_result
                    yield self._mk_event(
                        tool_call_id, "tool_short_circuited",
                        {"by_hook": sc_hook, "call": call.to_dict(),
                         "result": result.to_dict()},
                    )
                else:
                    result = await router.execute(call, ctx)

                # --- after_tool hooks ---
                try:
                    rewritten_result, _ = await self._run_after_tool(ctx, call, result)
                except Exception as exc:
                    yield self._mk_error("hook", exc, round_start_id,
                                          extra={"method": "after_tool",
                                                 "hook_class": exc.__class__.__name__,
                                                 "call_id": call.id})
                    return
                if rewritten_result is not None:
                    result = rewritten_result

                yield self._mk_event(
                    tool_call_id, "tool_result", result.to_dict(),
                )
                messages.append(
                    Message(role="tool", content=result.content, tool_call_id=call.id)
                )

            yield self._mk_event(
                round_start_id, "round_end", {"round": round_idx},
            )

        # 跑完 max_rounds 仍未 final_text:provider 没在 tools=None 时出 text
        # (按 § 8.4,理论不该发生)
        yield self._mk_event(
            last_round_start_id, "error",
            {
                "stage": "loop",
                "exc_type": "RuntimeError",
                "message": (
                    f"exhausted max_rounds={max_rounds} without final_text "
                    "(provider returned tool_calls on tools=None final round)"
                ),
                "traceback": "",
            },
        )

    # ---- helpers ----

    def _compose_messages(self, request: RunRequest) -> list[Message]:
        """初始 messages = optional system + user message。

        skill catalog 注入由 Runner(Stage 3)负责拼进 system_prelude,
        AgentLoop 只用 prelude 字符串本身。
        """
        prelude_parts: list[str] = []
        if self._prelude:
            prelude_parts.append(self._prelude)
        if request.system_prelude:
            prelude_parts.append(request.system_prelude)
        out: list[Message] = []
        if prelude_parts:
            out.append(Message(role="system", content="\n\n".join(prelude_parts)))
        out.append(Message(role="user", content=request.user_message))
        return out

    async def _run_before_model(
        self,
        ctx: ToolCallContext,
        messages: list[Message],
        tools: list[ToolSchema] | None,
    ) -> tuple[LlmResponse | None, str | None]:
        for hook in self._hooks:
            r = await hook.before_model(ctx, messages, tools)
            if r is not None:
                return r, hook.__class__.__name__
        return None, None

    async def _run_after_model(
        self,
        ctx: ToolCallContext,
        response: LlmResponse,
    ) -> tuple[LlmResponse | None, str | None]:
        for hook in self._hooks:
            r = await hook.after_model(ctx, response)
            if r is not None:
                return r, hook.__class__.__name__
        return None, None

    async def _run_before_tool(
        self,
        ctx: ToolCallContext,
        call: ToolCall,
    ) -> tuple[ToolResult | None, str | None]:
        for hook in self._hooks:
            r = await hook.before_tool(ctx, call)
            if r is not None:
                return r, hook.__class__.__name__
        return None, None

    async def _run_after_tool(
        self,
        ctx: ToolCallContext,
        call: ToolCall,
        result: ToolResult,
    ) -> tuple[ToolResult | None, str | None]:
        for hook in self._hooks:
            r = await hook.after_tool(ctx, call, result)
            if r is not None:
                return r, hook.__class__.__name__
        return None, None

    # ---- event factories ----

    def _mk_event(
        self,
        parent_event_id: str | None,
        kind: EventKind,
        payload: dict[str, Any],
    ) -> Event:
        return Event(
            event_id=_new_event_id(),
            parent_event_id=parent_event_id,
            kind=kind,
            payload=payload,
            ts=time.time(),
        )

    def _mk_event_with_id(
        self,
        event_id: str,
        parent_event_id: str | None,
        kind: EventKind,
        payload: dict[str, Any],
    ) -> Event:
        return Event(
            event_id=event_id,
            parent_event_id=parent_event_id,
            kind=kind,
            payload=payload,
            ts=time.time(),
        )

    def _mk_error(
        self,
        stage: str,
        exc: BaseException,
        parent_event_id: str | None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> Event:
        leaf = unwrap_to_leaf(exc)
        payload: dict[str, Any] = {
            "stage": stage,
            "exc_type": leaf.__class__.__name__,
            "message": str(leaf),
            # traceback 用原 exc(可能 ExceptionGroup)保留完整链
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        }
        if extra:
            payload.update(extra)
        return self._mk_event(parent_event_id, "error", payload)


__all__ = ["RunRequest", "AgentLoop"]
