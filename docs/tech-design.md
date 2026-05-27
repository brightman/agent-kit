# agent-kit · 技术设计文档(Stage 1 实现 spec)

**版本**:Stage 1 spec(2026-05-24)
**前置**:[`proposal.md`](proposal.md) —— 抽象提案 + 4 个开放问题(Q1-Q4)
**状态**:Q1-Q4 已决议(见 § 13);本文档是 Stage 1 实现的**契约级**规范

读这份文档时:
- "**MUST** / **MUST NOT** / **SHOULD**" 是规范级措辞(RFC 2119)
- 每个签名都是 final;改签名需要先改本文档
- 鼓励 Stage 1 实现时**对照本文档逐条断言**(单元测试)

---

## 目录

1. [范围与非目标](#1-范围与非目标)
2. [整体架构](#2-整体架构)
3. [数据契约(types.py)](#3-数据契约typespy)
4. [Provider 契约(provider.py)](#4-provider-契约providerpy)
5. [Toolset 契约(toolset.py)](#5-toolset-契约toolsetpy)
6. [Skill 契约(skill.py)](#6-skill-契约skillpy)
7. [MCP 集成(mcp.py)](#7-mcp-集成mcppy)
8. [AgentLoop 核心(loop.py)](#8-agentloop-核心looppy)
9. [Runner 门面(runner.py)](#9-runner-门面runnerpy)
10. [System prompt 组装规则](#10-system-prompt-组装规则)
11. [事件 ID 规则](#11-事件-id-规则)
12. [测试策略](#12-测试策略)
13. [4 个开放问题的决议](#13-4-个开放问题的决议)
14. [迭代路线](#14-迭代路线)
15. [Out of scope](#15-out-of-scope)

---

## 1. 范围与非目标

### 范围(本文档定义)

- 单次 agent run 的多轮 LLM ↔ tool 循环
- Provider / Toolset / Skill / MCP 四类抽象的契约
- Event 流的完整 schema
- 错误传播 + 取消 + 资源生命周期语义

### 非目标(本文档**不**定义,留给上层)

- 多租户队列 / LRU / 资源调度
- **tenant 概念**(2026-05-25 修订):SDK 完全 tenant-agnostic。`RunRequest` /
  `ToolCallContext` / `SkillRegistry` / `Agent` 都不带 `tenant_id` 字段或参数。
  多租户 application 层每个 tenant **new 一份** `Agent` + per-tenant
  `SkillRegistry` / `workspace_provider` closure;tenant 标识若要传到 hook /
  toolset 内部,通过 `RunRequest.metadata` dict 自由载荷。决策依据:tenant
  是 deployment shape 而不是 SDK 机制(同 § 7.2 撤掉 McpLifecycle 的逻辑)
- 持久化(SQLite / OTel exporter)
- HTTP API
- 鉴权 / 配额
- UI / 前端
- Memory / Session(可能后续单独 spec)
- 多 agent 编排

---

## 2. 整体架构

### 组件依赖图

```
        ┌───────────────────────────────────────┐
        │              Runner                   │
        │  (run / run_to_completion 门面)       │
        └────────────────┬──────────────────────┘
                         │ owns
        ┌────────────────┴──────────────────────┐
        │             AgentLoop                 │
        │   (bounded round loop, emit Event)    │
        └──┬────────────┬───────────┬───────────┘
           │ uses       │ uses      │ uses
   ┌───────▼────┐  ┌────▼──────┐ ┌──▼──────────┐
   │ LlmProvider│  │ToolsetRtr │ │ToolCallCtx  │
   │ (Protocol) │  │ (Router)  │ │ (dataclass) │
   └────────────┘  └─────┬─────┘ └─────────────┘
                         │ contains
        ┌────────────────┼──────────────────┐
        │                │                  │
  ┌─────▼──────┐  ┌──────▼────────┐  ┌──────▼─────────┐
  │ McpToolset │  │SkillCatalog   │  │ Custom Toolset │
  │ (wrap mcp) │  │ Toolset       │  │ (user-defined) │
  └────────────┘  └──────┬────────┘  └────────────────┘
                         │ reads
                  ┌──────▼──────────┐
                  │ SkillRegistry   │
                  │ (ABC,实现在上层) │
                  └─────────────────┘
```

### 单次 run 的 sequence(简化)

```
caller ──run(request)──▶ Runner
                            │
                            ├─ build ToolCallContext (tenant/run/workspace/storage/cancel)
                            ├─ materialize toolsets (skill catalog + MCP + extras)
                            ├─ build AgentLoop(provider, toolsets, prelude)
                            │
                            └─ async for evt in loop.run(request, ctx):
                                  yield evt to caller
                            
loop.run:
   compose messages (system prelude + skill frontmatter + user message)
   schemas = router.all_schemas()
   for round_idx in range(request.max_rounds):
       check cancel ──▶ yield Event(cancelled), return
       yield Event(round_start, round=round_idx)
       
       tools = schemas if round_idx < max_rounds - 1 else None  # 最后一轮屏蔽
       yield Event(llm_request, {messages_len, tools_count})
       
       if request.stream:
           async for delta in provider.chat_stream(messages, tools):
               yield Event(llm_delta, {text_delta, tool_call_delta})
           # aggregate to LlmResponse
       else:
           response = await provider.chat(messages, tools)
       yield Event(llm_response, response.to_dict())
       
       if not response.tool_calls:
           yield Event(final_text, {text})
           return
       
       messages.append(response.to_assistant_message())
       for call in response.tool_calls:
           yield Event(tool_call, call.to_dict())
           result = await router.execute(call, ctx)
           yield Event(tool_result, result.to_dict())
           messages.append(result.to_tool_message())
       
       yield Event(round_end, {round=round_idx})
   
   # 跑完 max_rounds 仍没 final_text:正常退出(最后一轮屏蔽 tools 保证 LLM 会 emit text)
```

---

## 3. 数据契约(types.py)

### 3.1 Role

```python
Role = Literal["system", "user", "assistant", "tool"]
```

### 3.2 ToolCall

```python
@dataclass(frozen=True)
class ToolCall:
    id: str           # MUST 非空;OpenAI/Anthropic 都要求回引
    name: str         # MUST 在 ToolsetRouter 已注册的 schema name 集合内
    arguments: dict[str, Any]   # SHOULD 是 JSON-serializable
```

### 3.3 ToolResult

```python
@dataclass(frozen=True)
class ToolResult:
    call_id: str      # MUST 等于对应 ToolCall.id
    content: str      # MUST 是 str(序列化交给 toolset);多模态留给 ContentBlock 升级
    is_error: bool = False     # True 时 LLM 收到的内容仍可读,但事件流标记为错误
```

### 3.4 Message

```python
@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    tool_calls: list[ToolCall] | None = None    # MUST None unless role == "assistant"
    tool_call_id: str | None = None             # MUST 非空 iff role == "tool"
```

### 3.5 EventKind 与 payload schema

```python
EventKind = Literal[
    "round_start", "llm_request", "llm_delta", "llm_response",
    "tool_call", "tool_result", "round_end",
    "final_text", "error", "cancelled",
]
```

| kind | payload 必需字段 | 何时 emit |
|---|---|---|
| `round_start` | `{round: int}` | 每轮开始 |
| `llm_request` | `{messages_count: int, tools_count: int}` | provider.chat(_stream) 调用前 |
| `llm_delta` | `{text_delta: str?, tool_call_delta: dict?, finish_reason: str?}` | **仅 stream 模式**,每个 delta 一次 |
| `llm_response` | `{text: str, tool_calls: list[ToolCall], usage: dict?, raw: dict?}` | provider 返回完整 response 后(stream 模式下 aggregate 完毕也 emit) |
| `tool_call` | `{id, name, arguments}` | 单个工具调用前 |
| `tool_result` | `{call_id, content, is_error, duration_ms?}` | 单个工具返回后 |
| `round_end` | `{round: int}` | 每轮结束(在 final_text/cancelled 之前) |
| `final_text` | `{text: str}` | LLM 返回不含 tool_calls,或跑满 max_rounds 后 LLM 给出收尾文本 |
| `error` | `{exc_type, message, traceback, stage}` | 任意异常,run 终止前 emit 一次 |
| `cancelled` | `{round: int, reason: str?}` | cancel event 触发,run 终止前 emit 一次 |
| `context_compacted` | `{before_count, after_count, before_tokens, after_tokens, strategy: str}` | compactor 触发后、provider.chat 调用前 |
| `llm_short_circuited` | `{by_hook: str, response: dict}` | `before_model` hook 返回非 None → 替代 provider.chat 调用 |
| `tool_short_circuited` | `{by_hook: str, call: dict, result: dict}` | `before_tool` hook 返回非 None → 替代 tool 实调 |

`stage` ∈ `{"loop", "provider", "tool", "setup", "compactor", "hook"}` —— 异常发生在哪个阶段。
`strategy` 由 compactor 实现自填,推荐值:`"truncate"` / `"summarize"` / `"sliding_window"` / `"<custom>"`。
`by_hook` 由 loop 填入造成短路的 Hook 子类名(便于 trace UI 区分哪个 hook 介入了)。

### 3.6 Event

```python
@dataclass(frozen=True)
class Event:
    event_id: str                # ULID 推荐;实现见 § 11
    parent_event_id: str | None  # 父事件 ID;实现见 § 11
    kind: EventKind
    payload: dict[str, Any]
    ts: float                    # time.time(),seconds since epoch
```

### 3.7 RunRequest

```python
@dataclass
class RunRequest:
    # `tenant_id` 已删除(2026-05-25 修订);SDK 自身不带 tenant 概念。
    # 多租户应用层每个 tenant new 一份 Agent / Runner;tenant 可通过
    # `metadata` dict 传到 hook / toolset 供 application 使用
    agent_id: str                # MUST 非空
    user_message: str
    enabled_skills: list[str] = field(default_factory=list)
    # ↑ 每项 "name" 或 "name@version";version 缺省 = latest
    max_rounds: int = 10
    temperature: float = 0.7
    system_prelude: str = ""     # 调用方追加,在 Runner 自带 prelude 之后
    stream: bool = False         # Q1 决议:opt-in
    metadata: dict[str, Any] = field(default_factory=dict)
    # ↑ 透传给 Event payload 的 `request_metadata` 字段(给上层做关联)
    prior_messages: list[Message] = field(default_factory=list)
    # ↑ 已经发生过的 user/assistant/tool turns(spec § 3.7.1,2026-05-25 加)
```

#### 3.7.1 `prior_messages` 详解(2026-05-25 加)

**形态**:`list[Message]` —— 跟 loop 内部 messages 同形 (`Message(role, content,
tool_calls?, tool_call_id?)`)。**默认空 list**。

**用途**(两类):
1. **多轮 chat history**:每个新 user turn 把过去几轮 messages 作为
   `prior_messages` 一起传,`user_message` 是这一轮新的输入
2. **honesty / correction re-run**:LLM 上一轮 final_text 不达标,把上一轮
   `assistant(text)` 塞进 `prior_messages`,`user_message` 写
   `"runtime correction: ..."` 再跑一遍(baizhi-agent 实测路径,见
   baizhi `agent_kit_backend.py`)

**Compose 顺序**:`_compose_messages` 把消息组成 `[system?, *prior_messages, user]`。

**Invariants**(`RunRequest.__post_init__` 校验,构造时挂,不让脏 history 进
loop 后被 provider 400):
- `prior_messages` 不能含 `role="system"` —— system 走 `system_prelude` 或
  `Runner(system_prelude=...)`,独立到 conversation history 之外
- `tool_call ↔ tool_result` 配对完整(复用 `context.py::_assert_tool_pairs_intact`):
  每个 `role="tool"` 的 message 必须有 prior `role="assistant"` 含同 id 的
  tool_call
- 末尾如果是 `assistant + tool_calls`,后面必须紧跟对应的 tool messages
  (上面那条 invariant 顺便覆盖)

**Out of scope**(use site policy,SDK 不绑):
- **history 压缩 / 摘要 / 滑动窗口 cutoff** —— 选哪个 LLM 摘要、cutoff 多少、
  失败 fallback,都是 product 决策。需要的可以:
  1. use site 在构造 `RunRequest` 前自己跑 `compress(history) → prior_messages`
     (典型:slide window keep recent N + summarize older,见 baizhi-agent
     `baizhi_agent_runtime/history.py`)
  2. 或者用 `ContextCompactor`(loop 内 every-round 自动 compact,内置
     `TruncatingCompactor` 零 LLM 成本)
- **多模态 content block** —— Stage 0-5 维持 `str` content(Q3 决议),跟
  `user_message` / loop 内部 messages 同步演进

**spec 关联**:
- § 11.c `_assert_tool_pairs_intact` 实现 + 边界用例
- § 8.7 `ContextCompactor` Protocol(运行时压缩)— 跟 `prior_messages`
  正交:前者是 use site 给 fresh run 的 history,后者是 loop 内动态 compact

#### 3.7.2 `cancel_check` 详解(2026-05-25 加,spec gap #2 修复)

**形态**:`cancel_check: Callable[[], bool] | None = None`(默认 None)。

**用途**:外部 poll-based cancel。loop 在两个点 poll:
1. 每 round 顶部(provider.chat 之前)
2. 每 tool dispatch 之前(同一 round 内多 tool 时,每个 tool 前各 poll 一次)

返 `True` → emit `cancelled` event(payload `{"round": N, "reason": ...}`)+
loop return。其中 `reason` 4 个 known values:

| reason | 触发条件 |
|---|---|
| `external` | `ToolCallContext.cancel.is_set()`,round 顶部 check |
| `external_mid_tool` | `ToolCallContext.cancel.is_set()`,tool dispatch 前 check |
| `cancel_check` | `RunRequest.cancel_check()` returns True,round 顶部 |
| `cancel_check_mid_tool` | `RunRequest.cancel_check()` returns True,tool dispatch 前 |

**跟 `ToolCallContext.cancel` 的关系**:正交并存。两者顺序 check:`ctx.cancel`
先(reason 用 `external` / `external_mid_tool`),`cancel_check` 后。同时
触发 → `ctx.cancel` reason 胜出(更精确,因为它通常是 hook 内 set 的精准
信号)。

**两种用法分工**:
- `ctx.cancel`(asyncio.Event):**in-loop / in-hook** cancel。hook 内 set
  可以让 toolset 的 in-flight asyncio.task 立刻感知(通过 `await
  ctx.cancel.wait()` 之类);per-tool 粒度,可以 reset 后再用
- `cancel_check`(Callable):**外部 process / UI 按钮** cancel。loop 主动
  poll,不要求 caller 维护 asyncio.Event 引用。典型场景:baizhi UI
  "Cancel run" 按钮通过 closure capture run_id,application 层维护
  `{run_id: cancel_flag}` dict,closure 读取

**异常处理**:`cancel_check` raise → loop 吞掉 + 打 WARNING log(模块
`agent_kit.loop`)+ treat as False(run 继续)。理由:`cancel_check` 是
user code,频繁 poll(每 round + 每 tool),一次异常不该爆掉整 run;但
不能 silent,日志让运维看见。

**多 tool dispatch 行为**:同一 round 内 LLM 返多 tool_calls,loop 在**每个
tool dispatch 前**都 poll 一次。所以:`cancel_check` 第 N 次 poll 返 True
→ tool[N] 不执行,前面 N-1 个已经跑了。tool[N] 起的事件不发,直接 emit
cancelled。

#### 3.7.3 `max_tokens` 详解(2026-05-25 加,spec gap #3 修复)

**形态**:`max_tokens: int | None = None`(默认 None)。

**行为**:loop 每次 `provider.chat(...)` 调用时透传:

```python
response = await self._provider.chat(
    messages, tools_this_round,
    temperature=request.temperature,
    max_tokens=request.max_tokens,
)
```

None → provider 用自己 default(LiteLLM / Anthropic / OpenAI 各家 default
通常 4096)。非 None → provider 按 caller 给的值。SDK **不替 caller 决策**
合法范围(0 / 负数 / 巨大值 都如实传 —— provider/vendor 决定怎么 handle)。

**为啥之前缺失**:历史 oversight。`LlmProvider` Protocol 早就定义了
`max_tokens: int | None = None` 参数,但 `RunRequest` 没字段、loop 没传。
caller 设 `RunRequest(...)` 时无 max_tokens 字段,只能 default(provider
自己 default)。**baizhi 切片 D 实施时发现这条 "silent loss"**:
`LlmAgentRunner(provider, toolsets, max_tokens=N)` 构造时设的 N 会**完全
不生效**(adapter 收 None → baizhi.chat 用 default 1200)—— 调用方 grep
0 处真用,所以无 impact,但 contract loss 修了才干净。

**适用场景**:
- eval grader prompt:限 max_tokens=200 省 cost
- summarizer prompt:限 max_tokens=300-500 控成本
- long-form skill(deepresearch):放开到 8000

**配套测试**:`tests/test_max_tokens.py`(4 tests):default None 透传 /
custom 512 透传 / multi-round 每次都透传 / max_tokens=0 edge case 如实传。

### 3.8 RunResult(Q4 决议:`run_to_completion` 返回)

```python
@dataclass
class RunResult:
    final_text: str | None       # None iff 没有 final_text event(只可能因为 error/cancel)
    events: list[Event]
    rounds_used: int
    cancelled: bool
    error: dict | None           # error event 的 payload;None == 成功
```

---

## 4. Provider 契约(provider.py)

### 4.1 LlmProvider Protocol

```python
class LlmProvider(Protocol):
    name: str    # MUST 非空,推荐小写下划线:"minimax_m27" / "litellm_openai"

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LlmResponse:
        """非流式调用。MUST 返回完整 LlmResponse。"""

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LlmDelta]:
        """流式调用。MUST yield 至少一个 LlmDelta(可能是空的 finish marker)。
        provider 不支持 stream 时 MUST raise NotImplementedError —— loop 会在
        request.stream=True 但 provider 不支持时直接 fail-fast(error event)。"""
```

### 4.2 ToolSchema

```python
@dataclass(frozen=True)
class ToolSchema:
    name: str                    # MUST 符合 ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$ 或带 mcp__ 前缀
    description: str             # 给 LLM 看,推荐 ≤ 200 字符
    parameters: dict[str, Any]   # JSON Schema(draft 2020-12),MUST 是 object 类型
```

### 4.3 LlmResponse

```python
@dataclass
class LlmResponse:
    text: str                       # "" 当只有 tool_calls 时
    tool_calls: list[ToolCall]      # [] 表示 final
    usage: dict[str, Any]           # {prompt_tokens, completion_tokens, total_tokens, cost?}
    raw: dict[str, Any]             # provider 原始响应(留 trace / debug)
    finish_reason: str | None       # "stop" / "tool_calls" / "length" / ...
```

### 4.4 LlmDelta

```python
@dataclass
class LlmDelta:
    text_delta: str | None = None
    tool_call_delta: ToolCall | None = None    # 完整一次 tool_call,不是 partial
    # ↑ 偷懒决策:partial tool_call delta 跨家差异大(OpenAI / Anthropic / MiniMax 各有
    #   各的增量协议),Stage 1 让 provider 自己 buffer 完整后再 yield。后期再拆细
    finish_reason: str | None = None    # 最后一个 delta 带,其他 None
    usage: dict[str, Any] | None = None # 仅最后一个 delta 带
```

### 4.5 错误语义

- Provider 实现方 **SHOULD** 在 transient error(超时、429)自己重试 N 次后才向上抛
- Provider **MUST NOT** 把 transport error 包装成 LlmResponse(应抛异常)
- LLM 返回的 "error in completion" 文本 **是** 正常 LlmResponse(loop 会 emit `llm_response` event,LLM 自己自纠或调用方决定)

---

## 5. Toolset 契约(toolset.py)

### 5.1 BaseToolset

```python
class BaseToolset(ABC):
    name: str    # MUST 在 Runner 的所有 toolsets 集合内唯一(命名冲突 → 启动期 raise)

    @abstractmethod
    def build_schemas(self) -> list[ToolSchema]:
        """静态 schema 列表。无 request 上下文场景下被调(例如 caller 自己
        建 Router 而不经 AgentLoop;或 build_schemas_for_request 的默认实现)。"""

    def build_schemas_for_request(
        self, request: "RunRequest"
    ) -> list[ToolSchema]:
        """**per-run 动态 schema**(2026-05-24 Stage 5 修订,详见 § 5.4)。

        默认实现:`return self.build_schemas()`(静态 toolset 无需 override)。

        想 per-run 过滤 / 动态生成的 toolset(例如 baizhi SkillToolsetCatalog
        按 `request.skills` 暴露 skill_* 工具;`FilteredMcpToolset` 按 allow-list
        砍 MCP 工具)override 这个方法。

        Router 在每次 `AgentLoop.run()` 入口调它,所以:
        - schemas 缓存可以放在 toolset(常 case),也可以每次按 request 重新算
        - 同一个 toolset 实例跨多个 run **可以**返回不同 schemas
        - 命名冲突检测每个 run 重做(便宜)
        """
        return self.build_schemas()

    @abstractmethod
    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        """MUST 总是返回 ToolResult,不抛异常 —— 异常应内部 catch 后封到 is_error=True。
        Router 会在 toolset 抛异常时帮忙 catch + 转 ToolResult(防御),
        但 toolset 自己应该做这件事(更早的语义、更友好的错误文案)。"""

    async def aclose(self) -> None:
        """Runner.run 结束时按注册顺序 reverse 调用。默认 no-op。"""
        return None
```

### 5.2 ToolCallContext

```python
@dataclass
class ToolCallContext:
    # `tenant_id` 已删除(2026-05-25 修订);见 § 3.7 RunRequest
    # `skill_name` 已删除(2026-05-27 修订):无 toolset 实际读它;
    #   per-tool free-form metadata 走 `run_state` dict 即可
    # `storage` 已删除(2026-05-27 修订):YAGNI —— 无内置或 contrib
    #   toolset 用过 ctx.storage;需要持久化路径的 toolset 自己 hardcode
    run_id: str
    cancel: asyncio.Event         # toolset 长任务 SHOULD 周期性 check
    workspace: Path               # 见 § 9.3:ephemeral 模式 = SDK 自建/自删的子目录;
                                  # callable 模式 = 使用方注入的持久目录,SDK 不动
    emit: Callable[[Event], None] # toolset 内部进度事件,event_id 由 toolset 申请
    workspace_ephemeral: bool = True
    # ↑ True = workspace 是 SDK 自建,run 结束 rmtree;toolset 不应在此跨 run 缓存
    # ↑ False = 使用方通过 Runner(workspace=callable) 注入持久目录;toolset
    #   可放心物化 + 缓存(skill files / dependency cache 等)
    run_state: dict[str, Any] = field(default_factory=dict)
    # ↑ free-form per-run scratchpad shared across toolsets / hooks
```

### 5.3 ToolsetRouter

```python
class ToolsetRouter:
    def __init__(
        self,
        toolsets: list[BaseToolset],
        *,
        request: "RunRequest | None" = None,
    ) -> None:
        """启动期检测:
        1. 各 toolset.name 唯一(否则 raise ValueError)
        2. 跨 toolset 的 ToolSchema.name 无冲突(否则 raise ValueError)

        `request`:Stage 5 修订(§ 5.4)。
        - None(默认)= 调 `toolset.build_schemas()` 静态绑定。
          兼容 caller 自己建 Router 而不经过 AgentLoop 的场景。
        - RunRequest = 调 `toolset.build_schemas_for_request(request)`,
          支持 toolset 按 request 过滤 / 动态生成 schemas。AgentLoop 每个
          run 用这条路径。
        """

    def all_schemas(self) -> list[ToolSchema]:
        """合并所有 toolset 的 schema(已按 init 时的 request 模式确定)。"""

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        """根据 call.name 路由。
        - 未知 name → ToolResult(is_error=True, content="ERROR: unknown tool ...")
        - toolset.execute 抛异常 → ToolResult(is_error=True, content="ERROR: <exc>") + emit error event"""

    async def aclose(self) -> None:
        """按 reverse(registration order)调用各 toolset.aclose,异常 swallow + log。"""
```

### 5.4 静态 vs per-run schema 决议(Stage 5 修订 2026-05-24)

**问题**:`SkillCatalogToolset` 想"只暴露 enabled_skills 对应的 skill_* 工具",
`McpToolset` 在 multi-tenant 场景想按 allow-list 过滤可见的远程工具 ——
都需要 toolset 在 build_schemas 时**看到 request**。原 spec `build_schemas()`
无参,做不到。

**对比方案**:

| 方案 | 描述 | 否决理由 |
|---|---|---|
| 完全静态(现状) | `build_schemas()` 一次绑定 | baizhi/multi-tenant 场景做不到 |
| 完全动态 | `build_schemas(request)` 必接 request | MCP / 其他静态 toolset 被迫接无用参数;`build_schemas()` 那条"推荐返回同样 list"指引失去意义 |
| `toolsets_provider: Callable[[req], list]` | Runner 接 callable,per-run new toolsets 列表 | MCP toolset connect 成本高,per-run new 不可接受;复杂度转嫁使用方 |
| **C(本节决议)**:双方法 opt-in 动态 | 加 `build_schemas_for_request(request)` 默认 delegate 到静态 | **采用** —— 静态不动,动态 opt-in |

**采用方案 C**:

- 新增 `BaseToolset.build_schemas_for_request(request) -> list[ToolSchema]`,
  默认 `return self.build_schemas()`
- `ToolsetRouter.__init__` 加 `request: RunRequest | None = None`;给了就走
  per-request 路径
- **AgentLoop 每个 `run()` 入口重建 Router**(Router 从 `__init__` 移到 `run()`
  开头),所以同一个 AgentLoop 实例跨多 run 可以拿到不同的 tool 集合
- toolset 实例本身 **不**被 SDK new-per-run —— 还是 caller 一次性给定;
  per-run 变化的是它 advertise 的 schemas
- 命名冲突检测每个 run 重做(toolsets × schemas 数量级,便宜)
- `AgentLoop.aclose()` 改为直接 walk `self._toolsets` 反序关闭(原本委托给
  router.aclose,因为 router 是 per-run 的,它不再合适持有 close 责任)

**支持的过滤模式**(by example,SDK 不内置):

```python
# baizhi: SkillCatalogToolset 按 request.enabled_skills 暴露 skill_* 工具
class SkillToolsetCatalog(BaseToolset):
    def build_schemas(self) -> list[ToolSchema]:
        return []   # 没 request 时退化为空(或者全集 — 实现选择)
    def build_schemas_for_request(self, request) -> list[ToolSchema]:
        return [self._make_skill_tool_schema(s) for s in request.enabled_skills]

# 多租户:按租户 allow-list 过滤 MCP 工具
class FilteredMcpToolset(McpToolset):
    def __init__(self, config, *, tenant_tool_acl):
        super().__init__(config)
        self._acl = tenant_tool_acl  # dict[tenant_id, set[remote_tool_name]]
    def build_schemas_for_request(self, request) -> list[ToolSchema]:
        full = self.build_schemas()
        allow = self._acl.get(request.tenant_id, None)
        if allow is None: return full
        return [s for s in full if s.name.split("__", 2)[-1] in allow]
```

SDK 本身不知道 "enabled_tools" / "tenant ACL" 这些业务概念,只提供 hook。

---

## 6. Skill 契约(skill.py)

### 6.1 SKILL.md 格式

```markdown
---
name: paper_review
description: 给 ICML/NeurIPS 论文打分,7 维度评分 + 总评
version: 1.2.3
tools: [skill_storage]    # 可选,声明依赖的 toolset name
inputs:                   # 可选,JSON Schema 描述期望的输入字段
  type: object
  properties:
    paper_url: { type: string }
  required: [paper_url]
---

# Paper Review Skill

(正文 markdown,LLM 通过 load_skill 工具按需取)
```

- frontmatter **MUST** 是合法 YAML
- `name` / `description` / `version` 三字段 **MUST** 都有
- `version` 推荐 semver,但 SDK 不校验
- frontmatter 后必须紧跟一个空行,然后是 body

### 6.2 SkillFrontmatter

```python
@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str
    version: str
    tools: list[str] = field(default_factory=list)
    inputs: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)
```

### 6.3 Skill

```python
@dataclass(frozen=True)
class Skill:
    name: str
    frontmatter: SkillFrontmatter
    body: str                          # 不含 frontmatter
    files: dict[str, bytes]            # 同包附带的辅助文件;键是相对路径
    storage_root: Path                 # 持久存储根目录
```

### 6.4 parse_frontmatter

```python
def parse_frontmatter(md: str) -> tuple[SkillFrontmatter, str]:
    """解析 SKILL.md。
    - 第一行 MUST 是 '---\\n',否则 raise ValueError
    - 第二个 '---' 之间是 YAML
    - 之后是 body(strip leading whitespace + 一个换行)
    """
```

### 6.5 SkillRegistry(ABC)

```python
class SkillRegistry(ABC):
    # 2026-05-25 修订:所有方法删除 `tenant_id` 参数。SDK 不带 tenant
    # 概念;每个 Registry 实例**已经是** pre-scoped 的(application 层每
    # tenant new 一份 registry,通过 closure / instance 绑定)。

    @abstractmethod
    async def list(self) -> list[SkillFrontmatter]: ...

    @abstractmethod
    async def load(self, name: str, version: str | None = None) -> Skill:
        """Q2 决议:version=None 拿 latest;具体值拿 immutable publish。
        - name 不存在 → raise KeyError
        - version 给了但不存在 → raise KeyError"""

    @abstractmethod
    async def save_draft(
        self, name: str, md: str, files: dict[str, bytes]
    ) -> None: ...

    @abstractmethod
    async def publish(self, name: str) -> str:
        """draft → immutable version,返回新版本号。
        - 无 draft → raise FileNotFoundError"""
```

### 6.6 enabled_skills 解析

```python
def parse_skill_ref(ref: str) -> tuple[str, str | None]:
    """'paper_review'           → ('paper_review', None)
       'paper_review@1.2.3'     → ('paper_review', '1.2.3')
       'paper_review@'          → raise ValueError
       '@1.2.3'                 → raise ValueError"""
```

### 6.7 SkillCatalogToolset(内置)

`name = "skill_catalog"`

暴露三个工具:

| Tool name | 参数 | 返回 |
|---|---|---|
| `list_skills` | `{}` | JSON: `[{name, description, version}, ...]` |
| `load_skill` | `{name: str, version?: str}` | SKILL.md body 文本 |
| `load_skill_resource` | `{name: str, path: str, version?: str}` | 文件内容(text 或 base64 取决于是否 binary) |

**Progressive disclosure 策略**:
- Runner.run 启动时,把 enabled skills 的 `name / description / version` 拼进 system prompt(§ 10)
- 这给 LLM 一个**索引**,不浪费 token 在 body 上
- LLM 真想读 body,调 `load_skill`

---

## 7. MCP 集成(mcp.py)

### 7.1 McpServerConfig

```python
@dataclass
class McpServerConfig:
    name: str                                          # 用作工具命名前缀
    transport: Literal["stdio", "sse", "http"]
    command: list[str] | None = None                   # stdio
    url: str | None = None                             # sse / http
    headers: dict[str, str] = field(default_factory=dict)   # 支持 ${VAR}
    env: dict[str, str] = field(default_factory=dict)       # 支持 ${VAR}

    def __post_init__(self):
        if self.transport == "stdio" and not self.command:
            raise ValueError(...)
        if self.transport in ("sse", "http") and not self.url:
            raise ValueError(...)
```

> **修订 2026-05-24**:删除 `lifecycle: McpLifecycle` 字段。理由见 § 7.2。
> 原 4 档枚举见 `proposal.md § 八` 历史 + errata。

### 7.2 Lifecycle 设计 —— 实例生命周期即 session 生命周期

**SDK 不枚举 lifecycle**。`agent-kit` 采用 ADK 的模式:

- 一个 `McpToolset` 实例 == 一个 MCP server == 一个 MCP session
- session 的生命周期 == `McpToolset` 实例的生命周期
- 使用方控制何时构造、何时 `aclose` —— **这就是 lifecycle 的全部控制点**

**为什么不枚举**:之前考虑过 4 档(PER_CALL / PER_RUN / PER_TENANT / GLOBAL),
但 `PER_TENANT` 把"tenant"这一上层概念塞进 SDK 命名空间,与 § 1 / § 15 "SDK
不做多租户"的边界直接冲突。`GLOBAL` 也暗示 SDK 持有进程级缓存,挤占使用方
的部署决策权。

ADK / OpenHarness / baizhi-agent / fam-runtime **均无 lifecycle 枚举**:
- ADK:McpToolset 实例生命周期 = session 生命周期。使用方通过"何时 new"控制
- OH:进程单例(部署形态固定)
- baizhi-agent:per-call(代码内 hardcode)
- fam-runtime:per-family-id 缓存(代码内 hardcode)

**4 档行为仍可全部实现**,只是控制点回到使用方:

| 想要 | 使用方怎么写 |
|---|---|
| **per-call** 强隔离 | 包一层 `EphemeralMcpToolset`,每次 `execute` 内部 `McpToolset(cfg)` + use + `aclose` |
| **per-run** 默认 | `Runner(toolsets=[McpToolset(cfg)])`;Runner 在 run 结束统一 `router.aclose()` |
| **per-tenant** | 使用方维护 `dict[tenant_id, McpToolset]`,run 时取实例传给 Runner |
| **global** | 模块级单例 `MCP_GITHUB = McpToolset(cfg)`,所有 run 共享 |

### 7.3 ${VAR} 替换规则

- `${VAR}` 形式,**MUST** 在 McpToolset 创建时一次性完成替换
- 缺失变量 → raise KeyError(fail-fast,不留隐患)
- 替换源:`os.environ` + 可选的 `McpToolset(secrets: dict)` 参数(后者优先)

### 7.4 工具命名

- 暴露给 LLM 的工具 name = `mcp__<server.name>__<remote_tool_name>`
- 双下划线分隔 —— 与 baizhi-agent / OpenHarness / fam-runtime 共识
- `server.name` **MUST** match `^[a-z][a-z0-9_]{0,31}$`,不含双下划线

### 7.5 McpToolset

```python
class McpToolset(BaseToolset):
    def __init__(
        self,
        config: McpServerConfig,
        *,
        secrets: dict[str, str] | None = None,
        tool_filter: list[str] | Callable[[ToolSchema], bool] | None = None,
    ):
        self._config = config        # ${VAR} 已在此处一次性替换完(§ 7.3)
        self.name = f"mcp__{config.name}"
        self._tool_filter = tool_filter   # § 7.5.2
        self._session = None
        self._schemas: list[ToolSchema] = []
        self._connected = False

    async def connect(self) -> None:
        """启动 transport + ClientSession,initialize + list_tools,缓存 schemas。
        idempotent —— 多次调用只 connect 一次。Runner 在 setup 阶段自动调,
        直接用 AgentLoop 的使用方需自己 `await toolset.connect()`。"""

    def build_schemas(self) -> list[ToolSchema]:
        """返回 connect 时缓存的 schemas,经 `tool_filter` 过滤(§ 7.5.2)。
        未 connect 调用 raise RuntimeError。"""

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        """复用 self._session call tool,返回。
        - isError=True 的 MCP 响应 → ToolResult(is_error=True, content=msg)
        - 传输 / SDK 异常 → ToolResult(is_error=True, content=f"ERROR: {exc}")
        - 未 connect 调用 → ToolResult(is_error=True, content="ERROR: not connected")"""

    async def aclose(self) -> None:
        """关闭 self._session + transport(若已 connect)。idempotent。"""
```

#### 7.5.1 Lazy connect 与 sync `build_schemas` 的张力(Stage 4 修订 2026-05-24)

**问题**:`BaseToolset.build_schemas` 是同步方法(spec § 5.1),Router init 期会
立刻调用做命名冲突校验。但 MCP connect / list_tools 是 async。
原 spec § 7.5 草稿写"在 __init__ 里 `asyncio.run` 同步起 session" —— 这在
event loop 已经跑(Runner / 测试)的环境会直接 RuntimeError。

**决议**:**显式 `async connect()` + Runner 自动 pre-warm**。

- `McpToolset.connect()` 是 async,完成 transport 启动 + session.initialize
  + list_tools,把 schemas 缓存到实例字段
- `build_schemas()` 仅返回缓存;未 connect 时 raise `RuntimeError("call await
  toolset.connect() first")`(fail-fast,设计错而非数据错)
- **Runner 在 setup 阶段**遍历 `self._toolsets`,对每个有 `connect` 协程的
  实例 await 它(`hasattr(ts, "connect") and inspect.iscoroutinefunction
  (ts.connect)`)—— 让"用 Runner"的常路径无样板
- 使用方直接用 AgentLoop(没经 Runner)时,自己负责 `await
  toolset.connect()` 后再 new Loop
- `connect()` MUST idempotent:多次调用只生效一次,允许跨 run 复用(per-tenant
  / global 场景),搭配 `aclose()` 的 idempotent 一起构成"安全重入"

**为什么不改 build_schemas 为 async**:那是 spec 的下游断点 —— Router / 所有
现有 toolset / 所有现有测试都要跟着改。"explicit connect + runner pre-warm"
是侵入面最小的方案,且符合 ADK 的 toolset awakening 范式。

#### 7.5.2 `tool_filter` 选择性暴露 tool(Stage 5 修订 2026-05-24)

**问题**:一个 MCP server 可能 advertise 10+ tools,但 agent 只需要其中
几个(如 filesystem MCP 的 read_* 给只读 agent,write_* 给作者 agent;
multi-tenant 上线只暴露允许的子集)。LLM 看见越多无关 tool,context 越
大、选错越多。ADK 主推 `tool_filter`,我们对齐。

**形态**:`McpToolset(cfg, tool_filter=...)`,接受:

- `None`(默认):暴露全部 tools(原行为)
- `list[str]`:**白名单**,按 remote tool name 匹配(不含 `mcp__<server>__`
  前缀)。例:`tool_filter=["search", "fetch"]`
- `Callable[[ToolSchema], bool]`:**谓词**,对每个 ToolSchema 调一次,
  True 保留。例:`tool_filter=lambda s: not s.name.endswith("_write")`

**实现位置**:`build_schemas()` 内部过滤(filter 是 ctor-bound,不依赖
request,无需走 `build_schemas_for_request`)。Router 的命名冲突 / 路由
表只看 build_schemas 暴露出来的子集 —— filtered-out tool **不会**进
Router,LLM 也看不到。

**`execute()` 不重复 check**:Router 只路由 advertised tools,filtered-out
的 call 路由不到 McpToolset(Router 会返回 "unknown tool" error)。

**未来更高级的 per-request 过滤**(per-tenant ACL / 按 user 角色):
仍然走 `build_schemas_for_request(request)` override(spec § 5.4),
跟 `tool_filter` 不冲突。`tool_filter` 是简单白名单,per-request 是动态
策略 —— 两个都可以存在,filter 先生效,然后 per-request 再过一遍。

#### 7.5.3 Convenience factories(Stage 5 修订 2026-05-24)

把 transport 跟 `McpServerConfig` 嵌套省掉,3 个 classmethod 工厂跟 ADK 的
`StdioConnectionParams / SseConnectionParams / StreamableHTTPConnectionParams`
对位:

```python
McpToolset.http("brave-search", url="...", headers={...}, tool_filter=[...])
McpToolset.sse("ws", url="...", headers={...})
McpToolset.stdio("github", command=["mcp-github"], env={...})
```

每个工厂只接受**该 transport 真用得到的 kwargs**(http/sse 没有 command,
stdio 没有 url/headers),IDE 补全更准。`secrets` / `tool_filter` /
`connect_timeout` 三个工厂都有,共享语义。

**老路径仍然 work**:`McpToolset(McpServerConfig(name=..., transport=...))`
继续是 supported entry point —— 工厂只是糖,不破坏现有 caller。

### 7.6 便利函数(可选)

```python
def toolsets_from_configs(
    configs: list[McpServerConfig],
    *,
    secrets: dict[str, str] | None = None,
) -> list[McpToolset]:
    """批量构造。等价于 `[McpToolset(c, secrets=secrets) for c in configs]`。
    存在意义:语义化的工厂函数,非必需。"""
```

---

## 8. AgentLoop 核心(loop.py)

### 8.1 输入 / 输出

```python
class AgentLoop:
    def __init__(
        self,
        provider: LlmProvider,
        toolsets: list[BaseToolset],
        *,
        default_max_rounds: int = 10,
        system_prelude: str = "",
    ) -> None: ...

    async def run(
        self,
        request: RunRequest,
        ctx: ToolCallContext,
    ) -> AsyncIterator[Event]: ...
```

### 8.2 执行规则

按 § 2 的 sequence 图执行,加以下不变量:

- **每轮严格顺序**:`round_start` → `llm_request` → [`llm_delta`*] → `llm_response` → (终止 OR (`tool_call` → `tool_result`)*) → `round_end`
- **终止条件优先级**(从高到低):
  1. cancel 已触发 → `cancelled` event
  2. provider/toolset 异常 → `error` event
  3. `response.tool_calls == []` → `final_text` event
  4. `round_idx == max_rounds - 1` 且 `response.tool_calls` 非空 → 不该发生(因为这一轮 tools 被屏蔽 → response.tool_calls 必为 [])
- **取消 check 点**:
  - 每轮 round_start 前
  - provider.chat 调用前(因为 chat 不会自己 check)
  - 每个 tool_call 执行前

### 8.3 最后一轮屏蔽 tools

```python
tools_this_round = schemas if round_idx < request.max_rounds - 1 else None
```

**为什么**:防止 LLM 永远调工具不收尾。最后一轮强制 LLM 出文本。Stage 1 起就实现。

### 8.4 跑满 max_rounds 但 LLM 仍未出文本

不可能(最后一轮 tools=None,LLM 必出 text)。若发生(provider bug),emit `error` event with `stage="loop"`,return。

### 8.5 Stream 模式(Q1 决议)

```python
if request.stream:
    text_buf = []
    tool_calls_buf = []
    last_finish = None
    last_usage = None
    yield Event(kind="llm_request", payload={"messages_count": ..., "tools_count": ...})
    async for delta in provider.chat_stream(messages, tools_this_round, ...):
        yield Event(kind="llm_delta", payload={
            "text_delta": delta.text_delta,
            "tool_call_delta": delta.tool_call_delta.to_dict() if delta.tool_call_delta else None,
            "finish_reason": delta.finish_reason,
        })
        if delta.text_delta:
            text_buf.append(delta.text_delta)
        if delta.tool_call_delta:
            tool_calls_buf.append(delta.tool_call_delta)
        last_finish = delta.finish_reason or last_finish
        last_usage = delta.usage or last_usage
    response = LlmResponse(text="".join(text_buf), tool_calls=tool_calls_buf,
                            usage=last_usage or {}, raw={}, finish_reason=last_finish)
    yield Event(kind="llm_response", payload=response.to_dict())
else:
    yield Event(kind="llm_request", payload={...})
    response = await provider.chat(messages, tools_this_round, ...)
    yield Event(kind="llm_response", payload=response.to_dict())
```

**stream 模式下 `llm_response` 仍 emit** —— 下游持久化逻辑不分叉。

### 8.6 Hooks(切面 + 决策注入)

**问题**:使用方常需要在 LLM / tool 调用前后插一脚 —— 权限检查、PII 脱敏、
预算控制、童锁、mock 测试、内容 validate 等。这些是**横切关注点**(跨多
provider / toolset 统一应用),用装饰器逐个包很啰嗦。

**设计**:1 个 `Hook` 基类 + 4 个 no-op 默认方法。

```python
class Hook:
    async def before_model(self, ctx, messages, tools) -> LlmResponse | None: ...
    async def after_model(self, ctx, response) -> LlmResponse | None: ...
    async def before_tool(self, ctx, call) -> ToolResult | None: ...
    async def after_tool(self, ctx, call, result) -> ToolResult | None: ...
```

注册:`AgentLoop(..., hooks=[HookA(), HookB()])` 或 `Runner(..., hooks=[...])`。

**短路语义**(对 4 方法一致):
- 按注册顺序遍历 hook list
- 第一个返回非 None 的 hook **短路**:用其返回值替代正常路径,后续同名 hook 跳过
- 全 None 则正常进 provider.chat / toolset.execute

**Loop 集成位置**(扩展 § 2 的 sequence):

```
每轮:
  for hook in hooks:
      r = await hook.before_model(ctx, messages, tools)
      if r is not None: response = r; break
  else:
      response = await provider.chat(messages, tools)
      yield Event(llm_response, ...)
  
  if short-circuited:
      yield Event(llm_short_circuited, {by_hook: "<class>", response: ...})
  
  for hook in hooks:
      r = await hook.after_model(ctx, response)
      if r is not None: response = r; break
  
  if not response.tool_calls: yield final_text; return
  
  for call in response.tool_calls:
      yield Event(tool_call, ...)
      for hook in hooks:
          r = await hook.before_tool(ctx, call)
          if r is not None: result = r; break
      else:
          result = await router.execute(call, ctx)
      
      if short-circuited:
          yield Event(tool_short_circuited, {by_hook, call, result})
      
      for hook in hooks:
          r = await hook.after_tool(ctx, call, result)
          if r is not None: result = r; break
      
      yield Event(tool_result, ...)
      messages.append(result.to_tool_message())
```

**异常**:hook raise → loop catch → emit `Event(kind="error", stage="hook",
payload={hook_class, method, exc_type, message, traceback})` → return。
**不 swallow**(对齐 ADK,反对 baizhi-agent / fam-runtime 的 swallow)。

**关于"为什么没有 before_round / after_round"**:
agent-kit 的 round = "一次 LLM call + 0 个或多个 tool calls"。
- before_model 就是 before_round 的语义(每轮 LLM 调用前一次)
- after_model 是 LLM 返回后、tool dispatch 前(信号还没收齐 —— 不够 "after round")
- "真正的 after_round"(LLM 返回 + 所有 tool 跑完后看全局)曾经考虑过,被砍 ——
  详见 proposal.md § 八的迭代,或本次决策的 commit message

### 装饰器 vs hook 选择指南

| 关注点 | 推荐 | 理由 |
|---|---|---|
| **跨多 toolset / provider 统一规则**(权限 / quota / 童锁) | **hook** | hook 天然横切;装饰器要每个 wrap 一遍 |
| **单一 toolset 内聚的关注点**(GitHub MCP 重试 / 降级) | **装饰器**(`class RetryingMcpToolset(McpToolset)`) | 内聚 + 装饰器实例自带状态空间 |
| **单一 provider 内聚的关注点**(cost tracking / token rate limit) | **装饰器**(`class BillingProvider(LlmProvider)`) | 拿到 raw response 信息更全;每个 run new 一个就无 state 泄露 |
| **复杂跨轮状态机**(收集 N 轮再综合判断) | **wrap `runner.run`**(caller 自己外层循环) | SDK 单 run 是原子单元;跨 run 的逻辑在 caller 控制流 |
| **per-tool PII 脱敏** | 装饰器(scope 清晰) | 装饰器 → 该 toolset 才 redact;hook → 所有 tool 都过一遍 |
| **per-LLM PII 脱敏** | 装饰器(同上) | 同理 |
| **mock 测试**(替换 tool 行为) | hook(`before_tool` 短路) | 测试代码用 hook 比 monkey-patch toolset 简洁 |

**口诀**:**横切上 hook,内聚上装饰器,跨轮状态机往 caller 外推**。

### 8.7 Context compaction(防 context window 爆炸)

**问题**:多轮 loop 累积 messages,大 tool 输出(file read / web fetch /
mcp payload)很快可以打爆 context window。baizhi-agent / fam-runtime 都没
解决,靠小 `max_rounds`(8-10) + 小 `max_tokens`(1024-1200)兜底 —— 一
旦遇上大 tool 输出仍会爆。

**设计**(详细见 [§ 11.b 和 § 11.c](#11b-token-估算tokenspy)):

```python
class AgentLoop:
    def __init__(
        self,
        provider, toolsets, *,
        default_max_rounds=10,
        system_prelude="",
        compactor: ContextCompactor | None = None,   # 默认 None == 不 compact
    ): ...
```

每轮 provider.chat 调用前:

```python
if self._compactor and await self._compactor.should_compact(messages, last_usage):
    new_messages = await self._compactor.compact(messages)
    _assert_tool_pairs_intact(new_messages)         # SDK 兜底,失败 raise
    yield Event(kind="context_compacted", payload={
        "before_count": len(messages),
        "after_count": len(new_messages),
        "before_tokens": estimate_messages_tokens(messages),
        "after_tokens": estimate_messages_tokens(new_messages),
        "strategy": getattr(self._compactor, "name", "<unknown>"),
    })
    messages = new_messages
yield Event(kind="llm_request", payload={...})
response = await self._provider.chat(messages, tools_this_round, ...)
last_usage = response.usage           # ← 留给下一轮 should_compact 用
```

**职责划分**:
- SDK:Protocol 定义 + 一个内置 `TruncatingCompactor`(microcompact 无 LLM 成本) + `safe_split_messages` + `_assert_tool_pairs_intact` 兜底
- 使用方:若想用 LLM 摘要 / RAG 拉回 / 滑动窗口 等高级策略 → 实现 ContextCompactor Protocol,自带 LLM 调用 / 模型选择 / prompt 设计

**强约束**:
- compactor 返回的 messages **MUST** 通过 `_assert_tool_pairs_intact`,
  否则 loop raise(`stage="compactor"` 的 error event)
- compactor SHOULD 用 `safe_split_messages` helper 决定切点
- compactor SHOULD 在 `should_compact` 内**优先用** `last_usage["prompt_tokens"]`
  (API 返回的精确值),fallback 才用 `estimate_messages_tokens`

**关于 Q-CTX-2(compactor=None 行为)**:静默放行。爆 context window 是
使用方的选择(可能是 demo / 调试 / 知道场景短)。SDK 不强加保护性 warn,
保持 "minimal" 边界。

---

## 9. Runner 门面(runner.py)

### 9.1 构造

```python
class Runner:
    def __init__(
        self,
        provider: LlmProvider,
        toolsets: list[BaseToolset],            # 包括 SkillCatalogToolset / McpToolset / 自定义
        *,
        default_max_rounds: int = 10,
        system_prelude: str = "",
        compactor: ContextCompactor | None = None,
        hooks: list[Hook] | None = None,
        workspace: Path | Callable[[RunRequest, str], Path] | None = None,
    ) -> None: ...
```

`workspace`(修订 2026-05-27,合并 `workspace_root` + `workspace_provider`):

| `workspace=` | 行为 | `ctx.workspace_ephemeral` |
|---|---|---|
| `None` (默认) | `Path("/tmp/agent-kit-runs") / <run_id>`;SDK mkdir + finally rmtree | `True` |
| `Path` | `<path> / <run_id>`;SDK mkdir 子目录 + finally rmtree 子目录(父路径保留) | `True` |
| `Callable[(req, run_id), Path]` | 使用方完全掌控路径与生命周期;SDK 不 mkdir 不 rmtree | `False` |

`storage_root` 参数已删除(2026-05-27):见 § 5.2 注释,`ctx.storage` 同期下线。

> **修订 2026-05-24**:
> - 删除 `mcp_servers: list[McpServerConfig]` 参数(原来是糖)
> - 删除 `skill_registry: SkillRegistry` 参数(改由使用方手动包装为 `SkillCatalogToolset` 放进 `toolsets`)
> - `extra_toolsets` 重命名为 `toolsets`(不再"额外",而是"全部")
>
> 理由:Runner 不再为你 new toolset == Runner 不掌握 toolset 实例的 lifecycle
> == 使用方完全控制"什么时候 new、什么时候 close"。详见 § 7.2。

**典型构造**:

```python
runner = Runner(
    provider=LiteLlmProvider("minimax/MiniMax-M2.7"),
    toolsets=[
        SkillCatalogToolset(skill_registry, tenant_id="user_42"),
        *toolsets_from_configs([
            McpServerConfig(name="github", transport="stdio", command=["mcp-github"]),
            McpServerConfig(name="WebSearch", transport="http", url="..."),
        ]),
    ],
)
```

### 9.2 三 API(Q4 决议 + Stage 5 修订)

```python
async def run(self, request: RunRequest) -> AsyncIterator[Event]:
    """事件流形式。异常全部 catch,wrap 成 Event(kind="error") + return。
    SHOULD 用于服务端 / 持久化场景。"""

async def run_to_completion(self, request: RunRequest) -> RunResult:
    """聚合形式。遇 error event raise RuntimeError(包含原 exc_type / message)。
    SHOULD 用于脚本 / 测试 / 一次性 CLI 场景。"""

def run_sync(self, request: RunRequest) -> RunResult:
    """同步 wrapper —— `asyncio.run(self.run_to_completion(request))`。

    用于纯 sync 调用方(CLI、命令行脚本、Jupyter notebook 同步 cell,
    或要 incremental 迁移的 sync codebase 如 baizhi-agent)。

    MUST NOT 在已经跑着的 event loop 里调(FastAPI handler、async test、
    Jupyter async cell):detect 后 raise 友好错误,而不是让 Python 抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。

    形态参考 openai-agents Runner.run_sync;
    **不**返 Generator(若需要 sync 实时事件流,见 § 14 Stage 7+ 候选,
    类似 ADK 的"后台线程 + queue 桥接"模式,**当前不实现**)。
    """
```

### 9.3 资源生命周期(Stage 3 修订 2026-05-24)

```
Runner.run(request):
  1. allocate run_id (event-id-style; § 11)
  2. mkdir workspace = workspace_root / run_id
  3. build prelude:
       - Runner.system_prelude
       - skill catalog 段(if SkillCatalogToolset discovered + enabled_skills 非空,§ 10.1)
       - RunRequest.system_prelude
  4. build AgentLoop(provider, self._toolsets, system_prelude=composed_prelude, ...)
     ↑ AgentLoop 内部 build ToolsetRouter,做命名冲突校验
  5. build ToolCallContext (cancel = asyncio.Event(), workspace, storage, emit=no-op)
  6. try:
       async for evt in loop.run(request, ctx): yield evt
     except Exception as exc:
       yield Event(kind="error", stage="loop", payload={...}); return
     finally:
       await loop.aclose()                # 委托给 router.aclose() —— 单 router,无重复
       shutil.rmtree(workspace, ignore_errors=True)
```

> **关于"单 ToolsetRouter"**:原 spec 草稿要求 Runner 自建一份 Router 做命名
> 冲突 + aclose,与 AgentLoop 内部 Router 重复。Stage 3 修订改为
> **AgentLoop 暴露 `aclose()`** 委托给自己的 router;Runner 不再持有 Router 实例。
> 单一 Router,语义干净。

> **关于 toolset 复用**:Runner 不持有 toolset 实例 == 不掌控其生命周期。
> - 若使用方传入的是**短命** toolset(只这一次 run 用),Runner 的 `router.aclose()`
>   会在 finally 关掉,session 随之关 —— 这是 "per-run" 行为
> - 若使用方传入的是**长命** toolset(模块级单例 / 自家 cache),`router.aclose()`
>   仍会被调,但 toolset.aclose 可以 idempotent + 拒绝真关(由使用方自己实现)
>   —— 这是 "global" / "per-tenant" 行为
>
> 推荐惯例:**短命 toolset 让 Runner 关;长命 toolset 自己实现 aclose 为 no-op
> 或 refcount,使用方独立显式 close**。SDK 不强制。
```

### 9.4 取消(外部触发)

Runner **SHOULD** 暴露 `cancel(run_id)` 方法或 ctx.cancel 引用(具体形态 Stage 2 定)。

---

## 10. System prompt 组装规则

```
[Runner.system_prelude]
[RunRequest.system_prelude]

# Available Skills

You have access to the following skills. Use the `load_skill` tool to read
a skill's full instructions before invoking it.

- paper_review (v1.2.3): 给 ICML/NeurIPS 论文打分,7 维度评分 + 总评
- summarize (v2.0.0): 长文档摘要,支持中英文

# Available Tools

(由 ToolsetRouter 自动从 schemas 生成的工具列表,LLM provider 通过 tools 参数收到)

---

(然后是 user_message)
```

实现细节:
- `[Runner.system_prelude]` 和 `[RunRequest.system_prelude]` 拼接,用单个空行隔
- "Available Skills" 段当 `enabled_skills` 非空时才加
- 工具列表通过 provider 的 `tools` 参数发,不进 system prompt 文本

### 10.1 Skill catalog 来源(Stage 3 修订 2026-05-24)

Runner **不**新增 `skill_registry` 参数(保持 § 9.1 "不为你 new toolset" 的边界)。
取而代之:Runner 启动时**遍历 `self._toolsets`**,找到 `SkillCatalogToolset`
实例(`isinstance` 检测),从中读取 `_registry` 和 `_tenant_id`,调
`registry.list(tenant_id)` 拿到所有 frontmatter,再按 `request.enabled_skills`
过滤,生成 "Available Skills" 段。

边界规则:
- 若 toolsets 列表中**没有** `SkillCatalogToolset`,Available Skills 段始终为空
  (使用方必须自己把 enabled skill 描述塞进 `RunRequest.system_prelude`)
- 若有**多个** `SkillCatalogToolset`(多 tenant 场景,目前不推荐),取第一个
- `enabled_skills` 支持 `name@version` 字符串;prelude 注入用 `registry.list()` 拿
  的是 latest frontmatter,**version pin 只对 LLM 的 `load_skill` 调用生效**
  —— 因为 description 通常跨版本稳定,prelude 不值得对每个 ref 单独 `load()`
- `enabled_skills` 里指定了但 registry 没找到的 skill → 静默跳过(不 raise,
  不 emit warning event)。使用方有责任传入有效的 ref

> **为什么不加 `skill_registry` 参数**:Runner 的契约是"不 new toolset、不掌
> 控 toolset lifecycle"。如果 Runner 拿到 registry 就能自己 new
> `SkillCatalogToolset`,势必引入"Runner 是不是该新 / 该关"的歧义。改用
> discovery 把 SkillCatalogToolset 当成 prelude 数据源,语义清晰:**toolsets
> 列表既是工具源也是 prelude 元数据源**,单一真相。

### 10.2 Runner 与 ctx.workspace / ctx.emit

- `workspace`:见 § 9.1 表;ephemeral 模式 Runner mkdir + finally rmtree,
  callable 模式使用方掌控
- `ctx.storage` **已删除**(修订 2026-05-27,YAGNI:无 toolset 用过)
- `ctx.emit`:Stage 3 暂为 no-op(`lambda evt: None`)。toolsets 调它的进度事件
  会被丢弃。**Stage 3.5 / Stage 4 候选**:用 asyncio.Queue 把 emit 路由进 Runner
  yield 的事件流。当前 SDK 内置 toolset(SkillCatalogToolset)不调 emit,
  无影响

---

## 11. 事件 ID 规则

- `event_id` **MUST** 是 ULID(26 字符,按时间排序);用 `python-ulid` 包或自实现
- `parent_event_id` 规则:
  - `round_start` 的 parent = `None`(或上一轮的 `round_end`,Stage 1 用 None 简化)
  - `llm_request` / `llm_response` / `llm_delta` 的 parent = 同轮 `round_start`
  - `tool_call` 的 parent = 同轮 `llm_response`
  - `tool_result` 的 parent = 对应的 `tool_call`
  - `round_end` 的 parent = 同轮 `round_start`
  - `final_text` / `cancelled` / `error` 的 parent = 最近的 `round_start`(若有)否则 None

这一关系图让上层(baizhi-agent Traces UI)直接渲染树形。

---

## 11.b Token 估算(tokens.py)

```python
TOKEN_ESTIMATION_PADDING = 4 / 3        # 与 OpenHarness 一致

def estimate_text_tokens(text: str) -> int: ...
def estimate_messages_tokens(messages: list[Message]) -> int: ...
```

**公式**:`(len(text) + 3) // 4`,再乘 `TOKEN_ESTIMATION_PADDING` 作保守估
计。这是 OpenHarness `services/token_estimation.py:6-10` + `compact/__init__.py:75`
验证过的实用近似。Stage 1 不引 tiktoken。

**优先级**:
- **API 返回的** `LlmResponse.usage["prompt_tokens"]` 永远优先 ——
  loop 把 `last_usage` 传给 `compactor.should_compact`
- 没有 API 返回 → fallback 用 `estimate_messages_tokens`

参考依据:
- OpenHarness `services/token_estimation.py:6-10`(chars/4)
- OpenHarness `services/compact/__init__.py:75`(4/3 padding)
- ADK `apps/compaction.py:156-173`(prefer `prompt_token_count`)

---

## 11.c Safe split(context.py)

```python
def safe_split_messages(messages: list[Message], split_at: int) -> int:
    """返回 ≤ split_at 的安全 index,保证 tool_call 与 tool_result
    一定同存或同删。"""

def _assert_tool_pairs_intact(messages: list[Message]) -> None:
    """SDK 兜底校验:tool_call_id 配对完整。失败 raise ValueError。"""
```

**规则**(参考 ADK `apps/compaction.py:388-421`):

- 若 `messages[split_at].role == "tool"`,回退 split_at 到包含其对应
  `assistant.tool_calls` 的那个 message 之前
- 若 `messages[split_at-1].role == "assistant"` 且 `tool_calls` 非空,
  继续往前找,直到所有"前置 assistant tool_calls + 后续 tool messages"
  都在右侧
- 若回退到 0 还不能满足,raise(切不出安全点 == 不能 compact)

**为什么必须**:
LLM API 接收 messages 时,任何 `role="tool"` 的 message 必须紧接(或不远)
在它对应的 `assistant.tool_calls` 之后。**孤立的 tool message** 或
**没有 tool_call 配对的 assistant.tool_calls** 都会让下次 API 调用 400。

这是 4 家中 ADK 唯一显式处理的非显然 bug —— 任何 compactor 自己若忘了
处理,SDK 兜底失败会 raise,**比让用户在生产打 400 强**。

### ContextCompactor Protocol(context.py)

```python
class ContextCompactor(Protocol):
    async def should_compact(self, messages, last_usage) -> bool: ...
    async def compact(self, messages) -> list[Message]: ...
```

**实现合同**:
1. `messages[0].role == "system"` 时,返回值的 messages[0] **MUST** 保持
2. 最近 N 条 verbatim(N 由实现自定,推荐 ≥ 3)
3. tool_call / tool_result 配对 **MUST** 保持(SDK 兜底校验)

### 内置 TruncatingCompactor

```python
@dataclass
class TruncatingCompactor:
    token_budget: int = 100_000
    keep_recent_tool_results: int = 5
    placeholder: str = "[tool output omitted — older than retention window]"
```

**做法**:扫所有 `role="tool"` 的 message,从老到新,距末尾超过
`keep_recent_tool_results` 的,把 `content` 替换为 `placeholder`。
**不删 message**,只替 content —— 这样 `tool_call_id` 配对天然保持。

**适用**:tool 输出是 token bloat 主因的场景(file read / web fetch /
mcp 大 payload)。零 LLM 成本,覆盖 80% 场景。

**不适用**:user / assistant 文本本身就很长的场景(长文档喂进来) ——
那种需要 LLM 摘要,使用方自己实现 ContextCompactor。

参考依据:
- OpenHarness `services/compact/__init__.py:808-856` `microcompact_messages`

---

## 12. 测试策略

### 12.1 单元测试(每模块)

- `tests/test_types.py` —— dataclass 不变量、frozen 行为、event payload schema
- `tests/test_provider.py` —— FakeProvider 实现 Protocol;chat / chat_stream 契约
- `tests/test_toolset.py` —— ToolsetRouter 冲突检测、execute 路由、aclose 顺序
- `tests/test_skill.py` —— parse_frontmatter 各种边界、parse_skill_ref 解析
- `tests/test_mcp.py` —— McpServerConfig 校验、${VAR} 替换、命名规则
- `tests/test_tokens.py` —— estimate_text_tokens / estimate_messages_tokens
  公式 + 边界(空 string、纯 tool_calls assistant、多 tool_result)
- `tests/test_context.py` —— safe_split_messages 全部 ADK 边界、
  _assert_tool_pairs_intact 校验、TruncatingCompactor 行为
  (token_budget 触发 / keep_recent 保留 / placeholder 替换不影响配对)
- `tests/test_hooks.py` —— Hook 基类 4 方法 no-op、注册顺序遍历、
  first-non-None 短路 + 后续 hook 跳过、4 个 method 各自的短路点 emit
  对应 short_circuited event、hook 抛异常 emit
  `Event(error, stage="hook")` 含 hook_class / method 元数据
- `tests/test_loop.py` —— FakeProvider 脚本化 response,验证 event 顺序、
  终止条件、cancel、**compactor 集成**(`context_compacted` event 顺序、
  兜底校验失败时 emit error event with `stage="compactor"`)、
  **hook 集成**(4 个 hook 调用顺序、短路 emit short_circuited event、
  hook 异常 emit error event)
- `tests/test_runner.py` —— run vs run_to_completion 行为差异、资源清理

### 12.2 集成测试

- `tests/test_integration_flashidea.py` —— 用 baizhi-agent 现成的 flashidea SKILL.md
  跑完整流程(LLM 真打 OR FakeProvider 脚本化),验证最终产出 + 事件流形状

### 12.3 不 mock 真实 I/O

- 文件 I/O 用 `tmp_path` fixture(对齐 baizhi-agent / Fam 约定)
- LLM 用 FakeProvider(对齐 baizhi-agent `FakeLLM`)
- MCP 用 in-process `FastMCP` server(SDK 自带)

### 12.4 必跑测试用例(Stage 1 退出标准)

- `pytest tests/ -x` 全绿
- 至少:30 个单元 + 3 个集成
- 覆盖率(branch)≥ 80%(line)

---

## 13. 4 个开放问题的决议

| Q | 决议 | 落到本文档哪节 |
|---|---|---|
| **Q1 stream** | `RunRequest.stream: bool = False`,opt-in 走 `chat_stream` 路径;`llm_response` event 两种模式都 emit。**实现推迟**(原 Stage 5 → 推到有真消费者要求时再做,详见 § 14 修订 2026-05-24) | § 3.7 / § 4 / § 8.5 / § 14 |
| **Q2 版本 pin** | `enabled_skills` 支持 `"name@version"` 字符串语法;`SkillRegistry.load(version=None)` | § 3.7 / § 6.5 / § 6.6 |
| **Q3 多模态** | Stage 0–5 维持 `content: str`;真需求出现再破坏性升级 `str \| list[ContentBlock]` | § 3.4 / § 15 |
| **Q4 错误传播** | 双轨:`run` yield error event,`run_to_completion` raise;loop 内部 try/except 把异常封到 event | § 3.8 / § 9.2 / § 9.3 |

---

## 14. 迭代路线(修订 2026-05-24)

| Stage | 内容 | 退出标准 |
|---|---|---|
| **0** | 仓库骨架 + 设计文档 ✓ | 模块 stub 可 import ✓ |
| **1** | types / provider / toolset / skill / **tokens / context(含 TruncatingCompactor + safe_split) / hooks(Hook 基类 + 4 no-op)** 实现 + 单元测试 ✓ | 40+ 单测全绿,context 模块单测覆盖 ADK safe_split 全部边界,hooks 模块单测覆盖 4 个 method 签名 |
| **2** | loop 实现(非 stream + cancel + max_rounds + **compactor 集成 + 兜底校验 + 4 个 hook 调用 + first-non-None 短路 + short_circuited event**) + 集成测试 ✓ | FakeProvider 跑通 flashidea;TruncatingCompactor 触发后 `context_compacted` event emit;hook 短路触发后对应 short_circuited event emit;hook 异常被 catch + 转 error event |
| **3** | runner 实现(run + run_to_completion + 资源生命周期 + skill catalog discovery + AgentLoop.aclose) + 端到端测试 ✓ | RunResult 行为符合契约 |
| **4** | mcp 实现(lazy connect + aclose,wrap `mcp` SDK)+ Runner pre-warm + 真打测试 ✓ | 真打 in-memory FastMCP OK,4 种使用方 lifecycle 用例(per-call / per-run / per-tenant / global)各跑一次 |
| **4 附** | Runner.workspace_provider(让外部 workspace 注入)+ sandbox 决议(§ 16,2026-05-24 不内置 → 2026-05-26 修订:contrib diet)✓ | workspace_provider tests 全绿,§ 16 第一版落地 |
| **Sandbox B-F** | `contrib/sandbox/` diet 实现(§ 16.5):sample-first 冻接口 → LocalDir → SRT → MCP → live sample | 见 § 16.5 五个 sub-stage,各自独立 tag |
| **5** | **baizhi-agent 接入** —— 替换内部 runner → agent-kit.Runner + 真 provider / real SkillRegistry / real MCP 接通 + recipes 写 | baizhi-agent pytest 不退化;pptx e2e live test 跑通 |
| **6** | fam-runtime 接入 | fam-runtime pytest 不退化;per-family MCP lifecycle 用例验证 |
| ~~**5 (原)**~~ | ~~stream 实现~~ | **推迟,改为 Stage 7+ 候选** |
| **7+(候选)** | stream 实现(若 baizhi / fam 真要)+ provider.chat_stream 接 LiteLLM 等 | stream/non-stream 切换不破坏 |
| **7+(候选)** | `ctx.emit` 真路由 + Runner.cancel(run_id) | 真消费者驱动 |

**Stage 5 (原 stream) 为什么推迟**(决策 2026-05-24,讨论见对话历史):

- agent-kit 定位是 server-side machinery,主要消费者是持久化 trace 和后端聚合,**不需要 token-level 进度**
- 现有 event 流(round_start / llm_request / llm_response / tool_call / tool_result / round_end / final_text)已经覆盖 90% 用例
- stream 的两个真价值:
  - typing animation UX —— baizhi / fam server-side 场景几乎用不到
  - mid-LLM-call cancel —— non-stream 也能在轮间 cancel,粒度粗一点但够用
- spec § 4.4 已决议 tool_call delta 是**完整 ToolCall**(不切碎),所以 stream 在"提前感知 tool_call"上**也没增益**
- 成本:每个 provider 都要写 chat_stream;事件量 ~50:1
- 现状保留:`RunRequest.stream` flag、`LlmDelta` 数据类、`EventKind.llm_delta` 字面量都不动,真有消费者要时直接实现即可,不需要 break 兼容

**回退路径**:每 Stage 独立 commit + tag。任意 Stage 发现抽象不对,可回滚上一
Stage 重做;baizhi-agent / fam-runtime 在 Stage 5/6 之前完全不受影响。

---

## 15. Out of scope

明确**不**在本文档(及 Stage 1–7)定义的事:

| 主题 | 谁来做 | 备注 |
|---|---|---|
| 多租户队列 / LRU 驱逐 | baizhi-agent application 层 | 部署形态相关 |
| 持久化 trace(SQLite / OTel) | 监听 Event 自己写 | 数据库选型分歧大 |
| HTTP / WebSocket API | FastAPI / Flask / etc. | 框架口味不一 |
| 前端 UI / React | 上层 | 业务态 |
| 鉴权 / 配额 | 上层 | 业务态 |
| Memory / Session | 后续单独 spec(候选,无具体 Stage) | 四家分歧大 |
| Agent-to-agent 编排 | 上层 OR 后续候选(无具体 Stage) | ADK 有,其他无 |
| 多模态(ContentBlock) | Stage 6+ 看真实需求 | Q3 决议 |
| Partial tool_call delta | 后期 | § 4.4 暂用完整 ToolCall 当 delta |
| Tool 内部进度事件 helper | Stage 2 候选 | § 5.2 已留 `ctx.emit` |
| Cancel by run_id 的 Runner API | Stage 2 决定形态 | § 9.4 |
| **Sandbox 重抽象**(Manifest / Capability / Snapshot) | 永久 out | § 16.1 已论证 —— openai-agents 32k 行不抄 |
| **Sandbox diet 抽象** | `contrib/sandbox/`(见 § 16.3,Stage B-F) | core 不动一行,可选 import |
| **Script executor / Skill scripts toolset** | 上层 OR `SandboxToolset`(§ 16.3) | 已有 diet 实现兜底 |

> **Borrowed from pi-main(2026-05-27)** —— after surveying the pi agent
> harness mono-repo (~96k LoC TypeScript) we adopted two cheap wins:
>
> - **Steering queue**:`Agent.send_steering(text)` enqueues a user message
>   that the loop drains at the TOP of the next round via
>   `RunRequest.steering_drain: Callable`. Emits a `user_message_added`
>   event. Lets a UI ("agent is thinking" with active typing) inject
>   redirections without waiting for `final_text`. ~80 LoC + 9 tests.
> - **Parallel tool dispatch**:when an LLM turn returns N>1 tool_calls,
>   they run concurrently via `asyncio.gather`. `RunRequest.parallel_tools
>   = True` (default; set False for strict serial). Message ordering in
>   the transcript preserves the LLM's original tool_calls order
>   regardless of completion order — replay-safe. ~120 LoC + 8 tests.
>
> Explicitly NOT borrowed:Session persistence(spec § 1 Non-goals);
> LLM-summary compaction(spec § 15);per-provider native SDK shim
> (LiteLLM covers it);self-rolled TUI(Textual works);declarative
> Manifest / Capability for sandbox(spec § 16).

---

## 16. Sandbox 与 script 执行(Diet 抽象 + contrib 三家参考实现)

**决策(修订 2026-05-26)**:`agent_kit` **core MUST NOT** 内置任何 sandbox /
executor / Manifest / Capability / Snapshot 概念。但 `agent_kit.contrib.sandbox`
**SHOULD** 提供一个 **5-方法 `SandboxRunner` Protocol** + `SandboxToolset` +
三家参考实现(`LocalDir` / `SRT` / `MCP`),让使用方挂上就用,不用从零拼。

> 历史:2026-05-24 版本曾写"绝对不增加任何 sandbox 相关 module";2026-05-26
> 修订放开 —— **diet 抽象不算破规**,因为它就是一个 `BaseToolset` 子类 +
> 一个 Protocol,不引入新概念到 core。约束仍然成立:**core 不动一行**。

### 16.1 决策依据

讨论历史考察了三家方案:

| 参考 | 模型 | 我们怎么做 |
|---|---|---|
| **ADK `BaseCodeExecutor`** | 从 LLM response 抽 markdown code block,跑后回填 | **不抄**。要求 Gemini 风格 LLM 行为;跟 tool call 协议抢 turn |
| **openai-agents `BaseSandboxSession`** | 长 lived workspace + Manifest + Capability + 7 个 provider extension(E2B/Modal/Daytona/…),核心 20k 行 + 扩展 12.5k 行 ≈ **32,640 行** | **不抄重型抽象**。Manifest / Capability / Snapshot / PTY / exposed_port 全部排除 —— 它们是 product,不是机制 |
| **Diet `SandboxRunner` Protocol(本节方案)** | 5 个方法的 Protocol + 1 个 `BaseToolset` 子类 + 三家 backend,共 ~500 行 SDK | **采用,放 contrib**。代价是没有声明式 manifest 和内置 snapshot;换来 60× 代码量缩减,且不绑 vendor |

三大真实场景与对应解:

| 需求 | 解法 | 提供方 |
|---|---|---|
| 长 lived workspace 跑 agent(baizhi-agent tenant_agent 空间) | `Runner.workspace_provider` 注入持久路径 + `SandboxToolset(LocalDirRunner())` | core(§ 9.1) + contrib(§ 16.3) |
| 本地受限执行(macOS/Linux dev box) | `SandboxToolset(SrtRunner(profile=...))`,薄包装 Anthropic SRT | contrib(§ 16.3) |
| 远程 / 隔离执行(生产 / 不可信输入) | `SandboxToolset(McpSandboxRunner(McpToolset.http(...)))`,任何暴露 exec/read/write 工具的 MCP 服务都行 | contrib(§ 16.3) |

### 16.2 Core SDK 边界(不变)

- **新增** `Runner.workspace_provider`(§ 9.1):允许外部 workspace 注入,
  Runner 不建不删 → 让上面三场景都能拿到使用方持久空间
- **新增** `ToolCallContext.workspace_ephemeral`(§ 5.2):toolset 自检"我能不
  能在 workspace 里跨 run 缓存"
- `agent_kit/` 主目录 **MUST NOT** 引入 sandbox 相关 module 或 dataclass。
  sandbox 全部生活在 `agent_kit/contrib/sandbox/`,可选 import,跟 LiteLlm 一样
  通过 extras(`pip install agent-kit[sandbox]`)装

### 16.3 `agent_kit.contrib.sandbox` 契约(diet)

包结构(总 ~500 行 SDK + ~300 行 tests,对比 openai-agents 32k 行 → **60× 减少**):

```
agent_kit/contrib/sandbox/
├── __init__.py             # re-export
├── types.py                # SandboxRunner Protocol + ExecResult           ~25 行
├── toolset.py              # SandboxToolset(BaseToolset)                  ~150 行
└── runners/
    ├── localdir.py         # LocalDirRunner — host subprocess,无隔离      ~120 行
    ├── srt.py              # SrtRunner — Anthropic sandbox-runtime          ~80 行
    └── mcp.py              # McpSandboxRunner — 任何 MCP exec 服务          ~100 行
```

**核心 Protocol** (`types.py`):

```python
@runtime_checkable
class SandboxRunner(Protocol):
    name: str                                                  # 工具名前缀

    async def setup(self, workspace: Path) -> None: ...        # mkdir + 预热
    async def exec(
        self, cmd: list[str], *,                               # list,不上 shell
        cwd: str = "", env: dict[str, str] | None = None,
        timeout: float | None = None, stdin: bytes | None = None,
    ) -> ExecResult: ...
    async def read(self, path: str) -> bytes: ...
    async def write(self, path: str, content: bytes) -> None: ...
    async def aclose(self) -> None: ...
```

**`SandboxToolset(BaseToolset)`**:

- 工具名前缀 `sandbox__<runner.name>__`(同 `mcp__<server>__` 规则)
- 暴露 3 个 LLM 工具:`exec_command` / `read_file` / `write_file`,通过
  `tools=("exec_command",)` 可裁剪
- `connect()` 阶段做 `runner.warmup()`(如有);workspace 还没建,真 `setup()`
  推到 `execute()` 第一次被调,这时拿到 `ctx.workspace`
- stdout/stderr 截断在 toolset 层做(默认 8KiB / 4KiB),Runner 永远返完整 bytes
- 失败转 `ToolResult(is_error=True)`,不抛

**三家 Runner 行为契约**:

| 维度 | `LocalDirRunner` | `SrtRunner` | `McpSandboxRunner` |
|---|---|---|---|
| `setup(ws)` | `ws.mkdir(parents=True, exist_ok=True)` + 记 workspace | 同 LocalDir | **同 LocalDir**(决策 #3,2026-05-26)+ optionally call MCP `init_workspace` |
| 隔离层 | 无(allowlist 自检) | SRT profile(filesystem ACL + 网络限制) | 远端服务自己决定 |
| `read` / `write` | host fs 直读直写 | host fs 直读直写(SRT bind-mount workspace) | 走 MCP `read_file` / `write_file` tool |
| path traversal | 内置 `_is_within()` 防护 | 同 LocalDir | 远端服务自己保证 |
| 安全姿势 | secure-by-config(allowlist 显式) | secure-by-profile | secure-by-deployment |

**故意 SHOULD NOT 暴露的方法**(防止抽象漏出去):

| 没做的 | 为什么 |
|---|---|
| `pty_exec` / `write_stdin` | PTY 不是机制,product 级特性 → 用户自己包 `BaseToolset` |
| `persist_workspace` / `hydrate_workspace`(snapshot) | 用 `workspace_provider` 给持久路径就 == snapshot |
| `apply_patch` | LLM 自己用 `read` + `write` 组合;真要 diff 走 `exec(["patch", ...])` |
| `exposed_port` / port forwarding | 不是 SDK 的事;远程服务自己暴露 |
| `User` / 权限模型 | Runner 自己定:LocalDir 走 host 用户,SRT 走 profile,MCP 走服务端 |
| streaming exec | LLM 拿到的总是完整结果;`tail -f` 类需求走 MCP |

### 16.4 参考 sample

`samples/coding-agent/` 提供端到端用例:**LocalDirRunner 跑 fix-a-bug 任务**。
不写单独的 recipes —— 一份能跑的 sample 抵 10 篇 markdown。

### 16.5 实施阶段(2026-05-26)

| Stage | 内容 | tag |
|---|---|---|
| **B(sample-first)** | `samples/coding-agent/` 用 stub runner 跑通,冻结 SandboxRunner / SandboxToolset 接口 | `sandbox-api-frozen` |
| **C(sandbox-1)** | `contrib/sandbox/{types,toolset}.py` + `LocalDirRunner` + tests | `sandbox-1` |
| **D(sandbox-2)** | `SrtRunner` + tests | `sandbox-2` |
| **E(sandbox-3)** | `McpSandboxRunner` + tests | `sandbox-3` |
| **F(sandbox-sample-live)** | sample 替换 stub,end-to-end 跑真任务 | `sandbox-sample-live` |

---

## 17. Agent 便利层 + Provider contrib(Stage 5 修订 2026-05-24)

### 17.1 决策

参考 ADK / openai-agents,加两层 **薄 convenience**,降低 hello-world boilerplate
而不破坏底层契约。两层一起 + LiteLlm extras,等价的"4 行 ADK"现在 agent-kit
也能 8 行写出。

| 层 | 职责 | 放哪 |
|---|---|---|
| `Agent` class | bundle name + provider + instruction + tools + hooks/compactor/workspace_provider 这些 Runner ctor 参数;暴露 `run(message)` / `run_sync(message)` 一行调用 | `agent_kit/agent.py`,主包 export `from agent_kit import Agent` |
| `LiteLlm` provider | 实现 `LlmProvider` Protocol,内部调 `litellm.acompletion`;支持 LiteLLM 路由的所有 model(`gemini/...`,`anthropic/...`,`openai/...`,`minimax/...`,等)| `agent_kit/contrib/providers/litellm.py`,**optional extras**(`pip install "agent-kit[litellm]"`) |

### 17.2 Agent 契约

```python
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class Agent:
    name: str
    model: LlmProvider | str                       # str → 走 LiteLlm(需 extras)
    instruction: str = ""                          # → Runner.system_prelude
    tools: list[BaseToolset] = field(default_factory=list)

    # advanced:Runner 的其他 ctor 参数都暴露,默认值跟 Runner 一致
    hooks: list[Hook] = field(default_factory=list)
    compactor: ContextCompactor | None = None
    workspace: Path | Callable[[RunRequest, str], Path] | None = None

    # 默认的 per-run 参数(用 .run() 时不传就用这个,传了就 override)
    default_max_rounds: int = 10
    default_temperature: float = 0.7

    # **故意没有 `default_tenant_id`**:tenant 是上层多租户应用的概念,不是
    # agent 自身的固定属性

    def __post_init__(self) -> None:
        if isinstance(self.model, str):
            self.model = self._resolve_string_model(self.model)
        # 一次性构 Runner,跨多 .run() 复用(toolsets / provider 跨 run 状态保留)
        self._runner = Runner(
            provider=self.model, toolsets=self.tools,
            system_prelude=self.instruction,
            compactor=self.compactor, hooks=self.hooks,
            workspace=self.workspace,
            default_max_rounds=self.default_max_rounds,
        )

    async def run(
        self,
        user_message: str,
        *,
        tenant_id: str = "default",                # 不 store 在 Agent 上
        enabled_skills: list[str] | None = None,
        max_rounds: int | None = None,
        temperature: float | None = None,
    ) -> RunResult: ...

    def run_sync(self, user_message: str, **kwargs) -> RunResult:
        """同 run(),但通过 Runner.run_sync 走 asyncio.run。FastAPI 里别用。"""

    @staticmethod
    def _resolve_string_model(model: str) -> LlmProvider:
        """`from agent_kit.contrib.providers.litellm import LiteLlm` 失败 →
        raise 友好 ImportError 提示装 extras 或传 LlmProvider 实例。"""

    @property
    def runner(self) -> Runner:
        """advanced 用户拿底层 Runner 用 `run_to_completion(RunRequest(...))`
        全自由。Agent 是 thin wrapper,Runner 永远在。"""
```

**故意不做的**(防止 Agent 滑坡成 ADK):

| ADK 有 | agent-kit Agent 不做 | 理由 |
|---|---|---|
| `tools=[my_python_function]` 函数当工具(自动反射 signature)| 不做 | 用户写 `BaseToolset` 显式;反射魔法 YAGNI |
| `tools=[other_agent]` sub-agent / handoff | 不做 | spec § 15 |
| 内置 `google_search` / `web_search` tool | 不做 | spec § 1 边界;走 MCP |
| Multimodal `Content` blocks | 不做 | Q3 决议 |
| Sessions / Memory / 持久化 history | 不做 | spec § 15 |

### 17.3 LiteLlm provider 契约

```python
class LiteLlm:
    """`agent_kit.LlmProvider` 协议实现,内部 wrap LiteLLM.acompletion。

    Examples:
        LiteLlm("gemini/gemini-flash-latest")
        LiteLlm("anthropic/claude-haiku-4-5", api_key="sk-...")
        LiteLlm("openai/gpt-4o-mini")
        LiteLlm("minimax/MiniMax-M2.7",
                api_base="https://api.minimaxi.com/v1",
                api_key="...")

    Optional extras:`pip install "agent-kit[litellm]"`。装了再 import。
    """
    name: str                              # = "litellm:<model>"

    def __init__(self, model: str, **litellm_kwargs):
        # **litellm_kwargs 透传给 litellm.acompletion(api_key / api_base /
        # custom_llm_provider / 等)。我们不重新发明 LiteLLM 的 API。

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *, temperature: float = 0.7, max_tokens: int | None = None,
    ) -> LlmResponse: ...
        # 翻译 messages / tools → OpenAI 形态(LiteLLM 内部规范化);
        # 调 litellm.acompletion;翻译 response.choices[0] → LlmResponse

    async def chat_stream(self, *args, **kwargs):
        raise NotImplementedError       # 跟 stream 推迟一致(spec § 14 Stage 7+)
```

### 17.4 等价的 "4 行 ADK" 在 agent-kit

```python
from agent_kit import Agent
from agent_kit.mcp import McpToolset

agent = Agent(
    name="researcher",
    model="gemini/gemini-flash-latest",
    instruction="You help users research topics thoroughly.",
    skills="./skills",                                   # ← 自动 catalog,默认全列
    tools=[McpToolset.http("brave", url="...",
                            headers={"X-Key": "${BRAVE_KEY}"})],
)
print(agent.run_sync("...").final_text)
```

跟 ADK 的 4 行差 ~3 行(MCP server config + 1 个 print);**0 重依赖**
(LiteLlm 在 extras 后面)+ **底层 Runner 完全暴露**(`agent.runner` 访问)
+ **per-Agent 多租户**(每个 tenant new 一份 Agent)+ **skill catalog 一行接入**。

### 17.5 测试覆盖范围

- `tests/test_agent.py`:string-model resolution(litellm extras 装了 / 没装
  两条路径)、`run` / `run_sync` 行为、`enabled_skills` / `max_rounds` /
  `temperature` override、`agent.runner` 暴露
- `tests/test_agent_skills.py`:`skills=` 入口 4 形态、自动 enable / 显式
  覆盖 / 显式空、`DEFAULT_SKILLS_GUIDANCE` 默认 / "" 禁用 / 自定义、
  `InMemorySkillRegistry` 契约
- `tests/contrib/test_providers_litellm.py`:`messages` → OpenAI 翻译、
  `tools` → function 翻译、`response.choices[0]` → `LlmResponse` 反向翻译、
  tool_calls 多个 / 空 / 部分文本混合、usage 字段映射;`@pytest.skip` if no
  litellm

### 17.6 Agent.skills 简写 + DEFAULT_SKILLS_GUIDANCE(Stage 5 修订 2026-05-26)

参考 openai-agents `Skills` capability(`sandbox/capabilities/skills.py`)
做了**3 处对齐**(其他刻意不抄):

**对齐 1:`skills=` 接受多形态源**

```python
Agent(skills=None)                            # 无 catalog
Agent(skills=Path("./skills"))                # → FilesystemSkillRegistry
Agent(skills="./skills")                      # 同上(str)
Agent(skills=my_registry)                     # SkillRegistry 实例直传
Agent(skills=[Skill(...), Skill(...)])        # → InMemorySkillRegistry
```

非 None 时,Agent 自动 append 一个 `SkillCatalogToolset(registry)` 到 tools 末尾。

**对齐 2:默认 enable 全部(没有 `default_enabled_skills`)**

openai-agents `Skills.instructions()` 总是列**全部** skill metadata,LLM 通过
`$SkillName` trigger / 任务描述匹配自选。我们对齐:

```python
agent.run("...")                       # enabled_skills=None → 自动 registry.list() 全部
agent.run("...", enabled_skills=["a"]) # 显式 → 只列 a
agent.run("...", enabled_skills=[])    # 显式空 → 不列任何(catalog 工具仍可用)
```

`Agent` 故意**没有** `default_enabled_skills` 字段 —— "全列"就是默认行为,
不需要再额外配置。要 per-call subset 直接传 `enabled_skills=[...]`。

**对齐 3:`DEFAULT_SKILLS_GUIDANCE` 注入 prelude**

openai-agents 在 prelude 跟 skill 列表一起注入 ~30 行 "How to use skills"
指令(trigger 规则 / progressive disclosure / coordination / fallback)。
我们抄了**精简版**(~10 行,~110 token)到 `agent_kit.skill.DEFAULT_SKILLS_GUIDANCE`。

- `SkillCatalogToolset(reg)` 默认 instructions=None → 注入 DEFAULT_SKILLS_GUIDANCE
- `SkillCatalogToolset(reg, instructions="")` → 不注入
- `SkillCatalogToolset(reg, instructions="### Custom...")` → 自定义
- `Agent(skills=..., skills_instructions=...)` 透传给 SkillCatalogToolset

**故意不抄的 openai-agents 部分**:

| 不抄 | 理由 |
|---|---|
| `Skill(scripts/references/assets)` 结构化字段 | YAGNI;现有 `files dict[str, bytes]` 用 `scripts/foo.py` key 一样达到效果 |
| eager vs lazy 两种模式 | 我们 `load_skill` 工具本来就是 lazy(frontmatter 进 prelude,body 按需 load),不需要二选一 |
| `from_=Dir(...)` 物化到 sandbox workspace(声明式 manifest) | 我们 § 16.1 决定 diet sandbox 不带 Manifest;skill body 想物化文件,自己用 `SandboxToolset.write_file` 写 |

---

## 附录 A · 设计依据反查

| 设计点 | 来源 |
|---|---|
| AsyncIterator[Event] pull 模型 | ADK / OpenHarness |
| event_id + parent_event_id | baizhi-agent PR α(pr-trace-a) |
| 轮数 cap 是硬约束 | baizhi-agent / fam-runtime |
| 最后一轮屏蔽 tools | baizhi-agent 发明 |
| SKILL.md 即契约 | baizhi-agent GOALS.md N1 |
| Progressive disclosure | 四家收敛 |
| `mcp__<server>__<tool>` 命名 | OpenHarness / baizhi-agent / fam-runtime |
| 直接 wrap Anthropic `mcp` SDK | baizhi-agent PR f6 教训 |
| **MCP lifecycle 不枚举,实例生命周期 = session 生命周期** | ADK(2026-05-24 修订;原 4 档枚举见 proposal.md errata) |
| 每 skill 独立 storage | baizhi-agent / fam-runtime |
| **Context compaction:ContextCompactor Protocol + microcompact 默认实现** | OpenHarness `microcompact_messages` |
| **Safe split 兜底(保护 tool_call/tool_result 配对)** | ADK `_safe_token_compaction_split_index` |
| **Token 估算:chars/4 × 4/3,优先 API usage** | OpenHarness `token_estimation` + ADK `_latest_prompt_token_count` |
| **Hook 基类 + 4 no-op 方法(before/after × model/tool)** | ADK `LlmAgent` callback 字段 + plugin 系统的合并精简版 |
| **First-non-None 短路语义** | ADK `base_llm_flow.py` + `base_agent.py` + `functions.py` |
| **Hook 异常 raise → error event,不 swallow** | ADK plugins(反对 baizhi-agent / fam-runtime 的 swallow)|
| **`llm_short_circuited` / `tool_short_circuited` event 透明性** | 原创(给 trace UI 区分 hook 介入)|
| **装饰器 vs hook 选择指南**(横切 hook / 内聚装饰器 / 跨轮往外推) | 原创(基于 user-driven 收敛)|
| ToolCallContext 统一合同 | ADK Tool.run_async + baizhi-agent toolsets.py |
| 命名冲突启动期检测 | baizhi-agent LlmAgentRunner |
| 不引 google.genai / langchain | GOALS.md Non-goals |
| 双 API(run / run_to_completion)| Q4 决议(本文档原创) |

---

**审阅 checklist**(读完这份文档,Reviewer 应能回答):

- [ ] Stage 1 要实现哪几个模块?
- [ ] 每个 Event 的 payload 字段都有?
- [ ] Cancel 在哪些点 check?
- [ ] stream / non-stream 切换会破坏 llm_response event 吗?
- [ ] Skill 版本不存在时 SkillRegistry.load 怎么处理?
- [ ] MCP 工具名命名规则是什么?
- [ ] run_to_completion 遇到 error event 怎么办?
- [ ] 哪些事 SDK **不**做?
