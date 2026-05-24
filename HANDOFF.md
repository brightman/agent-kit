# agent-kit · Handoff Note

写给在 `/Users/karama/Documents/baizhi/agent-kit/` 根目录起 session 的下一个
Claude/Codex agent。读完应能直接接 Stage 4 干活。

---

## 一句话

agent-kit 是一个从 baizhi-agent / fam-runtime / ADK / OpenHarness 四家共识里
抽出来的最小 Python 工具包,提供 agent loop + skill + MCP 的机制(不绑策略)。
当前进度 = **Stage 3 已落地**,157 tests 全绿,Stage 4(MCP)是下一步。

---

## 一分钟速读

| 维度 | 状态 |
|---|---|
| 仓库根 | `/Users/karama/Documents/baizhi/agent-kit/`(无 remote,本地 main)|
| 最新 commit | Stage 3 — Runner + RunResult + workspace lifecycle |
| 最新 tag | `stage-3`(也存在 `stage-0/1/2`, `tech-design-v1/v2/v3`)|
| 测试 | **157/157 通过**,`~0.1s`(test_runner.py 加了 21 个) |
| Python | 3.11(`.venv/` 已建好,gitignored)|
| 关键 deps | `mcp>=1.0` / `pyyaml` / `pydantic`;dev: `pytest` / `pytest-asyncio` |

---

## 设计决议历史(不要重提)

按落地顺序,每条都已写进 tech-design / proposal,**不要重新讨论除非有新证据**:

| 决议 | 凭证 | 简述 |
|---|---|---|
| 项目名 = `agent-kit`(不是 baizhi-sdk / agent-sdk) | tag `stage-0` | "kit" 比 "sdk" 轻;PyPI 可注册 |
| MCP lifecycle 枚举撤回 | commit `93a110e` + proposal.md errata | 4 档 enum 把"tenant"塞进 SDK,违边界;改为 ADK 的实例生命周期 = session 生命周期 |
| Q1 stream:`RunRequest.stream=False` 默认,opt-in | tech-design § 3.7 / § 8.5 | 实际实现推到 Stage 5 |
| Q2 skill 版本 pin:`name@version` 字符串 | tech-design § 6.6 / skill.py `parse_skill_ref` | 已实现 + 5 个测试 |
| Q3 多模态:Stage 0-5 维持 `str` content | tech-design § 3.4 | 真需求出现再 break |
| Q4 错误传播:`run` yield error event,`run_to_completion` raise | tech-design § 9.2 | **Stage 3 全部落地** |
| Context compaction:ContextCompactor Protocol + 内置 TruncatingCompactor | tag `tech-design-v2` + Stage 1/2 | safe_split_messages 抄 ADK,microcompact 抄 OpenHarness |
| Hooks:4 个 ABC method(before/after × model/tool) | tag `tech-design-v3` + Stage 2 | 收敛过程 6→4→2→1→4;最终 4 个 + 装饰器指南 |
| `mcp__<server>__<tool>` 命名 | tech-design § 7.4 | 与 OH/baizhi-agent/fam-runtime 共识 |
| SDK 不内置 LLM 摘要式 compaction | tech-design § 15 | policy,留给使用方 |
| **Skill catalog 注入 = discovery,不加 `skill_registry` 参数** | tech-design § 10.1 (Stage 3 修订) | Runner 在 toolsets 列表里 isinstance 找 SkillCatalogToolset,从中读 registry + tenant_id;`isinstance` 命中多个时取第一个;version pin 不影响 prelude(description 跨版本稳定) |
| **AgentLoop.aclose() 委托给内部 router** | tech-design § 9.3 (Stage 3 修订) | Runner 不自建 router,单 Router 语义干净 |
| **ctx.emit Stage 3 = no-op** | tech-design § 10.2 | 进度事件路由 Stage 3.5/4 候选 |

---

## 已完成模块(都通过测试)

| 模块 | 实现状态 | 关键 API |
|---|---|---|
| `types.py` | ✅ frozen + invariants + to_dict/from_dict | `Message`, `ToolCall`, `ToolResult`, `Event`, `EventKind` |
| `provider.py` | ✅ Protocol + dataclasses(stub providers 见 Stage 5)| `LlmProvider`, `LlmResponse`, `LlmDelta`, `ToolSchema` |
| `toolset.py` | ✅ Router 唯一性 + execute 兜底 + aclose 反序 swallow | `BaseToolset`, `ToolCallContext`, `ToolsetRouter` |
| `skill.py` | ✅ parse_frontmatter + parse_skill_ref + SkillCatalogToolset(3 tools)| `parse_frontmatter`, `SkillRegistry` (ABC), `SkillCatalogToolset` |
| `tokens.py` | ✅ chars/4 × 4/3 + per-msg overhead | `estimate_text_tokens`, `estimate_messages_tokens` |
| `context.py` | ✅ TruncatingCompactor + safe_split_messages + _assert | `ContextCompactor` (Protocol), `TruncatingCompactor`, `safe_split_messages` |
| `hooks.py` | ✅ Hook ABC, 4 no-op async methods | `Hook` |
| `loop.py` | ✅ AgentLoop.run() 非 stream + `aclose()` 委托 | `AgentLoop`, `RunRequest` |
| `runner.py` | ✅ `Runner.run()` + `run_to_completion()` + `RunResult` + workspace lifecycle + skill catalog discovery | `Runner`, `RunResult` |
| `__init__.py` | ✅ 公开 API re-export | 见 `agent_kit/__init__.py` |

