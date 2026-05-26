"""SrtRunner — thin wrapper over Anthropic sandbox-runtime CLI (Stage D).

We do NOT install `srt` to run these tests. Instead, we monkeypatch
`asyncio.create_subprocess_exec` to inspect what CLI args the runner builds.
Real-subprocess paths shared with LocalDir (stdin/timeout/no-PATH) are
exercised in test_localdir.py — here we focus on what's unique to SRT:
- correct `srt run --workspace ... --image ... [--profile ...] [--cwd ...]
  [--env K=V ...] [--timeout N] -- <cmd>` shape
- `srt` binary missing → friendly exit 127
- cwd traversal blocked at the runner boundary
- read/write/path-traversal still work (bind-mount semantics)
"""

from __future__ import annotations

import asyncio

import pytest

from agent_kit.contrib.sandbox.runners.srt import SrtRunner
from agent_kit.contrib.sandbox.types import ExecResult, SandboxRunner


# ---- subprocess fake ----


class _FakeProc:
    """Mimics enough of asyncio.subprocess.Process for our usage."""

    def __init__(
        self, *, stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = exit_code

    async def communicate(self, _stdin: bytes | None = None):
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def _patch_subprocess(monkeypatch, *, stdout=b"", stderr=b"", exit_code=0):
    """Replace `asyncio.create_subprocess_exec` so it records argv + returns
    a fake process. Returns a list that captures every call's argv."""
    captured: list[tuple[str, ...]] = []

    async def fake_exec(*args, **_kwargs):
        captured.append(tuple(args))
        return _FakeProc(stdout=stdout, stderr=stderr, exit_code=exit_code)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


# ---- Protocol shape ----


def test_srt_satisfies_protocol() -> None:
    assert isinstance(SrtRunner(), SandboxRunner)


# ---- setup ----


async def test_setup_creates_workspace(tmp_path) -> None:
    ws = tmp_path / "ws_srt"
    assert not ws.exists()
    runner = SrtRunner()
    await runner.setup(ws)
    assert ws.exists() and ws.is_dir()


# ---- exec CLI shape ----


async def test_exec_builds_minimal_srt_cli(tmp_path, monkeypatch) -> None:
    """Default: `srt run --workspace <ws> --image default -- python -V`."""
    captured = _patch_subprocess(monkeypatch, stdout=b"Python 3.11.0\n")
    runner = SrtRunner()
    await runner.setup(tmp_path)
    result = await runner.exec(["python", "-V"])
    assert result.exit_code == 0
    assert result.stdout == b"Python 3.11.0\n"
    argv = list(captured[0])
    assert argv == [
        "srt", "run",
        "--workspace", str(tmp_path.resolve()),
        "--image", "default",
        "--", "python", "-V",
    ]


async def test_exec_includes_profile_when_set(tmp_path, monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    runner = SrtRunner(profile="/etc/srt/python-readonly.toml")
    await runner.setup(tmp_path)
    await runner.exec(["ls"])
    argv = list(captured[0])
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == "/etc/srt/python-readonly.toml"


async def test_exec_omits_profile_when_none(tmp_path, monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    runner = SrtRunner()  # profile=None default
    await runner.setup(tmp_path)
    await runner.exec(["ls"])
    assert "--profile" not in captured[0]


async def test_exec_passes_cwd(tmp_path, monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    runner = SrtRunner()
    await runner.setup(tmp_path)
    await runner.exec(["ls"], cwd="src/sub")
    argv = list(captured[0])
    assert argv[argv.index("--cwd") + 1] == "src/sub"


async def test_exec_passes_env_vars(tmp_path, monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    runner = SrtRunner()
    await runner.setup(tmp_path)
    await runner.exec(["printenv"], env={"FOO": "bar", "BAZ": "qux"})
    argv = list(captured[0])
    # SRT takes env as `--env K=V` pairs (repeated)
    env_pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert set(env_pairs) == {"FOO=bar", "BAZ=qux"}


async def test_exec_passes_timeout(tmp_path, monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    runner = SrtRunner()
    await runner.setup(tmp_path)
    await runner.exec(["sleep", "10"], timeout=30)
    argv = list(captured[0])
    assert argv[argv.index("--timeout") + 1] == "30"


async def test_exec_custom_image_and_binary(tmp_path, monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    runner = SrtRunner(srt_binary="/opt/srt/bin/srt", image="python3.12-slim")
    await runner.setup(tmp_path)
    await runner.exec(["python", "-V"])
    argv = list(captured[0])
    assert argv[0] == "/opt/srt/bin/srt"
    assert argv[argv.index("--image") + 1] == "python3.12-slim"


# ---- exec error paths ----


async def test_exec_empty_cmd_returns_127(tmp_path) -> None:
    runner = SrtRunner()
    await runner.setup(tmp_path)
    r = await runner.exec([])
    assert r.exit_code == 127
    assert b"empty command" in r.stderr


async def test_exec_absolute_cwd_blocked(tmp_path) -> None:
    """cwd is interpreted inside the sandbox; absolute path would escape."""
    runner = SrtRunner()
    await runner.setup(tmp_path)
    r = await runner.exec(["ls"], cwd="/etc")
    assert r.exit_code == 127
    assert b"invalid cwd" in r.stderr


async def test_exec_traversal_cwd_blocked(tmp_path) -> None:
    runner = SrtRunner()
    await runner.setup(tmp_path)
    r = await runner.exec(["ls"], cwd="../../etc")
    assert r.exit_code == 127
    assert b"invalid cwd" in r.stderr


async def test_srt_binary_missing_returns_127(tmp_path, monkeypatch) -> None:
    """If srt binary doesn't exist, FileNotFoundError → friendly exit 127."""
    async def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "srt")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    runner = SrtRunner(srt_binary="srt-doesnt-exist")
    await runner.setup(tmp_path)
    r = await runner.exec(["ls"])
    assert r.exit_code == 127
    assert b"srt binary not found" in r.stderr


async def test_exec_timeout_returns_124(tmp_path, monkeypatch) -> None:
    """asyncio.wait_for raising TimeoutError → ExecResult(exit_code=124)."""
    class _HangingProc(_FakeProc):
        async def communicate(self, *a, **k):
            await asyncio.sleep(10)
            return b"", b""

    async def fake_exec(*a, **k):
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = SrtRunner()
    await runner.setup(tmp_path)
    r = await runner.exec(["sleep", "5"], timeout=0.05)
    assert r.exit_code == 124
    assert b"timeout" in r.stderr


async def test_exec_nonzero_exit_propagates(tmp_path, monkeypatch) -> None:
    _patch_subprocess(monkeypatch, stdout=b"", stderr=b"oops", exit_code=2)
    runner = SrtRunner()
    await runner.setup(tmp_path)
    r = await runner.exec(["false"])
    assert r.exit_code == 2
    assert r.stderr == b"oops"
    assert r.ok() is False


# ---- exec stdin ----


async def test_exec_stdin_passed_to_subprocess(tmp_path, monkeypatch) -> None:
    received: dict = {}

    class _StdinProc(_FakeProc):
        async def communicate(self, stdin=None):
            received["stdin"] = stdin
            return b"got it", b""

    async def fake_exec(*a, stdin=None, **k):
        received["stdin_pipe_requested"] = stdin is asyncio.subprocess.PIPE
        return _StdinProc(stdout=b"got it")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = SrtRunner()
    await runner.setup(tmp_path)
    r = await runner.exec(["cat"], stdin=b"hello srt")
    assert r.exit_code == 0
    assert received["stdin"] == b"hello srt"
    assert received["stdin_pipe_requested"] is True


# ---- read / write (bind-mount semantics: host fs == sandbox fs) ----


async def test_write_then_read_roundtrip(tmp_path) -> None:
    runner = SrtRunner()
    await runner.setup(tmp_path)
    await runner.write("hello.txt", b"world")
    assert await runner.read("hello.txt") == b"world"
    # Verify host fs sees the same file (bind-mount semantics)
    assert (tmp_path / "hello.txt").read_bytes() == b"world"


async def test_read_path_traversal_blocked(tmp_path) -> None:
    runner = SrtRunner()
    await runner.setup(tmp_path)
    with pytest.raises(PermissionError, match="escapes workspace"):
        await runner.read("../../etc/passwd")


async def test_write_path_traversal_blocked(tmp_path) -> None:
    runner = SrtRunner()
    await runner.setup(tmp_path)
    with pytest.raises(PermissionError, match="escapes workspace"):
        await runner.write("../sneaky.txt", b"x")


async def test_read_missing_file_raises_filenotfound(tmp_path) -> None:
    runner = SrtRunner()
    await runner.setup(tmp_path)
    with pytest.raises(FileNotFoundError):
        await runner.read("ghost.txt")


# ---- aclose ----


async def test_aclose_is_noop(tmp_path) -> None:
    runner = SrtRunner()
    await runner.setup(tmp_path)
    await runner.write("keep.txt", b"x")
    await runner.aclose()
    assert (tmp_path / "keep.txt").exists()


# ---- runner.name → SandboxToolset prefix integration ----


def test_default_runner_name_is_srt() -> None:
    assert SrtRunner().name == "srt"


def test_custom_runner_name_propagates() -> None:
    """Multi-sandbox setups can give each runner a unique name."""
    assert SrtRunner(name="srt-untrusted").name == "srt-untrusted"
