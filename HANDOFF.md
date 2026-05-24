# agent-kit · Handoff Note

写给在 `/Users/karama/Documents/baizhi/agent-kit/` 根目录起 session 的下一个
Claude/Codex agent。读完应能直接接 Stage 5 干活。

---

## 一句话

agent-kit 是一个从 baizhi-agent / fam-runtime / ADK / OpenHarness 四家共识里
抽出来的最小 Python 工具包,提供 agent loop + skill + MCP 的机制(不绑策略)。
当前进度 = **Stage 4 已落地**,190 tests 全绿,Stage 5(stream)是下一步。

---

## 一分钟速读

| 维度 | 状态 |
|---|---|
| 仓库根 | `/Users/karama/Documents/baizhi/agent-kit/`(无 remote,本地 main)|
| 最新 commit | Stage 4 — McpToolset 真实现 + Runner pre-warm + 33 tests |
| 最新 tag | `stage-4`(也存在 `stage-0/1/2/3`, `tech-design-v1/v2/v3`)|
| 测试 | **190/190 通过**,`~0.35s`(test_mcp.py 加了 33 个) |
| Python | 3.11(`.venv/` 已建好,gitignored)|
| 关键 deps | `mcp>=1.0` / `pyyaml` / `pydantic`;dev: `pytest` / `pytest-asyncio` / `anyio` |

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
| **MCP lazy connect:explicit `async connect()` + Runner pre-warm** | tech-design § 7.5.1 (Stage 4 修订) commit `2aa9579` | 不在 `__init__` 里 asyncio.run。Runner 在 setup 阶段 `inspect.iscoroutinefunction(ts.connect)` 自动 await;直接用 AgentLoop 的使用方负责自调。connect/aclose 都 idempotent |

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
| `runner.py` | ✅ `Runner.run()` + `run_to_completion()` + `RunResult` + workspace lifecycle + skill catalog discovery + **toolset pre-warm** | `Runner`, `RunResult` |
| `mcp.py` | ✅ `McpServerConfig` + `McpToolset`(stdio/sse/http)+ `${VAR}` + lazy connect + idempotent aclose + `toolsets_from_configs` | `McpToolset`, `McpServerConfig`, `toolsets_from_configs` |
| `__init__.py` | ✅ 公开 API re-export(含 mcp) | 见 `agent_kit/__init__.py` |

---

## 未完成模块(stub)

| 模块 | 何时做 | 缺什么 |
|---|---|---|
| `loop.py` stream 路径 | **Stage 5 = 下一步** | 当前 `request.stream=True` 走 error event;spec 在 § 8.5 |
| Provider 真实现 | Stage 5 / Stage 6 | SDK 只有 Protocol;LiteLLM / Anthropic SDK 适配按使用方场景接 |
| `ctx.emit` 路由 | Stage 3.5 候选(已被 Stage 4 跳过) | 当前是 no-op;真路由需要 asyncio.Queue 把 emit 合并进 yield 流 |

---

## Stage 5 接力(立即可干)

**目标**:`AgentLoop.run()` 在 `request.stream=True` 时走 `provider.chat_stream`
路径,emit `llm_delta` event(每个 delta 一次),最后 emit `llm_response`
event(aggregate 完成)。

**spec 参考**:`docs/tech-design.md` § 8.5 + § 3.5 (llm_delta payload)
+ § 4.4 (LlmDelta dataclass) + § 14 Stage 5 退出标准。

### 任务清单

1. **`agent_kit/loop.py`**:把现在 `request.stream=True → error event` 的
   占位换成真路径。
   - 调 `await self._provider.chat_stream(messages, tools, ...)`,得到
     `AsyncIterator[LlmDelta]`
   - 每收一个 delta 就 yield `Event(kind="llm_delta", ...)`(parent = round_start)
   - 把 delta 聚合成最终 `LlmResponse`(累积 text、tool_calls、usage、finish_reason)
   - aggregate 完成后 yield `Event(kind="llm_response", ...)`(与 non-stream 一致,
     parent = round_start)
   - 后续逻辑(after_model hook / tool dispatch / final_text)完全复用 non-stream 路径
   - **partial tool_call delta**:spec § 4.4 已决定 LlmDelta.tool_call_delta 是
     完整 ToolCall(不是 partial)—— 不要自作主张拆分

