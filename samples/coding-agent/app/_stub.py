"""`StubRunner` — sample-local in-memory `SandboxRunner` for offline tests.

NOT shipped with the SDK. Real backends live at
`agent_kit.contrib.sandbox.runners.{localdir,srt,mcp}`. StubRunner stays here
because it's a test convenience, not a production runner.

Workspace is a `dict[str, bytes]`; `exec()` dispatches to scripted handlers;
setup() mkdirs the host workspace path (matching what real runners do per
spec § 16.3 decision #3) but never reads from it.

The SandboxRunner Protocol — the slot StubRunner fills — does not change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_kit.contrib.sandbox.types import ExecResult

CommandHandler = Callable[[list[str], "StubRunner"], Awaitable[ExecResult]]


class StubRunner:
    """Dict-backed fake sandbox.

    Args:
        files: initial workspace contents (relpath → bytes)
        commands: name → async handler. Unknown commands return exit 127.
        name: optional override for tool prefix (default `stub`)
    """

    def __init__(
        self,
        *,
        files: dict[str, bytes] | None = None,
        commands: dict[str, CommandHandler] | None = None,
        name: str = "stub",
    ) -> None:
        self.name = name
        self.files: dict[str, bytes] = dict(files or {})
        self._commands = dict(commands or {})
        self.exec_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.workspace: Path | None = None

    async def setup(self, workspace: Path) -> None:
        # Mirror LocalDir/SRT/MCP runners: mkdir even though we don't use host fs
        workspace.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str = "",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        self.exec_calls.append(
            (list(cmd), {"cwd": cwd, "env": env, "timeout": timeout, "stdin": stdin})
        )
        if not cmd:
            return ExecResult(b"", b"empty command", 127)
        handler = self._commands.get(cmd[0])
        if handler is None:
            return ExecResult(b"", f"unknown stub command: {cmd[0]}".encode(), 127)
        return await handler(cmd, self)

    async def read(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write(self, path: str, content: bytes) -> None:
        self.files[path] = content

    async def aclose(self) -> None:
        pass


# ---- pre-built command handlers (sample-friendly) ----


async def _handle_echo(cmd: list[str], _runner: StubRunner) -> ExecResult:
    """`echo a b c` → stdout=`a b c\\n`."""
    return ExecResult(
        stdout=(" ".join(cmd[1:]) + "\n").encode(),
        stderr=b"", exit_code=0,
    )


async def _handle_ls(_cmd: list[str], runner: StubRunner) -> ExecResult:
    """`ls` lists keys of the in-memory file dict."""
    names = "\n".join(sorted(runner.files))
    out = (names + "\n") if names else ""
    return ExecResult(stdout=out.encode(), stderr=b"", exit_code=0)


async def _handle_cat(cmd: list[str], runner: StubRunner) -> ExecResult:
    """`cat path` returns file content."""
    if len(cmd) < 2:
        return ExecResult(b"", b"cat: missing operand", 1)
    path = cmd[1]
    if path not in runner.files:
        return ExecResult(
            b"", f"cat: {path}: No such file or directory".encode(), 1
        )
    return ExecResult(stdout=runner.files[path], stderr=b"", exit_code=0)


async def _handle_pytest_pass(
    _cmd: list[str], _runner: StubRunner
) -> ExecResult:
    """`pytest` always passes — for sample demos that need a green test."""
    return ExecResult(stdout=b"1 passed in 0.01s\n", stderr=b"", exit_code=0)


DEFAULT_COMMANDS: dict[str, CommandHandler] = {
    "echo": _handle_echo,
    "ls": _handle_ls,
    "cat": _handle_cat,
    "pytest": _handle_pytest_pass,
}
