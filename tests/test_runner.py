"""tests/test_runner.py — Runner-specific contract (spec § 9 / § 14).

Most end-to-end behavior (run_sync, error→raise, cancel, toolset aclose, skill
catalog, workspace_provider) is covered through the **Agent** facade in
test_agent.py / test_agent_skills.py — Runner is the engine Agent drives.

What lives here is what's UNIQUE to Runner's contract and would be obscured
through Agent:

- `.run()` async-generator yielding events (Agent.run does run_to_completion)
- `workspace_root` lifecycle: mkdir before, rmtree after, even on error
- setup-stage error event when a toolset's build_schemas raises
- consistency: `.run_to_completion().events == [e async for e in .run()]`
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_kit.hooks import Hook
from agent_kit.provider import ToolSchema
from agent_kit.runner import Runner
from agent_kit.toolset import BaseToolset

from tests._helpers import (
    RaisingProvider,
    RecordingToolset,
    ScriptedProvider,
    make_request,
    text_response,
)


class _BlowupToolset(BaseToolset):
    """build_schemas raises — forces a setup error."""

    name = "blowup"

    def build_schemas(self) -> list[ToolSchema]:
        raise RuntimeError("schema build exploded")

    async def execute(self, call, ctx):
        raise NotImplementedError


# ---- async-generator surface ----


@pytest.mark.asyncio
async def test_run_yields_loop_events(tmp_path: Path) -> None:
    """Runner.run() is the **streaming** API — caller iterates events live."""
    provider = ScriptedProvider([text_response("hi")])
    runner = Runner(provider, toolsets=[], workspace=tmp_path / "ws")
    events = [evt async for evt in runner.run(make_request())]
    kinds = [e.kind for e in events]
    assert kinds == [
        "round_start", "llm_request", "llm_response",
        "final_text", "round_end",
    ]


@pytest.mark.asyncio
async def test_run_yields_error_event_does_not_raise(tmp_path: Path) -> None:
    """The streaming API never raises — errors become events. Distinct from
    run_to_completion which DOES raise (covered through Agent in test_agent)."""
    runner = Runner(
        RaisingProvider(message="boom"),
        toolsets=[], workspace=tmp_path / "ws",
    )
    events = [evt async for evt in runner.run(make_request())]
    error_evts = [e for e in events if e.kind == "error"]
    assert len(error_evts) == 1
    assert error_evts[0].payload["stage"] == "provider"
    assert "boom" in error_evts[0].payload["message"]


@pytest.mark.asyncio
async def test_run_to_completion_events_match_run(tmp_path: Path) -> None:
    """run_to_completion.events should be the same list run() yields."""
    provider1 = ScriptedProvider([text_response("x")])
    runner1 = Runner(provider1, toolsets=[], workspace=tmp_path / "w1")
    stream_kinds = [e.kind async for e in runner1.run(make_request())]

    provider2 = ScriptedProvider([text_response("x")])
    runner2 = Runner(provider2, toolsets=[], workspace=tmp_path / "w2")
    result = await runner2.run_to_completion(make_request())
    aggr_kinds = [e.kind for e in result.events]

    assert stream_kinds == aggr_kinds


# ---- workspace_root lifecycle (SDK-managed ephemeral path) ----


@pytest.mark.asyncio
async def test_workspace_mkdir_and_cleanup_success(tmp_path: Path) -> None:
    """workspace_root/<run_id> is created before model, removed after."""
    ws_root = tmp_path / "ws"
    seen: list[Path] = []

    class _PeekHook(Hook):
        async def before_model(self, ctx, messages, tools):
            seen.append(ctx.workspace)
            assert ctx.workspace.exists()
            return None

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[], workspace=ws_root, hooks=[_PeekHook()]
    )
    await runner.run_to_completion(make_request())
    assert seen, "before_model hook should have fired"
    ws = seen[0]
    assert ws.parent == ws_root
    assert not ws.exists(), "workspace must be removed in finally"


@pytest.mark.asyncio
async def test_workspace_cleanup_on_provider_error(tmp_path: Path) -> None:
    """Even when provider raises, workspace_root cleanup still runs."""
    seen: list[Path] = []

    class _Snoop(Hook):
        async def before_model(self, ctx, messages, tools):
            seen.append(ctx.workspace)
            return None

    runner = Runner(
        RaisingProvider(message="nope"),
        toolsets=[], workspace=tmp_path / "ws", hooks=[_Snoop()],
    )
    events = [evt async for evt in runner.run(make_request())]
    assert any(e.kind == "error" for e in events)
    assert seen and not seen[0].exists()


@pytest.mark.asyncio
async def test_ctx_run_id_matches_workspace(tmp_path: Path) -> None:
    """workspace path basename == run_id (handy for log correlation)."""
    captured: dict[str, object] = {}

    class _Probe(Hook):
        async def before_model(self, ctx, messages, tools):
            captured["run_id"] = ctx.run_id
            captured["workspace"] = ctx.workspace
            return None

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[], hooks=[_Probe()],
        workspace=tmp_path / "ws",
    )
    await runner.run_to_completion(make_request())
    assert captured["workspace"].name == captured["run_id"]


# ---- setup-stage error (toolset build_schemas failure) ----


@pytest.mark.asyncio
async def test_setup_error_yields_setup_stage(tmp_path: Path) -> None:
    """A toolset whose build_schemas raises trips loop init → setup error.

    Not exposed through Agent — Agent's tools= validates BaseToolset shape but
    doesn't try to build schemas eagerly, so this is the only way to verify
    Runner's setup-stage error wrapping.
    """
    provider = ScriptedProvider([text_response("x")])
    runner = Runner(
        provider,
        toolsets=[_BlowupToolset()],
        workspace=tmp_path / "ws",
    )
    events = [evt async for evt in runner.run(make_request())]
    error_evts = [e for e in events if e.kind == "error"]
    assert len(error_evts) == 1
    assert error_evts[0].payload["stage"] == "setup"


@pytest.mark.asyncio
async def test_toolsets_aclose_called_even_on_error(tmp_path: Path) -> None:
    """Toolsets close even when the run errors mid-flight."""
    ts = RecordingToolset("a", {"f": "1"})
    runner = Runner(
        RaisingProvider(message="x"),
        toolsets=[ts], workspace=tmp_path / "ws",
    )
    [e async for e in runner.run(make_request())]
    assert ts.closed == 1
