# agent-kit tutorial

A practical, runnable guide to building agents with agent-kit. Each section
introduces one concept and shows it working end-to-end.

> **Audience**: Python devs comfortable with `async def` and `pip install -e`.
> **Time**: about 30 minutes if you copy-paste, longer if you experiment.
> **Pre-req**: Python 3.11+ and a virtualenv. An API key only matters once
> you want to talk to a real LLM — most of this tutorial works offline.

---

## Table of contents

1. [Install + smoke test](#1-install--smoke-test)
2. [Your first agent](#2-your-first-agent)
3. [Adding tools](#3-adding-tools)
4. [Skills (progressive disclosure)](#4-skills-progressive-disclosure)
5. [MCP — talk to external tool servers](#5-mcp--talk-to-external-tool-servers)
6. [Hooks — cross-cutting concerns](#6-hooks--cross-cutting-concerns)
7. [Context compaction](#7-context-compaction)
8. [Sandbox — running shell commands safely](#8-sandbox--running-shell-commands-safely)
9. [Streaming events](#9-streaming-events)
10. [Multi-turn history](#10-multi-turn-history)
11. [Cancel](#11-cancel)
12. [Errors and how to handle them](#12-errors-and-how-to-handle-them)
13. [Going to production](#13-going-to-production)

---

## 1. Install + smoke test

```bash
pip install -e ".[litellm,dev]"   # from the repo root
python -m pytest -q               # expect 374 passed, 1 skipped
```

`pytest` doesn't need an API key — it uses scripted providers throughout.
Once that's green, you have a working install.

---

## 2. Your first agent

```python
# tutorial_01_hello.py
from agent_kit import Agent

agent = Agent(
    name="hello-agent",
    model="gemini/gemini-2.5-flash",        # LiteLLM model id
    instruction="You are concise. Reply in a single short sentence.",
)

result = agent.run_sync("What's the capital of France?")
print(result.final_text)
```

Run it (after setting `GOOGLE_API_KEY` for Gemini):

```bash
python tutorial_01_hello.py
# Paris.
```

### What just happened

1. `Agent(model="gemini/...")` recognized the string is a LiteLLM model id and
   constructed a `LiteLlm` provider behind the scenes. (`pip install -e
   .[litellm]` provides this; without it, you'd see a friendly `ImportError`.)
2. `run_sync(...)` ran one round of the loop: send messages → get response →
   no tool calls → emit `final_text` → done.
3. `result.final_text` is the model's reply. `result.events` has the full
   event stream if you want to inspect what happened.

### Without an API key (any-time pattern)

Every example below has a `ScriptedProvider` equivalent — passes any object
satisfying the `LlmProvider` Protocol as `model=`:

```python
from agent_kit.provider import LlmResponse

class FakeModel:
    name = "fake"
    async def chat(self, messages, tools=None, **kw):
        return LlmResponse(text="Paris.", tool_calls=[],
                           usage={}, raw={}, finish_reason="stop")
    async def chat_stream(self, *a, **k): raise NotImplementedError

agent = Agent(name="hello", model=FakeModel())
print(agent.run_sync("anything").final_text)   # → "Paris."
```

The whole test suite (`tests/`) uses this technique — useful for CI.

---

## 3. Adding tools

Tools are grouped into "toolsets" — one class per concept. Every toolset
subclass implements `BaseToolset` (build schemas + execute calls).

### A trivial calculator toolset

```python
# tutorial_02_calc.py
from agent_kit import Agent, BaseToolset, ToolCall, ToolResult
from agent_kit.provider import ToolSchema


class CalcToolset(BaseToolset):
    name = "calc"

    def build_schemas(self):
        return [ToolSchema(
            name="add",
            description="Add two integers.",
            parameters={
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
        )]

    async def execute(self, call: ToolCall, ctx) -> ToolResult:
        a, b = call.arguments["a"], call.arguments["b"]
        return ToolResult(call_id=call.id, content=str(a + b))


agent = Agent(
    name="calc-agent",
    model="gemini/gemini-2.5-flash",
    instruction="Use the add tool for arithmetic.",
    tools=[CalcToolset()],
)
print(agent.run_sync("What's 7+6?").final_text)    # → "13"
```

### Contract rules

- **Tool names are global.** `Router` rejects duplicates across toolsets, so
  prefix yours when in doubt (`mycorp__add`).
- **`execute()` must not raise.** Wrap your own errors and return
  `ToolResult(is_error=True, content="...")`. The Router catches anything
  that leaks, but failing loudly via `is_error=True` lets the LLM see and
  recover.
- **Async only.** If you have sync work, call `await asyncio.to_thread(...)`.

### What `ToolCallContext` gives you

Every `execute()` receives a `ctx` with:

| Field | What |
|---|---|
| `ctx.workspace: Path` | Per-run filesystem scratch (cleaned up unless `Agent(workspace=callable)` was passed) |
| `ctx.workspace_ephemeral: bool` | True if SDK manages the dir; tells your toolset if it can cache across runs |
| `ctx.cancel: asyncio.Event` | Check `.is_set()` for cooperative cancel |
| `ctx.emit: Callable[[Event], None]` | Currently no-op; reserved for progress events |
| `ctx.run_id: str` | Unique per-run; useful as a tracing key |
| `ctx.run_state: dict[str, Any]` | Free-form per-run scratchpad shared across toolsets / hooks |

---

## 4. Skills (progressive disclosure)

A **Skill** is a `SKILL.md` file (YAML frontmatter + Markdown body) + any
reference files. The agent sees only a one-line description by default; it
loads the body and references on demand. This keeps the system prompt small.

### Inline skill

```python
from agent_kit import Agent, Skill, SkillFrontmatter
from pathlib import Path

seo_skill = Skill(
    name="seo-checklist",
    frontmatter=SkillFrontmatter(
        name="seo-checklist",
        description="SEO optimization checklist for blog posts.",
        version="1.0",
    ),
    body=(
        "# SEO Checklist\n\n"
        "1. Title 50-60 chars, primary keyword near start\n"
        "2. Meta description 150-160 chars\n"
        "3. ...\n"
    ),
    files={},
    storage_root=Path("/tmp/seo"),
)

agent = Agent(
    name="blog-agent",
    model="gemini/gemini-2.5-flash",
    skills=[seo_skill],     # list[Skill]
)
agent.run_sync("Review my blog post 'Getting Started with Kubernetes' for SEO.")
```

Behind the scenes `Agent(skills=...)` accepts:
- `list[Skill]` → wrapped in `InMemorySkillRegistry`
- `Path` or `str` (directory of `SKILL.md` subfolders) → wrapped in
  `FilesystemSkillRegistry`
- `SkillRegistry` instance → used directly (for db-backed catalogs)

### File-based skills

```
my-skills/
├── blog-writer/
│   ├── SKILL.md
│   └── references/style-guide.md
└── content-research-writer/
    ├── SKILL.md
    └── references/seo-guidelines.md
```

```python
agent = Agent(
    name="writer",
    model="gemini/gemini-2.5-flash",
    skills=Path("./my-skills"),
)
```

The agent auto-gets three tools: `list_skills` (cheap L1), `load_skill` (L2),
`load_skill_resource` (L3). The system prompt only lists names + descriptions
— actual contents are fetched on demand.

### Full demo

See [`samples/agent-skills-tutorial/`](../samples/agent-skills-tutorial) for
a complete demo with 4 skill patterns (inline, file-based, "external",
meta-skill).

---

## 5. MCP — talk to external tool servers

MCP (Model Context Protocol) lets you plug remote tool servers into the
agent. agent-kit ships an `McpToolset` that wraps Anthropic's `mcp` Python
SDK. Three transport factories:

```python
from agent_kit import McpToolset

# stdio: spawn a local subprocess
gh = McpToolset.stdio("github", command=["mcp-github"], env={"GH_TOKEN": "..."})

# sse: long-lived SSE connection
ws = McpToolset.sse("ws", url="https://example.com/mcp", headers={"X-Key": "..."})

# http: streamable-HTTP transport
brave = McpToolset.http(
    "brave",
    url="https://api.brave.com/mcp/${TOKEN}",
    secrets={"TOKEN": "..."},      # ${VAR} substitution; secrets override env
    tool_filter=["search"],         # only expose specific remote tools
)

agent = Agent(name="research", model="gemini/...", tools=[gh, ws, brave])
```

### Things that come for free

- **`${VAR}` substitution** in `url`, `command`, `headers`, `env`. `secrets={}`
  overrides `os.environ`. Missing variable → `KeyError` at construct time.
- **Tool naming** is automatic: every remote tool shows up as
  `mcp__<server>__<remote-name>`. The LLM sees fully-qualified names, the
  Router routes by prefix.
- **`tool_filter`** can be a `list[str]` (whitelist of remote names) or a
  `Callable[[ToolSchema], bool]` for arbitrary filtering.
- **Lifecycle**: `connect()` is lazy and called by `Runner` during pre-warm.
  `aclose()` is idempotent and called in `finally`. You can reuse one
  `McpToolset` instance across runs, or build a new one per run — both work.

### Multiple lifecycles

```python
# Per-run (default): Runner connects + closes for you
runner = Runner(provider, toolsets=[gh])
await runner.run_to_completion(req)

# Global / shared: connect once, reuse across many runs
gh = McpToolset.stdio("github", command=["mcp-github"])
await gh.connect()
for req in batch:
    await Runner(provider, toolsets=[gh]).run_to_completion(req)
await gh.aclose()
```

---

## 6. Hooks — cross-cutting concerns

Hooks let you observe or intercept the loop without subclassing.

```python
from agent_kit import Hook, Agent
from agent_kit.provider import LlmResponse

class CountTokensHook(Hook):
    def __init__(self):
        self.total = 0

    async def after_model(self, ctx, response):
        self.total += response.usage.get("prompt_tokens", 0)
        return None    # don't rewrite the response

counter = CountTokensHook()
agent = Agent(name="x", model="...", hooks=[counter])
agent.run_sync("Hi.")
print("Prompt tokens this run:", counter.total)
```

### The four hooks

| Method | When | Return value |
|---|---|---|
| `before_model(ctx, messages, tools)` | Right before LLM call | `None` to continue; `LlmResponse` to short-circuit (skip the LLM) |
| `after_model(ctx, response)` | Right after LLM responds | `None` to keep response as-is; `LlmResponse` to rewrite |
| `before_tool(ctx, call)` | Right before tool dispatch | `None` to continue; `ToolResult` to short-circuit (skip the tool) |
| `after_tool(ctx, call, result)` | Right after tool returns | `None` to keep result as-is; `ToolResult` to rewrite |

### Multiple hooks

`hooks=[h1, h2, h3]` runs in order. **First non-None wins** — later hooks
are skipped on short-circuit.

### Common patterns

- **Audit log**: `before_tool` writes the `ToolCall` to a sink; always returns None
- **PII redaction**: `after_tool` walks `result.content` and returns a redacted version
- **Cost tracking**: `after_model` reads `response.usage` and increments a counter
- **Mock for tests**: `before_tool` returns a fake `ToolResult` for specific tools

---

## 7. Context compaction

LLMs have finite context windows. agent-kit's `ContextCompactor` Protocol
lets you trim history before each `chat()` call.

```python
from agent_kit.context import TruncatingCompactor
from agent_kit import Agent

agent = Agent(
    name="long-runner",
    model="...",
    compactor=TruncatingCompactor(
        token_budget=80_000,         # only compact when above this
        keep_recent_tool_results=2,  # most recent N tool results untouched
        placeholder="[OMITTED]",     # what to replace dropped content with
    ),
)
```

`TruncatingCompactor` keeps system / user / final-assistant turns; drops
older tool results down to the placeholder. **Tool-call ↔ tool-result
pairing is preserved** (the loop has an `_assert_tool_pairs_intact` guard
that emits an error event if any compactor breaks the invariant).

### Writing your own

```python
from agent_kit.context import ContextCompactor

class LlmSummaryCompactor:
    name = "llm-summary"

    async def should_compact(self, messages, last_usage):
        return last_usage.get("prompt_tokens", 0) > 50_000

    async def compact(self, messages):
        # Call a cheaper model to summarize messages[:-5], say.
        summary = await summarize(messages[:-5])
        return [Message(role="user", content=f"PRIOR CONTEXT:\n{summary}"), *messages[-5:]]
```

Any object implementing `should_compact` + `compact` works — Protocol-based
duck typing.

---

## 8. Sandbox — running shell commands safely

agent-kit ships a "diet sandbox" in `agent_kit.contrib.sandbox`. One Protocol
(`SandboxRunner`), one toolset (`SandboxToolset`), three backends.

```python
from agent_kit import Agent
from agent_kit.contrib.sandbox import SandboxToolset
from agent_kit.contrib.sandbox.runners import LocalDirRunner

agent = Agent(
    name="coding-agent",
    model="gemini/gemini-2.5-flash",
    tools=[SandboxToolset(LocalDirRunner(
        command_allowlist=["ls", "cat", "python", "pytest"],
        env_passthrough=("PATH", "HOME"),
    ))],
    workspace=lambda req, run_id: Path(f"/data/agents/{req.agent_id}"),
)
result = agent.run_sync("Read greet.py, run it, fix any typo, run it again.")
```

The LLM sees three tools:
- `sandbox__localdir__exec_command(cmd: list[str], cwd?, env?, timeout?, stdin?)`
- `sandbox__localdir__read_file(path)`
- `sandbox__localdir__write_file(path, content)`

### Three runners

| Runner | Isolation | When |
|---|---|---|
| `LocalDirRunner` | None — runs as host subprocess. Use `command_allowlist`. | Dev / trusted code / integration tests |
| `SrtRunner` | Anthropic [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) profile (filesystem ACL + network limits) | Local untrusted code (macOS/Linux) |
| `McpSandboxRunner` | Whatever the remote MCP server provides | Production / E2B / Modal / Daytona / your own |

### Backend swap = one line

```python
from agent_kit.contrib.sandbox.runners import SrtRunner, McpSandboxRunner

# Same toolset, different backend:
SandboxToolset(SrtRunner(profile="/etc/srt/python.toml"))
SandboxToolset(McpSandboxRunner(McpToolset.http("e2b", url="...")))
```

The LLM still sees `sandbox__<name>__exec_command` — your prompts and
skills stay backend-agnostic.

### What it deliberately doesn't do

PTY, exposed-port forwarding, snapshots, declarative manifests, Capability
abstractions — those are 30k+ lines of code in openai-agents-python's
sandbox. We say no on purpose; see spec § 16. If you need them, plug an MCP
backend that does.

See [`samples/coding-agent/`](../samples/coding-agent) for an end-to-end
"fix a real Python bug" demo using `LocalDirRunner` + a scripted LLM.

---

## 9. Streaming events

`Agent.run_sync(...)` aggregates everything into a `RunResult` at the end.
That's fine for batch / CLI / tests, but production usually wants the
event stream as it happens — for trace UIs, audit logs, progress bars,
WebSocket fan-out, etc.

### 9.1 The basics

Drop down to `runner.run()` instead of `run_sync` / `run_to_completion`:

```python
from agent_kit import RunRequest

req = RunRequest(agent_id="my-agent", user_message="Hello")
async for event in agent.runner.run(req):
    if event.kind == "tool_call":
        print(f"  → calling {event.payload['name']}")
    elif event.kind == "final_text":
        print(event.payload["text"])
    elif event.kind == "error":
        print(f"!!! {event.payload['stage']}: {event.payload['message']}")
```

`runner.run()` is an async generator. It **never raises** — errors come out
as `Event(kind="error", ...)`. Compare to `run_to_completion` which DOES
raise.

### 9.2 All event kinds + their payload

Every `Event` has:

```python
@dataclass(frozen=True)
class Event:
    event_id: str               # ULID, sortable by time
    kind: EventKind             # one of the 12 strings below
    payload: dict[str, Any]     # kind-specific (see table)
    parent_event_id: str | None # for rendering trees
    timestamp_ns: int           # monotonic ns
```

Reference of every `kind` and its `payload` fields:

| `kind` | When emitted | `payload` keys |
|---|---|---|
| `round_start` | Top of each loop round | `round: int` |
| `llm_request` | About to call `provider.chat` | `round: int`, `message_count: int`, `tool_count: int` |
| `llm_response` | Got LLM reply | `round: int`, `text: str`, `tool_calls: list[dict]`, `usage: dict`, `finish_reason: str` |
| `llm_delta` | (deferred, stream mode only — see § 14) | `round: int`, `delta_text: str`, `delta_tool_calls: list[dict]` |
| `llm_short_circuited` | A `before_model` hook returned a response | `round: int`, `by_hook: str`, `text: str` |
| `tool_call` | About to dispatch one tool call | `round: int`, `call_id: str`, `name: str`, `arguments: dict` |
| `tool_result` | Tool returned | `round: int`, `call_id: str`, `content: str`, `is_error: bool` |
| `tool_short_circuited` | A `before_tool` hook returned a result | `round: int`, `call_id: str`, `by_hook: str` |
| `context_compacted` | Compactor ran | `strategy: str`, `before_count: int`, `after_count: int`, `before_tokens: int`, `after_tokens: int` |
| `round_end` | Bottom of each loop round | `round: int` |
| `final_text` | LLM produced final answer (no more tool calls) | `text: str` |
| `cancelled` | Cancel triggered | `round: int`, `reason: str` (`"external"` / `"external_mid_tool"` / `"cancel_check"` / `"cancel_check_mid_tool"`) |
| `error` | Anything failed | `stage: str`, `exc_type: str`, `message: str`, `traceback: str`, optionally `method` / `hook_class` |

The `error.stage` values: `setup` / `provider` / `tool` / `hook` /
`compactor` / `loop`. See § 12 for what each means.

### 9.3 Rendering the event tree

`parent_event_id` lets you reconstruct a tree. Rules built into the loop:

- `round_start.parent` = `None`
- `llm_request.parent` / `llm_response.parent` / `llm_short_circuited.parent`
  = same round's `round_start`
- `tool_call.parent` = the `llm_response` it came from
- `tool_result.parent` / `tool_short_circuited.parent` = corresponding
  `tool_call`
- `round_end.parent` = round's `round_start`
- `final_text.parent` / `cancelled.parent` / `error.parent` = nearest
  prior `round_start`

A minimal ASCII tree renderer:

```python
from collections import defaultdict

def render_tree(events: list) -> str:
    children = defaultdict(list)
    roots = []
    for e in events:
        if e.parent_event_id is None:
            roots.append(e)
        else:
            children[e.parent_event_id].append(e)

    out = []
    def walk(e, depth):
        out.append("  " * depth + f"[{e.kind}] {_summary(e)}")
        for c in children[e.event_id]:
            walk(c, depth + 1)
    for r in roots:
        walk(r, 0)
    return "\n".join(out)

def _summary(e):
    p = e.payload
    if e.kind == "llm_response":
        return f"{len(p.get('tool_calls', []))} tool call(s), text={p.get('text','')[:30]!r}"
    if e.kind == "tool_call":
        return f"{p['name']}({list(p['arguments'])})"
    if e.kind == "tool_result":
        return f"call_id={p['call_id']} err={p['is_error']}"
    if e.kind == "final_text":
        return p["text"][:50]
    if e.kind == "error":
        return f"stage={p['stage']} {p['exc_type']}: {p['message'][:60]}"
    return ""
```

Use it after a run:

```python
result = agent.run_sync("Read README.md and summarize it.")
print(render_tree(result.events))
```

Sample output:

```
[round_start]
  [llm_request] 0 tool call(s), text=''
  [llm_response] 1 tool call(s), text=''
    [tool_call] sandbox__localdir__read_file(['path'])
      [tool_result] call_id=c1 err=False
  [round_end]
[round_start]
  [llm_request]
  [llm_response] 0 tool call(s), text='The README explains…'
  [final_text] The README explains…
  [round_end]
```

### 9.4 Persisting events live (SQLite example)

For trace UIs / audit logs, persist each event as it arrives:

```python
import sqlite3, json

class TraceStore:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                parent_event_id TEXT,
                run_id TEXT,
                kind TEXT,
                payload TEXT,
                timestamp_ns INTEGER
            )
        """)

    def write(self, run_id, event):
        self.conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
            (event.event_id, event.parent_event_id, run_id,
             event.kind, json.dumps(event.payload), event.timestamp_ns),
        )
        self.conn.commit()

store = TraceStore("./traces.db")
async for event in agent.runner.run(req):
    store.write(req.agent_id, event)
```

For OpenTelemetry, swap the `write()` body for `span.add_event(...)`.
For NDJSON files, swap for `f.write(json.dumps(event_to_dict(event)) + "\n")`.

### 9.5 Live UI fan-out (WebSocket / SSE)

`runner.run()` is an async iterator, so it composes naturally with anything
streaming. WebSocket pattern (FastAPI):

```python
from fastapi import WebSocket
from agent_kit import RunRequest

@app.websocket("/runs/{run_id}")
async def stream_run(ws: WebSocket, run_id: str):
    await ws.accept()
    req = RunRequest(agent_id="x", user_message=...)
    async for event in agent.runner.run(req):
        await ws.send_json({
            "id": event.event_id,
            "kind": event.kind,
            "payload": event.payload,
            "parent": event.parent_event_id,
        })
    await ws.close()
```

SSE flavor (Starlette `EventSourceResponse`):

```python
from sse_starlette.sse import EventSourceResponse

async def event_gen(req):
    async for event in agent.runner.run(req):
        yield {"event": event.kind, "data": json.dumps(event.payload)}

@app.get("/runs/{run_id}/stream")
async def stream(run_id: str):
    return EventSourceResponse(event_gen(req))
```

The whole event stream — including errors — is finite and well-typed:
every run ends with either `final_text` (success), `cancelled`, or
`error` as the last event. No need for sentinel "done" markers.

### 9.6 Filtering and aggregation patterns

Some common one-liners on `result.events`:

```python
# All tool calls and their results, paired
calls = {e.payload["call_id"]: e for e in result.events if e.kind == "tool_call"}
results = {e.payload["call_id"]: e for e in result.events if e.kind == "tool_result"}
for cid, call in calls.items():
    print(f"{call.payload['name']} → {results[cid].payload['content'][:80]}")

# Total prompt tokens used
total_in = sum(
    e.payload.get("usage", {}).get("prompt_tokens", 0)
    for e in result.events if e.kind == "llm_response"
)

# Rounds where the agent called a sandbox tool
sandbox_rounds = {
    e.payload["round"] for e in result.events
    if e.kind == "tool_call" and e.payload["name"].startswith("sandbox__")
}

# Did any hook short-circuit?
short_circuits = [e for e in result.events
                  if e.kind in ("llm_short_circuited", "tool_short_circuited")]
```

### 9.7 When to use `run_sync` vs `runner.run()`

| You want | API |
|---|---|
| Final answer only, sync caller | `agent.run_sync(prompt)` |
| Final answer + post-mortem on events | `agent.run_sync(prompt)`, then iterate `result.events` |
| Live progress / streaming UI | `async for evt in agent.runner.run(RunRequest(...))` |
| Custom `RunRequest` fields (metadata, prior_messages, etc.) | `agent.runner.run_to_completion(RunRequest(...))` |
| Build your own `Runner` from scratch | `Runner(provider, toolsets, ...).run(req)` |

> **Streaming LLM deltas** (token-by-token `llm_delta` events) are deferred —
> see [spec § 14](tech-design.md). `RunRequest(stream=True)` fast-fails
> today with a clear error event.

---

## 10. Multi-turn history

For chat-style agents, pass prior turns via `prior_messages`:

```python
from agent_kit import Message

history = []
while True:
    user = input(">>> ")
    if user == "quit":
        break
    result = agent.run_sync(user, prior_messages=history)
    print(result.final_text)
    history.append(Message(role="user", content=user))
    history.append(Message(role="assistant", content=result.final_text or ""))
```

Constraints (enforced at `RunRequest` construction):
- `role="system"` is rejected — system content goes through `instruction=`
  or `system_prelude=`, not `prior_messages`
- `tool_call` ↔ `tool_result` pairs must be intact (you can't smuggle an
  orphan tool result)

### Honesty re-run pattern

If your post-processor decides the LLM's answer is bad, send it back with a
correction prompt:

```python
first = agent.run_sync(user_msg)
if not is_acceptable(first.final_text):
    second = agent.run_sync(
        "Runtime correction: ground your answer in the data tool, not memory.",
        prior_messages=[
            Message(role="user", content=user_msg),
            Message(role="assistant", content=first.final_text),
        ],
    )
```

---

## 11. Cancel

Two ways to cancel a run:

### `cancel_check` callback

Best for "is the user still listening?" — polled at every round boundary
and before tool dispatch.

```python
import time

start = time.time()
def too_slow():
    return time.time() - start > 30   # 30s budget

result = agent.run_sync("Long task", cancel_check=too_slow)
assert result.cancelled is True if too_slow() else result.final_text
```

### External `ctx.cancel: asyncio.Event`

When you're driving `runner.run()` directly and want to fire-and-forget
cancel from another task, set `ctx.cancel.set()`.

Both paths emit a `cancelled` event with `reason ∈ {"external",
"external_mid_tool", "cancel_check", "cancel_check_mid_tool"}`.

---

## 12. Errors and how to handle them

### Two-track error propagation

| API | Behavior |
|---|---|
| `runner.run(req)` (streaming) | Errors become `Event(kind="error", payload={"stage": ..., "message": ...})`. **No raise.** |
| `runner.run_to_completion(req)` / `agent.run_sync(...)` | Errors raise `RuntimeError("[stage] ExcType: msg")`. |

Stages you can see in the `error` event's `payload["stage"]`:

| Stage | Means |
|---|---|
| `setup` | Workspace mkdir / toolset connect / schema build failed |
| `provider` | LLM provider raised |
| `hook` | Hook method raised (payload has `method` + `hook_class`) |
| `tool` | Toolset.execute() raised after the Router caught it |
| `compactor` | Compactor.compact() raised or broke pair invariants |
| `loop` | Anything else inside the round-loop machinery |

### Recoverable vs fatal

- **Recoverable** in `execute()` — return `ToolResult(is_error=True,
  content="...")`. The LLM sees the error and can adapt.
- **Fatal** in `execute()` — raise. The Router catches it and turns it into
  an error event; `run_to_completion` will re-raise as `RuntimeError`.

---

## 13. Going to production

### Provider

Use `LiteLlm` (the default when you pass a string `model=`) to talk to
~100 commercial providers via LiteLLM. Wrap it for retry / rate-limit /
cost tracking via a decorator pattern:

```python
class RetryingLlmProvider:
    def __init__(self, inner, *, retries=3, backoff=2.0):
        self._inner = inner; self._retries = retries; self._backoff = backoff
        self.name = inner.name

    async def chat(self, *a, **k):
        last = None
        for i in range(self._retries + 1):
            try:
                return await self._inner.chat(*a, **k)
            except Exception as e:
                last = e
                await asyncio.sleep(self._backoff * (2 ** i))
        raise last

    async def chat_stream(self, *a, **k):
        return await self._inner.chat_stream(*a, **k)
```

### Workspace

`Agent(workspace=...)` (and `Runner(workspace=...)`) accepts three shapes:

```python
Agent(workspace=None)                        # default: /tmp/agent-kit-runs/<run_id>, SDK rmtrees
Agent(workspace=Path("/var/cache/myapp"))    # /var/cache/myapp/<run_id>, SDK rmtrees the run subdir
Agent(workspace=lambda req, run_id: Path(    # caller-owned persistent path; SDK never touches it
    f"/data/{req.agent_id}"
))
```

When `workspace=` is a callable, `ctx.workspace_ephemeral` becomes `False`,
so toolsets know they can safely cache across runs.

### Hooks for ops

| Hook | Use case |
|---|---|
| `before_model` | Rate limit / quota check / pre-flight auth |
| `after_model` | Token / cost tracking, content moderation |
| `before_tool` | Authz, command audit log |
| `after_tool` | PII redaction, result sanitization |

### Trace events

Subscribe to the event stream (`runner.run()` async generator) and persist
events to your trace store (SQLite, OpenTelemetry, your own). Use
`event_id` + `parent_event_id` to reconstruct the tree.

### Multi-tenant

agent-kit is tenant-agnostic by design. Make one `Agent` (or `Runner`) per
tenant, with a tenant-bound `SkillRegistry` and `workspace`:

```python
def make_agent_for(tenant_id: str) -> Agent:
    return Agent(
        name=f"tenant-{tenant_id}",
        model=...,
        skills=MyDbSkillRegistry(db, tenant_id=tenant_id),
        workspace=lambda req, run_id: Path(f"/data/{tenant_id}/{req.agent_id}"),
    )
```

### When to drop down to `Runner` / `AgentLoop`

`Agent` covers ~95% of use cases. Drop down when you need:

- `runner.run()` async iterator for live event streaming → use
  `agent.runner.run(req)` or build `Runner` directly
- Construct a `RunRequest` once and rerun it → `runner.run_to_completion`
- Custom `system_prelude` / `compactor` / `hooks` per run rather than per
  agent → use `RunRequest` fields directly via `Runner`

---

## What next

- Read [`docs/tech-design.md`](tech-design.md) for the spec-level contracts
- Run the [`samples/`](../samples) demos and modify them
- File issues at the repo if anything is unclear

The whole SDK is ~3,300 lines. You're not too far from being able to read it
top-to-bottom in an afternoon.
