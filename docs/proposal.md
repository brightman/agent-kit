# agent-kit · Minimal Agent Loop + Skill + MCP 设计

**版本**:Stage 0(2026-05-24)  
**状态**:仓库骨架已出,模块 stub 占位;真实现等首次接进 baizhi-agent 时再补  
**作者**:综合 ADK / OpenHarness / baizhi-agent / fam-runtime 四套实现的共识

---

## 一、动机

`baizhi-agent`、`fam-runtime` 各自实现了一份 agent loop + skill + MCP 脚手架,
但 90% 的轮廓重叠:

- provider 抽象(LLM 接入层)
- 轮数 cap + tool dispatch 的核心循环
- `mcp__<server>__<tool>` 命名 + MCP server 生命周期管理
- SKILL.md frontmatter + progressive disclosure
- 每 skill 独立 storage 根目录

把这层公共骨架抽成独立 kit(`agent-kit`,自报"骨架而非全家桶"),后续两个
项目共用,新项目零成本起步。

---

## 二、设计原则

| # | 原则 | 来源 |
|---|------|------|
| P1 | Skill = SKILL.md,不是 Python class | baizhi-agent GOALS.md N1 |
| P2 | Loop 用 pull 模型(AsyncIterator[Event]) | ADK / OpenHarness |
| P3 | 轮数 cap 是硬约束,最后一轮屏蔽 tools | baizhi-agent / fam-runtime |
| P4 | MCP 直接 wrap Anthropic 官方 `mcp` SDK,**不自写 JSON-RPC** | baizhi-agent PR f6 教训 |
| P5 | SDK **不**做多租户队列 / 持久化 / HTTP / UI / 鉴权 | GOALS.md Non-goals |

---

## 三、模块划分

```
agent_kit/
  __init__.py     # 顶层 re-export
  types.py        # Message / ToolCall / ToolResult / Event(纯数据)
  provider.py     # LlmProvider Protocol + ToolSchema / LlmResponse / LlmDelta
  toolset.py      # BaseToolset ABC + ToolCallContext + ToolsetRouter
  skill.py        # Skill / SkillRegistry + SkillCatalogToolset
  mcp.py          # McpServerConfig + McpLifecycle + McpToolset(wrap `mcp`)
  loop.py         # RunRequest + AgentLoop.run(核心循环)
  runner.py       # Runner 门面
```

---

## 四、核心类型(types.py)

```python
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False

@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

@dataclass
class Event:
    event_id: str
    parent_event_id: str | None
    kind: EventKind   # round_start / llm_request / llm_response / tool_call /
                      # tool_result / round_end / final_text / error / cancelled
    payload: dict[str, Any]
    ts: float
```

**为什么是这样**:
- `ToolCall.id` 必填 —— OpenAI / Anthropic / MiniMax 都需要回引
- `event_id + parent_event_id` —— baizhi-agent PR α 已经验证了 replay / UI 树形渲染的价值
- `Event.kind` 是字面量类型不是开放 string —— SDK 范围内枚举完毕,使用方按
  kind switch,避免业务态侵入

---

## 五、Provider Protocol(provider.py)

```python
class LlmProvider(Protocol):
    name: str
    async def chat(self, messages, tools=None, *, temperature=0.7,
                   max_tokens=None) -> LlmResponse: ...
    async def chat_stream(self, messages, tools=None, *, temperature=0.7,
                          max_tokens=None) -> AsyncIterator[LlmDelta]: ...
```

**只要两个方法**。模型 id / API key / base_url / 重试 / 速率限制 全部在
provider 的构造函数里吃掉,loop 层永不感知。

**适配映射**:
- baizhi-agent 现有 `MiniMaxProvider` / `LiteLlmProvider` 已经是这个形状,迁移
  零成本
- Anthropic 原生:写一个 wrap `anthropic` SDK 的 provider
- ADK 的 `BaseLlm` 也是相同模式,可对照映射

---

## 六、Toolset(toolset.py)

