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

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_kit.hooks import Hook
from agent_kit.loop import RunRequest
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.runner import Runner, RunResult
from agent_kit.skill import (
    Skill,
    SkillCatalogToolset,
    SkillFrontmatter,
    SkillRegistry,
)
from agent_kit.toolset import BaseToolset, ToolCallContext
from agent_kit.types import Event, Message, ToolCall, ToolResult


# ---- helpers (mirror tests/test_loop.py + test_skill.py shapes) ----


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[Message], list[ToolSchema] | None]] = []

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append((list(messages), list(tools) if tools else None))
        if not self._responses:
            raise RuntimeError("scripted provider exhausted")
        return self._responses.pop(0)

    async def chat_stream(self, *_, **__):
        raise NotImplementedError


class _RecordingToolset(BaseToolset):
    def __init__(self, name: str, handlers: dict[str, Any]) -> None:
        self.name = name
        self._handlers = handlers
        self.execute_calls: list[ToolCall] = []
        self.closed = 0

    def build_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(name=n, description=f"stub {n}",
                       parameters={"type": "object"})
            for n in self._handlers
        ]

    async def execute(self, call, ctx):
        self.execute_calls.append(call)
        h = self._handlers.get(call.name)
        r = h(call.arguments) if callable(h) else h
        if isinstance(r, ToolResult):
            return r
        return ToolResult(call_id=call.id, content=str(r))

    async def aclose(self) -> None:
        self.closed += 1


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

    async def list(self, tenant_id: str):
        return [s.frontmatter for s in self._skills.values()]

    async def load(self, tenant_id, name, version=None):
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


def _basic_req(**overrides) -> RunRequest:
    base = dict(
        tenant_id="t",
        agent_id="a",
        user_message="hi",
        max_rounds=3,
    )
    base.update(overrides)
    return RunRequest(**base)


# ---- run() / run_to_completion happy path ----


@pytest.mark.asyncio
async def test_run_yields_loop_events(tmp_path: Path) -> None:
    provider = _ScriptedProvider([LlmResponse(text="hi", tool_calls=[])])
    runner = Runner(provider, toolsets=[], workspace_root=tmp_path / "ws")
    events = [evt async for evt in runner.run(_basic_req())]
    kinds = [e.kind for e in events]
    assert kinds == [
        "round_start", "llm_request", "llm_response",
        "final_text", "round_end",
    ]


@pytest.mark.asyncio
async def test_run_to_completion_returns_run_result(tmp_path: Path) -> None:
    provider = _ScriptedProvider([LlmResponse(text="hello", tool_calls=[])])
    runner = Runner(provider, toolsets=[], workspace_root=tmp_path / "ws")
    result = await runner.run_to_completion(_basic_req())
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
    provider = _ScriptedProvider([
        LlmResponse(text="", tool_calls=tool_calls),
        LlmResponse(text="all done", tool_calls=[]),
    ])
    ts = _RecordingToolset("test", {"echo": lambda a: f"echo:{a['x']}"})
    runner = Runner(provider, toolsets=[ts], workspace_root=tmp_path / "ws")
    result = await runner.run_to_completion(
        _basic_req(user_message="go", max_rounds=5)
    )
    assert result.final_text == "all done"
    assert result.rounds_used == 2
    assert ts.execute_calls[0].name == "echo"


# ---- error event → raise ----


@pytest.mark.asyncio
async def test_run_to_completion_raises_on_provider_error(tmp_path: Path) -> None:
    class _Boom:
        name = "boom"
        async def chat(self, *a, **k):
            raise RuntimeError("provider exploded")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    runner = Runner(_Boom(), toolsets=[], workspace_root=tmp_path / "ws")
    with pytest.raises(RuntimeError, match="provider exploded"):
        await runner.run_to_completion(_basic_req())


@pytest.mark.asyncio
async def test_run_to_completion_raise_message_includes_stage(tmp_path: Path) -> None:
    class _Boom:
        name = "boom"
        async def chat(self, *a, **k):
            raise ValueError("nope")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    runner = Runner(_Boom(), toolsets=[], workspace_root=tmp_path / "ws")
    with pytest.raises(RuntimeError, match=r"\[provider\] ValueError: nope"):
        await runner.run_to_completion(_basic_req())


