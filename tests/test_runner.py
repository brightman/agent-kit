"""tests/test_runner.py — Stage 3 Runner integration tests.

Coverage(对照 docs/tech-design.md § 9 / § 10 / § 14):

- run() 透传 loop event 流
- run_to_completion 返回 RunResult(final_text / rounds_used / events)
- run_to_completion 遇 error event raise RuntimeError(包含 stage + exc_type)
- workspace mkdir + finally 清理(成功 / 异常 / cancelled 三态)
- setup 阶段抛异常 → error event stage=setup,workspace 仍清理
- 取消(用 hook 在 before_tool 里 set ctx.cancel)→ cancelled=True
- toolsets aclose 被调用(`.closed >= 1`)
- skill catalog 注入 prelude:有 SkillCatalogToolset + enabled_skills → prelude 含
  "# Available Skills" 段;无 catalog 或 enabled_skills 空 → 不注入
- 多 SkillCatalogToolset 时取第一个
- Runner.system_prelude + skill catalog + RunRequest.system_prelude 三段拼接顺序
- enabled_skills 含 unknown skill ref → 静默跳过
- compactor / hooks 透传到 loop(snapshot)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_kit.hooks import Hook
from agent_kit.provider import ToolSchema
from agent_kit.runner import Runner, RunResult
from agent_kit.skill import (
    Skill,
    SkillCatalogToolset,
    SkillFrontmatter,
    SkillRegistry,
)
from agent_kit.toolset import BaseToolset
from agent_kit.types import ToolCall

from tests._helpers import (
    RaisingProvider,
    RecordingToolset,
    ScriptedProvider,
    make_request,
    text_response,
    tool_call_response,
)


# ---- runner-specific helpers (skills + setup-blowup not in shared helpers) ----


class _BlowupToolset(BaseToolset):
    """build_schemas raises on init — used to force a setup error."""

    name = "blowup"

    def build_schemas(self) -> list[ToolSchema]:
        raise RuntimeError("schema build exploded")

    async def execute(self, call, ctx):
        raise NotImplementedError


class _FakeRegistry(SkillRegistry):
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def add(self, fm: SkillFrontmatter) -> None:
        self._skills[fm.name] = Skill(
            name=fm.name, frontmatter=fm, body="body",
            files={}, storage_root=Path("/tmp"),
        )

    async def list(self):
        return [s.frontmatter for s in self._skills.values()]

    async def load(self, name, version=None):
        if name not in self._skills:
            raise KeyError(name)
        return self._skills[name]

    async def save_draft(self, *a, **k): ...

    async def publish(self, *a, **k) -> str:
        return "0"


class _CancelInBeforeTool(Hook):
    """Hook that flips ctx.cancel right before the first tool runs."""

    async def before_tool(self, ctx, call):
        ctx.cancel.set()
        return None


# ---- run() / run_to_completion happy path ----


@pytest.mark.asyncio
async def test_run_yields_loop_events(tmp_path: Path) -> None:
    provider = ScriptedProvider([text_response("hi")])
    runner = Runner(provider, toolsets=[], workspace_root=tmp_path / "ws")
    events = [evt async for evt in runner.run(make_request())]
    kinds = [e.kind for e in events]
    assert kinds == [
        "round_start", "llm_request", "llm_response",
        "final_text", "round_end",
    ]


@pytest.mark.asyncio
async def test_run_to_completion_returns_run_result(tmp_path: Path) -> None:
    provider = ScriptedProvider([text_response("hello")])
    runner = Runner(provider, toolsets=[], workspace_root=tmp_path / "ws")
    result = await runner.run_to_completion(make_request())
    assert isinstance(result, RunResult)
    assert result.final_text == "hello"
    assert result.cancelled is False
    assert result.error is None
    assert result.rounds_used == 1
    assert any(e.kind == "final_text" for e in result.events)


@pytest.mark.asyncio
async def test_run_to_completion_rounds_used_multi(tmp_path: Path) -> None:
    """Two LLM rounds(tool call then final_text)→ rounds_used==2."""
    tool_calls = [ToolCall(id="c1", name="echo", arguments={"x": 1})]
    provider = ScriptedProvider([
        tool_call_response(*tool_calls),
        text_response("all done"),
    ])
    ts = RecordingToolset("test", {"echo": lambda a: f"echo:{a['x']}"})
    runner = Runner(provider, toolsets=[ts], workspace_root=tmp_path / "ws")
    result = await runner.run_to_completion(
        make_request(user_message="go", max_rounds=5)
    )
    assert result.final_text == "all done"
    assert result.rounds_used == 2
    assert ts.execute_calls[0].name == "echo"


# ---- error event → raise ----


@pytest.mark.asyncio
async def test_run_to_completion_raises_on_provider_error(tmp_path: Path) -> None:
    runner = Runner(
        RaisingProvider(message="provider exploded"),
        toolsets=[], workspace_root=tmp_path / "ws",
    )
    with pytest.raises(RuntimeError, match="provider exploded"):
        await runner.run_to_completion(make_request())


@pytest.mark.asyncio
async def test_run_to_completion_raise_message_includes_stage(tmp_path: Path) -> None:
    runner = Runner(
        RaisingProvider(error_cls=ValueError, message="nope"),
        toolsets=[], workspace_root=tmp_path / "ws",
    )
    with pytest.raises(RuntimeError, match=r"\[provider\] ValueError: nope"):
        await runner.run_to_completion(make_request())


@pytest.mark.asyncio
async def test_run_yields_error_event_does_not_raise(tmp_path: Path) -> None:
    """run() must NOT raise on error — error becomes an event."""
    runner = Runner(
        RaisingProvider(message="boom"),
        toolsets=[], workspace_root=tmp_path / "ws",
    )
    events = [evt async for evt in runner.run(make_request())]
    error_evts = [e for e in events if e.kind == "error"]
    assert len(error_evts) == 1
    assert error_evts[0].payload["stage"] == "provider"
    assert "boom" in error_evts[0].payload["message"]


# ---- workspace lifecycle ----


@pytest.mark.asyncio
async def test_workspace_mkdir_and_cleanup_success(tmp_path: Path) -> None:
    ws_root = tmp_path / "ws"
    workspaces_seen: list[Path] = []

    class _PeekHook(Hook):
        async def before_model(self, ctx, messages, tools):
            workspaces_seen.append(ctx.workspace)
            assert ctx.workspace.exists()
            return None

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[], workspace_root=ws_root, hooks=[_PeekHook()]
    )
    await runner.run_to_completion(make_request())
    assert workspaces_seen, "before_model hook should have fired"
    ws = workspaces_seen[0]
    assert ws.parent == ws_root
    assert not ws.exists(), "workspace must be removed in finally"


@pytest.mark.asyncio
async def test_workspace_cleanup_on_provider_error(tmp_path: Path) -> None:
    ws_root = tmp_path / "ws"
    seen: list[Path] = []

    class _Snoop(Hook):
        async def before_model(self, ctx, messages, tools):
            seen.append(ctx.workspace)
            return None

    runner = Runner(
        RaisingProvider(message="nope"),
        toolsets=[], workspace_root=ws_root, hooks=[_Snoop()],
    )
    events = [evt async for evt in runner.run(make_request())]
    assert any(e.kind == "error" for e in events)
    assert seen and not seen[0].exists()


@pytest.mark.asyncio
async def test_setup_error_yields_setup_stage(tmp_path: Path) -> None:
    """A toolset whose build_schemas raises trips loop init → setup error."""
    provider = ScriptedProvider([text_response("x")])
    runner = Runner(
        provider,
        toolsets=[_BlowupToolset()],
        workspace_root=tmp_path / "ws",
    )
    events = [evt async for evt in runner.run(make_request())]
    error_evts = [e for e in events if e.kind == "error"]
    assert len(error_evts) == 1
    assert error_evts[0].payload["stage"] == "setup"


# ---- cancel ----


@pytest.mark.asyncio
async def test_cancel_via_hook(tmp_path: Path) -> None:
    """Hook flips ctx.cancel mid-tool dispatch → cancelled=True."""
    tool_calls = [ToolCall(id="c1", name="echo", arguments={})]
    provider = ScriptedProvider([
        tool_call_response(*tool_calls),
        text_response("never reached"),
    ])
    ts = RecordingToolset("test", {"echo": "out"})
    runner = Runner(
        provider, toolsets=[ts],
        hooks=[_CancelInBeforeTool()],
        workspace_root=tmp_path / "ws",
    )
    result = await runner.run_to_completion(make_request(max_rounds=5))
    assert result.cancelled is True
    assert result.final_text is None


# ---- toolset aclose ----


@pytest.mark.asyncio
async def test_toolsets_aclose_called(tmp_path: Path) -> None:
    provider = ScriptedProvider([text_response()])
    ts1 = RecordingToolset("a", {"f": "1"})
    ts2 = RecordingToolset("b", {"g": "2"})
    runner = Runner(provider, toolsets=[ts1, ts2], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(make_request())
    assert ts1.closed == 1
    assert ts2.closed == 1


@pytest.mark.asyncio
async def test_toolsets_aclose_called_even_on_error(tmp_path: Path) -> None:
    ts = RecordingToolset("a", {"f": "1"})
    runner = Runner(
        RaisingProvider(message="x"),
        toolsets=[ts], workspace_root=tmp_path / "ws",
    )
    [e async for e in runner.run(make_request())]
    assert ts.closed == 1


# ---- skill catalog prelude injection (§ 10 / § 10.1) ----


@pytest.mark.asyncio
async def test_skill_catalog_injection(tmp_path: Path) -> None:
    """SkillCatalogToolset + enabled_skills → prelude has Available Skills."""
    reg = _FakeRegistry()
    reg.add(SkillFrontmatter(
        name="paper_review", description="scores ICML papers", version="1.2.3",
    ))
    reg.add(SkillFrontmatter(
        name="summarize", description="long-doc summarizer", version="2.0.0",
    ))
    catalog = SkillCatalogToolset(reg)

    provider = ScriptedProvider([text_response()])
    runner = Runner(provider, toolsets=[catalog], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(
        make_request(enabled_skills=["paper_review", "summarize"])
    )

    # provider saw a system message with the skill section
    system_msg = provider.calls[0]["messages"][0]
    assert system_msg.role == "system"
    assert "# Available Skills" in system_msg.content
    assert "paper_review (v1.2.3): scores ICML papers" in system_msg.content
    assert "summarize (v2.0.0): long-doc summarizer" in system_msg.content


@pytest.mark.asyncio
async def test_skill_catalog_no_enabled_skills_skips_section(tmp_path: Path) -> None:
    reg = _FakeRegistry()
    reg.add(SkillFrontmatter(name="x", description="d", version="1"))
    catalog = SkillCatalogToolset(reg)

    provider = ScriptedProvider([text_response()])
    runner = Runner(provider, toolsets=[catalog], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(make_request())  # no enabled_skills

    # Either no system msg, or one without "Available Skills"
    msgs = provider.calls[0]["messages"]
    sys_msgs = [m for m in msgs if m.role == "system"]
    if sys_msgs:
        assert "Available Skills" not in sys_msgs[0].content


@pytest.mark.asyncio
async def test_no_catalog_no_injection(tmp_path: Path) -> None:
    provider = ScriptedProvider([text_response()])
    runner = Runner(provider, toolsets=[], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(
        make_request(enabled_skills=["paper_review"])  # but no catalog!
    )
    msgs = provider.calls[0]["messages"]
    sys_msgs = [m for m in msgs if m.role == "system"]
    # No system message (or empty) because there's nothing to compose
    if sys_msgs:
        assert "Available Skills" not in sys_msgs[0].content


@pytest.mark.asyncio
async def test_unknown_skill_ref_silently_skipped(tmp_path: Path) -> None:
    """enabled_skills has a name that registry doesn't know → no row for it."""
    reg = _FakeRegistry()
    reg.add(SkillFrontmatter(name="exists", description="e", version="1"))
    catalog = SkillCatalogToolset(reg)

    provider = ScriptedProvider([text_response()])
    runner = Runner(provider, toolsets=[catalog], workspace_root=tmp_path / "ws")
    result = await runner.run_to_completion(
        make_request(enabled_skills=["exists", "ghost@9.9.9"])
    )
    assert result.error is None
    sys_msg = provider.calls[0]["messages"][0]
    assert "exists (v1):" in sys_msg.content
    assert "ghost" not in sys_msg.content


