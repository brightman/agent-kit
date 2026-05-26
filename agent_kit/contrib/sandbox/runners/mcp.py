"""`McpSandboxRunner` — adapter that routes exec / read / write through an MCP server.

Lets you point at E2B / Modal / Daytona / your-own MCP service and have the LLM
still see `sandbox__<name>__exec_command` instead of backend-specific tool
names. **Swap backends with one constructor change.**

Expected MCP tool shape (each name configurable via `*_tool` kwargs):

    exec_command(command: str, cwd?: str, env?: dict, timeout?: float, stdin?: str)
        → JSON: {"stdout": str, "stderr": str, "exit_code": int, "truncated"?: bool}

    read_file(path: str)
        → text body (or `BASE64:<b64>` for binary)

    write_file(path: str, content: str)
        → any ack ("ok" / "wrote N bytes" / etc.)

`setup()` mirrors LocalDir / SRT (spec § 16.3 decision #3): mkdirs the local
workspace path even though the remote sandbox manages its own filesystem. If
the MCP service exposes an `init_workspace_tool`, it's called too with the
local workspace path as argument.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_kit.toolset import ToolCallContext
from agent_kit.types import ToolCall

from ..types import ExecResult

if TYPE_CHECKING:
    from agent_kit.mcp import McpToolset


def _new_call_id() -> str:
    return f"sandbox-{uuid.uuid4().hex[:12]}"


def _unwrap_fastmcp_str(content: str) -> str:
    """FastMCP wraps a `-> str` tool's return as `{"result": "<str>"}` in
    `structuredContent`. `agent_kit.mcp._serialize_call_result` then dumps that
    as JSON, so the runner sees `'{"result": "<text>"}'` instead of `<text>`.

    This helper detects the wrapping and unwraps it. Real production MCP
    services (E2B, Modal) may return either shape, so the runner stays
    robust against both."""
    if not content.startswith('{"result"'):
        return content
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    if (
        isinstance(parsed, dict)
        and list(parsed.keys()) == ["result"]
        and isinstance(parsed["result"], str)
    ):
        return parsed["result"]
    return content


def _null_ctx() -> ToolCallContext:
    """McpToolset.execute() ignores ctx fields entirely (see agent_kit/mcp.py).
    We pass a minimal placeholder per call to satisfy the type."""
    return ToolCallContext(
        run_id="mcp-sandbox-internal",
        cancel=asyncio.Event(),
        workspace=Path("/tmp"),
        emit=lambda evt: None,
    )


class McpSandboxRunner:
    """Implements `SandboxRunner` Protocol by delegating to an MCP server."""

    def __init__(
        self,
        mcp_toolset: "McpToolset",
        *,
        name: str = "remote",
        exec_tool: str = "exec_command",
        read_tool: str = "read_file",
        write_tool: str = "write_file",
        init_workspace_tool: str | None = None,
    ) -> None:
        self.name = name
        self._mcp = mcp_toolset
        server_name = mcp_toolset.name.removeprefix("mcp__")
        prefix = f"mcp__{server_name}__"
        self._exec = prefix + exec_tool
        self._read = prefix + read_tool
        self._write = prefix + write_tool
        self._init_workspace = (
            prefix + init_workspace_tool if init_workspace_tool else None
        )

    # Called by SandboxToolset.connect() during Runner pre-warm (spec § 7.5.1)
    async def warmup(self) -> None:
        await self._mcp.connect()

    async def setup(self, workspace: Path) -> None:
        # spec § 16.3 decision #3: consistent with LocalDir/SRT
        workspace.mkdir(parents=True, exist_ok=True)
        if self._init_workspace is not None:
            await self._call(self._init_workspace, {"workspace": str(workspace)})

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str = "",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        if not cmd:
            return ExecResult(b"", b"empty command", 127)
        args: dict[str, Any] = {
            "command": " ".join(shlex.quote(a) for a in cmd),
        }
        if cwd:
            args["cwd"] = cwd
        if env:
            args["env"] = env
        if timeout is not None:
            args["timeout"] = timeout
        if stdin is not None:
            args["stdin"] = stdin.decode("utf-8", errors="replace")

        result = await self._call(self._exec, args)
        if result.is_error:
            return ExecResult(
                stdout=b"",
                stderr=result.content.encode("utf-8"),
                exit_code=1,
            )
        text = _unwrap_fastmcp_str(result.content)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # service didn't return JSON — wrap raw text as stdout
            return ExecResult(
                stdout=text.encode("utf-8"),
                stderr=b"",
                exit_code=0,
            )
        if not isinstance(payload, dict) or "exit_code" not in payload:
            # JSON but not the expected shape — fall back to raw stdout
            return ExecResult(
                stdout=text.encode("utf-8"),
                stderr=b"",
                exit_code=0,
            )
        return ExecResult(
            stdout=payload.get("stdout", "").encode("utf-8"),
            stderr=payload.get("stderr", "").encode("utf-8"),
            exit_code=int(payload.get("exit_code", 0)),
            truncated=bool(payload.get("truncated", False)),
        )

    async def read(self, path: str) -> bytes:
        result = await self._call(self._read, {"path": path})
        if result.is_error:
            raise FileNotFoundError(f"{path}: {result.content}")
        text = _unwrap_fastmcp_str(result.content)
        if text.startswith("BASE64:"):
            return base64.b64decode(text[7:])
        return text.encode("utf-8")

    async def write(self, path: str, content: bytes) -> None:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = "BASE64:" + base64.b64encode(content).decode("ascii")
        result = await self._call(self._write, {"path": path, "content": text})
        if result.is_error:
            raise IOError(f"{path}: {result.content}")

    async def aclose(self) -> None:
        await self._mcp.aclose()

    # ---- internal ----

    async def _call(self, tool_name: str, arguments: dict[str, Any]):
        """Invoke an MCP tool via the wrapped McpToolset."""
        return await self._mcp.execute(
            ToolCall(id=_new_call_id(), name=tool_name, arguments=arguments),
            _null_ctx(),
        )
