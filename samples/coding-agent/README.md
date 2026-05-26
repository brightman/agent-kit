# coding-agent (agent-kit sample · sandbox API demo)

This sample wires `Agent` against `agent_kit.contrib.sandbox.SandboxToolset`.
Originally it lived in Stage B to **freeze** the SandboxRunner / SandboxToolset
API; the freeze tests now serve as a regression suite that pins the contract
across Stage C-E backends.

It ships:

- A local `StubRunner` (`app/_stub.py`) backed by an in-memory `dict[str, bytes]`
  — no real subprocess, no API key, no sandbox image
- 14 offline tests that pin the contract — any Stage C-E change that breaks
  them = freeze broken
- (As of Stage C) `LocalDirRunner` is shipped in
  `agent_kit.contrib.sandbox.runners.localdir` and tested in
  `tests/contrib/sandbox/test_localdir.py` — swap the import in `app/agent.py`
  to use it with real subprocesses

See spec § 16 in `docs/tech-design.md` for the wider design.

## The frozen contract

### `SandboxRunner` Protocol (5 methods)

```python
@runtime_checkable
class SandboxRunner(Protocol):
    name: str

    async def setup(self, workspace: Path) -> None: ...
    async def exec(
        self, cmd: list[str], *,
        cwd: str = "", env: dict[str, str] | None = None,
        timeout: float | None = None, stdin: bytes | None = None,
    ) -> ExecResult: ...
    async def read(self, path: str) -> bytes: ...
    async def write(self, path: str, content: bytes) -> None: ...
    async def aclose(self) -> None: ...
```

### `SandboxToolset` (BaseToolset)

- Tool names: `sandbox__<runner.name>__{exec_command,read_file,write_file}`
- Default exposes 3 tools; `tools=("exec_command",)` to subset
- `connect()` calls `runner.warmup()` if present (image pull, MCP connect)
- `setup(workspace)` is **lazy** — fires on first `execute()` (so it gets
  `ctx.workspace` from Runner)
- Failures become `ToolResult(is_error=True)`, never raises
- stdout/stderr truncation done at toolset layer (defaults 8 KiB / 4 KiB);
  Runner always returns full bytes

### `ExecResult` dataclass (frozen)

```python
ExecResult(stdout: bytes, stderr: bytes, exit_code: int, truncated: bool = False)
```

`ok()` returns `exit_code == 0`.

## Layout

```
samples/coding-agent/
├── app/
│   ├── agent.py          Agent + SandboxToolset(StubRunner) wiring
│   ├── main.py           CLI (one-shot + REPL)
│   ├── test_agent.py     14 freeze tests (pin the contract)
│   └── _stub.py          Sample-local StubRunner + DEFAULT_COMMANDS
├── pyproject.toml
└── .env.example

agent_kit/contrib/sandbox/        ← Moved here in Stage C
├── types.py                      SandboxRunner Protocol + ExecResult
├── toolset.py                    SandboxToolset (BaseToolset)
└── runners/
    └── localdir.py               Real-subprocess runner (Stage C)
```

## Run

### Offline tests (no API key)

```bash
cd samples/coding-agent
PYTHONPATH=../.. python -m pytest app/test_agent.py -v
# 14 passed
```

### Interactive (with a real LLM)

```bash
cp .env.example .env
# edit .env: GOOGLE_API_KEY=...

cd samples/coding-agent
PYTHONPATH=../.. python -m app.main "Read task.md and do as told."
```

## What StubRunner does

- `files: dict[str, bytes]` — workspace contents (read/write touches this dict)
- `commands: dict[name, async handler]` — scripts `exec` behavior
- `DEFAULT_COMMANDS` ships handlers for `echo` / `ls` / `cat` / `pytest`
- `setup(workspace)` calls `workspace.mkdir(parents=True, exist_ok=True)` —
  matches the contract decision in spec § 16.3 (all three real runners do the
  same)

## Swap StubRunner for LocalDirRunner

Once Stage C ships, you can swap to a real-subprocess runner in 3 lines:

```python
# in app/agent.py
from agent_kit.contrib.sandbox.runners import LocalDirRunner

runner = LocalDirRunner(command_allowlist=["python", "pytest", "ls", "cat"])
# instead of: StubRunner(files=..., commands=DEFAULT_COMMANDS)
```

`LocalDirRunner` runs commands as actual host subprocesses (no isolation —
trusted code only) under the agent's workspace dir, with path-traversal
defense and an optional `command_allowlist`. See
`tests/contrib/sandbox/test_localdir.py` for the full contract.

## Stage history

| Stage | Tag | What changed |
|---|---|---|
| B | `sandbox-api-frozen` | SandboxRunner / SandboxToolset frozen via 14 tests |
| C | `sandbox-1` | Moved to `agent_kit/contrib/sandbox/`; added LocalDirRunner |
| D | (planned) | SrtRunner — Anthropic sandbox-runtime |
| E | (planned) | McpSandboxRunner — any MCP exec server |

## Mapping to spec § 16

| Spec section | Where |
|---|---|
| § 16.3 `SandboxRunner` Protocol | `app/sandbox/types.py` |
| § 16.3 `SandboxToolset` | `app/sandbox/toolset.py` |
| § 16.3 "SHOULD NOT expose" list | enforced by the absent methods |
| § 16.3 runner contract decision #3 (setup mkdirs workspace) | `StubRunner.setup` |
| § 16.5 Stage table | tracks this PR as Stage B |
