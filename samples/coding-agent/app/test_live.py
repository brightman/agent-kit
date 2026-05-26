"""Stage F — end-to-end "fix-a-bug" demo with the real LocalDirRunner.

These tests prove the freeze API is more than theory: the same SandboxToolset
that the freeze tests pin against StubRunner also drives a real host
subprocess through LocalDirRunner. No LLM API key needed (a ScriptedProvider
plays the model), but every `exec_command` actually spawns Python, every
`read_file` / `write_file` touches the tmp_path filesystem, and the bug
actually gets fixed.

Run:
    PYTHONPATH=../.. python -m pytest app/test_live.py -v
"""

from __future__ import annotations

import json

import pytest

from agent_kit import Agent, ToolCall
from agent_kit.contrib.sandbox import SandboxToolset
from agent_kit.contrib.sandbox.runners import LocalDirRunner
from agent_kit.provider import LlmResponse


# ---- scripted bug-fix conversation ----


_BROKEN_SRC = b"def greet():\n    return 'wrold'\n\nprint(greet())\n"
_FIXED_SRC = b"def greet():\n    return 'world'\n\nprint(greet())\n"


class _BugFixProvider:
    """Scripts a 5-round LLM conversation that:
       r1 → exec_command(["ls"])                # discover files
       r2 → read_file("greet.py")                # inspect bug
       r3 → exec_command(["python", "greet.py"]) # confirm broken output
       r4 → write_file("greet.py", FIXED_SRC)   # fix the bug
       r5 → exec_command(["python", "greet.py"]) # confirm fix
       r6 → final answer
    """

    name = "bug-fix-script"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._round = 0

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append({"messages": list(messages), "round": self._round})
        self._round += 1
        if self._round == 1:
            return _tool_call("c1", "sandbox__localdir__exec_command", {"cmd": ["ls"]})
        if self._round == 2:
            return _tool_call("c2", "sandbox__localdir__read_file", {"path": "greet.py"})
        if self._round == 3:
            return _tool_call(
                "c3", "sandbox__localdir__exec_command",
                {"cmd": ["python3", "greet.py"]},
            )
        if self._round == 4:
            return _tool_call(
                "c4", "sandbox__localdir__write_file",
                {"path": "greet.py", "content": _FIXED_SRC.decode("utf-8")},
            )
        if self._round == 5:
            return _tool_call(
                "c5", "sandbox__localdir__exec_command",
                {"cmd": ["python3", "greet.py"]},
            )
        return LlmResponse(
            text="Fixed 'wrold' → 'world' in greet.py; verified output.",
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


# ---- the e2e test ----


def test_localdir_end_to_end_bug_fix(tmp_path) -> None:
    """Full chain: ScriptedProvider → AgentLoop → SandboxToolset →
    LocalDirRunner → real Python subprocess → real fs writes.

    Verifies that:
    - Pre-run: bug file on disk says "wrold"
    - During run: real `python` subprocess prints "wrold", then "world"
    - Post-run: same file on disk now says "world"
    """
    # Seed the workspace BEFORE the run.
    (tmp_path / "greet.py").write_bytes(_BROKEN_SRC)

    provider = _BugFixProvider()
    agent = Agent(
        name="bug-fixer",
        model=provider,
        tools=[SandboxToolset(LocalDirRunner(
            command_allowlist=["ls", "python", "python3"],
            env_passthrough=("PATH",),
        ))],
        workspace=lambda _req, _run_id: tmp_path,
        default_max_rounds=10,
    )

    result = agent.run_sync("Fix the typo in greet.py and verify.")

    # Run succeeded
    assert result.error is None
    assert result.cancelled is False
    assert "world" in (result.final_text or "")

    # All 6 LLM rounds happened (5 tool calls + 1 final)
    assert provider._round == 6

    # tool_result events tell us what actually happened end-to-end
    tool_results = {
        e.payload["call_id"]: e.payload["content"]
        for e in result.events if e.kind == "tool_result"
    }

    # c1: ls saw greet.py
    c1 = json.loads(tool_results["c1"])
    assert c1["exit_code"] == 0
    assert "greet.py" in c1["stdout"]

    # c2: read returned the broken source
    assert tool_results["c2"] == _BROKEN_SRC.decode("utf-8")

    # c3: python printed the typo (real subprocess output)
    c3 = json.loads(tool_results["c3"])
    assert c3["exit_code"] == 0
    assert "wrold" in c3["stdout"]

    # c4: write acked
    assert tool_results["c4"] == "ok"

    # c5: python on the fixed source prints "world"
    c5 = json.loads(tool_results["c5"])
    assert c5["exit_code"] == 0
    assert "world" in c5["stdout"]
    assert "wrold" not in c5["stdout"]

    # Post-condition: the actual file on disk is fixed
    assert (tmp_path / "greet.py").read_bytes() == _FIXED_SRC


# ---- guardrails: same SandboxToolset, real allowlist enforcement ----


def test_localdir_allowlist_blocks_disallowed_command(tmp_path) -> None:
    """Allowlist is enforced end-to-end through the toolset."""

    class _OneBadCmd:
        name = "bad"
        def __init__(self) -> None:
            self._n = 0
        async def chat(self, messages, tools=None, **k):
            self._n += 1
            if self._n == 1:
                return _tool_call(
                    "c1", "sandbox__localdir__exec_command",
                    {"cmd": ["rm", "-rf", "/anywhere"]},
                )
            return LlmResponse(text="blocked", tool_calls=[],
                                usage={}, raw={}, finish_reason="stop")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    agent = Agent(
        name="t", model=_OneBadCmd(),
        tools=[SandboxToolset(LocalDirRunner(command_allowlist=["ls"]))],
        workspace=lambda req, run_id: tmp_path,
    )
    result = agent.run_sync("do bad")
    assert result.error is None
    tr = [e for e in result.events if e.kind == "tool_result"][0]
    assert tr.payload["is_error"] is True
    payload = json.loads(tr.payload["content"])
    assert payload["exit_code"] == 126
    assert "allowlist" in payload["stderr"]


def test_localdir_path_traversal_blocked_e2e(tmp_path) -> None:
    """read_file with traversing path is wrapped as ToolResult(is_error=True),
    not raised, and the host file at the resolved path is NOT read."""

    class _Traversal:
        name = "t"
        def __init__(self) -> None:
            self._n = 0
        async def chat(self, messages, tools=None, **k):
            self._n += 1
            if self._n == 1:
                return _tool_call(
                    "c1", "sandbox__localdir__read_file",
                    {"path": "../../etc/passwd"},
                )
            return LlmResponse(text="blocked", tool_calls=[],
                                usage={}, raw={}, finish_reason="stop")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    agent = Agent(
        name="t", model=_Traversal(),
        tools=[SandboxToolset(LocalDirRunner())],
        workspace=lambda req, run_id: tmp_path,
    )
    result = agent.run_sync("read root")
    assert result.error is None
    tr = [e for e in result.events if e.kind == "tool_result"][0]
    assert tr.payload["is_error"] is True
    assert "escapes workspace" in tr.payload["content"]


# ---- prove build_agent(backend="localdir") wires correctly ----


def test_build_agent_localdir_backend_wires_real_runner(tmp_path) -> None:
    """`build_agent(backend="localdir")` returns an Agent whose toolset is
    SandboxToolset(LocalDirRunner). The agent is callable end-to-end with
    a ScriptedProvider; seed files materialize to disk via workspace_provider.
    """
    from .agent import build_agent

    seed = {
        "data.txt": b"hello from disk\n",
    }

    class _ReadAndDone:
        name = "rd"
        def __init__(self) -> None:
            self._n = 0
        async def chat(self, messages, tools=None, **k):
            self._n += 1
            if self._n == 1:
                return _tool_call(
                    "c1", "sandbox__localdir__read_file", {"path": "data.txt"},
                )
            return LlmResponse(
                text="read successfully", tool_calls=[],
                usage={}, raw={}, finish_reason="stop",
            )
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    agent = build_agent(
        model=_ReadAndDone(),
        backend="localdir",
        seed_files=seed,
        workspace_root=tmp_path,
    )
    result = agent.run_sync("read data.txt")
    assert result.error is None
    tr = [e for e in result.events if e.kind == "tool_result"][0]
    assert tr.payload["content"] == "hello from disk\n"