@pytest.mark.asyncio
async def test_prelude_three_part_compose(tmp_path: Path) -> None:
    """Runner.system_prelude → skill catalog → RunRequest.system_prelude."""
    reg = _FakeRegistry()
    reg.add(SkillFrontmatter(name="s1", description="d1", version="1"))
    catalog = SkillCatalogToolset(reg)
    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[catalog],
        system_prelude="RUNNER_PRELUDE",
        workspace_root=tmp_path / "ws",
    )
    await runner.run_to_completion(
        make_request(enabled_skills=["s1"], system_prelude="REQUEST_PRELUDE")
    )
    sys_msg = provider.calls[0]["messages"][0]
    content = sys_msg.content
    runner_idx = content.index("RUNNER_PRELUDE")
    skill_idx = content.index("Available Skills")
    request_idx = content.index("REQUEST_PRELUDE")
    assert runner_idx < skill_idx < request_idx


@pytest.mark.asyncio
async def test_multiple_catalogs_picks_first(tmp_path: Path) -> None:
    """Discovery (`isinstance` walk) picks the first SkillCatalogToolset.

    Two real catalogs would collide on `name='skill_catalog'` AND on the three
    shared tool names, so the second is a no-schemas subclass — it's still
    `isinstance SkillCatalogToolset` (the only thing discovery cares about),
    and the test proves the **first** one's registry is the one that wins.
    """
    reg_a = _FakeRegistry()
    reg_a.add(SkillFrontmatter(name="from_a", description="A", version="1"))
    reg_b = _FakeRegistry()
    reg_b.add(SkillFrontmatter(name="from_b", description="B", version="1"))

    class _QuietCatalog(SkillCatalogToolset):
        name = "skill_catalog_b"

        def build_schemas(self):
            return []

    cat_a = SkillCatalogToolset(reg_a)
    cat_b = _QuietCatalog(reg_b)
    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[cat_a, cat_b], workspace_root=tmp_path / "ws"
    )
    await runner.run_to_completion(
        make_request(enabled_skills=["from_a", "from_b"])
    )
    sys_msg = provider.calls[0]["messages"][0]
    assert "from_a (v1):" in sys_msg.content
    assert "from_b" not in sys_msg.content


