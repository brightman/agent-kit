"""Skill 抽象 —— SKILL.md 是契约,不是 Python class。

设计原则(对齐 baizhi-agent GOALS.md N1):
- Skill 的"行为"靠 LLM 读 SKILL.md body 后调工具实现
- SDK 只负责:解析 frontmatter、按需暴露 body 给 LLM、给每个 skill 独立 storage 根目录
- Progressive disclosure 是默认:启动时只把 frontmatter 注入 system prompt,正文按需 load_skill

SkillCatalogToolset 是内置 toolset,提供三个工具:
- list_skills() —— 已经在 system prompt 里给了 frontmatter,这里返回更结构化的元数据
- load_skill(name, version?) —— 返回完整 SKILL.md body
- load_skill_resource(name, path, version?) —— 返回 skill 包内附带的辅助文件
"""

from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .provider import ToolSchema
from .toolset import BaseToolset, ToolCallContext
from .types import ToolCall, ToolResult


@dataclass(frozen=True)
class SkillFrontmatter:
    """SKILL.md 头部 YAML 解析结果。"""

    name: str
    description: str
    version: str
    tools: tuple[str, ...] = ()
    inputs: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
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
    async def load(
        self, tenant_id: str, name: str, version: str | None = None
    ) -> Skill:
        """Q2 决议:version=None 拿 latest;给具体值拿 immutable publish。
        不存在 → raise KeyError。"""

    @abstractmethod
    async def save_draft(
        self, tenant_id: str, name: str, md: str, files: dict[str, bytes]
    ) -> None: ...

    @abstractmethod
    async def publish(self, tenant_id: str, name: str) -> str:
        """publish draft → immutable version,返回新版本号。
        无 draft → raise FileNotFoundError。"""


def parse_skill_ref(ref: str) -> tuple[str, str | None]:
    """解析 'name' 或 'name@version' 字符串。

    >>> parse_skill_ref("paper_review")
    ('paper_review', None)
    >>> parse_skill_ref("paper_review@1.2.3")
    ('paper_review', '1.2.3')
    """
    if not ref:
        raise ValueError("skill ref must be non-empty")
    if "@" not in ref:
        return ref, None
    name, _, version = ref.partition("@")
    if not name:
        raise ValueError(f"skill ref missing name before '@': {ref!r}")
    if not version:
        raise ValueError(f"skill ref missing version after '@': {ref!r}")
    return name, version


def parse_frontmatter(md: str) -> tuple[SkillFrontmatter, str]:
    """解析 SKILL.md,返回 (frontmatter, body)。

    格式约定:
    - 第一行 MUST 是 '---\\n'
    - 之后到下一个 '---\\n' 之间是 YAML
    - 之后 strip 前导空行后是 body

    Required frontmatter fields: name / description。
    version 可缺省,兼容现有 bundled skills;缺省为 "0.0.0"。
    """
    if not md.startswith("---"):
        raise ValueError("SKILL.md must start with '---' frontmatter delimiter")
    # split into 3: '', yaml, body
    parts = md.split("---\n", 2)
    if len(parts) < 3:
        # try '\n---\n' alternative if file uses different style
        if md.count("---\n") < 2:
            raise ValueError(
                "SKILL.md frontmatter missing closing '---'"
            )
    # 取 first '---\n' 之后到 second '---\n' 之前
    after_open = md[md.index("---\n") + 4 :]
    if "---\n" not in after_open and "---" not in after_open:
        raise ValueError("SKILL.md frontmatter missing closing '---'")
    # find closing delimiter
    close_idx = after_open.find("---\n")
    if close_idx == -1:
        # maybe last line is '---' without newline
        close_idx = after_open.find("---")
        if close_idx == -1:
            raise ValueError("SKILL.md frontmatter missing closing '---'")
        yaml_part = after_open[:close_idx]
        body_part = after_open[close_idx + len("---"):]
    else:
        yaml_part = after_open[:close_idx]
        body_part = after_open[close_idx + len("---\n"):]

    raw = yaml.safe_load(yaml_part) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"SKILL.md frontmatter must be YAML mapping, got {type(raw).__name__}")

    for required in ("name", "description"):
        if required not in raw:
            raise ValueError(f"SKILL.md frontmatter missing required field: {required!r}")

    fm = SkillFrontmatter(
        name=str(raw["name"]),
        description=str(raw["description"]),
        version=str(raw.get("version", "0.0.0")),
        tools=tuple(raw.get("tools", []) or []),
        inputs=raw.get("inputs"),
        raw=raw,
    )
    body = body_part.lstrip("\n")
    return fm, body


