"""Coding-agent sample — Agent + SandboxToolset(StubRunner) wired together.

Demonstrates the Stage B frozen API: any SandboxRunner implementation drops
into SandboxToolset, which exposes 3 LLM-facing tools (`exec_command` /
`read_file` / `write_file`). The LLM never sees backend-specific names.

Stage C swaps StubRunner for LocalDirRunner with one import change.
"""

from __future__ import annotations

import os

from agent_kit import Agent

from agent_kit.contrib.sandbox import SandboxToolset

from ._stub import DEFAULT_COMMANDS, StubRunner

INSTRUCTION = (
    "You are a coding agent with a sandbox workspace.\n\n"
    "You have three tools:\n"
    "- `exec_command(cmd, cwd?, env?, timeout?)` — run a command (cmd is a list)\n"
    "- `read_file(path)` — read a file from the workspace\n"
    "- `write_file(path, content)` — write a file in the workspace\n\n"
    "Workflow:\n"
    "1. Inspect the task file (e.g. `read_file(\"task.md\")`)\n"
    "2. Investigate the codebase with `exec_command([\"ls\"])` / `read_file`\n"
    "3. Make changes with `write_file`\n"
    "4. Verify with `exec_command([\"pytest\"])` or similar\n"
    "5. Summarize what you did"
)


def build_agent(
    *,
    model: str | None = None,
    seed_files: dict[str, bytes] | None = None,
) -> Agent:
    """Construct a coding agent.

    `seed_files` seeds the StubRunner's in-memory workspace, e.g.
    `{"task.md": b"Fix bug X."}`. Defaults to a small demo set.
    """
    seed = seed_files if seed_files is not None else _default_seed()
    runner = StubRunner(files=seed, commands=DEFAULT_COMMANDS)
    return Agent(
        name="coding-agent",
        model=model or os.environ.get("MODEL", "gemini/gemini-2.5-flash"),
        instruction=INSTRUCTION,
        tools=[SandboxToolset(runner)],
        default_max_rounds=12,
    )


def _default_seed() -> dict[str, bytes]:
    return {
        "task.md": b"Read README.md and src/main.py, summarize the project,"
                   b" then write your summary to notes.md.",
        "README.md": b"# Coding Agent Sample\n\n"
                     b"A demo project for the agent-kit sandbox API.\n",
        "src/main.py": b"def hello():\n    return 'world'\n",
    }
