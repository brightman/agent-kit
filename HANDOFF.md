# agent-kit · Handoff Note

写给在 `/Users/karama/Documents/baizhi/agent-kit/` 根目录起 session 的下一个
Claude/Codex agent。读完应能直接接 Stage 3 干活。

---

## 一句话

agent-kit 是一个从 baizhi-agent / fam-runtime / ADK / OpenHarness 四家共识里
抽出来的最小 Python 工具包,提供 agent loop + skill + MCP 的机制(不绑策略)。
当前进度 = **Stage 2 已落地**,136 tests 全绿,Stage 3(Runner)是下一步。

---

## 一分钟速读

| 维度 | 状态 |
|---|---|
| 仓库根 | `/Users/karama/Documents/baizhi/agent-kit/`(无 remote,本地 main)|
| 最新 commit | `8e4c837` — `Stage 2: AgentLoop.run() implementation + 20 integration tests` |
| 最新 tag | `stage-2`(也存在 `stage-0`, `stage-1`, `tech-design-v1/v2/v3`)|
| 测试 | **136/136 通过**,`.07s` |
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
| Q4 错误传播:`run` yield error event,`run_to_completion` raise | tech-design § 9.2 | run 已实现;run_to_completion 是 Stage 3 |
| Context compaction:ContextCompactor Protocol + 内置 TruncatingCompactor | tag `tech-design-v2` + Stage 1/2 | safe_split_messages 抄 ADK,microcompact 抄 OpenHarness |
| Hooks:4 个 ABC method(before/after × model/tool) | tag `tech-design-v3` + Stage 2 | 收敛过程 6→4→2→1→4;最终 4 个 + 装饰器指南 |
| `mcp__<server>__<tool>` 命名 | tech-design § 7.4 | 与 OH/baizhi-agent/fam-runtime 共识 |
| SDK 不内置 LLM 摘要式 compaction | tech-design § 15 | policy,留给使用方 |

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
| `loop.py` | ✅ AgentLoop.run()**非 stream**,集成 compactor + 4 hooks + cancel + max_rounds | `AgentLoop`, `RunRequest` |

---

## 未完成模块(stub)

| 模块 | 何时做 | 缺什么 |
|---|---|---|
| `runner.py` | **Stage 3 = 下一步** | `Runner.run()` (yield Event) + `Runner.run_to_completion()` (raises on error) + workspace 生命周期 + skill catalog 注入到 system_prelude |
| `mcp.py` | Stage 4 | `McpToolset` 真实现 wrap Anthropic `mcp` SDK + `${VAR}` 替换 + lazy connect + idempotent aclose + `toolsets_from_configs` helper |
| `loop.py` stream 路径 | Stage 5 | 当前 `request.stream=True` 走 error event；spec 在 § 8.5 |

---

## Stage 3 接力(立即可干)

**目标**:`Runner` 真实现 + `RunResult` 数据类 + 资源生命周期 + 端到端测试。

**spec 参考**:`docs/tech-design.md` § 3.8(RunResult)+ § 9(Runner)+ § 10
(System prompt 组装)+ § 14 Stage 3 退出标准。

### 任务清单

1. **`agent_kit/runner.py` 实现**:
   - `Runner.run(request: RunRequest) -> AsyncIterator[Event]`:
     - 分配 `run_id`(ULID-ish 同 `loop._new_event_id` 模式)
     - mkdir `workspace = workspace_root / run_id`
     - 构造 `ToolCallContext`(cancel=asyncio.Event 新建)
     - 拼装 `system_prelude`:Runner-level + RunRequest-level + **skill catalog 索引**(从 `SkillRegistry` 拿 enabled_skills 的 frontmatter,按 § 10 格式注入)
     - 起 `AgentLoop(provider, toolsets, hooks, compactor, system_prelude=...)`
     - `async for evt in loop.run(req, ctx): yield evt`
     - 全程 try/except 把异常 wrap 成 `Event(kind="error", stage="setup" 或 "loop")` + `return`
     - finally: `router.aclose()` + `shutil.rmtree(workspace)`
   - `Runner.run_to_completion(request) -> RunResult`:
     - 内部 `async for` 调 `self.run()`
     - 收集 events
     - 遇 `error` event → raise `RuntimeError(payload["message"])`
     - 遇 `final_text` event → 记录 `final_text`
     - 跑完返回 `RunResult(final_text, events, rounds_used, cancelled, error)`

