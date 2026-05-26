"""`SandboxToolset` — wraps any SandboxRunner into 3 LLM-facing tools.

Frozen at Stage B (spec § 16.3). Stage C-E move this file to
`agent_kit/contrib/sandbox/toolset.py` **unchanged**.

Tools exposed (subset-able via `tools=`):
    sandbox__<runner.name>__exec_command(cmd: list[str], cwd?, env?, timeout?, stdin?)
    sandbox__<runner.name>__read_file(path)
    sandbox__<runner.name>__write_file(path, content)

Wire-up:
- `connect()` calls `runner.warmup()` if present (image pull / MCP connect)
- `setup()` is lazy — runs on first `execute()` so it can see `ctx.workspace`
- Failures become `ToolResult(is_error=True)`, never raise
- stdout/stderr truncation done here; runner returns full bytes
"""

from __future__ import annotations

import base64
import json

from agent_kit import BaseToolset, ToolCall, ToolCallContext, ToolResult
from agent_kit.provider import ToolSchema

from .types import SandboxRunner

_DEFAULT_STDOUT_CAP = 8 * 1024
_DEFAULT_STDERR_CAP = 4 * 1024


class SandboxToolset(BaseToolset):
    def __init__(
        self,
        runner: SandboxRunner,
        *,
        tools: tuple[str, ...] = ("exec_command", "read_file", "write_file"),
        stdout_cap: int = _DEFAULT_STDOUT_CAP,
        stderr_cap: int = _DEFAULT_STDERR_CAP,
    ) -> None:
        unknown = set(tools) - {"exec_command", "read_file", "write_file"}
        if unknown:
            raise ValueError(f"unknown sandbox tool(s): {sorted(unknown)}")
        self.name = f"sandbox__{runner.name}"
        self._runner = runner
        self._tools = tuple(tools)
        self._stdout_cap = stdout_cap
        self._stderr_cap = stderr_cap
        self._setup_done = False

    # spec § 7.5.1 pre-warm — image pull / remote connect before tool-call round
    async def connect(self) -> None:
        warmup = getattr(self._runner, "warmup", None)
        if warmup is not None:
            await warmup()

    def build_schemas(self) -> list[ToolSchema]:
        all_schemas = {
            "exec_command": ToolSchema(
                name=f"{self.name}__exec_command",
                description=(
                    f"Execute a shell command in the {self._runner.name} sandbox. "
                    "Returns {exit_code, stdout, stderr, truncated} as JSON."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "cmd": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Command split into a list (no shell parsing).",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Working dir, relative to workspace root.",
                        },
                        "env": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Extra env vars (merged on top of runner defaults).",
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Seconds before the process is killed.",
                        },
                        "stdin": {
                            "type": "string",
                            "description": "Optional stdin content (utf-8).",
                        },
                    },
                    "required": ["cmd"],
                },
            ),
            "read_file": ToolSchema(
                name=f"{self.name}__read_file",
                description=(
                    f"Read a file from the {self._runner.name} sandbox workspace. "
                    "Binary files are returned as `BASE64:<b64>` strings."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
            "write_file": ToolSchema(
                name=f"{self.name}__write_file",
                description=f"Write a file in the {self._runner.name} sandbox workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            ),
        }
        return [all_schemas[t] for t in self._tools]

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        # Lazy setup — first call has the workspace path
        if not self._setup_done:
            await self._runner.setup(ctx.workspace)
            self._setup_done = True

        try:
            short = call.name.removeprefix(f"{self.name}__")
            if short == "exec_command":
                return await self._exec(call)
            if short == "read_file":
                return await self._read(call)
            if short == "write_file":
                return await self._write(call)
            return ToolResult(
                call_id=call.id,
                content=f"unknown sandbox tool: {short}",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 — toolset-level boundary
            return ToolResult(
                call_id=call.id,
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
            )

    async def aclose(self) -> None:
        await self._runner.aclose()

    # ---- per-tool handlers ----

    async def _exec(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        result = await self._runner.exec(
            args["cmd"],
            cwd=args.get("cwd", ""),
            env=args.get("env"),
            timeout=args.get("timeout"),
            stdin=args["stdin"].encode() if "stdin" in args else None,
        )
        stdout = result.stdout[: self._stdout_cap]
        stderr = result.stderr[: self._stderr_cap]
        payload = {
            "exit_code": result.exit_code,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "truncated": (
                result.truncated
                or len(result.stdout) > self._stdout_cap
                or len(result.stderr) > self._stderr_cap
            ),
        }
        return ToolResult(
            call_id=call.id,
            content=json.dumps(payload),
            is_error=not result.ok(),
        )

    async def _read(self, call: ToolCall) -> ToolResult:
        data = await self._runner.read(call.arguments["path"])
        try:
            return ToolResult(call_id=call.id, content=data.decode("utf-8"))
        except UnicodeDecodeError:
            return ToolResult(
                call_id=call.id,
                content=f"BASE64:{base64.b64encode(data).decode()}",
            )

    async def _write(self, call: ToolCall) -> ToolResult:
        await self._runner.write(
            call.arguments["path"],
            call.arguments["content"].encode("utf-8"),
        )
        return ToolResult(call_id=call.id, content="ok")