2. **provider 实现需要一个真实例**:
   - 现有 `_ScriptedProvider` 只实现了 chat;加一个 `_ScriptedStreamProvider`
     在 test_loop.py(或新 test 文件),yield 一连串 LlmDelta
   - 测试用例:text-only stream / tool_call-only / mixed text+tool_call /
     finish_reason 在最后一个 delta / usage 在最后一个 delta

3. **provider 不支持 stream 时 fail-fast**:
   - spec § 4.1:provider.chat_stream MUST `raise NotImplementedError` 若不支持
   - loop catch 后 emit error event(stage="provider")

4. **tests/test_loop.py 加 stream 子集**(估算 10-15 测试):
   - 基本 stream → 多 delta → 一个 llm_response 收尾
   - cancel 在 stream 中段:停止消费 delta,emit cancelled
   - delta 流抛异常:emit error stage=provider
   - non-stream / stream 在同一 RunRequest 切换不会破坏 messages 序列

5. **commit + tag**:
   ```bash
   git -C /Users/karama/Documents/baizhi/agent-kit add ...
   git -C /Users/karama/Documents/baizhi/agent-kit commit -m "Stage 5: AgentLoop stream path + LlmDelta aggregation"
   git -C /Users/karama/Documents/baizhi/agent-kit tag stage-5
   ```

### Stage 5 退出标准(对照 tech-design § 14)

| 标准 | 怎么验 |
|---|---|
| stream/non-stream 切换不破坏 messages 流 | 同一 _ScriptedToolset,两次 run(stream + non-stream),tool_call 序列一致 |
| `llm_delta` event payload 与 spec § 3.5 一致 | 测试断言 |
| provider 不支持 stream → fail-fast 而不挂起 | NotImplementedError 测试 |
| 200+ 测试全绿(估算)| `.venv/bin/python -m pytest` |

---

## 已完成 Stage 4(MCP)摘要 — 不要重做

- `agent_kit/mcp.py`:`McpServerConfig`、`McpToolset`、`toolsets_from_configs`、`${VAR}` 替换、3 种 transport(stdio / sse / http)、显式 `async connect()`、idempotent `aclose()`
- `agent_kit/runner.py`:setup 阶段 pre-warm 所有带 async `connect` 的 toolset
- `agent_kit/loop.py`:`aclose()` 委托(Stage 3 已加)
- `docs/tech-design.md § 7.5.1`(新):lazy connect 决策
- `tests/test_mcp.py`:33 测试覆盖配置校验 / ${VAR} / 命名 / 真链路(in-memory FastMCP) / idempotent / 4 lifecycle / Runner pre-warm

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

## 决议未来要做的事(不阻塞 Stage 5)

| 决策 | 推荐 | 何时定 |
|---|---|---|
| `ctx.emit` 真路由(asyncio.Queue 合并进 yield 流) | 是 | Stage 5/6 候选 |
| Runner 是否暴露 `cancel(run_id)`? | Stage 3 没加;若需要,加 `_active_runs` dict + `cancel(run_id) -> bool` | 看真使用方需求 |
| `ctx.run_state` 字典 hook 之间命名冲突,推不推荐惯例? | 推荐"用 Hook 类名做 key 前缀";写进 hook.py docstring | 任何时候 |
| OpenTelemetry / Langfuse exporter? | **不在 SDK** —— event 流 + 上层订阅。在 README 加段说明 | Stage 6 前 |
| MCP 真打 stdio subprocess 测试是否要加? | 在 Stage 6 接入真使用方时加 fixture(spawn 进程);Stage 4 只做 in-memory | Stage 6 |

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
- `fcdb699` tag `stage-3` — **Runner.run() / run_to_completion() / RunResult + workspace lifecycle + skill catalog discovery + AgentLoop.aclose() + 21 集成 tests = 157 total**

### Stage 4(MCP)
- `2aa9579` Fix MCP lazy-connect spec(决议先行)
- Stage 4 commit + tag `stage-4` — **McpToolset 真实现(3 transports + ${VAR} + idempotent connect/aclose)+ Runner pre-warm + 33 tests = 190 total**

---

## 给新 agent 的第一条命令建议

```bash
cd /Users/karama/Documents/baizhi/agent-kit
.venv/bin/python -m pytest 2>&1 | tail -5
# Expected: 190 passed in <1s

# 看现状
cat HANDOFF.md
cat docs/tech-design.md | sed -n '/^## 8\.5/,/^## 9\./p'   # Stage 5 重点(stream)

# 开 Stage 5
git -C /Users/karama/Documents/baizhi/agent-kit log --oneline --decorate | head
```

---

最后更新:2026-05-24,stage-4 落地后。
