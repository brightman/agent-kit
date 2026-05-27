"""AgentLoop —— SDK 的核心:bounded 多轮 LLM ↔ tool 循环。

设计要点:
- pull 模型:`AsyncIterator[Event]`
- 轮数 cap 是硬约束,防 LLM 死循环 / 失控费用
- 最后一轮屏蔽 tools 强制收尾(provider 看到 `tools=None`,只能返 text)
- 终止条件:`response.tool_calls is []` 退出
- 取消用 asyncio.Event,在 round/chat/tool 边界 check
- Compactor 在每次 chat 前调用,SDK 兜底 `_assert_tool_pairs_intact`
- 4 个 Hook(before/after × model/tool),first-non-None 短路

完整契约见 docs/tech-design.md § 8。
"""

from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from ._errors import unwrap_to_leaf
from .context import ContextCompactor, _assert_tool_pairs_intact
from .hooks import Hook
from .provider import LlmProvider, LlmResponse, ToolSchema
from .tokens import estimate_messages_tokens
from .toolset import BaseToolset, ToolCallContext, ToolsetRouter
from .types import Event, EventKind, Message, ToolCall, ToolResult


def _check_request_cancel(cancel_check: Any) -> bool:
    """安全调 RunRequest.cancel_check(可能 None / 抛异常)。

    None → False。raise → 吞掉 + log + False(不让 caller bug 爆 run)。
    设计:cancel_check 是 user code,在 loop 内每 round 频繁调,不能让一次
    异常爆掉整个 run;但也不能 silent ——- log 让运维看见。
    """
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "RunRequest.cancel_check raised; treating as False (run continues)",
            exc_info=True,
        )
        return False


