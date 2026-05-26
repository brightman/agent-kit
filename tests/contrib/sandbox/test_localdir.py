"""LocalDirRunner — host-subprocess SandboxRunner (spec § 16.3, Stage C).

Uses real subprocesses + tmp_path. The freeze contract (Protocol shape, lazy
setup, error wrapping) is already covered by samples/coding-agent's freeze
tests — here we focus on what's unique to LocalDir: subprocess plumbing, path
traversal defense, allowlist, env handling, timeouts.
"""

from __future__ import annotations

import os
import sys

import pytest

from agent_kit.contrib.sandbox.runners.localdir import LocalDirRunner
from agent_kit.contrib.sandbox.types import ExecResult, SandboxRunner


# ---- Protocol shape ----


def test_localdir_satisfies_protocol() -> None:
    assert isinstance(LocalDirRunner(), SandboxRunner)


# ---- setup ----


async def test_setup_creates_workspace(tmp_path) -> None:
    ws = tmp_path / "ws_new"
    assert not ws.exists()
    runner = LocalDirRunner()
    await runner.setup(ws)
    assert ws.exists() and ws.is_dir()


async def test_setup_idempotent_on_existing_dir(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)   # already exists
    await runner.setup(tmp_path)   # second call must not raise


# ---- exec basics ----


async def test_exec_echo_returns_stdout_exit_0(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    result = await runner.exec(["echo", "hello"])
    assert result.exit_code == 0
    assert result.stdout == b"hello\n"
    assert result.stderr == b""
    assert result.ok() is True


async def test_exec_nonzero_exit(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    result = await runner.exec(["sh", "-c", "exit 7"])
    assert result.exit_code == 7
    assert result.ok() is False


async def test_exec_empty_cmd_returns_127(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    result = await runner.exec([])
    assert result.exit_code == 127
    assert b"empty command" in result.stderr


# ---- allowlist ----


async def test_allowlist_blocks_unlisted_command(tmp_path) -> None:
    runner = LocalDirRunner(command_allowlist=["echo"])
    await runner.setup(tmp_path)
    result = await runner.exec(["rm", "-rf", "/anywhere"])
    assert result.exit_code == 126
    assert b"allowlist" in result.stderr


async def test_allowlist_permits_listed_command(tmp_path) -> None:
    runner = LocalDirRunner(command_allowlist=["echo"])
    await runner.setup(tmp_path)
    result = await runner.exec(["echo", "ok"])
    assert result.exit_code == 0


async def test_allowlist_none_means_no_restriction(tmp_path) -> None:
    runner = LocalDirRunner()    # allowlist=None default
    await runner.setup(tmp_path)
    result = await runner.exec(["echo", "free"])
    assert result.exit_code == 0


# ---- cwd ----


async def test_exec_cwd_relative_to_workspace(tmp_path) -> None:
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "marker.txt").write_text("here")
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    result = await runner.exec(["cat", "marker.txt"], cwd="subdir")
    assert result.exit_code == 0
    assert result.stdout == b"here"


async def test_exec_cwd_traversal_blocked(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    with pytest.raises(PermissionError, match="escapes workspace"):
        await runner.exec(["ls"], cwd="../../etc")


async def test_exec_cwd_missing_returns_127(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    result = await runner.exec(["echo", "x"], cwd="ghost")
    assert result.exit_code == 127
    assert b"cwd does not exist" in result.stderr


# ---- env ----


async def test_env_passthrough_explicit_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MY_SECRET", "leaked")
    monkeypatch.setenv("MY_OK", "fine")
    runner = LocalDirRunner(env_passthrough=("MY_OK",))
    await runner.setup(tmp_path)
    result = await runner.exec(["sh", "-c", "echo MY_OK=$MY_OK MY_SECRET=$MY_SECRET"])
    assert b"MY_OK=fine" in result.stdout
    assert b"MY_SECRET=" in result.stdout      # var name present
    assert b"MY_SECRET=leaked" not in result.stdout  # but value blocked


async def test_env_kwarg_merges_on_top_of_extra(tmp_path) -> None:
    runner = LocalDirRunner(extra_env={"K": "from-extra"})
    await runner.setup(tmp_path)
    result = await runner.exec(
        ["sh", "-c", "echo K=$K Q=$Q"],
        env={"K": "overridden", "Q": "from-call"},
    )
    assert b"K=overridden" in result.stdout
    assert b"Q=from-call" in result.stdout


async def test_path_auto_added_so_binaries_resolve(tmp_path) -> None:
    """Without auto-PATH, `echo` (a builtin via sh) would fail to spawn directly."""
    runner = LocalDirRunner()  # no env_passthrough — PATH still auto-added
    await runner.setup(tmp_path)
    result = await runner.exec(["echo", "ok"])
    assert result.exit_code == 0


# ---- timeout ----


async def test_exec_timeout_returns_124(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    result = await runner.exec(["sh", "-c", "sleep 5"], timeout=0.2)
    assert result.exit_code == 124
    assert b"timeout" in result.stderr


# ---- stdin ----


async def test_exec_stdin_passed_through(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    result = await runner.exec(["cat"], stdin=b"piped content\n")
    assert result.exit_code == 0
    assert result.stdout == b"piped content\n"


# ---- read / write ----


async def test_write_then_read_roundtrip(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    await runner.write("hello.txt", b"world")
    assert await runner.read("hello.txt") == b"world"


async def test_write_creates_parent_dirs(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    await runner.write("nested/sub/file.bin", b"deep")
    assert (tmp_path / "nested" / "sub" / "file.bin").read_bytes() == b"deep"


async def test_read_path_traversal_blocked(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    with pytest.raises(PermissionError, match="escapes workspace"):
        await runner.read("../../etc/passwd")


async def test_write_path_traversal_blocked(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    with pytest.raises(PermissionError, match="escapes workspace"):
        await runner.write("../sneaky.txt", b"x")


async def test_read_missing_file_raises_filenotfound(tmp_path) -> None:
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    with pytest.raises(FileNotFoundError):
        await runner.read("ghost.txt")


# ---- aclose ----


async def test_aclose_does_not_delete_workspace(tmp_path) -> None:
    """workspace lifecycle is Runner.workspace_provider's concern."""
    runner = LocalDirRunner()
    await runner.setup(tmp_path)
    await runner.write("keep.txt", b"x")
    await runner.aclose()
    assert (tmp_path / "keep.txt").exists()