---

## 未完成模块(stub)

| 模块 | 何时做 | 缺什么 |
|---|---|---|
| `mcp.py` | **Stage 4 = 下一步** | `McpToolset` 真实现 wrap Anthropic `mcp` SDK + `${VAR}` 替换 + lazy connect + idempotent aclose + `toolsets_from_configs` helper |
| `loop.py` stream 路径 | Stage 5 | 当前 `request.stream=True` 走 error event;spec 在 § 8.5 |
| `ctx.emit` 路由 | Stage 3.5 或 Stage 4 | 当前是 no-op;真路由需要 asyncio.Queue 把 emit 合并进 yield 流 |

---

## Stage 4 接力(立即可干)

**目标**:`McpToolset` 真实现 wrap Anthropic `mcp` SDK + lifecycle 干净 + 4 种使用方
lifecycle 用例(per-call / per-run / per-tenant / global)各一个测试。

**spec 参考**:`docs/tech-design.md` § 7 全部(McpServerConfig / McpToolset /
lifecycle / 命名)+ § 14 Stage 4 退出标准。

### 任务清单(从 spec § 7 派生)

1. **`agent_kit/mcp.py` 真实现**:
   - `McpServerConfig` 字段校验(name / transport / command / url / env / connect_timeout)
   - `${VAR}` 环境变量替换(command / args / url / headers / env values)
   - `McpToolset.__init__(config)` —— **不**立刻 connect(spec § 7.2 lazy)
   - `McpToolset._ensure_connected()` —— 首次 build_schemas / execute 触发
     - `mcp.client.stdio.stdio_client` 或 `mcp.client.sse.sse_client` 按 transport 选
     - `ClientSession(transport_read, transport_write)`
     - `await session.initialize()` 后 `list_tools()`
     - cache 工具 schema(`McpServerName__toolname`)+ session 引用
   - `McpToolset.build_schemas()` —— 触发 lazy connect,返回 cached schemas
     - **注意**:spec § 5.1 说 `build_schemas` 推荐同步;Router init 期间已调
       —— 这意味着 **lazy connect 必须发生在 build_schemas 第一次调用时**,
       而 build_schemas 不能是 async 方法。一个解决:把 connect 推到 execute
       (build_schemas 返回空 OR cached);或者 Router init 先 async pre-init MCP
     - **决策建议**:Router init 期间不 connect;`build_schemas` 第一次调用 raise
       `RuntimeError("MCP server not connected; call await connect() first")`;
       使用方/Runner 在 setup 时显式 `await mcp_toolset.connect()`。
       **这是 spec 不一致 ——先决议再实现**。
   - `McpToolset.execute(call, ctx)` —— `session.call_tool(name, args)` + 序列化结果
   - `McpToolset.aclose()` —— 关 session + transport;idempotent + safe 重复调用
   - `toolsets_from_configs(configs: list[McpServerConfig]) -> list[McpToolset]`
     —— spec § 9.1 例子里出现

2. **`tests/test_mcp.py`**:
   - McpServerConfig 字段校验(空 name / 重名 / 缺 url 等)
   - `${VAR}` 替换 + 缺失变量行为(raise vs leave)
   - 用 `mcp.server.fastmcp.FastMCP` 起 in-process server(stdio 或 memory),
     验证:connect → list_tools → call_tool → aclose 完整链
   - aclose idempotent(重复调不抛)
   - 4 个 lifecycle 场景:per-call / per-run / per-tenant / global
   - tool name 前缀:`mcp__<server>__<tool>`(spec § 7.4)
   - 异常路径:server crash → execute 返回 ToolResult(is_error=True);
     aclose 在 crashed session 上仍然 idempotent

3. **解决 spec § 7 与 § 5 的 lazy connect 张力**:
   - 必须先在 tech-design.md / HANDOFF.md 决策 + commit "Fix MCP lazy connect spec"
   - 不要在 Stage 4 代码里"自然"做了

4. **commit + tag**:
   ```bash
   git -C /Users/karama/Documents/baizhi/agent-kit add ...
   git -C /Users/karama/Documents/baizhi/agent-kit commit -m "Stage 4: McpToolset real impl"
   git -C /Users/karama/Documents/baizhi/agent-kit tag stage-4
   ```

### Stage 4 退出标准(对照 tech-design § 14)

