"""`SrtRunner` — runs commands via Anthropic sandbox-runtime (`srt` CLI).

Best for: local untrusted command execution on macOS / Linux dev boxes without
spinning a full Docker container. SRT provides filesystem ACL + network limits
via profile files (`srt --profile <toml>`).

Project: https://github.com/anthropic-experimental/sandbox-runtime

This runner is a **thin wrapper**: it builds `srt run --workspace <ws> ...`
and shells out. It does NOT reimplement profile parsing, image pulls, or
PTY — those are SRT's job.

SRT bind-mounts the workspace dir into the sandbox, so host-side `read()` /
`write()` talk directly to the same files — no need to round-trip through
the sandbox for I/O. Path traversal defense matches LocalDir.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..types import ExecResult


class SrtRunner:
    """Implements `SandboxRunner` Protocol by shelling out to `srt`."""

    def __init__(
        self,
        *,
        name: str = "srt",
        srt_binary: str = "srt",
        profile: str | Path | None = None,
        image: str = "default",
    ) -> None:
        self.name = name
        self._srt = srt_binary
        self._profile = str(profile) if profile else None
        self._image = image
        self._workspace: Path | None = None

    async def setup(self, workspace: Path) -> None:
        # spec § 16.3 decision #3: every real runner mkdirs the workspace.
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
        # `cwd` is interpreted by srt inside its sandbox view; absolute paths
        # or `..` parents would escape. Defend at the boundary.
        if cwd:
            if cwd.startswith("/") or ".." in Path(cwd).parts:
                return ExecResult(
                    b"", f"invalid cwd (must be relative, no '..'): {cwd!r}".encode(), 127
                )

        srt_cmd: list[str] = [
            self._srt, "run",
            "--workspace", str(self._workspace),
            "--image", self._image,
        ]
        if self._profile:
            srt_cmd.extend(["--profile", self._profile])
        if cwd:
            srt_cmd.extend(["--cwd", cwd])
        for k, v in (env or {}).items():
            srt_cmd.extend(["--env", f"{k}={v}"])
        if timeout:
            srt_cmd.extend(["--timeout", f"{int(timeout)}"])
        srt_cmd.append("--")
        srt_cmd.extend(cmd)

        return await self._invoke(srt_cmd, timeout=timeout, stdin=stdin)

    async def read(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    async def write(self, path: str, content: bytes) -> None:
        full = self._resolve(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)

    async def aclose(self) -> None:
        # Nothing to clean up — every exec is a fresh srt subprocess.
        pass

    # ---- internal ----

    async def _invoke(
        self,
        cmd: list[str],
        *,
        timeout: float | None,
        stdin: bytes | None,
    ) -> ExecResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return ExecResult(
                b"",
                f"srt binary not found: {self._srt!r} ({exc})".encode(),
                127,
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(b"", f"timeout after {timeout}s".encode(), 124)
        return ExecResult(stdout, stderr, proc.returncode or 0)

    def _resolve(self, path: str) -> Path:
        """Resolve `path` relative to workspace; raise on traversal."""
        assert self._workspace is not None
        full = (self._workspace / path).resolve()
        try:
            full.relative_to(self._workspace)
        except ValueError:
            raise PermissionError(f"path escapes workspace: {path!r}") from None
        return full
