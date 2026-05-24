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

`stage` ∈ `{"loop", "provider", "tool", "setup"}` —— 异常发生在哪个阶段。

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
    tenant_id: str               # MUST 非空
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
```

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
        """SHOULD 在每次调用返回同样的 list(允许动态变化但不推荐)。"""

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
    tenant_id: str
    run_id: str
    skill_name: str | None        # 当前调用所归属的 skill(若工具调用来自 SKILL.md 描述触发)
    cancel: asyncio.Event         # toolset 长任务 SHOULD 周期性 check
    workspace: Path               # /tmp/agent-kit-runs/<run_id>/,run 结束后由 Runner 删除
    storage: Path                 # 持久存储根目录,toolset 决定子结构
    emit: Callable[[Event], None] # toolset 内部进度事件,event_id 由 toolset 申请
    # ↑ 进度事件可在 Stage 2 加 helper("tool_progress")
```

### 5.3 ToolsetRouter

```python
class ToolsetRouter:
    def __init__(self, toolsets: list[BaseToolset]) -> None:
        """启动期检测:
        1. 各 toolset.name 唯一(否则 raise ValueError)
        2. 跨 toolset 的 ToolSchema.name 无冲突(否则 raise ValueError)"""

    def all_schemas(self) -> list[ToolSchema]:
        """合并所有 toolset 的 schema。顺序:registration order。"""

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        """根据 call.name 路由。
        - 未知 name → ToolResult(is_error=True, content="ERROR: unknown tool ...")
        - toolset.execute 抛异常 → ToolResult(is_error=True, content="ERROR: <exc>") + emit error event"""

    async def aclose(self) -> None:
        """按 reverse(registration order)调用各 toolset.aclose,异常 swallow + log。"""
```

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
    @abstractmethod
    async def list(self, tenant_id: str) -> list[SkillFrontmatter]: ...

    @abstractmethod
    async def load(self, tenant_id: str, name: str, version: str | None = None) -> Skill:
        """Q2 决议:version=None 拿 latest;具体值拿 immutable publish。
        - tenant_id 不存在 → raise KeyError
        - name 不存在 → raise KeyError
        - version 给了但不存在 → raise KeyError"""

    @abstractmethod
    async def save_draft(
        self, tenant_id: str, name: str, md: str, files: dict[str, bytes]
    ) -> None: ...

    @abstractmethod
    async def publish(self, tenant_id: str, name: str) -> str:
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
    def __init__(self, config: McpServerConfig, *, secrets: dict[str, str] | None = None):
        self._config = config
        self._secrets = secrets or {}
        self.name = f"mcp__{config.name}"
        self._session = None     # lazy: 首次 build_schemas / execute 时 connect

    def build_schemas(self) -> list[ToolSchema]:
        """首次调用 lazy connect + list_tools + 缓存。后续返回缓存 schemas。
        Stage 1 实现:同步 wrapper(asyncio.run 在 init 中,与 baizhi-agent
        mcp_session 一致)。"""

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        """复用 self._session(若未 connect 则 lazy connect),call tool,返回。
        - isError=True 的 MCP 响应 → ToolResult(is_error=True, content=msg)
        - 传输 / SDK 异常 → ToolResult(is_error=True, content=f"ERROR: {exc}")"""

    async def aclose(self) -> None:
        """关闭 self._session(若已 connect)。idempotent。"""
```

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
        workspace_root: Path = Path("/tmp/agent-kit-runs"),
        storage_root: Path = Path("./persistent"),
    ) -> None: ...
```

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

### 9.2 双 API(Q4 决议)

```python
async def run(self, request: RunRequest) -> AsyncIterator[Event]:
    """事件流形式。异常全部 catch,wrap 成 Event(kind="error") + return。
    SHOULD 用于服务端 / 持久化场景。"""

async def run_to_completion(self, request: RunRequest) -> RunResult:
    """聚合形式。遇 error event raise RuntimeError(包含原 exc_type / message)。
    SHOULD 用于脚本 / 测试 / 一次性 CLI 场景。"""
```

### 9.3 资源生命周期

```
Runner.run(request):
  1. allocate run_id (ULID)
  2. mkdir workspace = workspace_root / run_id
  3. build ToolCallContext (cancel = asyncio.Event())
  4. build ToolsetRouter(self._toolsets)
     ↑ toolsets 由使用方构造,Runner 不 new。Router init 期间检测命名冲突。
  5. build AgentLoop(provider, self._toolsets, prelude=self._prelude + request.system_prelude)
  6. try:
       async for evt in loop.run(request, ctx): yield evt
     except Exception as exc:
       yield Event(kind="error", payload={...}); return
     finally:
       await router.aclose()              # 关 MCP session / 释放 toolset 资源
       shutil.rmtree(workspace)           # 删 workspace
```

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

## 12. 测试策略

### 12.1 单元测试(每模块)

- `tests/test_types.py` —— dataclass 不变量、frozen 行为、event payload schema
- `tests/test_provider.py` —— FakeProvider 实现 Protocol;chat / chat_stream 契约
- `tests/test_toolset.py` —— ToolsetRouter 冲突检测、execute 路由、aclose 顺序
- `tests/test_skill.py` —— parse_frontmatter 各种边界、parse_skill_ref 解析
- `tests/test_mcp.py` —— McpServerConfig 校验、${VAR} 替换、命名规则
- `tests/test_loop.py` —— FakeProvider 脚本化 response,验证 event 顺序、终止条件、cancel
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
| **Q1 stream** | `RunRequest.stream: bool = False`,opt-in 走 `chat_stream` 路径;`llm_response` event 两种模式都 emit | § 3.7 / § 4 / § 8.5 |
| **Q2 版本 pin** | `enabled_skills` 支持 `"name@version"` 字符串语法;`SkillRegistry.load(version=None)` | § 3.7 / § 6.5 / § 6.6 |
| **Q3 多模态** | Stage 0–5 维持 `content: str`;真需求出现再破坏性升级 `str \| list[ContentBlock]` | § 3.4 / § 15 |
| **Q4 错误传播** | 双轨:`run` yield error event,`run_to_completion` raise;loop 内部 try/except 把异常封到 event | § 3.8 / § 9.2 / § 9.3 |

---

## 14. 迭代路线

| Stage | 内容 | 退出标准 |
|---|---|---|
| **0** | 仓库骨架 + 设计文档 ✓ | 模块 stub 可 import ✓ |
| **1** | types / provider / toolset / skill 实现 + 单元测试 | 30+ 单测全绿 |
| **2** | loop 实现(非 stream + cancel + max_rounds) + 集成测试 | FakeProvider 跑通 flashidea |
| **3** | runner 实现(run + run_to_completion + 资源生命周期) + 端到端测试 | RunResult 行为符合契约 |
| **4** | mcp 实现(lazy connect + aclose,wrap `mcp` SDK) + 真打测试 | 真打 DashScope WebSearch OK,4 种使用方 lifecycle 用例(per-call / per-run / per-tenant / global)各跑一次 |
| **5** | stream 实现(Q1 决议) + provider.chat_stream 接 LiteLLM 等 | stream/non-stream 切换不破坏 |
| **6** | baizhi-agent 替换内部 runner → agent-kit.Runner | baizhi-agent pytest 不退化 |
| **7** | fam-runtime 替换 | fam-runtime pytest 不退化 |

**回退路径**:每 Stage 独立 commit + tag。任意 Stage 发现抽象不对,可回滚上一
Stage 重做;baizhi-agent / fam-runtime 在 Stage 6/7 之前完全不受影响。

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