```python
@dataclass
class ToolCallContext:
    tenant_id: str
    run_id: str
    skill_name: str | None
    cancel: asyncio.Event
    workspace: Path        # /tmp/runs/<run_id>/
    storage: Path          # persistent/skills/<name>/
    emit: Callable[[Event], None]

class BaseToolset(ABC):
    name: str
    def build_schemas(self) -> list[ToolSchema]: ...
    async def execute(self, call, ctx) -> ToolResult: ...
    async def aclose(self) -> None: ...

class ToolsetRouter:
    """合并多个 toolset,按 ToolCall.name 路由。冲突直接 raise。"""
```

**ToolCallContext 是合同**:所有 toolset 的 execute 拿到一样的上下文(租户 /
运行 / 取消 / 工作目录 / 持久目录 / 事件回调)。这点决定了 SDK 内部不必为
每种 toolset 单独造 context 类型。

**Router 在 init 时做命名冲突检测**(对齐 baizhi-agent LlmAgentRunner),把
"工具同名"错误前置到启动期。

---

## 七、Skill(skill.py)

```python
@dataclass
class SkillFrontmatter:
    name: str
    description: str
    version: str
    tools: list[str]                  # 可选:声明依赖的 toolset 名
    inputs: dict | None
    raw: dict

@dataclass
class Skill:
    name: str
    frontmatter: SkillFrontmatter
    body: str                        # SKILL.md 去掉 frontmatter
    files: dict[str, bytes]          # 同包附带的文件
    storage_root: Path

class SkillRegistry(ABC):
    async def list(self, tenant_id) -> list[SkillFrontmatter]: ...
    async def load(self, tenant_id, name) -> Skill: ...
    async def save_draft(self, tenant_id, name, md, files) -> None: ...
    async def publish(self, tenant_id, name) -> str: ...

class SkillCatalogToolset(BaseToolset):
    """暴露 list_skills / load_skill / load_skill_resource 给 LLM。"""
```

**Progressive disclosure 是默认行为**:
1. 启动时把 enabled skills 的 frontmatter(name + description + version)拼进
   system prompt —— 这给 LLM 一个目录索引
2. 想看正文,调 `load_skill(name)` 工具
3. 想看辅助文件(脚本 / template / 配置),调 `load_skill_resource(name, path)`

这是四家在最近半年内独立收敛到的设计 —— ADK SkillToolset / OH SkillTool /
baizhi-agent SkillCatalogToolset / Fam READ_SKILL_TOOL 都是这个形状。

---

## 八、MCP(mcp.py)

```python
class McpLifecycle(Enum):
    PER_CALL    = "per_call"      # baizhi-agent 当前:每次调用拉起 + 关闭
    PER_RUN     = "per_run"       # 单次 agent run 内复用
    PER_TENANT  = "per_tenant"    # Fam 风格:按 family_id 缓存
    GLOBAL      = "global"        # OH 风格:进程全局

@dataclass
class McpServerConfig:
    name: str
    transport: Literal["stdio", "sse", "http"]
    command: list[str] | None = None
    url: str | None = None
    headers: dict[str, str] = {}    # ${VAR} 模板,SDK 负责注入
    env: dict[str, str] = {}
    lifecycle: McpLifecycle = McpLifecycle.PER_CALL

class McpToolset(BaseToolset):
    """一个 server 一份 toolset。工具命名 mcp__<server>__<remote_name>。"""
```

**关键决策**:
- **不重写 transport**。直接 `from mcp import ClientSession` +
  `from mcp.client.streamable_http import streamablehttp_client` 等。baizhi-agent
  PR f6 的教训:自写 JSON-RPC 跳过 initialize 握手 → 严格 server 拒连;跟规范
  升级是无底洞
- **lifecycle 是配置**:四家自己用的实际上分别落在 4 档,SDK 提供枚举,使用方
  按部署形态挑

---

## 九、AgentLoop(loop.py)

