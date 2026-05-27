# agent-kit

> Minimal, framework-agnostic Python toolkit for building agent runtimes.

```python
from agent_kit import Agent

agent = Agent(name="my-agent", model="gemini/gemini-2.5-flash")
result = agent.run_sync("What's 7 * 6?")
print(result.final_text)   # "42"
```

That's the whole thing for the simple case. Tools, skills, sandbox, MCP, hooks
— they all stack on top without changing this shape.

## Why

agent-kit is **the smallest viable agent runtime** that still covers what
production agents actually need: a bounded LLM ↔ tool loop, progressive-
disclosure skills, MCP integration, context compaction, hooks, and an
optional diet sandbox. About **3,300 lines of code** plus extras.

The kit gives you the *mechanism*; you bring the policy.

## Install

```bash
pip install agent-kit                # core only (mcp + pyyaml + pydantic)
pip install "agent-kit[litellm]"     # add LiteLLM for ~100 commercial models
pip install "agent-kit[sandbox]"     # reserved for future sandbox extras
```

Python 3.11+. No google.genai / langchain / openai SDK dependencies.

## Features

| Piece | What it does | Where |
|---|---|---|
| `Agent` | High-level façade — model + tools + skills + hooks + sandbox | `agent_kit/agent.py` |
| `Runner` / `AgentLoop` | Bounded multi-round LLM ↔ tool loop with cancel + max_rounds | `agent_kit/{runner,loop}.py` |
| `LlmProvider` Protocol | Any chat-completions backend. `LiteLlm` ships in `contrib` | `agent_kit/provider.py` |
| `Skill` + `SkillCatalogToolset` | SKILL.md frontmatter parsing, progressive disclosure (L1/L2/L3) | `agent_kit/skill.py` |
| `McpToolset` | Anthropic MCP SDK wrapper — `.stdio()` / `.sse()` / `.http()` factories, `tool_filter`, `${VAR}` env substitution | `agent_kit/mcp.py` |
| `Hook` ABC | 4 cross-cutting hooks: `before_model` / `after_model` / `before_tool` / `after_tool` | `agent_kit/hooks.py` |
| `ContextCompactor` | Pluggable history compaction; built-in `TruncatingCompactor` | `agent_kit/context.py` |
| `SandboxToolset` + 3 runners | Diet sandbox: LocalDir / SRT / MCP via one Protocol | `agent_kit/contrib/sandbox/` |

## Quick map

```
agent_kit/
  agent.py          # high-level Agent façade
  runner.py         # Runner + RunResult + workspace lifecycle
  loop.py           # AgentLoop core (bounded LLM ↔ tool rounds)
  provider.py       # LlmProvider Protocol + LlmResponse
  toolset.py        # BaseToolset ABC + ToolCallContext
  skill.py          # Skill / SkillRegistry / SkillCatalogToolset
  mcp.py            # McpServerConfig + McpToolset
  hooks.py          # Hook ABC (4 no-op methods)
  context.py        # ContextCompactor Protocol + TruncatingCompactor
  contrib/
    providers/      # LiteLlm provider
    skills.py       # FilesystemSkillRegistry
    sandbox/        # SandboxRunner Protocol + LocalDir/SRT/MCP runners
```

## Documentation

- **[docs/tutorial.md](docs/tutorial.md)** — start here. Walk through every
  feature with runnable code.
- **[docs/tech-design.md](docs/tech-design.md)** — full spec, RFC-style.
  Read this if you're implementing an alternative `LlmProvider` /
  `SkillRegistry` / `Hook` / `SandboxRunner`.
- **[docs/sandbox.md](docs/sandbox.md)** — testing recipes for the three
  sandbox backends (LocalDir / SRT / MCP): unit tests, in-process smoke,
  real-binary smoke, live-agent integration, troubleshooting.
- **[samples/](samples/)** — runnable demos:
  - [`agent-skills-tutorial`](samples/agent-skills-tutorial) — agent-kit port
    of Google ADK's skills demo. Inline / file-based / external / meta skills.
  - [`coding-agent`](samples/coding-agent) — sandbox demo. Stub backend for
    offline tests; LocalDir backend for real subprocess + bug-fix end-to-end.

## What it deliberately doesn't do

- Multi-tenant queues, persistence, HTTP / WebSocket APIs, auth, quotas —
  bring your own framework
- LangChain / google.genai compatibility — won't be added
- Built-in Memory / Session abstractions — use `prior_messages` + your store
- Multi-agent orchestration — single-agent runtime by design
- LLM-summary compaction — write a `ContextCompactor` if you need it

See [spec § 15](docs/tech-design.md) for the full out-of-scope list.

## Testing

```bash
pip install -e ".[dev,litellm]"
python -m pytest                            # 374 tests, ~3s
python -m pytest tests/contrib/sandbox/ -v  # 65 sandbox-specific
```

## License

Apache-2.0.
