"""Offline smoke tests — verify agent wires up and the event rendering helpers
work without needing real LLM / MCP / API keys.

Run from this dir:
    PYTHONPATH=../.. python -m pytest app/test_app.py -v
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent_kit import McpToolset
from agent_kit.contrib.sandbox.types import ExecResult  # noqa: F401  (sanity import)

from .agent import build_agent
from .tui import _render_event, _summary


# ---- agent wiring ----


class _StubProvider:
    name = "stub"
    async def chat(self, messages, tools=None, **kw):
        from agent_kit.provider import LlmResponse
        return LlmResponse(text="hi", tool_calls=[], usage={}, raw={}, finish_reason="stop")
    async def chat_stream(self, *a, **k): raise NotImplementedError


def test_agent_builds_with_skill_and_mcp(monkeypatch) -> None:
    """build_agent() returns a working Agent with skill-creator + websearch
    MCP wired in. We use a stub provider so no key is needed.
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-real")
    agent = build_agent(model=_StubProvider())
    # tools list should include the McpToolset (websearch) AND the
    # SkillCatalogToolset (auto-added by Agent from skills=...)
    tool_names = {t.name for t in agent.runner._toolsets}
    assert "mcp__websearch" in tool_names
    assert "skill_catalog" in tool_names


def test_skill_creator_is_discoverable(monkeypatch) -> None:
    """The skill-creator skill loads from disk via FilesystemSkillRegistry."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-real")
    import asyncio
    agent = build_agent(model=_StubProvider())
    skills = asyncio.run(agent._skills_registry.list())
    names = {fm.name for fm in skills}
    assert "skill-creator" in names


def test_websearch_mcp_substitutes_auth_header(monkeypatch) -> None:
    """The ${DASHSCOPE_API_KEY} placeholder is filled from env at construct time."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-abc123")
    agent = build_agent(model=_StubProvider())
    mcp = next(t for t in agent.runner._toolsets if isinstance(t, McpToolset))
    assert mcp._config.headers["Authorization"] == "Bearer sk-abc123"


def test_websearch_mcp_missing_secret_raises() -> None:
    """Without DASHSCOPE_API_KEY, ${VAR} substitution fails at construct."""
    import os
    saved = os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        with pytest.raises(KeyError, match="DASHSCOPE_API_KEY"):
            build_agent(model=_StubProvider())
    finally:
        if saved is not None:
            os.environ["DASHSCOPE_API_KEY"] = saved


# ---- event-render helpers ----


@dataclass
class _FakeEvent:
    kind: str
    payload: dict[str, Any]


def test_summary_round_start() -> None:
    assert _summary("round_start", {"round": 0}) == "#0"


def test_summary_llm_response_with_tool_calls() -> None:
    s = _summary("llm_response", {
        "round": 1,
        "tool_calls": [{"name": "x"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40},
        "text": "calling tool",
    })
    assert "1 tool_call(s)" in s
    assert "in=120" in s
    assert "out=40" in s
    assert 'text="calling tool"' in s


def test_summary_tool_call_args_truncated() -> None:
    s = _summary("tool_call", {
        "name": "mcp__websearch__search",
        "arguments": {"query": "x" * 200, "limit": 5},
    })
    assert s.startswith("mcp__websearch__search(")
    assert "query=" in s
    assert "..." in s  # long arg truncated


def test_summary_tool_result_error_marker() -> None:
    s = _summary("tool_result", {
        "call_id": "abcdefghij",
        "content": "boom",
        "is_error": True,
    })
    assert s.startswith("ERR ")
    assert "boom" in s


def test_summary_error_event() -> None:
    s = _summary("error", {
        "stage": "provider",
        "exc_type": "ValueError",
        "message": "rate limited",
    })
    assert "[provider]" in s
    assert "ValueError" in s
    assert "rate limited" in s


def test_render_event_writes_one_line_per_event() -> None:
    """_render_event calls log.write() exactly once per event."""
    written: list[str] = []

    class _FakeLog:
        def write(self, s): written.append(s)

    log = _FakeLog()
    _render_event(log, _FakeEvent("round_start", {"round": 0}))
    _render_event(log, _FakeEvent("tool_call", {
        "name": "mcp__websearch__search", "arguments": {"q": "py"},
    }))
    assert len(written) == 2
    assert "round_start" in written[0]
    assert "tool_call" in written[1]
    assert "mcp__websearch__search" in written[1]
