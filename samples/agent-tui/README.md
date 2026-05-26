# agent-tui (agent-kit sample · live event stream in a Textual TUI)

A two-pane terminal UI that drives an `agent_kit.Agent` and **renders every
event live** as the agent thinks, calls tools, and replies.

The pre-wired agent has two real capabilities:

| Capability | Provider |
|---|---|
| **`skill-creator`** skill | Anthropic's official [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) (vendored under `app/skills/`) |
| **WebSearch** MCP | Aliyun Bailian (`https://dashscope.aliyuncs.com/.../WebSearch/mcp`), streamable HTTP |

So you can ask it to *"search the web for X"* OR *"help me design a new
SKILL.md for Y"* — and watch the loop in real time.

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
# edit .env:
#   DASHSCOPE_API_KEY=...     (required — WebSearch MCP auth)
#   GOOGLE_API_KEY=...        (or any LiteLLM-supported provider)

# load .env into your shell
set -a; source .env; set +a

PYTHONPATH=../.. python -m app.tui
```

The TUI launches; type a question at the bottom prompt. Examples that
exercise both tools:

| Prompt | What you'll see |
|---|---|
| `Search for the latest Python 3.13 release date` | `tool_call mcp__websearch__search(...)` → `tool_result` → final text with citations |
| `Help me write a SKILL.md for reviewing Python code` | `tool_call load_skill(name="skill-creator")` → `tool_call load_skill_resource(path="references/schemas.md")` → final draft |
| `Search for "context engineering" and then make a skill for it` | Both: websearch first, then skill-creator |

## File layout

```
samples/agent-tui/
├── README.md
├── pyproject.toml
├── .env.example
└── app/
    ├── __init__.py
    ├── agent.py         build_agent(): LiteLlm + skill-creator + WebSearch MCP
    ├── tui.py           Textual app + live event renderer
    ├── test_app.py      10 offline tests (no LLM key needed)
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

## Customizing

- **Different LLM** — set `MODEL=anthropic/claude-3-5-sonnet-20240620` (or
  any LiteLLM string) plus the corresponding key
- **Different MCP server** — edit `app/agent.py::_build_websearch_mcp` or
  add another `McpToolset.http(...)` / `.stdio(...)` to the `tools=` list
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
