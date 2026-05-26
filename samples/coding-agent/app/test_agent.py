"""Stage B freeze contract — these tests are the API freeze for SandboxRunner
+ SandboxToolset. If Stage C/D/E breaks them, the freeze is broken and we go
back to Stage B.

Run from this directory:
    PYTHONPATH=../.. python -m pytest app/test_agent.py -v

No API key needed — uses StubRunner + ScriptedProvider.
"""

from __future__ import annotations

import json

import pytest

from agent_kit import Agent, ToolCall
from agent_kit.provider import LlmResponse

from .agent import build_agent
from .sandbox import ExecResult, SandboxRunner, SandboxToolset
from .sandbox.runners.stub import DEFAULT_COMMANDS, StubRunner


# ---- Protocol shape (the freeze) ----


def test_stub_runner_satisfies_protocol() -> None:
    """StubRunner is a structural match for SandboxRunner."""
    assert isinstance(StubRunner(), SandboxRunner)


def test_exec_result_shape() -> None:
    """ExecResult fields locked: stdout/stderr/exit_code/truncated + ok()."""
    r = ExecResult(stdout=b"hi", stderr=b"", exit_code=0)
    assert r.ok() is True
    assert r.truncated is False  # default
    assert ExecResult(b"", b"x", 1).ok() is False


# ---- SandboxToolset advertised tools ----


def test_toolset_advertises_three_tools_by_default() -> None:
    ts = SandboxToolset(StubRunner())
    names = {s.name for s in ts.build_schemas()}
    assert names == {
        "sandbox__stub__exec_command",
        "sandbox__stub__read_file",
        "sandbox__stub__write_file",
    }


def test_toolset_tools_kwarg_filters_subset() -> None:
    ts = SandboxToolset(StubRunner(), tools=("read_file",))
    assert [s.name for s in ts.build_schemas()] == ["sandbox__stub__read_file"]


def test_toolset_unknown_tools_kwarg_raises() -> None:
    with pytest.raises(ValueError, match="unknown sandbox tool"):
        SandboxToolset(StubRunner(), tools=("read_file", "ghost"))  # type: ignore[arg-type]


def test_toolset_runner_name_propagates_to_tool_prefix() -> None:
    """`runner.name` controls the `sandbox__<name>__` prefix so multi-sandbox
    agents don't collide (e.g. localdir + remote)."""
    ts = SandboxToolset(StubRunner(name="remote"))
    assert ts.name == "sandbox__remote"
    assert all(s.name.startswith("sandbox__remote__") for s in ts.build_schemas())


# ---- Lazy setup ----


def test_setup_runs_lazily_on_first_execute(tmp_path) -> None:
    """SandboxToolset.connect() does NOT trigger setup; first execute() does."""
    runner = StubRunner(commands=DEFAULT_COMMANDS)
    ts = SandboxToolset(runner)
    provider = _one_then_done(
        "c1", "sandbox__stub__exec_command", {"cmd": ["echo", "hi"]},
    )
    agent = Agent(
        name="t", model=provider, tools=[ts],
        workspace_root=tmp_path / "ws",
    )
    assert runner.workspace is None        # before run
    agent.run_sync("go")
    assert runner.workspace is not None    # setup happened
    # workspace path is under workspace_root (ephemeral; Runner deletes it on exit)
    assert runner.workspace.parent == tmp_path / "ws"


# ---- End-to-end read → exec → write → final ----


class _ScriptedCodingProvider:
    """4-round conversation:
       r1 → read_file(task.md)
       r2 → exec_command(["ls"])
       r3 → write_file(notes.md, "...")
       r4 → final answer
    """

    name = "scripted"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._round = 0

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append({
            "messages": list(messages),
            "tools": sorted(t.name for t in (tools or [])),
        })
        self._round += 1
        if self._round == 1:
            return _tool_call("c1", "sandbox__stub__read_file", {"path": "task.md"})
        if self._round == 2:
            return _tool_call("c2", "sandbox__stub__exec_command", {"cmd": ["ls"]})
        if self._round == 3:
            return _tool_call(
                "c3", "sandbox__stub__write_file",
                {"path": "notes.md", "content": "I read task.md and listed files."},
            )
        return LlmResponse(
            text="Done. See notes.md.",
            tool_calls=[], usage={}, raw={}, finish_reason="stop",
        )

    async def chat_stream(self, *a, **k):
        raise NotImplementedError


def _tool_call(call_id: str, name: str, args: dict) -> LlmResponse:
    return LlmResponse(
        text="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        usage={}, raw={}, finish_reason="tool_calls",
    )


def _one_then_done(call_id: str, name: str, args: dict, *, final: str = "done"):
    """Build a provider that returns one tool_call then final text."""

    class _P:
        name = "p"
        def __init__(self) -> None:
            self._n = 0
        async def chat(self, messages, tools=None, **k):
            self._n += 1
            if self._n == 1:
                return _tool_call(call_id, name, args)
            return LlmResponse(text=final, tool_calls=[], usage={}, raw={}, finish_reason="stop")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    return _P()


