# agent-kit

Minimal, framework-agnostic Python toolkit for building agent runtimes around:

- **Agent loop** — bounded multi-round LLM ↔ tool conversation
- **Skill** — SKILL.md-defined capability with progressive disclosure
- **MCP** — first-class Model Context Protocol toolset(基于 Anthropic 官方 `mcp` SDK)

## Why

`baizhi-agent`、`fam-runtime` 各自实现了一份 agent loop + skill + MCP
脚手架,但 90% 的轮廓重叠(provider 抽象、轮数 cap、tool dispatch、
`mcp__<server>__<tool>` 命名、SKILL.md frontmatter、progressive disclosure)。
本仓库把这层公共骨架抽出来,作为独立可复用的 Python 工具包(kit,而非全家桶
SDK)—— 只提供骨架,不绑定上层业务。

## Non-goals

按 [GOALS.md](../baizhi-agent/GOALS.md) Non-goals 对齐,本 kit **不**做:

- 多租户队列 / 资源调度
- 持久化 / SQLite / 数据库
- HTTP API / FastAPI 路由
- 前端 UI / React
- 鉴权 / 配额
- LangChain / google.genai 等重依赖

这些都是 **使用方** 的事(baizhi-agent / fam-runtime 等);agent-kit 只暴露
`Runner.run(request) -> AsyncIterator[Event]`,使用方在外面包队列、写
持久化、起 HTTP server。

## Status

**Stage 0(2026-05-24)**:仓库骨架 + 设计文档。模块 stub 还没实现,真接
进 baizhi-agent 之前不会发版。

## Design

- **[docs/tech-design.md](docs/tech-design.md)** —— Stage 1 实现 spec(契约级,
  RFC 2119 措辞,Reviewer 读完应能逐条断言)
- **[docs/proposal.md](docs/proposal.md)** —— 原始抽象提案 + 4 个开放问题的
  讨论历史(已在 tech-design § 13 决议)

模块划分:

```
agent_kit/
  types.py     # Message / ToolCall / ToolResult / Event
  provider.py  # LlmProvider Protocol
  toolset.py   # BaseToolset ABC + ToolCallContext
  skill.py     # Skill / SkillRegistry + SKILL.md 解析
  mcp.py       # McpServerConfig + McpToolset(wrap Anthropic `mcp`)
  loop.py      # AgentLoop.run 核心循环
  runner.py    # Runner 门面
```

## References

四套参考实现(SDK 设计的输入,**不抄代码,抄思路**):

- `../adk-python-main/` — Google ADK,BaseLlmFlow + McpToolset
- `../OpenHarness/` — QueryEngine pull 模型 + 全局 McpClientManager
- `../baizhi-agent/` — LlmAgentRunner + 进步式 disclosure + 每 skill scoped storage
- `../Fam/fam_runtime/` — FamRuntime + 按 family_id 缓存 MCP

## License

待定(初始候选:Apache-2.0)。
