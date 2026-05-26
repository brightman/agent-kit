"""tests/test_agent.py — Agent convenience layer(spec § 17)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_kit import Agent, Message, RunRequest, RunResult, ToolCall
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.runner import Runner
from agent_kit.toolset import BaseToolset, ToolCallContext
from agent_kit.types import ToolResult


# ---- helpers ----


class _StubProvider:
    """Scriptable LlmProvider. Each `chat()` returns the next queued response."""

    name = "stub"

    def __init__(self, responses: list[LlmResponse] | None = None) -> None:
        self._responses = list(responses or [
            LlmResponse(text="default reply", tool_calls=[],
                        usage={}, raw={}, finish_reason="stop"),
        ])
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append({
            "messages": list(messages),
            "tools": list(tools) if tools else None,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if len(self._responses) == 1:
            return self._responses[0]   # 重复同一 response
        return self._responses.pop(0)

    async def chat_stream(self, *a, **k):
        raise NotImplementedError


class _NoopToolset(BaseToolset):
    """Single tool; never called by tests but counts as a registered toolset."""

    name = "noop"

    def build_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name="noop_tool",
                description="does nothing",
                parameters={"type": "object", "properties": {}},
            )
        ]

    async def execute(self, call, ctx):
        return ToolResult(call_id=call.id, content="ok")


# ---- construction ----


def test_agent_basic_construction() -> None:
    a = Agent(name="x", model=_StubProvider())
    assert a.name == "x"
    assert isinstance(a.runner, Runner)


def test_agent_holds_long_lived_runner() -> None:
    """Same Runner across multiple .run() calls — toolset state preserved."""
    a = Agent(name="x", model=_StubProvider())
    r1 = a.runner
    a.run_sync("first")
    a.run_sync("second")
    assert a.runner is r1   # same instance


def test_agent_passes_instruction_as_system_prelude() -> None:
    provider = _StubProvider()
    a = Agent(name="x", model=provider, instruction="be brief")
    a.run_sync("hi")
    # First call's first message is the system prompt = instruction
    msgs = provider.calls[0]["messages"]
    assert msgs[0].role == "system"
    assert "be brief" in msgs[0].content


def test_agent_passes_tools_to_provider() -> None:
    provider = _StubProvider()
    ts = _NoopToolset()
    a = Agent(name="x", model=provider, tools=[ts])
    a.run_sync("hi")
    tool_names = {t.name for t in (provider.calls[0]["tools"] or [])}
    assert "noop_tool" in tool_names


# ---- run / run_sync behavior ----


def test_run_sync_returns_run_result() -> None:
    a = Agent(name="x", model=_StubProvider([
        LlmResponse(text="answer", tool_calls=[],
                    usage={}, raw={}, finish_reason="stop"),
    ]))
    result = a.run_sync("question")
    assert isinstance(result, RunResult)
    assert result.final_text == "answer"


@pytest.mark.asyncio
async def test_run_async_returns_run_result() -> None:
    a = Agent(name="x", model=_StubProvider([
        LlmResponse(text="async-answer", tool_calls=[],
                    usage={}, raw={}, finish_reason="stop"),
    ]))
    result = await a.run("question")
    assert result.final_text == "async-answer"


@pytest.mark.asyncio
async def test_run_sync_rejected_in_running_loop() -> None:
    """Inherits Runner.run_sync's loop detection."""
    a = Agent(name="x", model=_StubProvider())
    with pytest.raises(RuntimeError, match="cannot be called from a running event loop"):
        a.run_sync("hi")


# ---- per-run overrides ----


def test_run_uses_default_max_rounds_temperature_max_tokens() -> None:
    provider = _StubProvider()
    a = Agent(
        name="x", model=provider,
        default_max_rounds=7,
        default_temperature=0.3,
        default_max_tokens=512,
    )
    a.run_sync("hi")
    assert provider.calls[0]["temperature"] == 0.3
    assert provider.calls[0]["max_tokens"] == 512
    # max_rounds isn't passed to provider.chat; verified via behavior elsewhere


def test_run_overrides_per_call() -> None:
    provider = _StubProvider()
    a = Agent(name="x", model=provider, default_temperature=0.7, default_max_tokens=1000)
    a.run_sync("hi", temperature=0.0, max_tokens=42)
    assert provider.calls[0]["temperature"] == 0.0
    assert provider.calls[0]["max_tokens"] == 42


def test_agent_id_in_request_matches_agent_name() -> None:
    provider = _StubProvider()
    seen: dict[str, Any] = {}

    from agent_kit import Hook

    class _Cap(Hook):
        async def before_model(self, ctx, messages, tools):
            # ToolCallContext doesn't carry agent_id directly; we check via
            # workspace path which includes the run_id (not agent_id). Better
            # check is via the request the Runner saw — easiest:
            # we can capture by reading ctx.run_id and trust Agent.run wires
            # agent_id via _build_request. Smoke check below verifies.
            return None

    a = Agent(name="my-agent", model=provider, hooks=[_Cap()])
    result = a.run_sync("hi")
    # agent_id propagates into events through metadata; we don't have that
    # surface today, so this is a weak smoke test — the real assertion is
    # in test_run_request_builder below.
    assert result.error is None