2. **`agent_kit/types.py` 加 `RunResult` 数据类**(其实在 § 3.8 已 spec):
   ```python
   @dataclass
   class RunResult:
       final_text: str | None
       events: list[Event]
       rounds_used: int
       cancelled: bool
       error: dict | None
   ```
   或者放在 `runner.py`(避免 types.py 引入业务态)。**建议放 runner.py**,
   types.py 保持纯数据。

3. **Skill catalog 注入**:
   - Runner 拿到 `SkillRegistry`(从一个新参数 `skill_registry` 还是 toolsets 里 find?)
   - 决策建议:**新参数** `skill_registry: SkillRegistry | None = None`,
     Runner 持有它,既用来构造 `SkillCatalogToolset` 也用来读 frontmatter 注入 prompt
   - 注意:这跟之前 "Runner 不为你 new toolset" 的决议有张力 ——
     Runner 仍**不为你 new MCP toolset**,但可以 new SkillCatalogToolset(因为它需要 registry+tenant_id,不构造它使用方就得手动重复)
   - 这个决议要先在 HANDOFF.md / tech-design.md 里 explicit;别"自然"做了

4. **`tests/test_runner.py`(预计 15-20 测试)**:
   - run() yield events(透传 loop 的 + Runner 自加的 setup error event)
   - run_to_completion 返回 RunResult,final_text 正确
   - run_to_completion 遇 error event raise RuntimeError
   - workspace mkdir + 清理(end-to-end 跑完检查目录是否存在)
   - 异常路径:provider 抛 → error event + workspace 仍清理
   - cancel:run_to_completion 反映 `cancelled=True`
   - skill catalog 注入 system prompt(prelude 包含 enabled skills 的 name/version/description)
   - 验证 toolsets 被 aclose

5. **commit + tag**:
   ```bash
   git -C /Users/karama/Documents/baizhi/agent-kit add ...
   git -C /Users/karama/Documents/baizhi/agent-kit commit -m "Stage 3: Runner + RunResult + workspace lifecycle"
   git -C /Users/karama/Documents/baizhi/agent-kit tag stage-3
   ```

### Stage 3 退出标准(对照 tech-design § 14)

| 标准 | 怎么验 |
|---|---|
| `RunResult` 行为符合契约 | run_to_completion 测试覆盖 final_text / error / cancelled 三态 |
| workspace 创建 + 删除 | tmp_path fixture + 检查 path 跑完不存在 |
| router.aclose 被调 | 用记录性 stub toolset 验证 `.closed >= 1` |
| 150+ 测试全绿(估算)| `.venv/bin/python -m pytest` |

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

## 决议未来要做的事(不阻塞 Stage 3,但 Stage 3 前要决定一项)

| 决策 | 推荐 | 何时定 |
|---|---|---|
| Runner 构造接 `skill_registry` 参数(自己 new SkillCatalogToolset)还是要求使用方手动塞? | 推荐前者(降低使用方样板) | **Stage 3 开始前** |
| Runner 是否暴露 `cancel(run_id)` 方法,还是只接受 ctx.cancel 引用? | Stage 2 时延后,Stage 3 实施时定 | Stage 3 中 |
| `ctx.run_state` 字典 hook 之间命名冲突,推不推荐惯例? | 推荐"用 Hook 类名做 key 前缀";写进 hook.py docstring | Stage 3 文档化 |
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

---

## 给新 agent 的第一条命令建议

```bash
cd /Users/karama/Documents/baizhi/agent-kit
.venv/bin/python -m pytest 2>&1 | tail -5
# Expected: 136 passed in <1s

# 看现状
cat HANDOFF.md
cat docs/tech-design.md | sed -n '/^## 14/,/^## 15/p'   # Stage roadmap

# 开 Stage 3
git -C /Users/karama/Documents/baizhi/agent-kit log --oneline --decorate | head
```

---

最后更新:2026-05-24,stage-2 落地后。
