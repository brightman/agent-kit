"""tests/test_agent.py — Agent convenience layer(spec § 17)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_kit import Agent, Hook, Message, RunRequest, RunResult, ToolCall
from agent_kit.runner import Runner

from tests._helpers import (
    RecordingToolset,
    ScriptedProvider,
    text_response,
    tool_call_response,
)


def _noop_toolset() -> RecordingToolset:
    """Single tool; never actually invoked by most tests."""
    return RecordingToolset(name="noop", handlers={"noop_tool": "ok"})


# ---- construction ----


def test_agent_basic_construction() -> None:
    a = Agent(name="x", model=ScriptedProvider())
    assert a.name == "x"
    assert isinstance(a.runner, Runner)


def test_agent_holds_long_lived_runner() -> None:
    """Same Runner across multiple .run() calls — toolset state preserved."""
    a = Agent(name="x", model=ScriptedProvider())
    r1 = a.runner
    a.run_sync("first")
    a.run_sync("second")
    assert a.runner is r1   # same instance


def test_agent_passes_instruction_as_system_prelude() -> None:
    provider = ScriptedProvider()
    a = Agent(name="x", model=provider, instruction="be brief")
    a.run_sync("hi")
    # First call's first message is the system prompt = instruction
    msgs = provider.calls[0]["messages"]
    assert msgs[0].role == "system"
    assert "be brief" in msgs[0].content


def test_agent_passes_tools_to_provider() -> None:
    provider = ScriptedProvider()
    a = Agent(name="x", model=provider, tools=[_noop_toolset()])
    a.run_sync("hi")
    tool_names = {t.name for t in (provider.calls[0]["tools"] or [])}
    assert "noop_tool" in tool_names


# ---- run / run_sync behavior ----


def test_run_sync_returns_run_result() -> None:
    a = Agent(name="x", model=ScriptedProvider([text_response("answer")]))
    result = a.run_sync("question")
    assert isinstance(result, RunResult)
    assert result.final_text == "answer"


@pytest.mark.asyncio
async def test_run_async_returns_run_result() -> None:
    a = Agent(name="x", model=ScriptedProvider([text_response("async-answer")]))
    result = await a.run("question")
    assert result.final_text == "async-answer"


@pytest.mark.asyncio
async def test_run_sync_rejected_in_running_loop() -> None:
    """Inherits Runner.run_sync's loop detection."""
    a = Agent(name="x", model=ScriptedProvider())
    with pytest.raises(RuntimeError, match="cannot be called from a running event loop"):
        a.run_sync("hi")


# ---- per-run overrides ----


def test_run_uses_default_max_rounds_temperature_max_tokens() -> None:
    provider = ScriptedProvider()
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
    provider = ScriptedProvider()
    a = Agent(name="x", model=provider, default_temperature=0.7, default_max_tokens=1000)
    a.run_sync("hi", temperature=0.0, max_tokens=42)
    assert provider.calls[0]["temperature"] == 0.0
    assert provider.calls[0]["max_tokens"] == 42


def test_agent_id_in_request_matches_agent_name() -> None:
    """Smoke check that Agent.run wires agent_id from name; real assertion
    in test_run_request_builder below."""

    class _Cap(Hook):
        async def before_model(self, ctx, messages, tools):
            return None

    a = Agent(name="my-agent", model=ScriptedProvider(), hooks=[_Cap()])
    result = a.run_sync("hi")
    assert result.error is None