```python
class AgentLoop:
    async def run(self, request: RunRequest, ctx: ToolCallContext
                  ) -> AsyncIterator[Event]:
        messages = self._compose_messages(request)
        schemas = self._router.all_schemas()
        for round_idx in range(request.max_rounds):
            if ctx.cancel.is_set():
                yield Event(kind="cancelled", ...); return
            yield Event(kind="round_start", payload={"round": round_idx})
            # 最后一轮屏蔽 tools 强制收尾
            tools = schemas if round_idx < request.max_rounds - 1 else None
            resp = await self._provider.chat(messages, tools)
            yield Event(kind="llm_response", payload=resp.to_dict())
            if not resp.tool_calls:
                yield Event(kind="final_text", payload={"text": resp.text})
                return
            messages.append(resp.to_assistant_message())
            for call in resp.tool_calls:
                yield Event(kind="tool_call", payload=call.__dict__)
                result = await self._router.execute(call, ctx)
                yield Event(kind="tool_result", payload=result.__dict__)
                messages.append(result.to_tool_message())
            yield Event(kind="round_end", payload={"round": round_idx})
```

**关键决策**:
- **pull 模型**(AsyncIterator[Event])。callback 模型让使用方在外面包很容易
  (`async for evt in loop.run(...): callback(evt)`);反过来不成立
- **轮数 cap 硬约束**:`for round_idx in range(max_rounds)` —— 无限循环是
  bug,不是 feature
- **最后一轮屏蔽 tools**:baizhi-agent 发明的小技巧,避免模型永远调工具不收尾
- **取消在 round 边界 check**:粒度足够细(单轮最多 5–30s),实现简单
- **不引入 is_final_response 业务判断**(ADK 风格)—— 让 `tool_calls is None`
  自然终止,语义干净

---

## 十、Runner(runner.py)—— 使用者入口

```python
class Runner:
    def __init__(self, provider, skill_registry,
                 mcp_servers=None, extra_toolsets=None,
                 *, default_max_rounds=10, system_prelude=""):
        ...
    async def run(self, request: RunRequest) -> AsyncIterator[Event]:
        ctx = self._build_ctx(request)
        toolsets = await self._materialize_toolsets(request, ctx)
        loop = AgentLoop(self._provider, toolsets,
                         default_max_rounds=self._default_max_rounds,
                         system_prelude=self._prelude)
        async for evt in loop.run(request, ctx):
            yield evt
```

**典型用法**(对照 baizhi-agent 当前代码):

```python
runner = Runner(
    provider=LiteLlmProvider("minimax/MiniMax-M2.7"),
    skill_registry=FileSystemSkillRegistry(root="./persistent/skills"),
    mcp_servers=[
        McpServerConfig(name="WebSearch", transport="http",
                        url="https://dashscope.aliyuncs.com/...",
                        headers={"Authorization": "Bearer ${DASHSCOPE_API_KEY}"}),
    ],
)
async for evt in runner.run(RunRequest(
    tenant_id="user_42",
    agent_id="my_agent",
    user_message="帮我搜一下 ICML 2025 best paper",
    enabled_skills=["paper_review"],
)):
    print(evt.kind, evt.payload)
```

---

## 十一、SDK **不**做的事

| 功能 | 为什么不放 | 谁来做 |
|------|----------|--------|
| 多租户队列 + LRU 驱逐 | 部署形态相关 | baizhi-agent application 层 |
| 持久化 trace(SQLite / OTel) | 数据库选型分歧大 | 监听 Event 自己写 |
| HTTP API / FastAPI | 框架口味不一 | 上层 |
| 前端 UI / React | 业务态 | 上层 |
| 鉴权 / 配额 | 业务态 | 上层 |
| Memory / Session | 四家做法分歧太大,不强收敛 | 后续作为可选 toolset 加 |
| Agent-to-agent 编排 | ADK 有,其他三家无,放进来过早 | 后续选做 |

---

## 十二、设计依据对照

| SDK 决策 | 主要来源 | 次要来源 |
|---------|---------|---------|
| AsyncIterator[Event] pull 模型 | ADK BaseLlmFlow / OH QueryEngine | — |
| event_id + parent_event_id | baizhi-agent PR α | — |
| 轮数硬 cap + 最后一轮屏蔽 tools | baizhi-agent / fam-runtime | — |
| SKILL.md frontmatter + progressive disclosure | 四家共识 | Anthropic Skill spec |
| `mcp__<server>__<tool>` 命名 | OH / baizhi-agent / fam-runtime | — |
| 直接 wrap Anthropic `mcp` SDK | baizhi-agent PR f6 教训 | — |
| MCP lifecycle 枚举(4 档) | 四家全集 | — |
| 每 skill 独立 storage 根目录 | baizhi-agent / fam-runtime | — |
| ToolCallContext 合同 | ADK Tool.run_async | baizhi-agent toolsets.py |
| 内置 SkillCatalogToolset | baizhi-agent | Fam READ_SKILL_TOOL |
| 命名冲突启动期检测 | baizhi-agent LlmAgentRunner | — |
| 不引 google.genai / langchain | GOALS.md Non-goals | — |

