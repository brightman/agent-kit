"""Skill 抽象 —— SKILL.md 是契约,不是 Python class。

设计原则(对齐 baizhi-agent GOALS.md N1):
- Skill 的"行为"靠 LLM 读 SKILL.md body 后调工具实现
- SDK 只负责:解析 frontmatter、按需暴露 body 给 LLM、给每个 skill 独立 storage 根目录
- Progressive disclosure 是默认:启动时只把 frontmatter 注入 system prompt,正文按需 load_skill

SkillCatalogToolset 是内置 toolset,提供三个工具:
- list_skills() —— 已经在 system prompt 里给了 frontmatter,这里返回更结构化的元数据
- load_skill(name) —— 返回完整 SKILL.md body
- load_skill_resource(name, path) —— 返回 skill 包内附带的辅助文件
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .provider import ToolSchema
from .toolset import BaseToolset, ToolCallContext
from .types import ToolCall, ToolResult


@dataclass
class SkillFrontmatter:
    """SKILL.md 头部 YAML 解析结果。"""

    name: str
    description: str
    version: str
    tools: list[str] = field(default_factory=list)
    inputs: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    """完整的 skill 包。"""

    name: str
    frontmatter: SkillFrontmatter
    body: str                        # SKILL.md 去掉 frontmatter 后的正文
    files: dict[str, bytes]          # 同包附带的辅助文件
    storage_root: Path               # persistent/skills/<name>/ 或等价路径


class SkillRegistry(ABC):
    """skill 持久层的统一接口。baizhi-agent / fam-runtime 各自实现。"""

    @abstractmethod
    async def list(self, tenant_id: str) -> list[SkillFrontmatter]: ...

    @abstractmethod
    async def load(self, tenant_id: str, name: str) -> Skill: ...

    @abstractmethod
    async def save_draft(
        self, tenant_id: str, name: str, md: str, files: dict[str, bytes]
    ) -> None: ...

    @abstractmethod
    async def publish(self, tenant_id: str, name: str) -> str:
        """publish draft → immutable version,返回新版本号。"""


def parse_frontmatter(md: str) -> tuple[SkillFrontmatter, str]:
    """解析 SKILL.md,返回 (frontmatter, body)。stub —— 实现见后续 PR。"""
    raise NotImplementedError


class SkillCatalogToolset(BaseToolset):
    """内置 toolset,把 SkillRegistry 暴露成 list_skills / load_skill / load_skill_resource。"""

    name = "skill_catalog"

    def __init__(self, registry: SkillRegistry, tenant_id: str) -> None:
        self._registry = registry
        self._tenant_id = tenant_id

    def build_schemas(self) -> list[ToolSchema]:
        raise NotImplementedError

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        raise NotImplementedError


__all__ = [
    "SkillFrontmatter",
    "Skill",
    "SkillRegistry",
    "SkillCatalogToolset",
    "parse_frontmatter",
]
