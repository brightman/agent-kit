"""tests/contrib/test_skills.py — FilesystemSkillRegistry behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_kit.contrib.skills import FilesystemSkillRegistry
from agent_kit.skill import Skill


# ---- helpers ----


def _make_skill_dir(
    root: Path,
    dirname: str,
    *,
    name: str | None = None,
    version: str | None = "1.0.0",
    description: str = "a test skill",
    extra_files: dict[str, str | bytes] | None = None,
) -> Path:
    """Create <root>/<dirname>/SKILL.md + any extra_files (relpath → content)."""
    name = name or dirname
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)
    if version is None:
        fm = f"---\nname: {name}\ndescription: {description}\n---\n"
    else:
        fm = (
            f"---\nname: {name}\ndescription: {description}\n"
            f"version: {version}\n---\n"
        )
    (skill_dir / "SKILL.md").write_text(fm + "# body\n", encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            target.write_bytes(content)
    return skill_dir


# ---- construction ----


def test_init_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FilesystemSkillRegistry(tmp_path / "nope")


def test_init_rejects_root_that_is_a_file(tmp_path: Path) -> None:
    file = tmp_path / "x.txt"
    file.write_text("x")
    with pytest.raises(NotADirectoryError):
        FilesystemSkillRegistry(file)


# ---- list ----


@pytest.mark.asyncio
async def test_list_empty(tmp_path: Path) -> None:
    reg = FilesystemSkillRegistry(tmp_path)
    assert await reg.list() == []


@pytest.mark.asyncio
async def test_list_single(tmp_path: Path) -> None:
    _make_skill_dir(tmp_path, "alpha", description="d")
    reg = FilesystemSkillRegistry(tmp_path)
    fms = await reg.list()
    assert len(fms) == 1
    assert fms[0].name == "alpha"
    assert fms[0].description == "d"
    assert fms[0].version == "1.0.0"


@pytest.mark.asyncio
async def test_list_multiple_sorted(tmp_path: Path) -> None:
    _make_skill_dir(tmp_path, "gamma")
    _make_skill_dir(tmp_path, "alpha")
    _make_skill_dir(tmp_path, "beta")
    reg = FilesystemSkillRegistry(tmp_path)
    names = [fm.name for fm in await reg.list()]
    # sorted iterdir → deterministic alpha, beta, gamma
    assert names == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_list_skips_non_skill_dirs_and_files(tmp_path: Path) -> None:
    """NOTICE.md / README and child dirs without SKILL.md are ignored."""
    _make_skill_dir(tmp_path, "real_skill")
    (tmp_path / "NOTICE.md").write_text("license blurb")
    (tmp_path / "README.md").write_text("readme")
    (tmp_path / "empty_dir").mkdir()
    (tmp_path / "almost_skill").mkdir()
    (tmp_path / "almost_skill" / "other.txt").write_text("no SKILL.md")
    reg = FilesystemSkillRegistry(tmp_path)
    names = [fm.name for fm in await reg.list()]
    assert names == ["real_skill"]


@pytest.mark.asyncio
async def test_list_skill_name_from_frontmatter_not_dirname(tmp_path: Path) -> None:
    """spec § 6.1: frontmatter wins over dir name."""
    _make_skill_dir(tmp_path, "dir-name-x", name="frontmatter_name_y")
    reg = FilesystemSkillRegistry(tmp_path)
    fms = await reg.list()
    assert [fm.name for fm in fms] == ["frontmatter_name_y"]


@pytest.mark.asyncio
async def test_duplicate_skill_name_raises(tmp_path: Path) -> None:
    _make_skill_dir(tmp_path, "a", name="dup")
    _make_skill_dir(tmp_path, "b", name="dup")
    reg = FilesystemSkillRegistry(tmp_path)
    with pytest.raises(ValueError, match="duplicate skill name 'dup'"):
        await reg.list()


# ---- load ----


@pytest.mark.asyncio
async def test_load_returns_skill_with_files(tmp_path: Path) -> None:
    _make_skill_dir(
        tmp_path,
        "pptx",
        extra_files={
            "templates/title.pptx": b"\x50\x4b\x03\x04binary",
            "scripts/render.py": "print('hi')\n",
            "deep/nested/data.json": '{"k": 1}',
        },
    )
    reg = FilesystemSkillRegistry(tmp_path)
    skill = await reg.load('pptx')
    assert isinstance(skill, Skill)
    assert skill.name == "pptx"
    assert skill.body.strip() == "# body"
    assert set(skill.files.keys()) == {
        "templates/title.pptx",
        "scripts/render.py",
        "deep/nested/data.json",
    }
    assert skill.files["templates/title.pptx"].startswith(b"\x50\x4b")
    assert skill.files["scripts/render.py"] == b"print('hi')\n"


@pytest.mark.asyncio
async def test_load_excludes_skill_md(tmp_path: Path) -> None:
    _make_skill_dir(tmp_path, "x", extra_files={"a.txt": "a"})
    reg = FilesystemSkillRegistry(tmp_path)
    skill = await reg.load('x')
    assert "SKILL.md" not in skill.files
    assert skill.files == {"a.txt": b"a"}


@pytest.mark.asyncio
async def test_load_unknown_skill_raises_keyerror(tmp_path: Path) -> None:
    _make_skill_dir(tmp_path, "alpha")
    reg = FilesystemSkillRegistry(tmp_path)
    with pytest.raises(KeyError, match="ghost"):
        await reg.load('ghost')


@pytest.mark.asyncio
async def test_load_version_match(tmp_path: Path) -> None:
    _make_skill_dir(tmp_path, "v", version="2.0.0")
    reg = FilesystemSkillRegistry(tmp_path)
    skill = await reg.load("v", version="2.0.0")
    assert skill.frontmatter.version == "2.0.0"


@pytest.mark.asyncio
async def test_load_version_mismatch_raises(tmp_path: Path) -> None:
    _make_skill_dir(tmp_path, "v", version="2.0.0")
    reg = FilesystemSkillRegistry(tmp_path)
    with pytest.raises(KeyError, match=r"v@9\.9\.9 not found.*v@2\.0\.0"):
        await reg.load("v", version="9.9.9")


@pytest.mark.asyncio
async def test_load_version_none_returns_latest(tmp_path: Path) -> None:
    """No version pin → don't care about version, return whatever's there."""
    _make_skill_dir(tmp_path, "v", version="2.0.0")
    reg = FilesystemSkillRegistry(tmp_path)
    skill = await reg.load('v')  # version=None
    assert skill.frontmatter.version == "2.0.0"


@pytest.mark.asyncio
async def test_load_skill_without_version_field(tmp_path: Path) -> None:
    """SKILL.md without explicit version: defaults to '0.0.0' (skill.py contract)."""
    _make_skill_dir(tmp_path, "v", version=None)
    reg = FilesystemSkillRegistry(tmp_path)
    skill = await reg.load('v')
    assert skill.frontmatter.version == "0.0.0"


@pytest.mark.asyncio
async def test_load_storage_root_per_skill(tmp_path: Path) -> None:
    """storage_root in Skill is `<registry storage_root>/<skill name>`."""
    _make_skill_dir(tmp_path, "x", name="my_skill")
    reg = FilesystemSkillRegistry(tmp_path, storage_root=tmp_path / "persist")
    skill = await reg.load('my_skill')
    assert skill.storage_root == tmp_path / "persist" / "my_skill"


# ---- write-side / cache ----


@pytest.mark.asyncio
async def test_save_draft_raises(tmp_path: Path) -> None:
    reg = FilesystemSkillRegistry(tmp_path)
    with pytest.raises(NotImplementedError, match="read-only"):
        await reg.save_draft("x", "---\nname: x\ndescription: y\n---\n", {})


@pytest.mark.asyncio
async def test_publish_raises(tmp_path: Path) -> None:
    reg = FilesystemSkillRegistry(tmp_path)
    with pytest.raises(NotImplementedError, match="read-only"):
        await reg.publish("x")


@pytest.mark.asyncio
async def test_invalidate_picks_up_new_skill(tmp_path: Path) -> None:
    _make_skill_dir(tmp_path, "alpha")
    reg = FilesystemSkillRegistry(tmp_path)
    assert [fm.name for fm in await reg.list()] == ["alpha"]
    # add a new skill, registry caches → still 1
    _make_skill_dir(tmp_path, "beta")
    assert [fm.name for fm in await reg.list()] == ["alpha"]
    # invalidate → 2
    reg.invalidate()
    assert [fm.name for fm in await reg.list()] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_list_is_stable_across_calls(tmp_path: Path) -> None:
    """Same registry, multiple list() calls → same result(cache stable)."""
    _make_skill_dir(tmp_path, "shared")
    reg = FilesystemSkillRegistry(tmp_path)
    a = await reg.list()
    b = await reg.list()
    assert [fm.name for fm in a] == [fm.name for fm in b] == ["shared"]