---

## 十三、迭代路线

| Stage | 内容 | 入口验证 |
|-------|------|---------|
| **0** | 仓库骨架 + 设计文档 + 模块 stub(本提案) | — |
| **1** | types / provider / toolset 实现 + 单元测试 | `pytest` 全绿 |
| **2** | skill 模块实现(frontmatter 解析 + SkillCatalogToolset) | 单元测试覆盖 progressive disclosure |
| **3** | loop 实现 + 集成测试(FakeProvider + EchoToolset) | 多轮 + 取消 + max_rounds |
| **4** | mcp 模块实现(wrap `mcp` SDK + 4 种 lifecycle) | 真打 DashScope WebSearch |
| **5** | runner 门面 + 端到端样例 | 完整跑通一个 flashidea 类 skill |
| **6** | 接进 baizhi-agent(替换其内部 LlmAgentRunner + toolsets) | baizhi-agent `pytest` 不退化 |
| **7** | 接进 fam-runtime | fam-runtime `pytest` 不退化 |

**回退路径**:每 stage 都是独立 commit + tag。任意 stage 发现抽象不对,可
回滚到上一 stage 重做;baizhi-agent / fam-runtime 在 stage 6/7 之前不受影响。

---

## 十四、开放问题

1. **stream 怎么进 Event 流**:目前 `chat_stream` 是 provider 层方法,但 loop
   里只用了 `chat`。要在 loop 增加 `run_stream()` 还是把 stream 收口在 provider
   层(只暴露 final response)?
2. **Skill 版本固定**:`enabled_skills: list[str]` 现在只传名字,版本默认拿
   latest。要不要支持 `["paper_review@1.2.3"]` 这种 pin?
3. **多模态**:`Message.content` 当前是 str。`list[ContentBlock]` 何时引入?
   等真有图片 / 音频 input 时。
4. **错误传播**:`provider.chat` 抛异常时,是 yield `Event(kind="error")`
   后退出,还是直接向上抛?目前倾向前者(更"事件流"),但要确认。

这些不阻塞 Stage 1,但 Stage 3(loop 实现)之前要定。

---

## Errata · MCP lifecycle 4 档枚举撤回(2026-05-24)

**类别**:本提案 § 八 中提出的 `McpLifecycle` 4 档枚举(per_call / per_run /
per_tenant / global)在 [`tech-design.md` § 7.2](tech-design.md#72-lifecycle-设计--实例生命周期即-session-生命周期)
中**已撤回**,采纳 ADK 的"实例生命周期 = session 生命周期"模式。

**为什么撤回**:
- `PER_TENANT` 把"tenant"这一上层概念塞进 SDK 命名空间,与 SDK Non-goals
  ("不做多租户")直接冲突
- `GLOBAL` 暗示 SDK 持有进程级缓存,挤占使用方的部署决策权
- ADK / OpenHarness / baizhi-agent / fam-runtime **均无 lifecycle 枚举**,
  各自按部署形态在使用方代码里 hardcode 一档
- 4 档行为仍可全部实现,只是控制点回到使用方(`McpToolset` 何时构造、何时 aclose)

**连带变更**(详见 tech-design.md):
- `McpServerConfig.lifecycle` 字段删除
- `Runner.__init__` 参数从 `(provider, skill_registry, mcp_servers, extra_toolsets)`
  简化为 `(provider, toolsets)` —— Runner 不再为使用方 new toolset
- Stage 4 退出标准更新:不再"实现 PER_CALL + PER_RUN",改为"lazy connect + aclose
  打通,4 种使用方 lifecycle 用例各跑一次"

**提案 § 八 / § 十二 / § 十三的原文不动**(append-only),仅以本 errata 标注修订。

---

