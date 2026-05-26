"""tests/test_skill.py — Stage 1 skill module contracts."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from agent_kit.skill import (
    Skill,
    SkillCatalogToolset,
    SkillFrontmatter,
    SkillRegistry,
    parse_frontmatter,
    parse_skill_ref,
)
from agent_kit.toolset import ToolCallContext
from agent_kit.types import ToolCall


# ---- parse_skill_ref ----


def test_parse_ref_name_only() -> None:
    assert parse_skill_ref("paper_review") == ("paper_review", None)


def test_parse_ref_name_and_version() -> None:
    assert parse_skill_ref("paper_review@1.2.3") == ("paper_review", "1.2.3")


def test_parse_ref_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_skill_ref("")


def test_parse_ref_missing_version_raises() -> None:
    with pytest.raises(ValueError, match="missing version"):
        parse_skill_ref("paper_review@")


def test_parse_ref_missing_name_raises() -> None:
    with pytest.raises(ValueError, match="missing name"):
        parse_skill_ref("@1.0.0")


# ---- parse_frontmatter ----


MINIMAL_MD = """---
name: hello
description: a hello skill
version: 1.0.0
---

# Body

This is the skill body.
"""


def test_parse_minimal() -> None:
    fm, body = parse_frontmatter(MINIMAL_MD)
    assert fm.name == "hello"
    assert fm.description == "a hello skill"
    assert fm.version == "1.0.0"
    assert body.startswith("# Body")


def test_parse_with_optional_fields() -> None:
    md = """---
name: paper_review
description: scores papers
version: 2.0.0
tools:
  - skill_storage
  - mcp__github
inputs:
  type: object
  properties:
    paper_url:
      type: string
---

body content
"""
    fm, body = parse_frontmatter(md)
    assert fm.tools == ("skill_storage", "mcp__github")
    assert fm.inputs is not None
    assert fm.inputs["type"] == "object"
    assert body == "body content\n"


def test_parse_missing_opening_raises() -> None:
    with pytest.raises(ValueError, match="frontmatter delimiter"):
        parse_frontmatter("no frontmatter here")


def test_parse_missing_closing_raises() -> None:
    with pytest.raises(ValueError, match="missing closing"):
        parse_frontmatter("---\nname: x\ndescription: y\nversion: 1\n")


def test_parse_missing_version_defaults_for_bundled_skills() -> None:
    md = """---
name: x
description: bundled skill without explicit version
---

