# agent-tui (agent-kit sample · live event stream in a Textual TUI)

A two-pane terminal UI that drives an `agent_kit.Agent` and **renders every
event live** as the agent thinks, calls tools, and replies.

The pre-wired agent has four real pieces:

| Piece | Provider |
|---|---|
| **LLM** | **Qwen3.6-Plus** via DashScope's OpenAI-compatible endpoint (LiteLLM `openai/qwen3.6-plus` + custom `api_base`); reasoning / thinking mode opt-in via env |
| **`skill-creator`** skill | Anthropic's official [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) (vendored under `app/skills/`) |
| **WebSearch** MCP | Aliyun Bailian (`https://dashscope.aliyuncs.com/.../WebSearch/mcp`), streamable HTTP |
| **Sandbox** | `SandboxToolset(LocalDirRunner)` over a **persistent workspace** (`~/.agent-tui-workspace` by default). Exposes `exec_command` / `read_file` / `write_file` to the LLM with a tight command allowlist |

`DASHSCOPE_API_KEY` powers the LLM + the WebSearch MCP. The sandbox runs
on your local machine (no key needed). So you can ask the agent to
*"search the web for X"*, *"help me design a new SKILL.md for Y and save
it to disk"*, or *"validate the SKILL.md you wrote yesterday"* — and watch
the loop in real time.

## Layout

```
┌─ agent-kit TUI demo · skill-creator + WebSearch MCP ─────────────────┐
│ ┌─ Chat ─────────────────────┐ ┌─ Events ───────────────────────────┐│
│ │ Welcome. Try:              │ │ event stream — every agent event   ││
│ │   • Search the web for…    │ │ lands here live                    ││
│ │   • Use skill-creator to…  │ │ 14:02:31  round_start         #0   ││
│ │                            │ │ 14:02:31  llm_request    msgs=2…   ││
│ │ You  what's the latest…    │ │ 14:02:32  llm_response   1 tool…   ││
│ │ Agent  Here are 3 results… │ │ 14:02:32  tool_call      mcp__we…  ││
│ │                            │ │ 14:02:33  tool_result    call=ab…  ││
│ │                            │ │ 14:02:34  llm_response   text=…    ││
│ │                            │ │ 14:02:34  final_text     Here ar…  ││
│ │                            │ │ 14:02:34  round_end           #1   ││
│ └────────────────────────────┘ └────────────────────────────────────┘│
│ > Ask the agent…                                                     │
├──────────────────────────────────────────────────────────────────────┤
│ ^C Quit  ^L Clear events                                             │
└──────────────────────────────────────────────────────────────────────┘
```

Color-coded event kinds: round/llm = blue/cyan, tool = magenta, final_text
= green, error = red, short-circuit / compact / cancel = yellow.

## Quick start

```bash
# From the repo root, with .venv active
pip install -e ".[litellm]"          # gets agent-kit + LiteLLM
pip install "textual>=0.50"          # the TUI lib

cd samples/agent-tui
cp .env.example .env
# Edit .env, set just ONE key:
#   DASHSCOPE_API_KEY=sk-...   (powers Qwen LLM + WebSearch MCP)

# Load .env into your shell
set -a; source .env; set +a

PYTHONPATH=../.. python -m app.tui
```

### Switch model / enable thinking mode

```bash
# Different Qwen flavor
export QWEN_MODEL=qwen-max          # or qwen-turbo, qwen-plus, ...

# Turn on Qwen3.6 reasoning / thinking mode (default: off)
export QWEN_THINKING=1
export QWEN_THINKING_BUDGET=4000    # tokens of thinking budget

PYTHONPATH=../.. python -m app.tui
```

### Use a completely different LLM (Claude / GPT / Gemini)

```bash
# Bypass Qwen by passing your own model string:
DASHSCOPE_API_KEY=sk-... \
  MODEL=anthropic/claude-3-5-sonnet-20240620 \
  ANTHROPIC_API_KEY=sk-ant-... \
  PYTHONPATH=../.. python -c "
from app.agent import build_agent
from app.tui import AgentTui
import os
AgentTui().run()
"
```

…or edit `app/tui.py::AgentTui.__init__` to pass `model=...` into
`build_agent(...)`.

The TUI launches; type a question at the bottom prompt. Examples that
exercise both tools:

| Prompt | What you'll see |
|---|---|
| `Search for the latest Python 3.13 release date` | `tool_call mcp__websearch__search(...)` → `tool_result` → final text with citations |
| `Help me write a SKILL.md for reviewing Python code, save it to my workspace` | `load_skill(name="skill-creator")` → `load_skill_resource(path="references/schemas.md")` → `sandbox__localdir__write_file(path="code-review/SKILL.md", …)` → final summary |
| `List my workspace files, then cat the most recent SKILL.md` | `sandbox__localdir__exec_command(cmd=["ls"])` → `sandbox__localdir__exec_command(cmd=["cat", "<file>"])` |
| `Validate the code-review skill using skill-creator's quick_validate.py` | `sandbox__localdir__exec_command(cmd=["python", "/path/to/skill-creator/scripts/quick_validate.py", "code-review"])` → output piped through |
| `Search for "context engineering" and then make a skill for it on disk` | WebSearch → skill-creator → sandbox write_file (all in one run) |