@pytest.mark.asyncio
async def test_enabled_skills_versioned_ref_uses_latest_for_prelude(
    tmp_path: Path,
) -> None:
    """Per § 10.1: version pin doesn't affect prelude; description is whatever
    list() returns."""
    reg = _FakeRegistry()
    reg.add(SkillFrontmatter(name="paper_review", description="latest desc",
                             version="2.0.0"))
    catalog = SkillCatalogToolset(reg)
    provider = ScriptedProvider([text_response()])
    runner = Runner(provider, toolsets=[catalog], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(
        make_request(enabled_skills=["paper_review@1.0.0"])
    )
    sys_msg = provider.calls[0]["messages"][0]
    assert "paper_review (v2.0.0): latest desc" in sys_msg.content


# ---- ctx population ----


@pytest.mark.asyncio
async def test_ctx_run_id_matches_workspace(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _Probe(Hook):
        async def before_model(self, ctx, messages, tools):
            captured["run_id"] = ctx.run_id
            captured["workspace"] = ctx.workspace
            return None

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[], hooks=[_Probe()],
        workspace_root=tmp_path / "ws",
    )
    await runner.run_to_completion(make_request())
    assert captured["workspace"].name == captured["run_id"]


# ---- run_result.events full stream ----


@pytest.mark.asyncio
async def test_run_to_completion_events_match_run(tmp_path: Path) -> None:
    """run_to_completion.events should be the same list run() yields."""
    provider1 = ScriptedProvider([text_response("x")])
    runner1 = Runner(provider1, toolsets=[], workspace_root=tmp_path / "w1")
    stream_kinds = [e.kind async for e in runner1.run(make_request())]

    provider2 = ScriptedProvider([text_response("x")])
    runner2 = Runner(provider2, toolsets=[], workspace_root=tmp_path / "w2")
    result = await runner2.run_to_completion(make_request())
    aggr_kinds = [e.kind for e in result.events]

    assert stream_kinds == aggr_kinds


# ---- workspace_provider (external workspace injection) ----


@pytest.mark.asyncio
async def test_workspace_provider_returned_path_used(tmp_path: Path) -> None:
    """Runner uses provider's path for ctx.workspace, ignores workspace_root."""
    external = tmp_path / "tenant_42" / "agent_x"
    external.mkdir(parents=True)

    seen: dict[str, Any] = {}

    class _Probe(Hook):
        async def before_model(self, ctx, messages, tools):
            seen["workspace"] = ctx.workspace
            seen["ephemeral"] = ctx.workspace_ephemeral
            return None

    def provider_fn(req, run_id):
        seen["provider_called_with"] = (req.agent_id, run_id)
        return external

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[], hooks=[_Probe()],
        workspace_root=tmp_path / "ws_should_be_ignored",
        workspace_provider=provider_fn,
    )
    await runner.run_to_completion(make_request())

    assert seen["workspace"] == external
    assert seen["ephemeral"] is False
    aid, rid = seen["provider_called_with"]
    assert aid == "a"
    assert rid  # non-empty


