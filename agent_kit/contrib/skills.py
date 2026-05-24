"""文件系统 backed `SkillRegistry` reference 实现。

Layout 约定:

    <skills_root>/
        <skill_dir>/
            SKILL.md       # required;frontmatter 至少 name + description
            <other files>  # 任意辅助文件,跟 SKILL.md 同包,被 load() 全量打包

特性:
- **读 only**:`save_draft` / `publish` 抛 `NotImplementedError`。真要编辑用
  db-backed registry(baizhi-agent / fam-runtime 各自实现)
- **单租户**:`tenant_id` 参数被忽略;所有 caller 看到同样的 skill 集
- **版本**:每个 skill 目录代表"latest";`load(version=X)` 命中 frontmatter.version
  就 OK,不匹配 / 缺省版本字段(默认 "0.0.0")时给具体 version 直接 KeyError
- **扫描**:首次 `list` / `load` 触发,后续缓存。`invalidate()` 重新扫
- **子目录里没 SKILL.md**:跳过(允许 NOTICE.md / README 等顶层散件混在 skills_root)
- **frontmatter.name 跟目录名不一致**:**以 frontmatter 为准**(spec § 6.1 contract)
- **重名 skill**:扫描期 raise `ValueError`(fail-fast,使用方应该自己保证唯一)

跟 `SkillRegistry` ABC 的关系:
- reference 实现,不是唯一实现
- baizhi / fam 真持久层(db / 远程 catalog)继续 implement ABC,**不**通过 contrib
"""

from __future__ import annotations

from pathlib import Path

from ..skill import (
    Skill,
    SkillFrontmatter,
    SkillRegistry,
    parse_frontmatter,
)


class FilesystemSkillRegistry(SkillRegistry):
    """读 only,扫 `<skills_root>/<skill_dir>/SKILL.md` 的 reference 持久层。"""

    def __init__(
        self,
        skills_root: Path,
        *,
        storage_root: Path | None = None,
    ) -> None:
        skills_root = Path(skills_root)
        if not skills_root.exists():
            raise FileNotFoundError(f"skills_root does not exist: {skills_root}")
        if not skills_root.is_dir():
            raise NotADirectoryError(f"skills_root is not a directory: {skills_root}")
        self._skills_root = skills_root
        self._storage_root = Path(storage_root) if storage_root else (
            Path("./persistent/skills")
        )
        self._cache: dict[str, _CachedEntry] | None = None

    def invalidate(self) -> None:
        """清缓存,下次 list / load 重新扫描文件系统。"""
        self._cache = None

    async def list(self, tenant_id: str) -> list[SkillFrontmatter]:
        cache = self._ensure_scanned()
        return [entry.frontmatter for entry in cache.values()]

    async def load(
        self, tenant_id: str, name: str, version: str | None = None
    ) -> Skill:
        cache = self._ensure_scanned()
        entry = cache.get(name)
        if entry is None:
            raise KeyError(name)
        if version is not None and entry.frontmatter.version != version:
            raise KeyError(
                f"{name}@{version} not found (registry has "
                f"{name}@{entry.frontmatter.version})"
            )
        # 读 files —— 不缓存 bytes,避免持有大对象;每次 load 现读
        files: dict[str, bytes] = {}
        for path in entry.skill_dir.rglob("*"):
            if path.is_file() and path.name != "SKILL.md":
                rel = path.relative_to(entry.skill_dir).as_posix()
                files[rel] = path.read_bytes()
        return Skill(
            name=entry.frontmatter.name,
            frontmatter=entry.frontmatter,
            body=entry.body,
            files=files,
            storage_root=self._storage_root / entry.frontmatter.name,
        )

    async def save_draft(
        self, tenant_id: str, name: str, md: str, files: dict[str, bytes]
    ) -> None:
        raise NotImplementedError(
            "FilesystemSkillRegistry is read-only; use a db-backed registry "
            "for editable skills."
        )

    async def publish(self, tenant_id: str, name: str) -> str:
        raise NotImplementedError(
            "FilesystemSkillRegistry is read-only; use a db-backed registry "
            "for editable skills."
        )

    # ---- internal ----

    def _ensure_scanned(self) -> dict[str, "_CachedEntry"]:
        if self._cache is not None:
            return self._cache
        cache: dict[str, _CachedEntry] = {}
        for child in sorted(self._skills_root.iterdir()):
            if not child.is_dir():
                continue
            md_path = child / "SKILL.md"
            if not md_path.exists():
                continue
            md_text = md_path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(md_text)
            if fm.name in cache:
                raise ValueError(
                    f"duplicate skill name {fm.name!r}: "
                    f"{cache[fm.name].skill_dir} and {child}"
                )
            cache[fm.name] = _CachedEntry(
                skill_dir=child, frontmatter=fm, body=body
            )
        self._cache = cache
        return cache


class _CachedEntry:
    """扫描结果缓存项 —— 只持 metadata,不持 files bytes(load 现读)。"""

    __slots__ = ("skill_dir", "frontmatter", "body")

    def __init__(
        self, skill_dir: Path, frontmatter: SkillFrontmatter, body: str
    ) -> None:
        self.skill_dir = skill_dir
        self.frontmatter = frontmatter
        self.body = body


__all__ = ["FilesystemSkillRegistry"]
