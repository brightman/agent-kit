# agent-kit · Handoff Note

Written for the next agent picking up work in this repo. Read this + the
last 5 git commits + skim `docs/tech-design.md`. That's enough to ship.

---

## One-paragraph state

agent-kit is a minimal Python toolkit (~3,300 LOC) for agent runtimes:
bounded LLM↔tool loop, progressive-disclosure skills, MCP integration,
context compaction, hooks, and a diet sandbox with three backends
(LocalDir / SRT / MCP). **374 tests passing locally** (~3s). Stages A-F
of the sandbox roadmap are all landed; baizhi-agent has been migrated to
ADK so it's no longer a downstream consumer to consider.

---

## Quick check

```bash
cd /Users/karama/Documents/baizhi/agent-kit
.venv/bin/python -m pytest            # → 374 passed, 1 skipped (live)
.venv/bin/python -m pytest tests/contrib/sandbox/ -v  # 65 sandbox tests
```

If both green, you're set.

---

## Recent landmarks (most recent first)

| Commit | Tag | What |
|---|---|---|
| `ee46c05` | `sandbox-sample-live` | Stage F: coding-agent runs LocalDir end-to-end with real subprocess (bug-fix demo) |
| `1ecd788` | `sandbox-3` | Stage E: `McpSandboxRunner` — any MCP exec server adapter |
| `0fa156e` | `sandbox-2` | Stage D: `SrtRunner` — Anthropic sandbox-runtime wrapper |
| `d06758b` | `sandbox-1` | Stage C: `LocalDirRunner` + freeze held |
| `39e3b45` | `sandbox-api-frozen` | Stage B: SandboxRunner Protocol + SandboxToolset + 14 freeze tests |
| `f55d873` | `sandbox-spec-v1` | Stage A: spec § 16 revision (diet sandbox in contrib) |
| `ae40f14` |   | `samples/agent-skills-tutorial` — port of ADK skills demo |
| `f95dc6a` |   | Phase 2: rewrite test_loop / test_runner through Agent (309 tests) |
| `049c05e` |   | Extract tests/_helpers.py (1072 → 696 net lines) |

---

## Where things live

```
agent_kit/
  agent.py          high-level Agent façade
  runner.py         Runner + RunResult + workspace lifecycle
  loop.py           AgentLoop core
  provider.py       LlmProvider Protocol
  toolset.py        BaseToolset ABC + ToolCallContext
  skill.py          Skill / SkillRegistry / SkillCatalogToolset
  mcp.py            McpServerConfig + McpToolset
  hooks.py          Hook ABC
  context.py        ContextCompactor + TruncatingCompactor
  contrib/
    providers/litellm.py    LiteLlm provider
    skills.py               FilesystemSkillRegistry
    sandbox/
      types.py              SandboxRunner Protocol + ExecResult
      toolset.py            SandboxToolset
      runners/
        localdir.py         Real host subprocess
        srt.py              Anthropic sandbox-runtime wrapper
        mcp.py              Any MCP exec-tool server adapter

docs/
  tech-design.md    full spec (read this for contract questions)
  proposal.md       historical context + Q1-Q4 decision log
  tutorial.md       walk-through for new users (NEW)

samples/
  agent-skills-tutorial/    4 skill patterns (inline / file / external / meta)
  coding-agent/             SandboxToolset demo, stub + LocalDir backends

tests/                      374 tests, ~3s
  contrib/sandbox/          65 sandbox-specific tests
```

---

## Known gaps / deferred

- **Streaming LLM deltas** (token-by-token `llm_delta` events): deferred,
  see [spec § 14](docs/tech-design.md). Set `RunRequest(stream=True)` and
  the loop fast-fails with a clear error event.
- **Multimodal content blocks** (image/audio in `Message.content`): str
  only for now. Will look at when a real need surfaces.
- **`ctx.emit`**: currently no-op. Reserved for tools that want to push
  progress events into the Runner's event stream. Not blocking.

---

## Doing new work

Spec-first protocol still holds:

1. Spec change in `docs/tech-design.md` (commit + tag if substantial)
2. Code change with tests
3. `pytest -x` green before commit
4. Conventional commit message
5. Update `docs/tutorial.md` if user-facing surface changed
6. This file: append a line to "Recent landmarks", trim if needed

For stages-style work (multi-commit feature with clear milestones), use
the pattern from Stages A-F: explicit stage table, tag each stage, freeze
tests pin contracts across stages.

---

## Quick contacts

- **Tech design spec**: `docs/tech-design.md`. Final, RFC-style.
- **Decision history**: `docs/proposal.md`. Read if you want the *why*.
- **Project conventions**: `CLAUDE.md`. Read before changing build / commit / test workflow.

Last updated: 2026-05-26
