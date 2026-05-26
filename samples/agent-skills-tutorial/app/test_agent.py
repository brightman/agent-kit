"""Offline smoke test — verifies the sample's wiring without hitting a real LLM.

Run from this directory:
    PYTHONPATH=../.. python -m pytest app/test_agent.py -v
"""

from __future__ import annotations

import json

import pytest

from agent_kit import ToolCall
from agent_kit.provider import LlmResponse

from .agent import build_agent, registry


@pytest.mark.asyncio
async def test_registry_has_all_four_skills() -> None:
    fms = await registry.list()
    names = {fm.name for fm in fms}
    assert names == {
        "seo-checklist",          # inline
        "blog-writer",            # file-based
        "content-research-writer",  # "external" (file-based)
        "skill-creator",          # meta (inline + embedded files)
    }


@pytest.mark.asyncio
async def test_skill_creator_has_embedded_reference_files() -> None:
    sc = await registry.load("skill-creator")
    assert set(sc.files) == {
        "references/skill-spec.md",
        "references/example-skill.md",
    }
    assert b"SKILL.md Format" in sc.files["references/skill-spec.md"]


@pytest.mark.asyncio
async def test_blog_writer_loads_files_from_disk() -> None:
    bw = await registry.load("blog-writer")
    assert "references/style-guide.md" in bw.files
    assert b"Blog Writing Style Guide" in bw.files["references/style-guide.md"]


# ---- end-to-end with a scripted provider (no API key needed) ----


class _ScriptedProvider:
    """Drives a 3-round conversation:
    round 1 → list_skills
    round 2 → load_skill(name='skill-creator')
    round 3 → load_skill_resource(name='skill-creator', path='references/skill-spec.md')
    round 4 → final answer
    """

    name = "scripted"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._round = 0

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.calls.append({
            "round": self._round,
            "messages": list(messages),
            "tool_names": sorted(t.name for t in (tools or [])),
        })
        self._round += 1
        if self._round == 1:
            return _tool_call("c1", "list_skills", {})
        if self._round == 2:
            return _tool_call("c2", "load_skill", {"name": "skill-creator"})
        if self._round == 3:
            return _tool_call(
                "c3", "load_skill_resource",
                {"name": "skill-creator", "path": "references/skill-spec.md"},
            )
        return LlmResponse(
            text="here is your new SKILL.md draft",
            tool_calls=[], usage={}, raw={}, finish_reason="stop",
        )

    async def chat_stream(self, *a, **k):
        raise NotImplementedError


def _tool_call(call_id: str, name: str, args: dict) -> LlmResponse:
    return LlmResponse(
        text="", tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        usage={}, raw={}, finish_reason="tool_calls",
    )


def test_three_catalog_tools_are_advertised() -> None:
    """The catalog toolset wires exactly the three standard tools."""
    provider = _ScriptedProvider()
    agent = build_agent(model=provider)
    agent.run_sync("noop")
    assert provider.calls[0]["tool_names"] == [
        "list_skills", "load_skill", "load_skill_resource",
    ]


def test_progressive_disclosure_l1_l2_l3_flow() -> None:
    """Full L1 → L2 → L3 progression returns real data at each step."""
    provider = _ScriptedProvider()
    agent = build_agent(model=provider)
    result = agent.run_sync(
        "Create a new skill for Python security review.",
        max_rounds=8,
    )

    assert result.error is None
    assert result.final_text == "here is your new SKILL.md draft"

    # Inspect tool_result events for each L-tier
    results_by_call = {
        e.payload["call_id"]: e.payload["content"]
        for e in result.events if e.kind == "tool_result"
    }

    # L1: list_skills returns JSON with all four skills
    l1 = json.loads(results_by_call["c1"])
    assert {s["name"] for s in l1} == {
        "seo-checklist", "blog-writer", "content-research-writer", "skill-creator",
    }

    # L2: load_skill returns the body of skill-creator (instructions)
    assert "Skill Creator Instructions" in results_by_call["c2"]

    # L3: load_skill_resource returns the embedded spec
    assert "SKILL.md Format" in results_by_call["c3"]


def test_prelude_lists_all_skills_by_default() -> None:
    """No explicit `enabled_skills` → all four skills appear in the prelude."""
    provider = _ScriptedProvider()
    agent = build_agent(model=provider)
    agent.run_sync("noop")
    sys_msg = next(
        (m.content for m in provider.calls[0]["messages"] if m.role == "system"),
        "",
    )
    for name in ("seo-checklist", "blog-writer",
                  "content-research-writer", "skill-creator"):
        assert f"{name} (v1.0):" in sys_msg