| 标准 | 怎么验 |
|---|---|
| 真打 MCP server OK | in-process FastMCP test 跑通完整链 |
| 4 种 lifecycle 都跑过 | 各一个 test |
| 命名规则 `mcp__<server>__<tool>` | test_mcp.py 断言 |
| 170+ 测试全绿(估算)| `.venv/bin/python -m pytest` |

---

## 操作环境注意

### cwd 漂移问题

本 session 跟 baizhi-agent / Fam 同一棵 macOS 桌面 app 项目树,bash 工具的
默认 cwd 可能漂回 `/Users/karama/Documents/Fam/`。**两个习惯**:

1. 所有 pytest:`cd /Users/karama/Documents/baizhi/agent-kit && .venv/bin/python -m pytest`
2. 所有 git:`git -C /Users/karama/Documents/baizhi/agent-kit ...`

如果新 session 是从 agent-kit/ 直接起的,这个问题应该消失(cwd 默认就对)。
但仍建议养成 `git -C` 习惯,防御性。

### .env / secrets

目前 agent-kit 不需要 secrets。Stage 4 接 MCP 真打时可能需要(例如
`DASHSCOPE_API_KEY` for WebSearch);加进 `.gitignore` 里的 `.env.local`
或类似,**永不 commit**。

### Codex 协作

baizhi-agent 项目有 CODEX.md 约定 Claude × Codex 协作;agent-kit 目前**没有**,
因为只有 Claude 一个 agent 在搞。等 Stage 6 真接进 baizhi-agent 时可能加入。

---

## 决议未来要做的事(不阻塞 Stage 4,但 Stage 4 前要决定一项)

| 决策 | 推荐 | 何时定 |
|---|---|---|
| MCP lazy connect 与 sync `build_schemas` 张力(见 Stage 4 任务 #3) | `build_schemas` 第一次调用 raise 提示要先 `await connect()`;或 Router init 期 async pre-init | **Stage 4 开始前** |
| `ctx.emit` 真路由(asyncio.Queue 合并进 yield 流) | 是 | Stage 3.5 或 Stage 4 中 |
| Runner 是否暴露 `cancel(run_id)`? | Stage 3 没加;若需要,加 `_active_runs` dict + `cancel(run_id) -> bool` | 看真使用方需求 |
| `ctx.run_state` 字典 hook 之间命名冲突,推不推荐惯例? | 推荐"用 Hook 类名做 key 前缀";写进 hook.py docstring | 任何时候 |
| OpenTelemetry / Langfuse exporter? | **不在 SDK** —— event 流 + 上层订阅。在 README 加段说明 | Stage 6 前 |

---

## 跟用户协作的方式

- 用户(karama)风格:**经常 push back 设计**,不要怕被挑战;那是好事
- 用户中文为主,代码注释 / commit message 中文 OK
- 用户喜欢简短答复,1-2 句结论先,再上证据
- **不要长篇总结**,跑完测试给数字
- 用户关心 git 历史可读性 —— 每个 commit message 写得起码 2 段(what + why + 影响)
- 重要架构决策落 commit message + tech-design.md;**不只是聊天里说一下**

---

## 文件清单(本 session 新增 / 修改 + tag)

### Stage 0(scaffold + 命名 + rename)
- `9a83a5f` Stage 0 仓库骨架(baizhi-sdk 命名)
- `9d2fc32` tag `stage-0` — rename → agent-kit

### Stage 1(基础模块真实现)
- `94dff9b` tag `tech-design-v1` — tech-design.md 落地 + Q1-Q4 决议
- `93a110e` Drop McpLifecycle enum
- `f9419a8` tag `tech-design-v2` — context compaction spec
- `f0df42f` tag `tech-design-v3` — Hook 基类
- `6420b45` tag `stage-1` — **types / provider / toolset / skill / tokens / context 全部真实现 + 116 tests**

### Stage 2(loop)
- `8e4c837` tag `stage-2` — **AgentLoop.run() 非 stream + 20 集成 tests = 136 total**

### Stage 3(runner)
- Stage 3 commit — **Runner.run() / run_to_completion() / RunResult + workspace lifecycle + skill catalog discovery + AgentLoop.aclose() + 21 集成 tests = 157 total**
- tag `stage-3`

---

## 给新 agent 的第一条命令建议

```bash
cd /Users/karama/Documents/baizhi/agent-kit
.venv/bin/python -m pytest 2>&1 | tail -5
# Expected: 157 passed in <1s

# 看现状
cat HANDOFF.md
cat docs/tech-design.md | sed -n '/^## 7\./,/^## 8\./p'   # Stage 4 重点

# 开 Stage 4(先决议 lazy connect 张力,见 Stage 4 任务 #3)
git -C /Users/karama/Documents/baizhi/agent-kit log --oneline --decorate | head
```

---

最后更新:2026-05-24,stage-3 落地后。
