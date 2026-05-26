"""Build the Agent for the TUI sample.

Wires:
- LLM provider via LiteLLM string ("gemini/gemini-2.5-flash" by default)
- **Anthropic skill-creator** skill, loaded from `app/skills/`
- **Aliyun Bailian WebSearch** MCP toolset (streamable HTTP, env-substituted
  Authorization header)
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_kit import Agent, McpToolset

_HERE = Path(__file__).parent


INSTRUCTION = (
    "You are a research + skill-creation assistant.\n\n"
    "You have two capabilities:\n"
    "1. **WebSearch** (MCP) — real-time web search via Aliyun Bailian.\n"
    "   Use `mcp__websearch__*` tools when the user asks a factual /\n"
    "   time-sensitive question or wants links / citations.\n"
    "2. **skill-creator** (skill) — the official Anthropic skill for\n"
    "   designing new SKILL.md files. Use `load_skill(\"skill-creator\")`\n"
    "   when the user wants to create / refine / evaluate a skill.\n\n"
    "Workflow rules:\n"
    "- For search tasks: search first, cite URLs, summarize in 3-5 bullets.\n"
    "- For skill tasks: load the skill, then use `load_skill_resource` to\n"
    "  read schemas + agent definitions before drafting.\n"
    "- Always announce briefly which tool / skill you're about to use.\n"
)


def build_agent(model: str | None = None) -> Agent:
    """Construct the demo agent.

    `model` defaults to `$MODEL` env var, else `gemini/gemini-2.5-flash`.
    """
    return Agent(
        name="research-assistant",
        model=model or os.environ.get("MODEL", "gemini/gemini-2.5-flash"),
        instruction=INSTRUCTION,
        skills=_HERE / "skills",       # → FilesystemSkillRegistry
        tools=[_build_websearch_mcp()],
        default_max_rounds=12,
    )


def _build_websearch_mcp() -> McpToolset:
    """Aliyun Bailian WebSearch MCP — streamable HTTP transport.

    Requires `DASHSCOPE_API_KEY` env var; substituted into Authorization header.
    """
    return McpToolset.http(
        "websearch",
        url="https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp",
        headers={"Authorization": "Bearer ${DASHSCOPE_API_KEY}"},
        # ${VAR} is filled from os.environ at construction time (see spec § 7);
        # `secrets=` would override but env is enough here.
    )
