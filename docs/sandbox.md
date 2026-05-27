# Sandbox — testing the three backends

`agent_kit.contrib.sandbox` ships a 5-method `SandboxRunner` Protocol plus
three reference implementations. This doc covers **how to verify each one
actually works** — from zero-setup unit tests through to live integration
against real binaries / remote services.

For the design rationale, see [spec § 16](tech-design.md). For the user
guide, see [docs/tutorial.md § 8](tutorial.md). For a runnable end-to-end
demo, see [`samples/coding-agent/`](../samples/coding-agent/).

---

## TL;DR — the four testing layers

| Layer | What it proves | Setup |
|---|---|---|
| **Unit tests** | Protocol shape, every error path, every code branch | `pip install -e .[dev]` |
| **In-process smoke** | The runner talks to a real backend protocol (subprocess / MCP) | none (uses in-memory FastMCP) |
| **Real-binary smoke** | The runner talks to the *actual* `srt` CLI / remote MCP | install `srt` OR have a key |
| **Live agent** | Full LLM → tool → sandbox round-trip via `SandboxToolset` | `samples/agent-tui/` |

---

## The three runners at a glance

| Runner | Isolation | When | Default name |
|---|---|---|---|
| `LocalDirRunner` | None — host subprocess. `command_allowlist` + path-traversal guard. | Dev / trusted code / integration tests. | `localdir` |
| `SrtRunner` | Anthropic [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) profile (filesystem ACL + network limits). | Local untrusted code on macOS / Linux. | `srt` |
| `McpSandboxRunner` | Whatever the remote MCP server provides. | Production / E2B / Modal / Daytona / your own. | `remote` |

All three implement the same 5-method `SandboxRunner` Protocol, so the
LLM-facing tools (`sandbox__<name>__exec_command` / `read_file` /
`write_file`) are identical across backends. **Swap backends with one line
of code; the agent doesn't notice.**

---

## Layer 1 — existing unit tests (zero setup)

The fastest verification. No `srt`, no API key, no network.

```bash
cd /Users/karama/Documents/baizhi/agent-kit

# LocalDir runner (23 tests) — real subprocess in tmp_path
.venv/bin/python -m pytest tests/contrib/sandbox/test_localdir.py -v

# SRT runner (23 tests) — argv-shape verified via subprocess monkeypatch
.venv/bin/python -m pytest tests/contrib/sandbox/test_srt.py -v

# MCP sandbox runner (19 tests) — real MCP round-trips via in-memory FastMCP
.venv/bin/python -m pytest tests/contrib/sandbox/test_mcp.py -v

# All 65 sandbox tests in ~1 second
.venv/bin/python -m pytest tests/contrib/sandbox/ -v
```

Expected: `65 passed`.

**What they cover** (matters because you don't need anything else to
verify a refactor):