@pytest.mark.asyncio
async def test_run_yields_error_event_does_not_raise(tmp_path: Path) -> None:
    """run() must NOT raise on error — error becomes an event."""

    class _Boom:
        name = "boom"
        async def chat(self, *a, **k):
            raise RuntimeError("boom")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    runner = Runner(_Boom(), toolsets=[], workspace_root=tmp_path / "ws")
    events = [evt async for evt in runner.run(_basic_req())]
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

    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(
        provider, toolsets=[], workspace_root=ws_root, hooks=[_PeekHook()]
    )
    await runner.run_to_completion(_basic_req())
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

    class _Boom:
        name = "boom"
        async def chat(self, *a, **k):
            raise RuntimeError("nope")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    runner = Runner(_Boom(), toolsets=[], workspace_root=ws_root, hooks=[_Snoop()])
    events = [evt async for evt in runner.run(_basic_req())]
    assert any(e.kind == "error" for e in events)
    assert seen and not seen[0].exists()


@pytest.mark.asyncio
async def test_setup_error_yields_setup_stage(tmp_path: Path) -> None:
    """A toolset whose build_schemas raises trips loop init → setup error."""
    provider = _ScriptedProvider([LlmResponse(text="x", tool_calls=[])])
    runner = Runner(
        provider,
        toolsets=[_BlowupToolset()],
        workspace_root=tmp_path / "ws",
    )
    events = [evt async for evt in runner.run(_basic_req())]
    error_evts = [e for e in events if e.kind == "error"]
    assert len(error_evts) == 1
    assert error_evts[0].payload["stage"] == "setup"


# ---- cancel ----


@pytest.mark.asyncio
async def test_cancel_via_hook(tmp_path: Path) -> None:
    """Hook flips ctx.cancel mid-tool dispatch → cancelled=True."""
    tool_calls = [ToolCall(id="c1", name="echo", arguments={})]
    provider = _ScriptedProvider([
        LlmResponse(text="", tool_calls=tool_calls),
        LlmResponse(text="never reached", tool_calls=[]),
    ])
    ts = _RecordingToolset("test", {"echo": "out"})
    runner = Runner(
        provider, toolsets=[ts],
        hooks=[_CancelInBeforeTool()],
        workspace_root=tmp_path / "ws",
    )
    result = await runner.run_to_completion(_basic_req(max_rounds=5))
    assert result.cancelled is True
    assert result.final_text is None


# ---- toolset aclose ----