def test_run_request_builder_carries_all_fields() -> None:
    """Verify _build_request packs agent_id + every override."""
    a = Agent(name="my-agent", model=ScriptedProvider(),
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
    provider = ScriptedProvider()
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
    a = Agent(name="x", model=ScriptedProvider())
    result = a.runner.run_sync(
        RunRequest(agent_id="x", user_message="hi"),
    )
    assert result.final_text == "ok"   # default from helpers' _default_response


# ---- workspace_provider passthrough ----


def test_workspace_provider_passthrough(tmp_path) -> None:
    seen: dict[str, Any] = {}
    external = tmp_path / "shared"
    external.mkdir()

    class _Cap(Hook):
        async def before_model(self, ctx, messages, tools):
            seen["workspace"] = ctx.workspace
            seen["ephemeral"] = ctx.workspace_ephemeral
            return None

    a = Agent(
        name="x", model=ScriptedProvider(),
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
    provider = ScriptedProvider([
        tool_call_response(ToolCall(id="c1", name="noop_tool", arguments={})),
        text_response("never reached"),
    ])
    a = Agent(name="x", model=provider, tools=[_noop_toolset()])
    # cancel after the first LLM round
    state = {"checks": 0}

    def cancel_check() -> bool:
        state["checks"] += 1
        return state["checks"] > 1   # first check returns False, then True

    result = a.run_sync("hi", cancel_check=cancel_check, max_rounds=5)
    assert result.cancelled is True


# ---- end-to-end behaviors (migrated from test_runner.py) ----


def test_run_to_completion_rounds_used_multi() -> None:
    """Two LLM rounds (tool call then final_text) → rounds_used==2."""
    tool_calls = [ToolCall(id="c1", name="echo", arguments={"x": 1})]
    provider = ScriptedProvider([
        tool_call_response(*tool_calls),
        text_response("all done"),
    ])
    ts = RecordingToolset("test", {"echo": lambda a: f"echo:{a['x']}"})
    a = Agent(name="x", model=provider, tools=[ts])
    result = a.run_sync("go", max_rounds=5)
    assert result.final_text == "all done"
    assert result.rounds_used == 2
    assert ts.execute_calls[0].name == "echo"


def test_run_raises_on_provider_error() -> None:
    """Q4 contract: error event → RuntimeError, with stage + exc_type prefix."""
    from tests._helpers import RaisingProvider
    a = Agent(
        name="x",
        model=RaisingProvider(error_cls=ValueError, message="nope"),
    )
    with pytest.raises(RuntimeError, match=r"\[provider\] ValueError: nope"):
        a.run_sync("hi")


def test_toolsets_aclose_called_after_run() -> None:
    """Per-run aclose: toolsets get closed even on success."""
    ts1 = RecordingToolset("a", {"f": "1"})
    ts2 = RecordingToolset("b", {"g": "2"})
    a = Agent(name="x", model=ScriptedProvider(), tools=[ts1, ts2])
    a.run_sync("hi")
    assert ts1.closed == 1
    assert ts2.closed == 1


def test_workspace_provider_path_used_and_persisted(tmp_path) -> None:
    """workspace_provider returns a caller-owned path; survives the run."""
    external = tmp_path / "persistent"
    external.mkdir()
    sentinel = external / "marker.txt"
    sentinel.write_text("hello")

    seen: dict[str, Any] = {}

    class _Probe(Hook):
        async def before_model(self, ctx, messages, tools):
            seen["workspace"] = ctx.workspace
            seen["ephemeral"] = ctx.workspace_ephemeral
            return None

    a = Agent(
        name="x", model=ScriptedProvider(),
        workspace_provider=lambda req, run_id: external,
        hooks=[_Probe()],
    )
    a.run_sync("hi")
    assert seen["workspace"] == external
    assert seen["ephemeral"] is False
    assert external.exists() and sentinel.read_text() == "hello"


def test_workspace_ephemeral_default_true_without_provider(tmp_path) -> None:
    """No workspace_provider → SDK self-managed (ephemeral=True, dir deleted)."""
    seen: dict[str, Any] = {}

    class _Probe(Hook):
        async def before_model(self, ctx, messages, tools):
            seen["ephemeral"] = ctx.workspace_ephemeral
            seen["workspace"] = ctx.workspace
            return None

    a = Agent(
        name="x", model=ScriptedProvider(),
        workspace_root=tmp_path / "ws",
        hooks=[_Probe()],
    )
    a.run_sync("hi")
    assert seen["ephemeral"] is True
    assert not seen["workspace"].exists()


def test_workspace_provider_raises_yields_setup_error() -> None:
    """A workspace_provider that raises → RuntimeError with stage=setup prefix."""
    def boom(req, run_id):
        raise RuntimeError("provider exploded")

    a = Agent(
        name="x", model=ScriptedProvider(),
        workspace_provider=boom,
    )
    with pytest.raises(RuntimeError, match=r"\[setup\].*provider exploded"):
        a.run_sync("hi")
