"""Build the Agent for the TUI sample.

Wires:
- **Qwen3.6-Plus** via DashScope's OpenAI-compatible endpoint(默认)
- **Anthropic skill-creator** skill, loaded from `app/skills/`
- **Aliyun Bailian WebSearch** MCP toolset (streamable HTTP, env-substituted
  Authorization header)

Single `DASHSCOPE_API_KEY` powers BOTH the LLM and the WebSearch MCP.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_kit import Agent, McpToolset
from agent_kit.contrib.providers.litellm import LiteLlm
from agent_kit.provider import LlmProvider

_HERE = Path(__file__).parent

_DASHSCOPE_OPENAI_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


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


def build_agent(model: LlmProvider | str | None = None) -> Agent:
    """Construct the demo agent.

    `model` defaults to a Qwen3.6-Plus `LiteLlm` instance configured against
    DashScope's OpenAI-compatible endpoint. Pass a string (LiteLLM model id)
    or your own `LlmProvider` to override.
    """
    return Agent(
        name="research-assistant",
        model=model if model is not None else _build_qwen_llm(),
        instruction=INSTRUCTION,
        skills=_HERE / "skills",       # → FilesystemSkillRegistry
        tools=[_build_websearch_mcp()],
        default_max_rounds=12,
    )


def _build_qwen_llm() -> LiteLlm:
    """Qwen3.6-Plus via DashScope's OpenAI-compatible endpoint.

    Same shape as the user's reference snippet:

        client = OpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        client.chat.completions.create(
            model="qwen3.6-plus",
            extra_body={"enable_thinking": ..., "thinking_budget": ...},
            ...
        )

    LiteLLM's `openai/<model>` prefix + `api_base` lets us reuse the same
    OpenAI-compatible wire format. Thinking mode is opt-in via env vars so
    the default "agent loop" path stays low-latency:

        QWEN_THINKING=1  enable_thinking=true
        QWEN_THINKING_BUDGET=4000  thinking_budget tokens (default 4000)
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise KeyError(
            "DASHSCOPE_API_KEY env var is required for the default Qwen LLM "
            "(also used by the WebSearch MCP). See samples/agent-tui/.env.example."
        )

    model = os.environ.get("QWEN_MODEL", "qwen3.6-plus")
    extra_body: dict[str, Any] = {}
    if os.environ.get("QWEN_THINKING") in {"1", "true", "True", "yes"}:
        extra_body["enable_thinking"] = True
        extra_body["thinking_budget"] = int(
            os.environ.get("QWEN_THINKING_BUDGET", "4000")
        )

    kwargs: dict[str, Any] = {
        "api_base": _DASHSCOPE_OPENAI_BASE,
        "api_key": api_key,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body
    return LiteLlm(f"openai/{model}", **kwargs)


def _build_websearch_mcp() -> McpToolset:
    """Aliyun Bailian WebSearch MCP — streamable HTTP transport.

    Requires `DASHSCOPE_API_KEY` env var; substituted into Authorization header.
    """
    return McpToolset.http(
        "websearch",
        url="https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp",
        headers={"Authorization": "Bearer ${DASHSCOPE_API_KEY}"},
        # ${VAR} is filled from os.environ at construction time (see spec § 7).
    )
