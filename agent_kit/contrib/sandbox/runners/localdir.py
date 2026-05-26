"""`LocalDirRunner` — runs commands as host subprocesses under a workspace dir.

**No OS-level isolation.** This runner is for trusted code paths:
- dev environments
- integration tests where you want a real subprocess
- coding agents you actually trust to run on your laptop

For untrusted input, use `SrtRunner` (local) or `McpSandboxRunner` (remote).

Key defaults:
- `command_allowlist=None` means "no allowlist; anything goes" — explicit
  opt-in for secure-by-config (`allowlist=["python", "pytest"]`)
- `env_passthrough=()` means "no host env vars forwarded" — `PATH` is auto-
  added so subprocesses can find binaries; everything else is explicit
- path traversal is blocked: `cwd`, `read(path)`, `write(path)` MUST resolve
  inside the workspace root
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ..types import ExecResult


class LocalDirRunner:
    """Implements `SandboxRunner` Protocol against host subprocesses."""

    def __init__(
        self,
        *,
        name: str = "localdir",
        command_allowlist: list[str] | None = None,
        env_passthrough: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._allowlist = set(command_allowlist) if command_allowlist is not None else None
        self._env_passthrough = tuple(env_passthrough)
        self._extra_env = dict(extra_env or {})
        self._workspace: Path | None = None

    async def setup(self, workspace: Path) -> None:
        # spec § 16.3 decision #3: all real runners mkdir workspace consistently.
        workspace.mkdir(parents=True, exist_ok=True)
        self._workspace = workspace.resolve()

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str = "",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        assert self._workspace is not None, "setup() must be called before exec()"
        if not cmd:
            return ExecResult(b"", b"empty command", 127)
        if self._allowlist is not None and cmd[0] not in self._allowlist:
            return ExecResult(
                stdout=b"",
                stderr=f"command {cmd[0]!r} not in allowlist".encode(),
                exit_code=126,
            )

        full_cwd = self._resolve(cwd) if cwd else self._workspace
        if not full_cwd.is_dir():
            return ExecResult(
                b"", f"cwd does not exist: {cwd!r}".encode(), 127
            )

        full_env = {k: os.environ[k] for k in self._env_passthrough if k in os.environ}
        full_env.update(self._extra_env)
        if env:
            full_env.update(env)
        # subprocess MUST be able to find binaries — auto-include PATH
        full_env.setdefault("PATH", os.environ.get("PATH", ""))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=full_cwd,
            env=full_env,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(
                stdout=b"",
                stderr=f"timeout after {timeout}s".encode(),
                exit_code=124,
            )
        return ExecResult(
            stdout=stdout, stderr=stderr, exit_code=proc.returncode or 0
        )

    async def read(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    async def write(self, path: str, content: bytes) -> None:
        full = self._resolve(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)

    async def aclose(self) -> None:
        # workspace lifecycle is the Runner(workspace=...) concern, not ours
        pass

    # ---- internal ----

    def _resolve(self, path: str) -> Path:
        """Resolve `path` relative to workspace; raise on traversal."""
        assert self._workspace is not None
        full = (self._workspace / path).resolve()
        try:
            full.relative_to(self._workspace)
        except ValueError:
            raise PermissionError(
                f"path escapes workspace: {path!r}"
            ) from None
        return full
