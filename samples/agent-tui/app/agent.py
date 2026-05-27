"""Build the Agent for the TUI sample.

Wires:
- **Qwen3.6-Plus** via DashScope's OpenAI-compatible endpoint(默认)
- **Anthropic skill-creator** skill, loaded from `app/skills/`
- **Aliyun Bailian WebSearch** MCP toolset (streamable HTTP, env-substituted
  Authorization header)
- **Sandbox** (LocalDirRunner) — read/write files + exec shell commands in a
  **persistent workspace** at `~/.agent-tui-workspace` (override via
  `AGENT_TUI_WORKSPACE`). Lets skill-creator actually save generated
  SKILL.md files and run its built-in validation scripts.

Single `DASHSCOPE_API_KEY` powers the LLM AND the WebSearch MCP.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_kit import Agent, McpToolset
from agent_kit.contrib.providers.litellm import LiteLlm
from agent_kit.contrib.sandbox import SandboxToolset
from agent_kit.contrib.sandbox.runners import LocalDirRunner
from agent_kit.provider import LlmProvider

_HERE = Path(__file__).parent

_DASHSCOPE_OPENAI_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# Persistent workspace default: ~/.agent-tui-workspace. Override via env.
_DEFAULT_WORKSPACE = Path.home() / ".agent-tui-workspace"

# Sandbox command allowlist — exploration + Python + safe shell utilities.
# Notably absent: `rm`, `mv`, `chmod`, `curl`, `bash -c …` (allowlist is by
# binary name, so `bash -c` is blocked because `bash` isn't listed).
_SANDBOX_ALLOWLIST = [
    "ls", "cat", "head", "tail", "wc",   # read / inspect
    "grep", "rg", "find", "tree",        # search / explore
    "python", "python3",                  # run scripts (esp. skill-creator's)
]


INSTRUCTION_TMPL = (
    "You are a research + skill-creation assistant.\n\n"
    "You have THREE capability groups:\n"
    "1. **WebSearch** (MCP) — real-time web search via Aliyun Bailian.\n"
    "   Use `mcp__websearch__*` tools for factual / time-sensitive questions\n"
    "   or when the user wants links / citations.\n"
    "2. **skill-creator** (skill) — the official Anthropic skill for\n"
    "   designing new SKILL.md files. Use `load_skill(\"skill-creator\")`\n"
    "   when the user wants to create / refine / evaluate a skill.\n"
    "3. **Sandbox** (file + exec) — `sandbox__localdir__*` tools to\n"
    "   read / write files and run shell commands in your **persistent\n"
    "   workspace** at `{workspace}`. Workspace files survive across runs.\n"
    "   Allowed commands: {allowlist}.\n\n"
    "Workflow rules:\n"
    "- For search tasks: search first, cite URLs, summarize in 3-5 bullets.\n"
    "- For skill tasks: load skill-creator, read its schemas + agent\n"
    "  references, then *write* the drafted SKILL.md to disk via\n"
    "  `sandbox__localdir__write_file` so the user can inspect / iterate.\n"
    "  Use `sandbox__localdir__exec_command` with `python` to run\n"
    "  skill-creator's validation scripts (e.g.\n"
    "  `quick_validate.py <skill-dir>`) when the user asks to verify.\n"
    "- Use relative paths (workspace-rooted). Path-traversal is blocked.\n"
    "- Always announce briefly which tool / skill you're about to use.\n"
)


def build_agent(model: LlmProvider | str | None = None) -> Agent:
    """Construct the demo agent.

    `model` defaults to a Qwen3.6-Plus `LiteLlm` instance configured against
    DashScope's OpenAI-compatible endpoint. Pass a string (LiteLLM model id)
    or your own `LlmProvider` to override.
    """
    workspace_path = _resolve_workspace_path()
    workspace_path.mkdir(parents=True, exist_ok=True)

    instruction = INSTRUCTION_TMPL.format(
        workspace=workspace_path,
        allowlist=", ".join(_SANDBOX_ALLOWLIST),
    )

    return Agent(
        name="research-assistant",
        model=model if model is not None else _build_qwen_llm(),
        instruction=instruction,
        skills=_HERE / "skills",       # → FilesystemSkillRegistry
        tools=[
            _build_websearch_mcp(),
            _build_sandbox_toolset(),
        ],
        # Callable → persistent workspace, ctx.workspace_ephemeral=False.
        # Every run sees the same dir; toolsets can cache files across runs.
        workspace=lambda _req, _run_id: workspace_path,
        default_max_rounds=12,
    )


def _resolve_workspace_path() -> Path:
    """Pick the persistent workspace directory.

    Order:
    1. `$AGENT_TUI_WORKSPACE` env (absolute or relative, expanded)
    2. `~/.agent-tui-workspace` (default)
    """
    env = os.environ.get("AGENT_TUI_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return _DEFAULT_WORKSPACE


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


def _build_sandbox_toolset() -> SandboxToolset:
    """LocalDirRunner-backed sandbox: 3 tools (`exec_command`, `read_file`,
    `write_file`) operating on the persistent workspace dir.

    NO OS-level isolation — runs as the host user. Allowlist + path-traversal
    defense are the only guardrails. Trust your LLM accordingly.
    """
    return SandboxToolset(LocalDirRunner(
        command_allowlist=_SANDBOX_ALLOWLIST,
        env_passthrough=("PATH", "HOME"),
    ))
