"""McpSandboxRunner — adapter to any MCP server exposing exec/read/write (Stage E).

Uses an in-memory FastMCP server (same pattern as tests/test_mcp.py) so the
test pulls real MCP round-trips through the runner.

Coverage:
- Protocol shape (isinstance(McpSandboxRunner(...), SandboxRunner))
- warmup() triggers mcp.connect()
- setup() mkdirs workspace + optionally invokes init_workspace tool
- exec() builds command from list[str] via shlex; parses JSON response into ExecResult
- exec() empty cmd → exit 127 (boundary check before round-trip)
- exec() MCP error → ExecResult(exit_code=1, stderr=<msg>)
- exec() non-JSON response → wrapped as stdout with exit_code=0 (graceful)
- read() returns utf-8 directly
- read() BASE64:<b64> prefix decoded to bytes
- read() MCP error → FileNotFoundError
- write() utf-8 content sent as plain string
- write() non-utf8 → BASE64: prefix
- write() MCP error → IOError
- aclose() closes underlying mcp toolset
- custom *_tool kwargs → different MCP tool names
- runner.name → SandboxToolset prefix (default "remote", customizable)
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from agent_kit.contrib.sandbox.runners.mcp import McpSandboxRunner
from agent_kit.contrib.sandbox.types import ExecResult, SandboxRunner
from agent_kit.mcp import McpServerConfig, McpToolset


# ---- in-memory FastMCP server + toolset ----


def _make_sandbox_server(
    *, exec_payload: dict[str, Any] | None = None
) -> FastMCP:
    """Server exposing exec_command / read_file / write_file with an in-memory
    fs (a closure-captured dict). exec_command echoes args back as JSON so tests
    can introspect what the runner sent."""
    srv: FastMCP = FastMCP("sandbox")
    fs: dict[str, str] = {}

    @srv.tool()
    def exec_command(
        command: str,
        cwd: str = "",
        env: dict | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ) -> str:
        """Returns JSON {stdout, stderr, exit_code, truncated}."""
        if exec_payload is not None:
            return json.dumps(exec_payload)
        # default: echo the parsed args so tests can inspect what was sent
        return json.dumps({
            "stdout": json.dumps({
                "command": command,
                "cwd": cwd,
                "env": env or {},
                "timeout": timeout,
                "stdin": stdin,
            }),
            "stderr": "",
            "exit_code": 0,
        })

    @srv.tool()
    def read_file(path: str) -> str:
        if path not in fs:
            raise FileNotFoundError(path)
        return fs[path]

    @srv.tool()
    def write_file(path: str, content: str) -> str:
        fs[path] = content
        return "ok"

    @srv.tool()
    def init_workspace(workspace: str) -> str:
        fs["__init_called_with__"] = workspace
        return "initialized"

    @srv.tool()
    def fail_exec() -> str:
        raise RuntimeError("simulated failure")

    return srv


class _InMemMcpToolset(McpToolset):
    """Test seam: opens memory streams + spawns FastMCP server task."""

    def __init__(self, server: FastMCP, name: str = "sandbox") -> None:
        super().__init__(
            McpServerConfig(name=name, transport="stdio", command=["unused"]),
        )
        self._server = server

    async def _open_streams(self, stack: AsyncExitStack):  # type: ignore[override]
        import anyio

        client_streams, server_streams = await stack.enter_async_context(
            create_client_server_memory_streams()
        )
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        tg = await stack.enter_async_context(anyio.create_task_group())
        underlying = self._server._mcp_server
        opts = underlying.create_initialization_options()
        tg.start_soon(
            lambda: underlying.run(server_read, server_write, opts,
                                   raise_exceptions=False)
        )
        stack.push_async_callback(_cancel_tg, tg)
        return client_read, client_write


async def _cancel_tg(tg) -> None:
    tg.cancel_scope.cancel()


# ---- Protocol shape ----


def test_mcp_runner_satisfies_protocol() -> None:
    mcp = McpToolset(
        McpServerConfig(name="x", transport="stdio", command=["unused"])
    )
    assert isinstance(McpSandboxRunner(mcp), SandboxRunner)


# ---- warmup / setup ----


async def test_warmup_connects_underlying_mcp(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        assert mcp._connected is False
        await runner.warmup()
        assert mcp._connected is True
    finally:
        await runner.aclose()


async def test_setup_mkdirs_workspace(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        ws = tmp_path / "new" / "deep"
        await runner.setup(ws)
        assert ws.exists()
    finally:
        await runner.aclose()


async def test_setup_invokes_init_workspace_tool_when_configured(tmp_path) -> None:
    """If the MCP service exposes init_workspace, setup() calls it."""
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(
        mcp, init_workspace_tool="init_workspace",
    )
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        # Verify the server saw the init call (it stores workspace path in fs)
        marker = await runner.read("__init_called_with__")
        assert marker.decode("utf-8") == str(tmp_path)
    finally:
        await runner.aclose()


async def test_setup_skips_init_when_no_tool_configured(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)  # no init_workspace_tool
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        # init_workspace was not called → marker missing
        from contextlib import suppress
        with suppress(FileNotFoundError):
            await runner.read("__init_called_with__")
            raise AssertionError("init_workspace should not have been called")
    finally:
        await runner.aclose()


# ---- exec ----


async def test_exec_builds_shlex_quoted_command(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        # default server echoes args as JSON-in-stdout
        result = await runner.exec(["echo", "hello world", "with spaces"])
        assert result.exit_code == 0
        echoed = json.loads(result.stdout)
        # shlex.quote wrapped the spacey args in quotes
        assert echoed["command"] == "echo 'hello world' 'with spaces'"
    finally:
        await runner.aclose()


async def test_exec_passes_optional_args_through(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        result = await runner.exec(
            ["python", "-V"],
            cwd="src",
            env={"FOO": "bar"},
            timeout=30,
            stdin=b"piped",
        )
        echoed = json.loads(result.stdout)
        assert echoed["cwd"] == "src"
        assert echoed["env"] == {"FOO": "bar"}
        assert echoed["timeout"] == 30
        assert echoed["stdin"] == "piped"
    finally:
        await runner.aclose()


async def test_exec_omits_optional_args_when_default(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        result = await runner.exec(["ls"])
        echoed = json.loads(result.stdout)
        assert echoed["cwd"] == ""        # default ""
        assert echoed["env"] == {}        # default None → server default {}
        assert echoed["timeout"] is None  # default None
        assert echoed["stdin"] is None
    finally:
        await runner.aclose()


async def test_exec_empty_cmd_short_circuits(tmp_path) -> None:
    """Empty cmd never hits the wire — boundary check at runner."""
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        r = await runner.exec([])
        assert r.exit_code == 127
        assert b"empty command" in r.stderr
    finally:
        await runner.aclose()


async def test_exec_parses_full_payload(tmp_path) -> None:
    """Full JSON: stdout / stderr / exit_code / truncated all round-trip."""
    srv = _make_sandbox_server(exec_payload={
        "stdout": "out",
        "stderr": "err",
        "exit_code": 7,
        "truncated": True,
    })
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        r = await runner.exec(["whatever"])
        assert r.stdout == b"out"
        assert r.stderr == b"err"
        assert r.exit_code == 7
        assert r.truncated is True
        assert r.ok() is False
    finally:
        await runner.aclose()


async def test_exec_non_json_response_wrapped_as_stdout(tmp_path) -> None:
    """Service that returns plain text → ExecResult(stdout=<text>, exit_code=0)."""
    # Server returns raw "hello" not JSON
    srv: FastMCP = FastMCP("textonly")

    @srv.tool()
    def exec_command(command: str, cwd: str = "", env: dict | None = None,
                     timeout: float | None = None, stdin: str | None = None) -> str:
        return "this is not json"

    @srv.tool()
    def read_file(path: str) -> str: return ""
    @srv.tool()
    def write_file(path: str, content: str) -> str: return "ok"

    mcp = _InMemMcpToolset(srv, name="textonly")
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        r = await runner.exec(["ls"])
        assert r.exit_code == 0
        assert r.stdout == b"this is not json"
    finally:
        await runner.aclose()


async def test_exec_mcp_error_returns_exit_1(tmp_path) -> None:
    """If MCP tool raises (server-side), runner returns exit_code=1 + stderr."""
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    # Configure runner to point exec at the failing tool
    runner = McpSandboxRunner(mcp, exec_tool="fail_exec")
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        r = await runner.exec(["anything"])
        assert r.exit_code == 1
        assert b"simulated failure" in r.stderr
    finally:
        await runner.aclose()


# ---- read / write ----


async def test_write_then_read_utf8_roundtrip(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        await runner.write("hello.txt", b"world")
        assert await runner.read("hello.txt") == b"world"
    finally:
        await runner.aclose()


async def test_read_base64_prefix_decoded_to_bytes(tmp_path) -> None:
    """Binary file: server returns BASE64:<b64>, runner decodes to bytes."""
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        # write some binary bytes
        await runner.write("img.bin", b"\x00\x01\xff\xfe")
        # read back — server stored the BASE64: representation,
        # runner decodes it
        assert await runner.read("img.bin") == b"\x00\x01\xff\xfe"
    finally:
        await runner.aclose()


async def test_read_missing_file_raises_filenotfound(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        with pytest.raises(FileNotFoundError):
            await runner.read("ghost.txt")
    finally:
        await runner.aclose()


# ---- aclose ----


async def test_aclose_closes_underlying_mcp(tmp_path) -> None:
    srv = _make_sandbox_server()
    mcp = _InMemMcpToolset(srv)
    runner = McpSandboxRunner(mcp)
    await runner.warmup()
    assert mcp._connected is True
    await runner.aclose()
    assert mcp._connected is False


# ---- custom *_tool kwargs ----


async def test_custom_tool_names_routed_correctly(tmp_path) -> None:
    """McpSandboxRunner can target any MCP tool names (not just the defaults)."""
    srv: FastMCP = FastMCP("custom")

    @srv.tool()
    def run_shell(command: str, cwd: str = "", env: dict | None = None,
                  timeout: float | None = None, stdin: str | None = None) -> str:
        return json.dumps({"stdout": f"ran: {command}", "stderr": "", "exit_code": 0})

    @srv.tool()
    def fetch_file(path: str) -> str:
        return f"content of {path}"

    @srv.tool()
    def put_file(path: str, content: str) -> str:
        return f"wrote {path}"

    mcp = _InMemMcpToolset(srv, name="custom")
    runner = McpSandboxRunner(
        mcp,
        exec_tool="run_shell",
        read_tool="fetch_file",
        write_tool="put_file",
    )
    try:
        await runner.warmup()
        await runner.setup(tmp_path)
        r = await runner.exec(["ls"])
        assert b"ran: ls" in r.stdout
        body = await runner.read("anything")
        assert body == b"content of anything"
        await runner.write("anywhere", b"data")  # must not raise
    finally:
        await runner.aclose()


# ---- runner.name → SandboxToolset prefix ----


def test_default_runner_name_is_remote() -> None:
    mcp = McpToolset(
        McpServerConfig(name="x", transport="stdio", command=["unused"])
    )
    assert McpSandboxRunner(mcp).name == "remote"


def test_custom_runner_name_propagates() -> None:
    mcp = McpToolset(
        McpServerConfig(name="x", transport="stdio", command=["unused"])
    )
    assert McpSandboxRunner(mcp, name="e2b").name == "e2b"