@pytest.mark.asyncio
async def test_toolsets_aclose_called(tmp_path: Path) -> None:
    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    ts1 = _RecordingToolset("a", {"f": "1"})
    ts2 = _RecordingToolset("b", {"g": "2"})
    runner = Runner(provider, toolsets=[ts1, ts2], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(_basic_req())
    assert ts1.closed == 1
    assert ts2.closed == 1


@pytest.mark.asyncio
async def test_toolsets_aclose_called_even_on_error(tmp_path: Path) -> None:
    class _Boom:
        name = "boom"
        async def chat(self, *a, **k):
            raise RuntimeError("x")
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    ts = _RecordingToolset("a", {"f": "1"})
    runner = Runner(_Boom(), toolsets=[ts], workspace_root=tmp_path / "ws")
    [e async for e in runner.run(_basic_req())]
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
    catalog = SkillCatalogToolset(reg, tenant_id="t")

    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(provider, toolsets=[catalog], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(
        _basic_req(enabled_skills=["paper_review", "summarize"])
    )

    # provider saw a system message with the skill section
    system_msg = provider.calls[0][0][0]
    assert system_msg.role == "system"
    assert "# Available Skills" in system_msg.content
    assert "paper_review (v1.2.3): scores ICML papers" in system_msg.content
    assert "summarize (v2.0.0): long-doc summarizer" in system_msg.content


@pytest.mark.asyncio
async def test_skill_catalog_no_enabled_skills_skips_section(tmp_path: Path) -> None:
    reg = _FakeRegistry()
    reg.add(SkillFrontmatter(name="x", description="d", version="1"))
    catalog = SkillCatalogToolset(reg, tenant_id="t")

    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(provider, toolsets=[catalog], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(_basic_req())  # no enabled_skills

    # Either no system msg, or one without "Available Skills"
    msgs = provider.calls[0][0]
    sys_msgs = [m for m in msgs if m.role == "system"]
    if sys_msgs:
        assert "Available Skills" not in sys_msgs[0].content


@pytest.mark.asyncio
async def test_no_catalog_no_injection(tmp_path: Path) -> None:
    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(provider, toolsets=[], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(
        _basic_req(enabled_skills=["paper_review"])  # but no catalog!
    )
    msgs = provider.calls[0][0]
    sys_msgs = [m for m in msgs if m.role == "system"]
    # No system message (or empty) because there's nothing to compose
    if sys_msgs:
        assert "Available Skills" not in sys_msgs[0].content


@pytest.mark.asyncio
async def test_unknown_skill_ref_silently_skipped(tmp_path: Path) -> None:
    """enabled_skills has a name that registry doesn't know → no row for it."""
    reg = _FakeRegistry()
    reg.add(SkillFrontmatter(name="exists", description="e", version="1"))
    catalog = SkillCatalogToolset(reg, tenant_id="t")

    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(provider, toolsets=[catalog], workspace_root=tmp_path / "ws")
    result = await runner.run_to_completion(
        _basic_req(enabled_skills=["exists", "ghost@9.9.9"])
    )
    assert result.error is None
    sys_msg = provider.calls[0][0][0]
    assert "exists (v1):" in sys_msg.content
    assert "ghost" not in sys_msg.content


@pytest.mark.asyncio
async def test_prelude_three_part_compose(tmp_path: Path) -> None:
    """Runner.system_prelude → skill catalog → RunRequest.system_prelude."""
    reg = _FakeRegistry()
    reg.add(SkillFrontmatter(name="s1", description="d1", version="1"))
    catalog = SkillCatalogToolset(reg, tenant_id="t")
    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(
        provider, toolsets=[catalog],
        system_prelude="RUNNER_PRELUDE",
        workspace_root=tmp_path / "ws",
    )
    await runner.run_to_completion(
        _basic_req(enabled_skills=["s1"], system_prelude="REQUEST_PRELUDE")
    )
    sys_msg = provider.calls[0][0][0]
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

    cat_a = SkillCatalogToolset(reg_a, tenant_id="t")
    cat_b = _QuietCatalog(reg_b, tenant_id="t")
    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(
        provider, toolsets=[cat_a, cat_b], workspace_root=tmp_path / "ws"
    )
    await runner.run_to_completion(
        _basic_req(enabled_skills=["from_a", "from_b"])
    )
    sys_msg = provider.calls[0][0][0]
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
    catalog = SkillCatalogToolset(reg, tenant_id="t")
    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(provider, toolsets=[catalog], workspace_root=tmp_path / "ws")
    await runner.run_to_completion(
        _basic_req(enabled_skills=["paper_review@1.0.0"])
    )
    sys_msg = provider.calls[0][0][0]
    assert "paper_review (v2.0.0): latest desc" in sys_msg.content


# ---- ctx population ----


@pytest.mark.asyncio
async def test_ctx_run_id_matches_workspace(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _Probe(Hook):
        async def before_model(self, ctx, messages, tools):
            captured["run_id"] = ctx.run_id
            captured["workspace"] = ctx.workspace
            captured["tenant_id"] = ctx.tenant_id
            return None

    provider = _ScriptedProvider([LlmResponse(text="ok", tool_calls=[])])
    runner = Runner(
        provider, toolsets=[], hooks=[_Probe()],
        workspace_root=tmp_path / "ws",
    )
    await runner.run_to_completion(_basic_req(tenant_id="user_42"))
    assert captured["workspace"].name == captured["run_id"]
    assert captured["tenant_id"] == "user_42"


# ---- run_result.events full stream ----


@pytest.mark.asyncio
async def test_run_to_completion_events_match_run(tmp_path: Path) -> None:
    """run_to_completion.events should be the same list run() yields."""
    provider1 = _ScriptedProvider([LlmResponse(text="x", tool_calls=[])])
    runner1 = Runner(provider1, toolsets=[], workspace_root=tmp_path / "w1")
    stream_kinds = [e.kind async for e in runner1.run(_basic_req())]

    provider2 = _ScriptedProvider([LlmResponse(text="x", tool_calls=[])])
    runner2 = Runner(provider2, toolsets=[], workspace_root=tmp_path / "w2")
    result = await runner2.run_to_completion(_basic_req())
    aggr_kinds = [e.kind for e in result.events]

    assert stream_kinds == aggr_kinds