@pytest.mark.asyncio
async def test_workspace_provider_path_not_deleted(tmp_path: Path) -> None:
    """Provider-injected workspace must survive after run() completes."""
    external = tmp_path / "persistent" / "agent_x"
    external.mkdir(parents=True)
    # drop a sentinel file so we can verify cross-run persistence
    sentinel = external / "sentinel.txt"
    sentinel.write_text("hello")

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[],
        workspace_provider=lambda req, run_id: external,
    )
    await runner.run_to_completion(make_request())

    assert external.exists()
    assert sentinel.read_text() == "hello"


@pytest.mark.asyncio
async def test_workspace_provider_does_not_mkdir(tmp_path: Path) -> None:
    """SDK never mkdirs the provider-returned path; provider owns lifecycle.

    If provider returns a non-existent path AND doesn't create it,
    setup itself doesn't fail (Runner just hands the path to ctx);
    any toolset that tries to write will get a normal FileNotFoundError —
    that's the provider's contract violation, not SDK's responsibility.
    """
    ghost = tmp_path / "never_created_by_anyone"
    assert not ghost.exists()

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[],
        workspace_provider=lambda req, run_id: ghost,
    )
    await runner.run_to_completion(make_request())

    # Runner did NOT silently create it
    assert not ghost.exists()


