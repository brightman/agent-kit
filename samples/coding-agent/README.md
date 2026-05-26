# coding-agent (agent-kit sample · Stage B sandbox API freeze)

This sample's job: **freeze the `SandboxToolset` + `SandboxRunner` API** before
Stages C-E build the real `LocalDir` / `SRT` / `MCP` runners. It ships:

- The full Stage-B contract (`app/sandbox/{types,toolset}.py`)
- A `StubRunner` backed by an in-memory `dict[str, bytes]` (no real subprocess,
  no API key, no sandbox image)
- 14 offline tests that pin the contract — Stage C breaks them = freeze broken

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
│   ├── agent.py          Agent construction
│   ├── main.py           CLI (one-shot + REPL)
│   ├── test_agent.py     14 freeze tests
│   └── sandbox/          ← Stage C moves this whole subtree to
│       │                   agent_kit/contrib/sandbox/ unchanged
│       ├── types.py      SandboxRunner Protocol + ExecResult
│       ├── toolset.py    SandboxToolset (BaseToolset)
│       └── runners/
│           └── stub.py   In-memory StubRunner + DEFAULT_COMMANDS
├── pyproject.toml
└── .env.example
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

## Going from Stage B → Stage C

1. `mv samples/coding-agent/app/sandbox agent_kit/contrib/sandbox`
2. Add `agent_kit/contrib/sandbox/runners/localdir.py` (real subprocess)
3. Update sample's `app/agent.py` to import from `agent_kit.contrib.sandbox`
4. Run the 14 tests — they MUST still pass without modification
5. If any test needed editing, freeze is broken — go back to Stage B

## Mapping to spec § 16

| Spec section | Where |
|---|---|
| § 16.3 `SandboxRunner` Protocol | `app/sandbox/types.py` |
| § 16.3 `SandboxToolset` | `app/sandbox/toolset.py` |
| § 16.3 "SHOULD NOT expose" list | enforced by the absent methods |
| § 16.3 runner contract decision #3 (setup mkdirs workspace) | `StubRunner.setup` |
| § 16.5 Stage table | tracks this PR as Stage B |