- Protocol shape (`isinstance(runner, SandboxRunner)` for each)
- `setup()` mkdirs the workspace (spec § 16.3 decision #3)
- `exec()` returns `ExecResult(stdout, stderr, exit_code)` correctly
- Allowlist enforcement
- Path-traversal block (`read("../../etc/passwd")` → `PermissionError`)
- Timeout handling (`exit_code=124`)
- Binary missing / connection failed → friendly `exit_code=127` (no raise)
- `aclose()` idempotency
- For MCP: full JSON parse, FastMCP `{"result": str}` unwrap, base64 binary

---

## Layer 2 — SRT real-binary smoke

Requires the `srt` CLI installed.

### Install

```bash
# Reference: https://github.com/anthropic-experimental/sandbox-runtime
# Install instructions vary by platform; check the repo. On macOS:
brew install anthropic-experimental/sandbox-runtime/srt   # if formula exists
# OR build from source per the repo's README

which srt    # should print a path
srt --version
```

### Smoke script

Save as `scripts/smoke_srt.py`:

```python
"""SRT real-binary smoke — exercises every SrtRunner method against the
actual srt CLI. Requires `srt` on PATH."""

import asyncio
from pathlib import Path

from agent_kit.contrib.sandbox.runners import SrtRunner


async def main() -> None:
    runner = SrtRunner(image="default")
    workspace = Path("/tmp/srt-smoke")
    await runner.setup(workspace)
    print(f"workspace: {workspace}")

    # (1) basic exec
    r = await runner.exec(["echo", "hello from srt"])
    print(f"echo:        exit={r.exit_code}  stdout={r.stdout.decode().strip()!r}")

    # (2) write → read (bind-mount semantics: host fs == sandbox fs)
    await runner.write("hello.txt", b"agent-kit + srt")
    assert (workspace / "hello.txt").read_bytes() == b"agent-kit + srt"
    body = await runner.read("hello.txt")
    print(f"write+read:  {body!r}")

    # (3) path-traversal defense
    try:
        await runner.read("../../etc/passwd")
        print("traversal:   NOT BLOCKED (unexpected)")
    except PermissionError as e:
        print(f"traversal:   blocked — {e}")

    # (4) timeout
    r = await runner.exec(["sleep", "10"], timeout=0.5)
    print(f"timeout:     exit={r.exit_code}  stderr={r.stderr.decode().strip()!r}")

    # (5) nonzero exit propagates
    r = await runner.exec(["sh", "-c", "exit 7"])
    print(f"nonzero:     exit={r.exit_code}  ok={r.ok()}")

    await runner.aclose()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
```

### Run

```bash
cd /Users/karama/Documents/baizhi/agent-kit
PYTHONPATH=. .venv/bin/python scripts/smoke_srt.py
```

Expected output (timestamps will differ):

```
workspace: /tmp/srt-smoke
echo:        exit=0  stdout='hello from srt'
write+read:  b'agent-kit + srt'
traversal:   blocked — path escapes workspace: '../../etc/passwd'
timeout:     exit=124  stderr='timeout after 0.5s'
nonzero:     exit=7  ok=False
OK
```

### If srt is not installed

You'll see `exit=127  stderr="srt binary not found: 'srt' (...)"` — this is
the *expected* fallback behavior. `SrtRunner` catches `FileNotFoundError`
from `asyncio.create_subprocess_exec` and turns it into a soft
`ExecResult(exit_code=127)` instead of raising. The unit tests verify this
path (`test_srt_binary_missing_returns_127`).

---

## Layer 3 — MCP sandbox in-process smoke

Verifies the runner does real MCP protocol round-trips. **No external
dependencies**: spins up FastMCP in the same Python process.

Save as `scripts/smoke_mcp_sandbox.py`:

```python
"""MCP sandbox smoke — spins up a FastMCP server with exec/read/write
tools in-process, wraps it via McpSandboxRunner, drives a few operations."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from agent_kit.contrib.sandbox.runners.mcp import McpSandboxRunner
from agent_kit.mcp import McpServerConfig, McpToolset


# ---- 1. fake-but-real MCP server: exec/read/write backed by a dict ----

srv = FastMCP("sandbox-smoke")
_fs: dict[str, str] = {}


@srv.tool()
def exec_command(command: str, cwd: str = "", env: dict | None = None,
                 timeout: float | None = None, stdin: str | None = None) -> dict:
    """Echo the command back; pretend we ran it."""
    return {"stdout": f"executed: {command}", "stderr": "", "exit_code": 0}


@srv.tool()
def read_file(path: str) -> str:
    if path not in _fs:
        raise FileNotFoundError(path)
    return _fs[path]


@srv.tool()
def write_file(path: str, content: str) -> str:
    _fs[path] = content
    return "ok"


# ---- 2. In-memory transport (same pattern as tests/test_mcp.py) ----

class _InMemMcp(McpToolset):
    def __init__(self) -> None:
        super().__init__(
            McpServerConfig(name="smoke", transport="stdio", command=["unused"]),
        )

    async def _open_streams(self, stack: AsyncExitStack):  # type: ignore[override]
        client, server = await stack.enter_async_context(
            create_client_server_memory_streams()
        )
        client_read, client_write = client
        server_read, server_write = server
        tg = await stack.enter_async_context(anyio.create_task_group())
        underlying = srv._mcp_server
        opts = underlying.create_initialization_options()
        tg.start_soon(
            lambda: underlying.run(server_read, server_write, opts,
                                   raise_exceptions=False)
        )
        stack.push_async_callback(lambda: tg.cancel_scope.cancel())
        return client_read, client_write


# ---- 3. drive the runner ----

async def main() -> None:
    mcp = _InMemMcp()
    runner = McpSandboxRunner(mcp, name="smoke")
    await runner.warmup()
    await runner.setup(Path("/tmp/mcp-smoke"))

    # (1) exec — goes through MCP exec_command tool, JSON-parsed back
    r = await runner.exec(["ls", "-la", "/some/path"])
    print(f"exec:        exit={r.exit_code}  stdout={r.stdout.decode()!r}")

    # (2) write → read round-trip
    await runner.write("note.txt", b"hi from mcp sandbox")
    body = await runner.read("note.txt")
    print(f"read:        {body!r}")

    # (3) missing file → FileNotFoundError (matches LocalDirRunner shape)
    try:
        await runner.read("ghost")
        print("missing:     NOT RAISED (unexpected)")
    except FileNotFoundError as e:
        print(f"missing:     raised — {e}")

    # (4) binary write + read via BASE64: prefix convention
    await runner.write("blob.bin", b"\x00\x01\xff\xfe")
    raw = await runner.read("blob.bin")
    print(f"binary:      {raw!r}")

    await runner.aclose()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
```

### Run

```bash
cd /Users/karama/Documents/baizhi/agent-kit
PYTHONPATH=. .venv/bin/python scripts/smoke_mcp_sandbox.py
```

Expected output:

```
exec:        exit=0  stdout='executed: ls -la /some/path'
read:        b'hi from mcp sandbox'
missing:     raised — note.txt: ghost: ...
binary:      b'\x00\x01\xff\xfe'
OK
```

This proves:
- `warmup()` connects via real MCP `initialize` handshake
- `exec()` packs args, calls remote tool, parses JSON response back to `ExecResult`
- `read()` / `write()` round-trip through `read_file` / `write_file` tools
- Binary content goes through `BASE64:` prefix correctly
- Missing-file errors translate to `FileNotFoundError` (so caller code that
  works against `LocalDirRunner` keeps working)

---

## Layer 4 — MCP sandbox against a real remote vendor

E2B, Modal, Daytona, or your own — anything that exposes the standard MCP
exec_command / read_file / write_file tool shape.

```python
from agent_kit import Agent, McpToolset
from agent_kit.contrib.sandbox import SandboxToolset
from agent_kit.contrib.sandbox.runners import McpSandboxRunner

# 1. Connect to the remote MCP server
mcp = McpToolset.http(
    "e2b",  # any short name; becomes part of tool prefix
    url="https://api.e2b.dev/mcp",
    headers={"Authorization": "Bearer ${E2B_API_KEY}"},
    # ${VAR} is substituted from os.environ at construct time
)

# 2. Wrap in SandboxToolset
sandbox = SandboxToolset(McpSandboxRunner(mcp, name="e2b"))

# 3. Drop into Agent — LLM sees sandbox__e2b__{exec_command,read_file,write_file}
agent = Agent(
    name="prod-coder",
    model="gemini/gemini-2.5-flash",
    tools=[sandbox],
)
result = agent.run_sync("Read /workspace/main.py and tell me what it does.")
```

If the remote server uses different tool names (e.g. `bash_exec` instead of
`exec_command`):

```python
McpSandboxRunner(
    mcp, name="e2b",
    exec_tool="bash_exec",        # remote tool name
    read_tool="fs_read",
    write_tool="fs_write",
    init_workspace_tool="init",   # optional: called from setup() if set
)
```

### Real-vendor smoke checklist

If you've never connected to that vendor's MCP before, run these 4 lines
**before** plugging into an Agent:

```python
import asyncio, os
from agent_kit import McpToolset, ToolCall
from agent_kit.contrib.sandbox.runners.mcp import McpSandboxRunner
from pathlib import Path

async def probe():
    mcp = McpToolset.http("vendor", url=os.environ["VENDOR_MCP_URL"],
                          headers={"Authorization": f"Bearer {os.environ['VENDOR_KEY']}"})
    runner = McpSandboxRunner(mcp, name="vendor")
    await runner.warmup()
    await runner.setup(Path("/tmp/vendor-smoke"))
    r = await runner.exec(["echo", "hello"])
    print("vendor live:", r.exit_code, r.stdout)
    await runner.aclose()

asyncio.run(probe())
```

If you see `exit_code=1` with `ERROR: not owned by ...` in stderr, you
have a tool-name mismatch — pass the right `exec_tool=` etc. If
`exit_code=0` but `stdout` is empty, the vendor probably uses a different
JSON shape; check what their `exec` tool returns (FastMCP-style
`{"result": "..."}` is auto-unwrapped; arbitrary shapes need an adapter).

---

## Layer 5 — live agent (full LLM → tool → sandbox)

The `samples/coding-agent/` sample drives `LocalDirRunner` end-to-end with
a scripted LLM. To run it with **any** of the three backends, change one
line in `app/agent.py`:

```python
# Current:
from agent_kit.contrib.sandbox.runners import LocalDirRunner
runner = LocalDirRunner(command_allowlist=["python", "pytest", ...])

# Swap for SRT (real isolation, needs srt installed):
from agent_kit.contrib.sandbox.runners import SrtRunner
runner = SrtRunner(name="localdir", profile="/etc/srt/python.toml")
#                  ^^^^^^^^^^^^^^^^ keep "localdir" so prompts and tests
#                  don't need to know about the swap. Otherwise update
#                  INSTRUCTION + sample tests to expect sandbox__srt__*.

# Or remote MCP (vendor-managed):
from agent_kit.contrib.sandbox.runners import McpSandboxRunner
runner = McpSandboxRunner(your_mcp_toolset, name="localdir")
```

The agent's INSTRUCTION, the LLM's tool calls, the freeze tests — none of
them change. That's the whole point of `SandboxRunner` Protocol: backends
are interchangeable.

### Live test with real LLM in agent-tui

`samples/agent-tui/` already uses `LocalDirRunner` against a persistent
`~/.agent-tui-workspace`. Swap to SRT or MCP the same way:

```bash
cd samples/agent-tui
# Edit app/agent.py::_build_sandbox_toolset to use SrtRunner / McpSandboxRunner
PYTHONPATH=../.. python -m app.tui
```

The events panel now shows real sandbox tool calls — `sandbox__srt__exec_command(...)`
or `sandbox__remote__exec_command(...)` — flowing through the LLM loop.

---

## Troubleshooting

### "command not in allowlist" (exit 126)

LocalDir: edit `_SANDBOX_ALLOWLIST` in the runner config to add the
binary. SRT: not allowlisted by our code; that's an SRT profile concern.

### "path escapes workspace" (`PermissionError`)

The LLM tried an absolute path or `..` traversal. This is by design —
`LocalDirRunner._resolve` and `SrtRunner._resolve` block any path resolving
outside the workspace root. If the LLM keeps hitting it, your INSTRUCTION
should explicitly tell the model to use workspace-relative paths.

### "srt binary not found" (exit 127)

`srt` isn't on `PATH`. Install it (see Layer 2) or set
`srt_binary="/full/path/to/srt"` on `SrtRunner(...)`.

### McpSandboxRunner exec returns exit_code=0 but empty stdout

The remote MCP tool returned a non-`exec_command`-shaped payload. The
runner falls back to "treat raw text as stdout" when it can't find an
`exit_code` key. Check what the server actually returns:

```python
# Bypass the adapter, see raw MCP response:
from agent_kit import ToolCall
from agent_kit.toolset import ToolCallContext
import asyncio
ctx = ToolCallContext(
    run_id="probe", cancel=asyncio.Event(),
    workspace=Path("/tmp"), emit=lambda e: None,
)
raw = await mcp.execute(
    ToolCall(id="x", name="mcp__vendor__exec_command", arguments={"command": "ls"}),
    ctx,
)
print(raw.content)
```

The expected shape is `'{"stdout": "...", "stderr": "...", "exit_code": 0}'`.
If you see a different shape, you may need a custom adapter — subclass
`McpSandboxRunner` and override `_call` / `exec`.

### MCP `connect()` hangs

The remote server isn't responding to MCP `initialize`. Check:
1. URL is reachable (`curl` the base URL — should at least 404 cleanly)
2. Authorization header is correct (env var actually populated?)
3. Server actually speaks MCP (some vendors use OpenAI-compatible REST,
   not MCP — pick a different transport)

### Tests pass but live agent's tool calls fail silently

Check the `event_stream`. Errors are reported as `Event(kind="error",
payload={"stage": "tool", ...})`. In `samples/agent-tui` the right panel
shows these in red. If the tool calls happen but the LLM ignores
results, your INSTRUCTION probably doesn't tell the model what the tools
do or when to use them — see the `INSTRUCTION_TMPL` pattern in
`samples/agent-tui/app/agent.py`.

---

## Cross-reference

- **Spec contract**: [docs/tech-design.md § 16](tech-design.md) — the
  full diet sandbox spec, including what's deliberately not in scope (no
  Manifest / Capability / Snapshot / PTY)
- **User guide**: [docs/tutorial.md § 8](tutorial.md) — how to use the
  sandbox in your own Agent
- **Runnable sample**: [samples/coding-agent](../samples/coding-agent) —
  ScriptedProvider + `LocalDirRunner` actually fixes a bug in Python
  source via real `python3` subprocess
- **TUI sample**: [samples/agent-tui](../samples/agent-tui) — Qwen +
  Anthropic skill-creator + Aliyun WebSearch MCP + LocalDir sandbox, all
  with live event-stream rendering
