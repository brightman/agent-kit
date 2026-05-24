# agent-kit · Project Guide for Claude Code

Minimal Python toolkit for building agent runtimes: agent loop + skill + MCP.

抽自 baizhi-agent / fam-runtime / Google ADK / OpenHarness 四家的共识收敛,
**不抄代码,抄思路**。

---

## 必读文档(改动前先看)

按重要性排序:

- [docs/tech-design.md](docs/tech-design.md) — **Stage 1 实施 spec**(契约级,
  RFC 2119 措辞)。每节都是 final;改签名前先改文档
- [docs/proposal.md](docs/proposal.md) — 原始抽象提案 + Q1-Q4 决议历史 +
  MCP lifecycle errata。看决策演化用
- [README.md](README.md) — 项目入口
- [HANDOFF.md](HANDOFF.md) — **最近 session 的接力状态**,改动前必读

---

## 项目结构

```
agent-kit/
├── agent_kit/           # Python 包(7 个模块)
│   ├── types.py        # Message / ToolCall / ToolResult / Event(frozen)
│   ├── provider.py     # LlmProvider Protocol + LlmResponse / LlmDelta
│   ├── toolset.py      # BaseToolset ABC + ToolCallContext + ToolsetRouter
│   ├── skill.py        # SKILL.md frontmatter parser + SkillCatalogToolset
│   ├── mcp.py          # McpServerConfig + McpToolset(wrap Anthropic `mcp` SDK)
│   ├── tokens.py       # estimate_*_tokens(chars/4 × 4/3)
│   ├── context.py      # TruncatingCompactor + safe_split_messages + assert
│   ├── hooks.py        # Hook ABC(4 no-op methods)
│   ├── loop.py         # AgentLoop.run()
│   └── runner.py       # Runner.run() / run_to_completion()
├── tests/              # pytest unit + integration
├── docs/               # tech-design / proposal
├── pyproject.toml
└── .venv/              # gitignored
```

---

## 4 家参考实现(都在隔壁)

| 项目 | 路径 | 用作 |
|---|---|---|
| **Google ADK** | `../adk-python-main/` | 主要参考,callback / MCP / safe_split 都抄自这里 |
| **OpenHarness** | `../OpenHarness/` | microcompact + chars/4 token 估算来源 |
| **baizhi-agent** | `../baizhi-agent/` | SDK 的第一个目标使用方;event_id / immutable publish 等设计来源 |
| **fam-runtime** | `../Fam/fam_runtime/` | SDK 的第二个目标使用方;per-family MCP 缓存等设计来源 |

不引入它们的依赖(尤其 google.genai / langchain);只抄思路,落地前在
`tech-design.md` 附录 A 记一行出处。

---

## 代码约定

### Python

- **Python 3.11+**(typing 用 `X | None` 而非 `Optional[X]`)
- 类型注解全开,`from __future__ import annotations` 顶部
- 数据类 frozen 默认(types.py / provider.py / skill.py)
- 文件级 docstring 中文,说明文件作用 + 在系统中的位置
- **不写 try/except 宽口子**:只在系统边界(provider API / 文件 I/O / 用户输入)做错误处理
- **不写防御性代码**:不会发生的分支别加
- 注释默认不写,只在 *为什么* 不显然时加一行

### Hook / Toolset / Compactor 是 Protocol / ABC

- 子类化 `Hook` 时只覆盖你需要的方法(no-op 默认)
- 自定义 toolset 继承 `BaseToolset`,**catch 自己的异常并返回 `ToolResult(is_error=True)`**(spec § 5.1);Router 兜底也会 catch
- 自定义 compactor 实现 `ContextCompactor` Protocol;loop 会 `_assert_tool_pairs_intact` 兜底,**别破坏 tool_call/tool_result 配对**

### Event 流是唯一对外形态

- `Runner.run()` / `AgentLoop.run()` yield `Event`,kind 决定 payload
- 别在 loop 里写 print / log warn —— 信息用 event 走出去
- 错误 emit `Event(kind="error", stage=...)` 而不是 raise(loop 内部已 catch + wrap)
- `Runner.run_to_completion()` 把 error event re-raise(Q4 双轨)

---

## SDK 边界(铁律)

agent-kit **不**做(送给使用方,Non-goals):

