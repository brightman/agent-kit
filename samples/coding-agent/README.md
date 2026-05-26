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

## Switch backends (Stage F)

`build_agent(backend=...)` picks the runner. Stage F adds the `localdir` option:

```python
from app.agent import build_agent

agent = build_agent(backend="stub")        # in-memory dict, no subprocess
agent = build_agent(backend="localdir")    # real host subprocess
```

Or via CLI:

```bash
PYTHONPATH=../.. python -m app.main --backend localdir \
    "Read greet.py, run it, fix the typo if any, run again"
```

When `backend="localdir"`:
- `seed_files` is materialized to disk by a `workspace_provider` lambda
- `LocalDirRunner` runs commands as actual host subprocesses (no isolation —
  trusted code only), with `command_allowlist=["ls","cat","echo","python",
  "python3","pytest","grep","head","tail","wc"]` and `env_passthrough=
  ("PATH","HOME")`
- Path-traversal defense + timeout / stdin plumbing come from
  `agent_kit.contrib.sandbox.runners.localdir`

## Live e2e tests (Stage F)

`app/test_live.py` proves the frozen API works end-to-end with a real
subprocess + real fs — no API key needed (ScriptedProvider plays the model):

| Test | What it proves |
|---|---|
| `test_localdir_end_to_end_bug_fix` | 5-tool conversation fixes a real Python bug; `python3 greet.py` actually runs in a subprocess and switches from "wrold" → "world" |
| `test_localdir_allowlist_blocks_disallowed_command` | `rm -rf /` is blocked at the runner; LLM sees `is_error=True, exit_code=126` |
| `test_localdir_path_traversal_blocked_e2e` | `read_file("../../etc/passwd")` is rejected at toolset boundary as `is_error=True` |
| `test_build_agent_localdir_backend_wires_real_runner` | `build_agent(backend="localdir")` produces a usable Agent end-to-end |

## Stage history

| Stage | Tag | What changed |
|---|---|---|
| B | `sandbox-api-frozen` | SandboxRunner / SandboxToolset frozen via 14 tests |
| C | `sandbox-1` | Moved to `agent_kit/contrib/sandbox/`; added LocalDirRunner |
| D | `sandbox-2` | SrtRunner — Anthropic sandbox-runtime wrapper |
| E | `sandbox-3` | McpSandboxRunner — any MCP exec server adapter |
| F | `sandbox-sample-live` | `build_agent(backend="localdir")` + `--backend` CLI flag + 4 live e2e tests with real subprocess |

## Mapping to spec § 16

| Spec section | Where |
|---|---|
| § 16.3 `SandboxRunner` Protocol | `app/sandbox/types.py` |
| § 16.3 `SandboxToolset` | `app/sandbox/toolset.py` |
| § 16.3 "SHOULD NOT expose" list | enforced by the absent methods |
| § 16.3 runner contract decision #3 (setup mkdirs workspace) | `StubRunner.setup` |
| § 16.5 Stage table | tracks this PR as Stage B |
