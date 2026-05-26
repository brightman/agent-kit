"""tests/test_agent_skills.py — Agent.skills= 简写 + DEFAULT_SKILLS_GUIDANCE
(spec § 17.6,参考 openai-agents Skills capability)。

Coverage:
- skills=None / Path / str / SkillRegistry / list[Skill] 四种入口
- 自动展开:.run() 不传 enabled_skills → registry.list() 全部
- 显式覆盖:.run(enabled_skills=[...]) 用调用方传的
- 显式空:.run(enabled_skills=[]) 不在 prelude 列任何 skill
- DEFAULT_SKILLS_GUIDANCE 默认 on / "" off / 自定义 str
- InMemorySkillRegistry:重名 raise / load 不存在 raise / version mismatch raise
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_kit import (
    DEFAULT_SKILLS_GUIDANCE,
    Agent,
    InMemorySkillRegistry,
    Message,
    Skill,
    SkillFrontmatter,
    SkillRegistry,
)
from agent_kit.contrib.skills import FilesystemSkillRegistry
from agent_kit.provider import LlmResponse


# ---- helpers ----


class _StubProvider:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append({"messages": list(messages), "tools": list(tools) if tools else None})
        return LlmResponse(
            text="ok", tool_calls=[], usage={}, raw={}, finish_reason="stop",
        )

    async def chat_stream(self, *a, **k):
        raise NotImplementedError


def _make_skill(name: str, desc: str = "d", version: str = "1.0") -> Skill:
    fm = SkillFrontmatter(name=name, description=desc, version=version)
    return Skill(
        name=name, frontmatter=fm, body=f"# {name} body",
        files={}, storage_root=Path("/tmp"),
    )


def _system_content(provider: _StubProvider) -> str:
    """First system message from the first chat call(empty string 若无)。"""
    msgs = provider.calls[0]["messages"]
    return next((m.content for m in msgs if m.role == "system"), "")


def _has_skill_catalog_tools(provider: _StubProvider) -> bool:
    tools = provider.calls[0]["tools"] or []
    names = {t.name for t in tools}
    return {"list_skills", "load_skill", "load_skill_resource"} <= names


# ---- skills= entry shapes ----


def test_skills_none_no_catalog() -> None:
    provider = _StubProvider()
    a = Agent(name="x", model=provider)
    a.run_sync("hi")
    assert provider.calls[0]["tools"] is None  # no tools advertised
    # no skill section in prelude
    sys = next((m.content for m in provider.calls[0]["messages"] if m.role == "system"), "")
    assert "Available Skills" not in sys


def test_skills_path_builds_filesystem_registry(tmp_path: Path) -> None:
    """Path → FilesystemSkillRegistry + SkillCatalogToolset."""
    skill_dir = tmp_path / "alpha"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: alpha skill\nversion: 1\n---\n# body\n"
    )
    provider = _StubProvider()
    a = Agent(name="x", model=provider, skills=tmp_path)
    a.run_sync("hi")
    assert _has_skill_catalog_tools(provider)
    sys = _system_content(provider)
    assert "Available Skills" in sys
    assert "alpha" in sys


def test_skills_str_also_works(tmp_path: Path) -> None:
    """str → same as Path."""
    skill_dir = tmp_path / "beta"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: beta\ndescription: x\nversion: 1\n---\nbody\n"
    )
    a = Agent(name="x", model=_StubProvider(), skills=str(tmp_path))
    assert isinstance(a._skills_registry, FilesystemSkillRegistry)


def test_skills_registry_instance_passthrough() -> None:
    """Passing a SkillRegistry instance → used directly."""
    reg = InMemorySkillRegistry([_make_skill("gamma")])
    a = Agent(name="x", model=_StubProvider(), skills=reg)
    assert a._skills_registry is reg


def test_skills_list_wraps_inmemory() -> None:
    """list[Skill] → InMemorySkillRegistry."""
    a = Agent(
        name="x", model=_StubProvider(),
        skills=[_make_skill("a"), _make_skill("b")],
    )
    assert isinstance(a._skills_registry, InMemorySkillRegistry)


def test_skills_list_non_skill_raises() -> None:
    with pytest.raises(TypeError, match="list of Skill objects"):
        Agent(name="x", model=_StubProvider(), skills=["not a skill"])  # type: ignore[list-item]


def test_skills_unsupported_type_raises() -> None:
    with pytest.raises(TypeError, match="Agent.skills expects"):
        Agent(name="x", model=_StubProvider(), skills=42)  # type: ignore[arg-type]


# ---- auto-enable all skills (the openai-agents default) ----


def test_run_without_enabled_skills_lists_all() -> None:
    """spec § 17.6: enabled_skills=None → fetch all from registry."""
    provider = _StubProvider()
    a = Agent(
        name="x", model=provider,
        skills=[_make_skill("alpha"), _make_skill("beta"), _make_skill("gamma")],
    )
    a.run_sync("hi")
    sys = _system_content(provider)
    assert "alpha (v1.0):" in sys
    assert "beta (v1.0):" in sys
    assert "gamma (v1.0):" in sys


def test_run_with_explicit_enabled_skills_subset() -> None:
    """Explicit list wins — only those go in prelude."""
    provider = _StubProvider()
    a = Agent(
        name="x", model=provider,
        skills=[_make_skill("a"), _make_skill("b"), _make_skill("c")],
    )
    a.run_sync("hi", enabled_skills=["b"])
    sys = _system_content(provider)
    assert "b (v1.0):" in sys
    assert "a (v1.0):" not in sys
    assert "c (v1.0):" not in sys


def test_run_with_explicit_empty_list_no_skills_in_prelude() -> None:
    """Explicit [] → no skills section."""
    provider = _StubProvider()
    a = Agent(
        name="x", model=provider,
        skills=[_make_skill("a")],
    )
    a.run_sync("hi", enabled_skills=[])
    sys = _system_content(provider)
    assert "Available Skills" not in sys
    # But the catalog tools are still wired (LLM can self-discover via list_skills)
    assert _has_skill_catalog_tools(provider)


def test_run_without_skills_source_explicit_enabled_is_noop() -> None:
    """No skills configured + caller passes enabled_skills → still no section."""
    provider = _StubProvider()
    a = Agent(name="x", model=provider)  # no skills
    a.run_sync("hi", enabled_skills=["nonexistent"])
    sys = next((m.content for m in provider.calls[0]["messages"] if m.role == "system"), "")
    assert "Available Skills" not in sys


# ---- DEFAULT_SKILLS_GUIDANCE prelude injection ----


def test_default_guidance_appears_in_prelude_by_default() -> None:
    """spec § 10 修订:`SkillCatalogToolset(instructions=None)` → 默认 guidance 注入 prelude."""
    provider = _StubProvider()
    a = Agent(name="x", model=provider, skills=[_make_skill("a")])
    a.run_sync("hi")
    sys = _system_content(provider)
    assert "How to use skills" in sys
    assert "Progressive disclosure" in sys


def test_custom_guidance_overrides_default() -> None:
    provider = _StubProvider()
    a = Agent(
        name="x", model=provider,
        skills=[_make_skill("a")],
        skills_instructions="### Custom guidance\n- Just use skills.",
    )
    a.run_sync("hi")
    sys = _system_content(provider)
    assert "Custom guidance" in sys
    assert "Progressive disclosure" not in sys   # default replaced


def test_empty_guidance_string_disables() -> None:
    """Explicit "" → no guidance block at all."""
    provider = _StubProvider()
    a = Agent(
        name="x", model=provider,
        skills=[_make_skill("a")],
        skills_instructions="",
    )
    a.run_sync("hi")
    sys = _system_content(provider)
    assert "Available Skills" in sys      # the catalog list IS there
    assert "How to use skills" not in sys  # but guidance is suppressed
    assert "Trigger" not in sys


def test_guidance_not_added_when_no_skills_listed() -> None:
    """No skills in prelude (enabled=[]) → no guidance either."""
    provider = _StubProvider()
    a = Agent(name="x", model=provider, skills=[_make_skill("a")])
    a.run_sync("hi", enabled_skills=[])
    sys = _system_content(provider)
    assert "Available Skills" not in sys
    assert "How to use skills" not in sys


# ---- InMemorySkillRegistry contract ----


@pytest.mark.asyncio
async def test_inmem_registry_list_load() -> None:
    reg = InMemorySkillRegistry([_make_skill("a"), _make_skill("b")])
    fms = await reg.list()
    assert {fm.name for fm in fms} == {"a", "b"}
    loaded = await reg.load("a")
    assert loaded.name == "a"
    assert loaded.body == "# a body"


@pytest.mark.asyncio
async def test_inmem_registry_load_unknown_raises() -> None:
    reg = InMemorySkillRegistry([_make_skill("a")])
    with pytest.raises(KeyError):
        await reg.load("ghost")


@pytest.mark.asyncio
async def test_inmem_registry_load_version_match() -> None:
    reg = InMemorySkillRegistry([_make_skill("a", version="2.0.0")])
    skill = await reg.load("a", version="2.0.0")
    assert skill.frontmatter.version == "2.0.0"


@pytest.mark.asyncio
async def test_inmem_registry_load_version_mismatch_raises() -> None:
    reg = InMemorySkillRegistry([_make_skill("a", version="1.0.0")])
    with pytest.raises(KeyError, match=r"a@2\.0\.0 not found"):
        await reg.load("a", version="2.0.0")


def test_inmem_registry_duplicate_name_raises() -> None:
    with pytest.raises(ValueError, match="duplicate skill name"):
        InMemorySkillRegistry([_make_skill("dup"), _make_skill("dup")])


@pytest.mark.asyncio
async def test_inmem_registry_save_draft_raises() -> None:
    reg = InMemorySkillRegistry([_make_skill("a")])
    with pytest.raises(NotImplementedError, match="read-only"):
        await reg.save_draft("a", "md", {})


@pytest.mark.asyncio
async def test_inmem_registry_publish_raises() -> None:
    reg = InMemorySkillRegistry([_make_skill("a")])
    with pytest.raises(NotImplementedError, match="read-only"):
        await reg.publish("a")


# ---- end-to-end: Agent + skills + LLM calls load_skill ----


def test_load_skill_tool_returns_body() -> None:
    """LLM calls `load_skill` → returns skill body via SkillCatalogToolset."""
    skill = _make_skill("alpha", desc="alpha skill")
    provider = _ToolCallingProvider(skill_to_load="alpha")
    a = Agent(name="x", model=provider, skills=[skill])
    result = a.run_sync("please use alpha")
    # The scripted provider does: round 1 → load_skill(alpha), round 2 → final
    # If alpha body reached the model, it returns "got it"
    assert result.final_text == "got it"
    # Verify the tool result contained the body
    tool_calls = [e for e in result.events if e.kind == "tool_call"]
    tool_results = [e for e in result.events if e.kind == "tool_result"]
    assert tool_calls[0].payload["name"] == "load_skill"
    assert "# alpha body" in tool_results[0].payload["content"]


class _ToolCallingProvider:
    """Scripted: round 1 calls load_skill, round 2 returns final."""

    name = "tool-caller"

    def __init__(self, skill_to_load: str) -> None:
        self._target = skill_to_load
        self._round = 0

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        from agent_kit import ToolCall

        self._round += 1
        if self._round == 1:
            return LlmResponse(
                text="", tool_calls=[
                    ToolCall(id="c1", name="load_skill",
                              arguments={"name": self._target}),
                ],
                usage={}, raw={}, finish_reason="tool_calls",
            )
        return LlmResponse(
            text="got it", tool_calls=[],
            usage={}, raw={}, finish_reason="stop",
        )

    async def chat_stream(self, *a, **k):
        raise NotImplementedError