def test_end_to_end_read_exec_write_final() -> None:
    provider = _ScriptedCodingProvider()
    runner = StubRunner(
        files={"task.md": b"Find the bug and fix it."},
        commands=DEFAULT_COMMANDS,
    )
    agent = Agent(
        name="t", model=provider,
        tools=[SandboxToolset(runner)],
        default_max_rounds=8,
    )
    result = agent.run_sync("Read task.md and do as told.")

    assert result.error is None
    assert result.final_text == "Done. See notes.md."

    results_by_call = {
        e.payload["call_id"]: e.payload["content"]
        for e in result.events if e.kind == "tool_result"
    }

    # L1: read_file returned task.md content
    assert results_by_call["c1"] == "Find the bug and fix it."

    # L2: exec_command(["ls"]) returned JSON with stdout listing files
    r2 = json.loads(results_by_call["c2"])
    assert r2["exit_code"] == 0
    assert "task.md" in r2["stdout"]

    # L3: write_file acked
    assert results_by_call["c3"] == "ok"

    # the in-memory workspace now has the new file
    assert runner.files["notes.md"] == b"I read task.md and listed files."


def test_three_tools_advertised_in_provider_calls() -> None:
    """Every chat() call sees the same 3 tools."""
    provider = _ScriptedCodingProvider()
    agent = Agent(
        name="t", model=provider,
        tools=[SandboxToolset(StubRunner(commands=DEFAULT_COMMANDS))],
        default_max_rounds=8,
    )
    agent.run_sync("anything")
    for call in provider.calls[:3]:  # last round masks tools
        assert call["tools"] == [
            "sandbox__stub__exec_command",
            "sandbox__stub__read_file",
            "sandbox__stub__write_file",
        ]


# ---- exec args plumbing ----


def test_exec_command_passes_cwd_env_timeout_stdin_to_runner() -> None:
    """All optional `exec_command` args reach the runner."""
    captured: dict = {}

    class _Capture:
        name = "cap"
        async def setup(self, ws): pass
        async def exec(self, cmd, *, cwd="", env=None, timeout=None, stdin=None):
            captured.update(
                cmd=cmd, cwd=cwd, env=env, timeout=timeout, stdin=stdin,
            )
            return ExecResult(b"ok", b"", 0)
        async def read(self, p): raise NotImplementedError
        async def write(self, p, c): raise NotImplementedError
        async def aclose(self): pass

    provider = _one_then_done(
        "c1", "sandbox__cap__exec_command",
        {
            "cmd": ["python", "-V"],
            "cwd": "src",
            "env": {"FOO": "bar"},
            "timeout": 5,
            "stdin": "hello",
        },
    )
    agent = Agent(name="t", model=provider, tools=[SandboxToolset(_Capture())])
    agent.run_sync("noop")
    assert captured == {
        "cmd": ["python", "-V"],
        "cwd": "src",
        "env": {"FOO": "bar"},
        "timeout": 5,
        "stdin": b"hello",
    }


# ---- Error paths (toolset wraps, never raises) ----


def test_unknown_command_returns_127_is_error() -> None:
    provider = _one_then_done(
        "c1", "sandbox__stub__exec_command", {"cmd": ["doesnotexist"]},
        final="saw the error",
    )
    agent = Agent(
        name="t", model=provider,
        tools=[SandboxToolset(StubRunner(commands=DEFAULT_COMMANDS))],
    )
    result = agent.run_sync("bad")
    tool_results = [e for e in result.events if e.kind == "tool_result"]
    assert tool_results[0].payload["is_error"] is True
    payload = json.loads(tool_results[0].payload["content"])
    assert payload["exit_code"] == 127


def test_read_nonexistent_file_returns_is_error_not_raise() -> None:
    """Runner raises FileNotFoundError → toolset → ToolResult(is_error=True)."""
    provider = _one_then_done(
        "c1", "sandbox__stub__read_file", {"path": "ghost"},
        final="couldn't read",
    )
    agent = Agent(
        name="t", model=provider,
        tools=[SandboxToolset(StubRunner())],
    )
    result = agent.run_sync("ghost")
    tr = [e for e in result.events if e.kind == "tool_result"][0]
    assert tr.payload["is_error"] is True
    assert "FileNotFoundError" in tr.payload["content"]


def test_binary_file_read_returns_base64_prefix() -> None:
    """Non-utf8 bytes from `read_file` come back as `BASE64:<b64>`."""
    runner = StubRunner(files={"img.bin": b"\x00\x01\xff\xfe"})
    provider = _one_then_done(
        "c1", "sandbox__stub__read_file", {"path": "img.bin"},
        final="binary handled",
    )
    agent = Agent(
        name="t", model=provider,
        tools=[SandboxToolset(runner)],
    )
    result = agent.run_sync("read")
    tr = [e for e in result.events if e.kind == "tool_result"][0]
    assert tr.payload["content"].startswith("BASE64:")


# ---- aclose lifecycle ----


def test_runner_aclose_called_after_run(tmp_path) -> None:
    closed = {"n": 0}

    class _Trackable:
        name = "trk"
        async def setup(self, ws): pass
        async def exec(self, *a, **k): raise NotImplementedError
        async def read(self, p): raise NotImplementedError
        async def write(self, p, c): raise NotImplementedError
        async def aclose(self):
            closed["n"] += 1

    class _Final:
        name = "x"
        async def chat(self, messages, tools=None, **k):
            return LlmResponse(text="done", tool_calls=[], usage={}, raw={}, finish_reason="stop")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    agent = Agent(
        name="t", model=_Final(),
        tools=[SandboxToolset(_Trackable())],
        workspace_root=tmp_path / "ws",
    )
    agent.run_sync("noop")
    assert closed["n"] == 1