@pytest.mark.asyncio
async def test_ctx_workspace_ephemeral_default_true(tmp_path: Path) -> None:
    """No provider → ctx.workspace_ephemeral is True (SDK self-managed)."""
    seen: dict[str, Any] = {}

    class _Probe(Hook):
        async def before_model(self, ctx, messages, tools):
            seen["ephemeral"] = ctx.workspace_ephemeral
            seen["workspace"] = ctx.workspace
            return None

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[], hooks=[_Probe()],
        workspace_root=tmp_path / "ws",
    )
    await runner.run_to_completion(make_request())

    assert seen["ephemeral"] is True
    # workspace was deleted by Runner finally
    assert not seen["workspace"].exists()


@pytest.mark.asyncio
async def test_workspace_provider_raises_yields_setup_error(tmp_path: Path) -> None:
    """provider that raises → error event stage=setup, NOT a crash."""

    def boom_provider(req, run_id):
        raise RuntimeError("provider exploded")

    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[],
        workspace_provider=boom_provider,
    )
    events = [e async for e in runner.run(make_request())]
    error_evts = [e for e in events if e.kind == "error"]
    assert len(error_evts) == 1
    assert error_evts[0].payload["stage"] == "setup"
    assert "provider exploded" in error_evts[0].payload["message"]


# ---- run_sync (spec § 9.2 Stage 5 修订) ----


def test_run_sync_returns_run_result(tmp_path: Path) -> None:
    """Pure-sync caller gets the same RunResult as run_to_completion."""
    provider = ScriptedProvider([text_response("hello sync")])
    runner = Runner(provider, toolsets=[], workspace_root=tmp_path / "ws")
    result = runner.run_sync(make_request())
    assert isinstance(result, RunResult)
    assert result.final_text == "hello sync"
    assert result.error is None
    assert result.cancelled is False
    assert result.rounds_used == 1


def test_run_sync_raises_on_error_event(tmp_path: Path) -> None:
    """Same Q4 contract as run_to_completion: error event → RuntimeError."""
    runner = Runner(
        RaisingProvider(error_cls=ValueError, message="nope"),
        toolsets=[], workspace_root=tmp_path / "ws",
    )
    with pytest.raises(RuntimeError, match=r"\[provider\] ValueError: nope"):
        runner.run_sync(make_request())


def test_run_sync_workspace_provider(tmp_path: Path) -> None:
    """workspace_provider works through the sync wrapper (no async-specific glue)."""
    external = tmp_path / "persistent"
    external.mkdir()
    provider = ScriptedProvider([text_response()])
    runner = Runner(
        provider, toolsets=[],
        workspace_provider=lambda req, run_id: external,
    )
    result = runner.run_sync(make_request())
    assert result.error is None
    assert external.exists()  # caller-owned, SDK doesn't delete


@pytest.mark.asyncio
async def test_run_sync_rejected_inside_running_event_loop(tmp_path: Path) -> None:
    """Called from inside a running loop → friendly RuntimeError, not the cryptic asyncio one."""
    provider = ScriptedProvider([text_response("x")])
    runner = Runner(provider, toolsets=[], workspace_root=tmp_path / "ws")
    with pytest.raises(
        RuntimeError,
        match=r"run_sync\(\).*cannot be called from a running event loop",
    ):
        runner.run_sync(make_request())