body
"""
    fm, body = parse_frontmatter(md)
    assert fm.version == "0.0.0"
    assert body == "body\n"


def test_parse_yaml_not_mapping_raises() -> None:
    md = "---\n- a\n- b\n---\n\nbody"
    with pytest.raises(ValueError, match="must be YAML mapping"):
        parse_frontmatter(md)


def test_parse_frontmatter_immutable() -> None:
    fm, _ = parse_frontmatter(MINIMAL_MD)
    with pytest.raises(Exception):
        fm.name = "other"  # type: ignore[misc]


# ---- SkillRegistry stub for testing ----


class _FakeRegistry(SkillRegistry):
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def add(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    async def list(self):
        return [s.frontmatter for s in self._skills.values()]

    async def load(self, name: str, version: str | None = None):
        if name not in self._skills:
            raise KeyError(name)
        return self._skills[name]

    async def save_draft(self, name, md, files): ...
    async def publish(self, name) -> str:
        return "0"


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        run_id="r1",
        cancel=asyncio.Event(), workspace=Path("/tmp"),
        emit=lambda evt: None,
    )


def _make_skill(name: str, body: str = "body", files: dict[str, bytes] | None = None) -> Skill:
    fm = SkillFrontmatter(name=name, description=f"{name} skill", version="1.0.0")
    return Skill(
        name=name, frontmatter=fm, body=body,
        files=files or {}, storage_root=Path("/tmp"),
    )


# ---- SkillCatalogToolset ----


def test_catalog_build_schemas_three_tools() -> None:
    reg = _FakeRegistry()
    ts = SkillCatalogToolset(reg)
    names = [s.name for s in ts.build_schemas()]
    assert set(names) == {"list_skills", "load_skill", "load_skill_resource"}


def test_catalog_name() -> None:
    ts = SkillCatalogToolset(_FakeRegistry())
    assert ts.name == "skill_catalog"


@pytest.mark.asyncio
async def test_catalog_list_skills_returns_json() -> None:
    reg = _FakeRegistry()
    reg.add(_make_skill("a"))
    reg.add(_make_skill("b"))
    ts = SkillCatalogToolset(reg)
    r = await ts.execute(ToolCall(id="1", name="list_skills", arguments={}), _ctx())
    assert not r.is_error
    items = json.loads(r.content)
    assert {i["name"] for i in items} == {"a", "b"}
    assert all("description" in i and "version" in i for i in items)


@pytest.mark.asyncio
async def test_catalog_load_skill_returns_body() -> None:
    reg = _FakeRegistry()
    reg.add(_make_skill("hello", body="# Hello\n\nThis is hello."))
    ts = SkillCatalogToolset(reg)
    r = await ts.execute(
        ToolCall(id="1", name="load_skill", arguments={"name": "hello"}), _ctx()
    )
    assert not r.is_error
    assert r.content == "# Hello\n\nThis is hello."


@pytest.mark.asyncio
async def test_catalog_load_skill_missing_returns_error() -> None:
    reg = _FakeRegistry()
    ts = SkillCatalogToolset(reg)
    r = await ts.execute(
        ToolCall(id="1", name="load_skill", arguments={"name": "nope"}), _ctx()
    )
    assert r.is_error
    assert "not found" in r.content


@pytest.mark.asyncio
async def test_catalog_load_skill_missing_name_arg() -> None:
    ts = SkillCatalogToolset(_FakeRegistry())
    r = await ts.execute(
        ToolCall(id="1", name="load_skill", arguments={}), _ctx()
    )
    assert r.is_error
    assert "missing 'name'" in r.content


@pytest.mark.asyncio
async def test_catalog_load_resource_text() -> None:
    reg = _FakeRegistry()
    reg.add(_make_skill("x", files={"helper.py": b"print('hi')\n"}))
    ts = SkillCatalogToolset(reg)
    r = await ts.execute(
        ToolCall(id="1", name="load_skill_resource",
                 arguments={"name": "x", "path": "helper.py"}),
        _ctx(),
    )
    assert not r.is_error
    assert r.content == "print('hi')\n"


@pytest.mark.asyncio
async def test_catalog_load_resource_binary_base64() -> None:
    reg = _FakeRegistry()
    binary = bytes([0xff, 0xfe, 0xfd, 0xfc])
    reg.add(_make_skill("x", files={"img.bin": binary}))
    ts = SkillCatalogToolset(reg)
    r = await ts.execute(
        ToolCall(id="1", name="load_skill_resource",
                 arguments={"name": "x", "path": "img.bin"}),
        _ctx(),
    )
    assert not r.is_error
    assert r.content.startswith("BASE64:")
    decoded = base64.b64decode(r.content[len("BASE64:"):])
    assert decoded == binary


@pytest.mark.asyncio
async def test_catalog_load_resource_missing_path() -> None:
    reg = _FakeRegistry()
    reg.add(_make_skill("x", files={}))
    ts = SkillCatalogToolset(reg)
    r = await ts.execute(
        ToolCall(id="1", name="load_skill_resource",
                 arguments={"name": "x", "path": "nope.py"}),
        _ctx(),
    )
    assert r.is_error
    assert "not found" in r.content


@pytest.mark.asyncio
async def test_catalog_unknown_tool_returns_error() -> None:
    ts = SkillCatalogToolset(_FakeRegistry())
    r = await ts.execute(
        ToolCall(id="1", name="unknown_tool", arguments={}), _ctx()
    )
    assert r.is_error