- 多租户队列 / LRU 驱逐 → baizhi-agent application 层
- 持久化 trace(SQLite / OTel exporter)→ 监听 Event 流自己写
- HTTP / WebSocket API → 上层框架(FastAPI / Flask)
- 前端 UI → 上层
- 鉴权 / 配额 → 上层 OR 用 `before_tool` / `before_model` hook
- Memory / Session 抽象 → 后续可能 spec,目前无
- 多 agent 编排 → 后续可能 spec,目前无
- LLM 摘要式 compaction → 使用方实现 `ContextCompactor` Protocol

如果想加上述任一项进 SDK,先在 `proposal.md` 写 errata + 讨论决策。

---

## 常用命令

```bash
# 总是在 agent-kit/ 根目录运行(避免 cwd 漂移)
cd /Users/karama/Documents/baizhi/agent-kit

# venv 已建好(python3.11)
source .venv/bin/activate    # 或 .venv/bin/python -m ...

# 单元 + 集成测试
.venv/bin/python -m pytest                  # 全量
.venv/bin/python -m pytest -x               # 失败即停
.venv/bin/python -m pytest tests/test_loop.py -v
.venv/bin/python -m pytest -k "compactor"   # 关键字过滤

# Lint(可选)
.venv/bin/ruff check agent_kit/ tests/
.venv/bin/mypy agent_kit/
```

---

## git 工作流

**强约束**:

- Stage 推进 = 1 个 `git commit` + 1 个 `git tag stage-N`
- Tech-design 改动 = 1 个 commit + 1 个 `git tag tech-design-vN`
- **不**在 spec 修订时 amend 已有 commit;**新增 commit 留迹**
- 提交前跑 `pytest`,绿才提
- 仓库目前**无 remote**,本地 main 即是主线

提交消息约定(对应 stage):
- `Stage N: <一句话>` (大特性)
- 设计修订:`<Verb> <decision>: <rationale>`
- BUG 修:`Fix <symptom>: <root cause>`

---

## 不要做的事

- ❌ 不要在 SDK 里塞 google.genai / langchain / openai 等重依赖(`mcp` 和 `pyyaml` + `pydantic` 已经是边界)
- ❌ 不要在 hook 里 swallow 异常 —— raise 让 loop 转成 error event
- ❌ 不要写"per-tenant" / "per-run" 等业务概念进 SDK 命名空间(参考 McpLifecycle 撤回案,proposal.md errata)
- ❌ 不要 push --force,本仓库 main 是 source of truth
- ❌ 不要 `pytest` 在 wrong cwd(本 session cwd 默认 `/Users/karama/Documents/Fam`)—— **总是先 `cd /Users/karama/Documents/baizhi/agent-kit`** 或者用 `pytest --rootdir` 显式
- ❌ 不要把 baizhi-agent / fam-runtime 当依赖装进来 —— 它们是 SDK 的**使用方**,反过来才对
- ❌ 不要给 SDK 加 retry / cost tracking / PII redact 内置 —— 这些是装饰器(包 LlmProvider / 包 BaseToolset)或 hook 的活,tech-design § 8.6 末尾"装饰器 vs hook 选择指南"写得清楚

---

## 测试约定

- **不 mock 真 I/O**:用 pytest tmp_path fixture
- LLM 测试用 `_ScriptedProvider`(tests/test_loop.py 内)而不是 mock
- 异步用 `@pytest.mark.asyncio`(pyproject 已配 `asyncio_mode = "auto"`)
- 一个 Stage 的退出标准 = 对应测试全绿,且新功能有专属测试

---

## 现状(@stage-2)

- **136 个测试全绿**(0.07s)
- types / provider / toolset / skill / tokens / context / hooks **真实现完成**
- `AgentLoop.run()` 非 stream 路径 **真实现完成**
- `Runner.run()` 仍是 stub —— Stage 3 入口
- `McpToolset` 仍是 stub —— Stage 4 入口
- stream 路径 stub —— Stage 5

完整接力见 [HANDOFF.md](HANDOFF.md)。

---

## 跟用户说话的方式

- 简短,1-2 句结论先,再上证据 / diff / commit hash
- 不重复 diff;不长篇总结
- **push back 是工作的一部分** —— 用户经常 challenge 设计,这是好事;别一上来 yes
- 跑 pytest 报数字("136/136 in 0.07s"),不空口说"通过"
- 重要决策落 commit message + tech-design.md;**不只是说一下就过**

---

最后更新:2026-05-24(stage-2 落地后)