## File layout

```
samples/agent-tui/
├── README.md
├── pyproject.toml
├── .env.example
└── app/
    ├── __init__.py
    ├── agent.py         build_agent(): LiteLlm + skill-creator + WebSearch MCP + Sandbox
    ├── tui.py           Textual app + live event renderer
    ├── test_app.py      18 offline tests (no LLM key needed)
    └── skills/
        └── skill-creator/   ← vendored from anthropics/skills
            ├── SKILL.md
            ├── LICENSE.txt  (Anthropic's Apache-2.0 file kept verbatim)
            ├── references/  (schemas.md)
            ├── agents/      (analyzer / comparator / grader sub-skills)
            ├── assets/      (eval_review.html)
            ├── eval-viewer/ (generate_review.py + viewer.html)
            └── scripts/     (8 helper scripts for eval / packaging)
```

## How the event stream lands in the TUI

`tui.py::_run_agent` is a background worker spawned via Textual's
`App.run_worker()`. It drives `agent.runner.run(req)` — the same
async-iterator pattern from `docs/tutorial.md` § 9:

```python
async for evt in self.agent.runner.run(req):
    _render_event(events_log, evt)            # → right pane, every kind
    if evt.kind == "final_text":
        final_text = evt.payload["text"]
    elif evt.kind == "error":
        chat.write(f"!! {evt.payload['stage']}: {evt.payload['message']}")
```

Every event from agent-kit's loop (`round_start`, `llm_request`,
`llm_response`, `tool_call`, `tool_result`, `context_compacted`,
`final_text`, etc.) gets one line in the right pane with a kind-specific
summary (token counts, tool name + args, content preview, error
stage/type). See `tui.py::_summary` for the per-kind formatter.

## Offline tests (no API key)

```bash
cd samples/agent-tui
PYTHONPATH=../.. python -m pytest app/test_app.py -v
# 10 passed
```

These verify:
- `build_agent()` wires the SkillCatalogToolset + WebSearch MCP correctly
- `skill-creator` is discoverable via `FilesystemSkillRegistry`
- `${DASHSCOPE_API_KEY}` substitution into the MCP Authorization header
- Missing secret → friendly `KeyError` at construct time
- The `_summary()` / `_render_event()` helpers produce the right one-liner
  for every event kind (round_start / llm_response / tool_call / tool_result
  / error / etc.)

## Persistent workspace

The sandbox toolset operates on a persistent dir that survives across runs
(and across `Ctrl-C` restarts of the TUI):

```bash
# Default location
~/.agent-tui-workspace/

# Override via env (path is mkdir'd at startup if missing)
export AGENT_TUI_WORKSPACE=/some/where/else
```

Inside the workspace, the LLM uses **workspace-relative paths**. The
LocalDirRunner blocks any path that resolves outside it (`PermissionError:
escapes workspace`), so `../../etc/passwd` is impossible.

Command allowlist (anything not in the list returns `exit_code=126`):

```
ls cat head tail wc grep rg find tree python python3
```

Edit `app/agent.py::_SANDBOX_ALLOWLIST` to widen / narrow. **Notably
absent**: `rm`, `mv`, `chmod`, `bash -c …`, network tools (`curl`, `wget`).

## Customizing

- **Different Qwen variant** — `export QWEN_MODEL=qwen-max` (or `qwen-turbo`,
  `qwen-plus`, etc.)
- **Qwen3.6 thinking mode** — `export QWEN_THINKING=1` + optionally
  `QWEN_THINKING_BUDGET=4000`. Off by default for speed
- **Completely different LLM** — pass `build_agent(model="anthropic/...")`
  or your own `LlmProvider` instance; the Qwen default is just the
  no-args path
- **Different MCP server** — edit `app/agent.py::_build_websearch_mcp` or
  add another `McpToolset.http(...)` / `.stdio(...)` to the `tools=` list
- **Different sandbox backend** — swap `LocalDirRunner` for `SrtRunner`
  (Anthropic sandbox-runtime, real isolation) or `McpSandboxRunner` (any
  E2B/Modal/Daytona MCP) in `app/agent.py::_build_sandbox_toolset`. The
  3 LLM tool names stay the same — no INSTRUCTION rewrite needed
- **More / fewer allowed commands** — edit `_SANDBOX_ALLOWLIST`
- **More skills** — drop another `<skill-name>/SKILL.md` (+ optional
  references/scripts) under `app/skills/`. The `FilesystemSkillRegistry`
  picks them all up automatically; no code changes
- **Hooks for audit / cost / PII** — pass `hooks=[...]` to `build_agent`'s
  `Agent(...)` call (see `docs/tutorial.md` § 6 for examples)

## Mapping to docs

| Topic | Where in tutorial |
|---|---|
| Agent + tools wiring | [§ 2-3](../../docs/tutorial.md) |
| Skills from filesystem | [§ 4](../../docs/tutorial.md) |
| MCP factories + `${VAR}` | [§ 5](../../docs/tutorial.md) |
| Event-stream renderer (this TUI is the canonical example) | [§ 9](../../docs/tutorial.md) |
