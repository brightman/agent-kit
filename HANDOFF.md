# agent-kit · Handoff Note

写给在 `/Users/karama/Documents/baizhi/agent-kit/` 根目录起 session 的下一个
Claude/Codex agent。读完应能直接接 Stage 5 干活。

---

## 一句话

agent-kit 是一个从 baizhi-agent / fam-runtime / ADK / OpenHarness 四家共识里
抽出来的最小 Python 工具包,提供 agent loop + skill + MCP 的机制(不绑策略)。
当前进度 = **Stage 4 + Runner.workspace_provider + 不内置 sandbox 决议** 已落地,
197 tests 全绿,**Stage 5 改为 baizhi-agent 接入**(原 Stage 5 stream 已推迟,
理由见 spec § 14 修订 2026-05-24)。

---

## 一分钟速读

| 维度 | 状态 |
|---|---|
| 仓库根 | `/Users/karama/Documents/baizhi/agent-kit/`(无 remote,本地 main)|
| 最新 commit | `1410c50` — baizhi pptx + websearch e2e harness;前一个 `32765a2` Runner.workspace_provider + § 16 sandbox-by-recipe |
| 最新 tag | `stage-4`(stage-5/6 标号 + 内容已重排,见下) |
| 测试 | **197 通过 + 1 skipped(live)**,`~0.35s` |
| Python | 3.11(`.venv/` 已建好,gitignored) |
| 关键 deps | `mcp>=1.0` / `pyyaml` / `pydantic`;dev: `pytest` / `pytest-asyncio` / `anyio` / `python-pptx`(给 baizhi e2e)|

---

## 设计决议历史(不要重提)

按落地顺序,每条都已写进 tech-design / proposal,**不要重新讨论除非有新证据**:

| 决议 | 凭证 | 简述 |
|---|---|---|
| 项目名 = `agent-kit`(不是 baizhi-sdk / agent-sdk) | tag `stage-0` | "kit" 比 "sdk" 轻;PyPI 可注册 |
| MCP lifecycle 枚举撤回 | commit `93a110e` + proposal.md errata | 4 档 enum 把"tenant"塞进 SDK,违边界;改为 ADK 的实例生命周期 = session 生命周期 |
| Q1 stream:`RunRequest.stream=False` 默认,opt-in;**实现推迟到 Stage 7+** | tech-design § 3.7 / § 8.5 / § 14 修订 2026-05-24 | server-side machinery 用不到 token UX;mid-LLM cancel non-stream 轮间也能做;§ 4.4 tool_call delta 完整也没增益 |
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
| `contrib/skills.py` | ✅ `FilesystemSkillRegistry` reference 实现(读 only,扫 `<skills_root>/<dir>/SKILL.md`) | `from agent_kit.contrib.skills import FilesystemSkillRegistry` |
| `_errors.py` | ✅ `unwrap_to_leaf(BaseExceptionGroup) → leaf BaseException` —— error event 诊断辅助 | 内部模块,loop / runner 自用 |

---

## 未完成模块(stub)

| 模块 | 何时做 | 缺什么 |
|---|---|---|
| Provider 真实现(LiteLLM / Anthropic SDK adapter) | Stage 5 = baizhi 接入时 | SDK 只有 Protocol;adapter **放使用方仓库**,不进 agent-kit |
| `loop.py` stream 路径 | **Stage 7+ 候选**(推迟,见 spec § 14 修订)| 当前 `request.stream=True` 走 error event;真消费者要求时再实现 |
| `ctx.emit` 路由 | Stage 7+ 候选 | 当前是 no-op;真路由需要 asyncio.Queue 把 emit 合并进 yield 流 |
| `Runner.cancel(run_id)` | Stage 7+ 候选 | 今天只能 hook 内 set ctx.cancel;外部触发要 `_active_runs` dict |
| `Runner.run_sync_stream()` 后台线程 + queue(ADK 形态)| Stage 7+ 候选 | 当前只有 `run_sync()` 一次性返,无 sync 实时事件流 |

---

## Stage 5 接力(立即可干)

**目标**:**baizhi-agent 接入** —— 把 baizhi-agent 内部 runner 替换成
`agent_kit.Runner`,真 provider / real SkillRegistry / real WebSearch MCP
接通,pptx e2e 跑通。原"Stage 5 stream"已推迟到 Stage 7+(理由见 spec § 14 修
订;一句话:agent-kit 是 server-side machinery,现有 event 流够覆盖 90%,
stream 真要时再做)。

### 已有的接入脚手架(本次 session 已交付)

- `tests/test_baizhi_pptx_websearch_integration.py` —— **in-process** 集成测试,
  覆盖真 pptx SKILL.md bundle + `web-search` MCP server_id + 整个 loop 用
  scripted provider 模拟,无网络、确定性。当前**全绿**
- `tests/test_baizhi_e2e_live_pptx_websearch.py` —— **opt-in live** 测试,
  `@pytest.mark.live`,默认跳过。`pytest -m live` + 真 LLM/MCP secrets
  exported 才跑;产物落 `e2e-output/`(已 gitignore)
- `agent_kit/mcp.py` 已支持 `web-search` 这种带 `-` 的 server name
- `agent_kit/skill.py` 已支持 SKILL.md 不显式声明 version(默认 "0.0.0",
  baizhi 现有 bundled skills 用这个形态)
