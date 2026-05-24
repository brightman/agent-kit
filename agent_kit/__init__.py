"""agent-kit: minimal agent loop + skill + MCP toolkit.

公开接口固定在本模块导出;子模块视为内部组织,不保证稳定性。
"""

from __future__ import annotations

__version__ = "0.0.0"

from .context import ContextCompactor, TruncatingCompactor
from .hooks import Hook
from .loop import AgentLoop, RunRequest
from .mcp import McpServerConfig, McpToolset, toolsets_from_configs
from .provider import LlmDelta, LlmProvider, LlmResponse, ToolSchema
from .runner import Runner, RunResult
from .skill import (
    Skill,
    SkillCatalogToolset,
    SkillFrontmatter,
    SkillRegistry,
    parse_frontmatter,
    parse_skill_ref,
)
from .toolset import BaseToolset, ToolCallContext, ToolsetRouter
from .types import Event, EventKind, Message, Role, ToolCall, ToolResult

__all__ = [
    "__version__",
    # core types
    "Event",
    "EventKind",
    "Message",
    "Role",
    "ToolCall",
    "ToolResult",
    # provider
    "LlmDelta",
    "LlmProvider",
    "LlmResponse",
    "ToolSchema",
    # toolset
    "BaseToolset",
    "ToolCallContext",
    "ToolsetRouter",
    # skill
    "Skill",
    "SkillCatalogToolset",
    "SkillFrontmatter",
    "SkillRegistry",
    "parse_frontmatter",
    "parse_skill_ref",
    # context / hooks
    "ContextCompactor",
    "TruncatingCompactor",
    "Hook",
    # mcp
    "McpServerConfig",
    "McpToolset",
    "toolsets_from_configs",
    # loop + runner
    "AgentLoop",
    "RunRequest",
    "Runner",
    "RunResult",
]