@dataclass
class RunRequest:
    """一次 run 的输入。

    ## prior_messages

    多轮对话场景把**已经发生过**的 user / assistant / tool turns 喂进 fresh
    `Runner.run()`,让 LLM 看到完整 conversation history。典型用法两种:

    1. **多轮 chat history**:每个新 user turn 把过去几轮 messages 作为
       `prior_messages` 一起传,`user_message` 是这一轮新的输入
    2. **honesty / correction re-run**:LLM 上一轮 final_text 不达标,把
       上一轮 `assistant(text)` 塞进 `prior_messages`,`user_message`
       写 "Runtime correction: ..." 再跑一遍

    **不在 SDK scope** 的两件事(留 use site policy):
    - history 压缩 / 摘要 / 滑动窗口 cutoff —— 用 `ContextCompactor`(loop
      内自动 compact) 或 use site 自己 `compress(history) → prior_messages`
    - 多模态 content block —— 目前 `content` 是 `str`,Stage 6+ 看需求扩

    ## Invariants(__post_init__ 校验)

    - `prior_messages` 不能含 `role="system"` —— system 走 `system_prelude` 或
      `Runner(system_prelude=...)`,独立到 conversation history 之外。塞进
      prior_messages 会让 _compose_messages 出现两个 system message,违反
      OpenAI/Anthropic wire format
    - tool_call ↔ tool_result 配对完整(context.py `_assert_tool_pairs_intact`):
      每个 `role="tool"` 的 message 必须有 prior `role="assistant"` 含同 id 的
      tool_call。orphan tool message → ValueError 构造时就挂,不让脏 history
      进 loop 后被 provider 400
    - 末尾如果是 `assistant + tool_calls`,后面必须紧跟对应的 tool messages
      (否则下一轮 LLM 会看见"我刚 call 了 tool 但没结果",大多 vendor 会
      拒绝);上面的 _assert_tool_pairs_intact 顺便覆盖
    """

    agent_id: str
    user_message: str
    enabled_skills: list[str] = field(default_factory=list)
    max_rounds: int = 10
    temperature: float = 0.7
    system_prelude: str = ""
    stream: bool = False                       # opt-in stream (deferred, spec § 14)
    metadata: dict[str, Any] = field(default_factory=dict)
    prior_messages: list[Message] = field(default_factory=list)
    cancel_check: Callable[[], bool] | None = None
    # ↑ poll-based 外部 cancel:loop 在 round 边界 + tool dispatch 前调,True
    #   → emit cancelled event(reason="cancel_check")+ return。spec § 3.7.2。
    #   None(默认)= 不 poll。跟 ToolCallContext.cancel(asyncio.Event)正交
    #   并存,两者任一触发都立刻 cancel。
    max_tokens: int | None = None
    # ↑ provider-side max_tokens cap(spec § 3.7.3,2026-05-25 加,gap #3 修复)。
    #   None(默认)= provider 用自己 default。loop 每次 provider.chat 透传。
    steering_drain: Callable[[], list[str]] | None = None
    # ↑ Mid-run steering. Loop calls this at the TOP of every round; each
    #   returned string is appended as `Message(role="user", content=text)`
    #   to the context and emits a `user_message_added` event. Lets a UI
    #   ("Agent.send_steering(...)") interrupt / redirect a running agent
    #   without waiting for it to finish. None(default) = no drain.
    parallel_tools: bool = True
    # ↑ When the LLM returns multiple tool_calls in one response, dispatch
    #   them concurrently via asyncio.gather (default). Set False to fall
    #   back to sequential. tool_call ↔ tool_result message ordering is
    #   preserved either way; only wall-clock changes.

    def __post_init__(self) -> None:
        if not self.prior_messages:
            return
        for i, m in enumerate(self.prior_messages):
            if m.role == "system":
                raise ValueError(
                    f"prior_messages[{i}] has role='system'; system content must "
                    "go into RunRequest.system_prelude or Runner(system_prelude=...). "
                    "Loop will only emit one system message at the head."
                )
        # tool_call ↔ tool_result 配对完整;复用 context.py 的 SDK 兜底实现
        _assert_tool_pairs_intact(self.prior_messages)


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
        # spec § 5.4: Router is rebuilt per-run (see `run()`) so toolsets
        # can advertise different schemas for different RunRequests.

    # ---- public ----

    async def aclose(self) -> None:
        """关闭所有 toolset(按 registration order 反序;每个的异常 swallow + log)。

        Not delegated to Router because Router is per-run (lifecycle mismatch);
        we walk the toolsets list directly.
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
            # Stream path is deferred (see spec § 14). Fail fast so callers
            # who flip the flag without a stream-capable provider see why.
            yield self._mk_event(
                None, "error",
                {"stage": "loop", "message": "stream mode is not yet implemented (spec § 14)"},
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
            # 两路 cancel:asyncio.Event(in-tool 用)+ poll-based callable
            # (外部用)。ctx.cancel 优先 check —— 一旦 hook 内 set 了,这条
            # 路径 reason="external" 更精确;其次 poll cancel_check。
            if ctx.cancel.is_set():
                yield self._mk_event(
                    last_round_start_id, "cancelled",
                    {"round": round_idx, "reason": "external"},
                )
                return
            if _check_request_cancel(request.cancel_check):
                yield self._mk_event(
                    last_round_start_id, "cancelled",
                    {"round": round_idx, "reason": "cancel_check"},
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

            # --- drain steering queue (mid-run user message injection) ---
            if request.steering_drain is not None:
                try:
                    pending = list(request.steering_drain())
                except Exception as exc:  # noqa: BLE001 — same swallow policy as cancel_check
                    import logging
                    logging.getLogger(__name__).warning(
                        "RunRequest.steering_drain raised; ignoring (run continues)",
                        exc_info=True,
                    )
                    pending = []
                for text in pending:
                    if not text:
                        continue
                    messages.append(Message(role="user", content=text))
                    yield self._mk_event(
                        round_start_id, "user_message_added",
                        {"round": round_idx, "text": text, "source": "steering"},
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
                        max_tokens=request.max_tokens,
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

            # Pre-batch cancel check (mirrors original sequential semantic)
            if ctx.cancel.is_set():
                yield self._mk_event(
                    round_start_id, "cancelled",
                    {"round": round_idx, "reason": "external_mid_tool"},
                )
                return
            if _check_request_cancel(request.cancel_check):
                yield self._mk_event(
                    round_start_id, "cancelled",
                    {"round": round_idx, "reason": "cancel_check_mid_tool"},
                )
                return

            # --- tool dispatch (parallel or sequential) ---
            tool_calls = list(response.tool_calls)
            use_parallel = request.parallel_tools and len(tool_calls) > 1
            if use_parallel:
                # Live-stream events from N concurrent tool tasks via a queue.
                # Helper writes final tool messages into `messages` in
                # original call order. Returns on first error event.
                aborted = False
                async for evt in self._dispatch_tools_parallel(
                    tool_calls, ctx, router, round_start_id, messages,
                ):
                    yield evt
                    if evt.kind == "error":
                        aborted = True
                if aborted:
                    return
            else:
                # Sequential path (single tool OR parallel_tools=False).
                for call in tool_calls:
                    aborted = False
                    result_content: str | None = None
                    async for evt in self._dispatch_one_tool(
                        call, ctx, router, round_start_id,
                    ):
                        yield evt
                        if evt.kind == "error":
                            aborted = True
                        elif evt.kind == "tool_result":
                            result_content = evt.payload["content"]
                    if aborted:
                        return
                    if result_content is not None:
                        messages.append(Message(
                            role="tool",
                            content=result_content,
                            tool_call_id=call.id,
                        ))

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

    # ---- tool dispatch helpers ----

    async def _dispatch_one_tool(
        self,
        call: ToolCall,
        ctx: ToolCallContext,
        router: ToolsetRouter,
        round_start_id: str,
    ) -> AsyncIterator[Event]:
        """Run one tool call end-to-end. Yields all events for this call:
        tool_call → (tool_short_circuited | execute) → tool_result. On hook
        failure yields a single error event and stops.

        The caller decides what to do with the trailing event(s) — append a
        tool message on `tool_result`, abort on `error`. This helper does NOT
        mutate the conversation messages list itself.
        """
        tool_call_id = _new_event_id()
        yield self._mk_event_with_id(
            tool_call_id, round_start_id, "tool_call", call.to_dict(),
        )

        # before_tool hook
        try:
            sc_result, sc_hook = await self._run_before_tool(ctx, call)
        except Exception as exc:  # noqa: BLE001
            yield self._mk_error(
                "hook", exc, round_start_id,
                extra={"method": "before_tool",
                       "hook_class": exc.__class__.__name__,
                       "call_id": call.id},
            )
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

        # after_tool hook
        try:
            rewritten_result, _ = await self._run_after_tool(ctx, call, result)
        except Exception as exc:  # noqa: BLE001
            yield self._mk_error(
                "hook", exc, round_start_id,
                extra={"method": "after_tool",
                       "hook_class": exc.__class__.__name__,
                       "call_id": call.id},
            )
            return
        if rewritten_result is not None:
            result = rewritten_result

        yield self._mk_event(tool_call_id, "tool_result", result.to_dict())

    async def _dispatch_tools_parallel(
        self,
        tool_calls: list[ToolCall],
        ctx: ToolCallContext,
        router: ToolsetRouter,
        round_start_id: str,
        messages: list[Message],
    ) -> AsyncIterator[Event]:
        """Concurrently run N `_dispatch_one_tool` coros, yielding events
        live as each child task emits them. After all tasks finish (or any
        aborts via error event), append tool-result messages to `messages`
        in **original tool_calls order** to keep the LLM conversation tidy.
        """
        import asyncio as _asyncio

        # Sentinel for "this child finished" so the producer can detect
        # global completion without polling.
        _DONE = object()
        event_queue: _asyncio.Queue[Event | object] = _asyncio.Queue()
        # Per-call slot: final tool content (None if aborted via error)
        results: list[str | None] = [None] * len(tool_calls)
        aborted_flags: list[bool] = [False] * len(tool_calls)

        async def run_one(i: int, call: ToolCall) -> None:
            async for evt in self._dispatch_one_tool(call, ctx, router, round_start_id):
                await event_queue.put(evt)
                if evt.kind == "error":
                    aborted_flags[i] = True
                elif evt.kind == "tool_result":
                    results[i] = evt.payload["content"]
            await event_queue.put(_DONE)

        # Fan-out
        tasks = [_asyncio.create_task(run_one(i, c)) for i, c in enumerate(tool_calls)]
        done_count = 0
        try:
            while done_count < len(tasks):
                item = await event_queue.get()
                if item is _DONE:
                    done_count += 1
                    continue
                yield item  # type: ignore[misc]
        finally:
            # Make sure no orphaned tasks linger; gather to surface any
            # genuine crash from a child coro (shouldn't happen — children
            # turn errors into events — but be safe).
            await _asyncio.gather(*tasks, return_exceptions=True)

        if any(aborted_flags):
            return  # caller already saw the error event(s) and will abort

        # Append tool messages in the LLM's original tool_calls order so the
        # conversation transcript is deterministic regardless of completion order.
        for i, call in enumerate(tool_calls):
            if results[i] is None:
                continue
            messages.append(Message(
                role="tool",
                content=results[i],
                tool_call_id=call.id,
            ))

    # ---- helpers ----

    def _compose_messages(self, request: RunRequest) -> list[Message]:
        """初始 messages = optional system + prior_messages + user message。

        顺序: `[system?, *prior_messages, user]`(spec § 3.x)

        skill catalog 注入由 Runner(Stage 3)负责拼进 system_prelude,
        AgentLoop 只用 prelude 字符串本身。prior_messages 的校验
        (no system role / tool-pair invariant)在 `RunRequest.__post_init__`
        构造时已经跑过,这里直接 splice。
        """
        prelude_parts: list[str] = []
        if self._prelude:
            prelude_parts.append(self._prelude)
        if request.system_prelude:
            prelude_parts.append(request.system_prelude)
        out: list[Message] = []
        if prelude_parts:
            out.append(Message(role="system", content="\n\n".join(prelude_parts)))
        out.extend(request.prior_messages)
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
