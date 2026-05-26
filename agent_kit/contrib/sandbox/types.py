"""Sandbox API contract — frozen at Stage B (spec § 16.3).

5-method `SandboxRunner` Protocol + `ExecResult`. Stage C-E move this file
to `agent_kit/contrib/sandbox/types.py` **unchanged**. If they need to change
it, the freeze is broken and Stage B must be re-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecResult:
    """Output of a sandbox `exec()` call.

    `stdout` / `stderr` are full bytes — caller decides truncation. `truncated`
    flags that the runner itself dropped data (e.g. capped output buffer).
    """

    stdout: bytes
    stderr: bytes
    exit_code: int
    truncated: bool = False

    def ok(self) -> bool:
        return self.exit_code == 0


@runtime_checkable
class SandboxRunner(Protocol):
    """Backend contract — 5 async methods. Failures may raise; `SandboxToolset`
    wraps them into `ToolResult(is_error=True)`.

    `setup(workspace)` MUST mkdir the workspace (`workspace.mkdir(parents=True,
    exist_ok=True)`) so all three reference runners (LocalDir / SRT / MCP) have
    consistent behavior — see § 16.3 runner contract table.
    """

    name: str

    async def setup(self, workspace: Path) -> None: ...

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str = "",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult: ...

    async def read(self, path: str) -> bytes: ...
    async def write(self, path: str, content: bytes) -> None: ...
    async def aclose(self) -> None: ...