# --- SkillCatalogToolset ---

_TOOL_LIST_SKILLS = "list_skills"
_TOOL_LOAD_SKILL = "load_skill"
_TOOL_LOAD_SKILL_RESOURCE = "load_skill_resource"


class SkillCatalogToolset(BaseToolset):
    """暴露 list_skills / load_skill / load_skill_resource 给 LLM。

    Progressive disclosure 的运行时实现:enabled skills 的 frontmatter 已经
    在 system prompt 里给了 LLM(loop 负责);LLM 想看 body 调 load_skill。
    """

    name = "skill_catalog"

    def __init__(self, registry: SkillRegistry, tenant_id: str) -> None:
        self._registry = registry
        self._tenant_id = tenant_id

    def build_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name=_TOOL_LIST_SKILLS,
                description=(
                    "List all skills available to this tenant. Returns JSON array of "
                    "{name, description, version}."
                ),
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            ToolSchema(
                name=_TOOL_LOAD_SKILL,
                description=(
                    "Read a skill's full SKILL.md body. Use when you need the detailed "
                    "instructions for a skill, beyond what's in the system prompt."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Skill name."},
                        "version": {
                            "type": "string",
                            "description": "Pin a specific version (default: latest).",
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            ToolSchema(
                name=_TOOL_LOAD_SKILL_RESOURCE,
                description=(
                    "Read a file packaged inside a skill (script / template / config). "
                    "Returns text for text files, or base64-encoded for binary."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "path": {"type": "string", "description": "Relative path inside skill bundle."},
                        "version": {"type": "string"},
                    },
                    "required": ["name", "path"],
                    "additionalProperties": False,
                },
            ),
        ]

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        try:
            if call.name == _TOOL_LIST_SKILLS:
                return await self._list_skills(call)
            if call.name == _TOOL_LOAD_SKILL:
                return await self._load_skill(call)
            if call.name == _TOOL_LOAD_SKILL_RESOURCE:
                return await self._load_skill_resource(call)
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: SkillCatalogToolset does not own tool {call.name!r}",
                is_error=True,
            )
        except KeyError as exc:
            return ToolResult(call_id=call.id, content=f"ERROR: not found: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: {type(exc).__name__}: {exc}",
                is_error=True,
            )

    async def _list_skills(self, call: ToolCall) -> ToolResult:
        items = await self._registry.list(self._tenant_id)
        payload = [
            {"name": fm.name, "description": fm.description, "version": fm.version}
            for fm in items
        ]
        return ToolResult(call_id=call.id, content=json.dumps(payload, ensure_ascii=False))

    async def _load_skill(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        if "name" not in args:
            return ToolResult(call_id=call.id, content="ERROR: missing 'name' argument", is_error=True)
        skill = await self._registry.load(
            self._tenant_id, str(args["name"]), version=args.get("version")
        )
        return ToolResult(call_id=call.id, content=skill.body)

    async def _load_skill_resource(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        for required in ("name", "path"):
            if required not in args:
                return ToolResult(
                    call_id=call.id,
                    content=f"ERROR: missing {required!r} argument",
                    is_error=True,
                )
        skill = await self._registry.load(
            self._tenant_id, str(args["name"]), version=args.get("version")
        )
        path = str(args["path"])
        data = skill.files.get(path)
        if data is None:
            return ToolResult(
                call_id=call.id,
                content=f"ERROR: resource {path!r} not found in skill {skill.name!r}",
                is_error=True,
            )
        # Try utf-8 text first; fall back to base64
        try:
            text = data.decode("utf-8")
            return ToolResult(call_id=call.id, content=text)
        except UnicodeDecodeError:
            b64 = base64.b64encode(data).decode("ascii")
            return ToolResult(call_id=call.id, content=f"BASE64:{b64}")


__all__ = [
    "SkillFrontmatter",
    "Skill",
    "SkillRegistry",
    "SkillCatalogToolset",
    "parse_frontmatter",
    "parse_skill_ref",
]