- `Runner.workspace_provider`(§ 9.1)+ `ctx.workspace_ephemeral`(§ 5.2)
  让 baizhi 的 tenant_agent 持久空间能映射进 SDK

### 任务清单

1. **真 provider 接通**:
   - baizhi 现在用什么 provider?(LiteLLM / Anthropic SDK 直连 / 别的?)
   - 写一个 thin adapter 实现 `agent_kit.LlmProvider` Protocol,
     **不**进 SDK,放 baizhi-agent 仓库里
   - 用 `tests/test_baizhi_pptx_websearch_integration.py` 的 scripted 形态
     作模板,替换成真 provider 跑

2. **真 SkillRegistry 接通**:
   - baizhi 现在的 skill 持久层在哪?接口跟 `agent_kit.SkillRegistry` 一致吗?
   - 写一个 baizhi 自家的 `SkillRegistry` 子类(filesystem / db / 别的)
   - 不进 SDK

3. **真 WebSearch MCP 接通**:
   - 用 `McpServerConfig(name="web-search", transport=..., url=...)` 接
     baizhi 的 WebSearch MCP server
   - SDK 已经支持,只是配置

4. **workspace 注入**:
   - 写 `workspace_provider`(callable)把 baizhi tenant_agent 空间映射成
     `ctx.workspace`(spec § 9.1 给了示例)
   - 注意:provider 自己负责 mkdir,SDK 不动

5. **跑 live e2e**:
   - 设好 secrets(WebSearch API key 等)
   - `pytest -m live tests/test_baizhi_e2e_live_pptx_websearch.py`
   - 产物在 `e2e-output/<filename>.pptx` 验
   - 跑通 = Stage 5 主要工作完成

6. **暴露的 SDK gap → 回填**:
   - 接入过程一定会暴露 spec / API 不足。**记录下来**,回头补 spec § 16 recipes
     或 SDK 本身。可能候选:
     - `ctx.emit` 真路由(toolset 进度事件)
     - `Runner.cancel(run_id)`
     - SkillRegistry 缺方法 / 形态不对
     - workspace_provider 边界(per-run 子目录 vs agent 级)
   - 每个 gap 先讨论再 commit,不要"自然"做了(对照之前 Stage 3 / 4 修订风格)

7. **commit + tag**:
   ```bash
   git -C /Users/karama/Documents/baizhi/agent-kit commit -m "Stage 5: baizhi-agent integration + recipes + ..."
   git -C /Users/karama/Documents/baizhi/agent-kit tag stage-5
   ```
   注意:大部分 Stage 5 工作在 **baizhi-agent 仓库**,不是 agent-kit。agent-kit
   这边主要是 recipes / gap 回填。

### Stage 5 退出标准(对照 tech-design § 14 修订)

| 标准 | 怎么验 |
|---|---|
| baizhi-agent pytest 不退化 | 在 baizhi-agent 仓库跑 |
| pptx e2e live test 跑通 | `pytest -m live tests/test_baizhi_e2e_live_pptx_websearch.py` 绿 + e2e-output/ 有有效 pptx |
| recipes 写齐 4 篇(workspace / SRT / MCP / skill-scripts) | `docs/recipes/` 4 个文件 |
| 200+ tests 全绿(估算)| `.venv/bin/python -m pytest` |

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
- `4ecd089` tag `stage-4` — **McpToolset 真实现(3 transports + ${VAR} + idempotent connect/aclose)+ Runner pre-warm + 33 tests = 190 total**

### Stage 4 后续(sandbox 决议 + workspace_provider)
- `32765a2` **Runner.workspace_provider + § 16 不内置 sandbox 决议 + 5 tests**(讨论历史:对比 ADK BaseCodeExecutor / openai-agents BaseSandboxSession,三场景都有外部解 → SDK 不内置)
- `1410c50` **MCP/SKILL.md 名规则放宽 + baizhi pptx + websearch e2e 脚手架**(包含 in-process integration test + opt-in live test);**197 total**

### Stage 5 (新) = baizhi-agent 接入(进行中)

工作大部分在 baizhi-agent 仓库,不在 agent-kit。详见上面"Stage 5 接力"。

### ~~Stage 5 (原)~~ stream 实现
**推迟到 Stage 7+**。理由 + 决策见 spec § 14 修订 2026-05-24。

---

## 给新 agent 的第一条命令建议

```bash
cd /Users/karama/Documents/baizhi/agent-kit
.venv/bin/python -m pytest 2>&1 | tail -5
# Expected: 197 passed, 1 skipped in <1s

# 看现状
cat HANDOFF.md
cat docs/tech-design.md | sed -n '/^## 14/,/^## 15/p'  # 路线图(stream 推迟,Stage 5=baizhi 接入)
cat docs/tech-design.md | sed -n '/^## 16/,/^## 附录/p' # sandbox 决议
ls tests/test_baizhi*.py                                 # baizhi 集成脚手架

# 开 Stage 5(baizhi-agent 接入)
git -C /Users/karama/Documents/baizhi/agent-kit log --oneline --decorate | head
```

---

最后更新:2026-05-24,Stage 4 后续 + 路线图重排后。