def test_run_request_builder_carries_all_fields() -> None:
    """Verify _build_request packs agent_id + every override."""
    a = Agent(name="my-agent", model=_StubProvider(),
              default_max_rounds=8, default_temperature=0.5, default_max_tokens=100)
    req = a._build_request(
        "the question",
        enabled_skills=["pptx"],
        max_rounds=None,        # use default
        temperature=None,
        max_tokens=None,
        prior_messages=[Message(role="user", content="prev")],
        cancel_check=None,
        metadata={"k": "v"},
    )
    assert req.agent_id == "my-agent"
    assert req.user_message == "the question"
    assert req.enabled_skills == ["pptx"]
    assert req.max_rounds == 8           # default
    assert req.temperature == 0.5        # default
    assert req.max_tokens == 100         # default
    assert len(req.prior_messages) == 1
    assert req.metadata == {"k": "v"}


def test_prior_messages_passed_to_loop() -> None:
    provider = _StubProvider()
    a = Agent(name="x", model=provider)
    a.run_sync(
        "follow-up",
        prior_messages=[
            Message(role="user", content="earlier"),
            Message(role="assistant", content="prior reply"),
        ],
    )
    # Loop composes: system_prelude + prior_messages + user_message
    contents = [m.content for m in provider.calls[0]["messages"]]
    assert "earlier" in contents
    assert "prior reply" in contents
    assert "follow-up" in contents


# ---- string model resolution ----


def test_agent_string_model_raises_clear_error_without_litellm(monkeypatch) -> None:
    """Without `agent-kit[litellm]` installed → friendly ImportError.

    Even if litellm IS installed in the test venv, we simulate it being absent
    by:
    1) evicting the cached module from sys.modules so a fresh import is needed
    2) setting sys.modules[...] = None which makes Python raise ImportError on
       any subsequent `import` of that name
    """
    import sys
    target = "agent_kit.contrib.providers.litellm"
    monkeypatch.delitem(sys.modules, target, raising=False)
    monkeypatch.setitem(sys.modules, target, None)

    with pytest.raises(ImportError, match=r"agent-kit\[litellm\]"):
        Agent(name="x", model="gemini/gemini-flash-latest")


def test_agent_string_model_constructs_litellm_if_available() -> None:
    """If litellm is installed, string model → LiteLlm instance.

    We don't make a real call (no API key, no network) — just verify the
    type swap happened.
    """
    try:
        import litellm  # noqa: F401
    except ImportError:
        pytest.skip("litellm not installed; this path covered by string-model error test")
    from agent_kit.contrib.providers.litellm import LiteLlm

    a = Agent(name="x", model="gemini/gemini-flash-latest")
    assert isinstance(a.model, LiteLlm)
    assert a.model.model == "gemini/gemini-flash-latest"


# ---- runner property + escape hatch ----


def test_runner_property_exposes_full_api() -> None:
    """Advanced users can drop down to Runner for `run_to_completion(RunRequest(...))`."""
    a = Agent(name="x", model=_StubProvider())
    result = a.runner.run_sync(
        RunRequest(agent_id="x", user_message="hi"),
    )
    assert result.final_text == "default reply"


# ---- workspace_provider passthrough ----


def test_workspace_provider_passthrough(tmp_path) -> None:
    seen: dict[str, Any] = {}
    external = tmp_path / "shared"
    external.mkdir()

    from agent_kit import Hook

    class _Cap(Hook):
        async def before_model(self, ctx, messages, tools):
            seen["workspace"] = ctx.workspace
            seen["ephemeral"] = ctx.workspace_ephemeral
            return None

    a = Agent(
        name="x", model=_StubProvider(),
        workspace_provider=lambda req, run_id: external,
        hooks=[_Cap()],
    )
    a.run_sync("hi")
    assert seen["workspace"] == external
    assert seen["ephemeral"] is False
    # Provider-injected workspace must survive after the run
    assert external.exists()


# ---- cancel ----


def test_cancel_check_passed_through() -> None:
    """Caller-provided cancel_check is honored."""
    provider = _StubProvider([
        LlmResponse(text="round1", tool_calls=[
            ToolCall(id="c1", name="noop_tool", arguments={}),
        ], usage={}, raw={}, finish_reason="tool_calls"),
        LlmResponse(text="never reached", tool_calls=[],
                    usage={}, raw={}, finish_reason="stop"),
    ])
    a = Agent(name="x", model=provider, tools=[_NoopToolset()])
    # cancel after the first LLM round
    state = {"checks": 0}

    def cancel_check() -> bool:
        state["checks"] += 1
        return state["checks"] > 1   # first check returns False, then True

    result = a.run_sync("hi", cancel_check=cancel_check, max_rounds=5)
    assert result.cancelled is True
