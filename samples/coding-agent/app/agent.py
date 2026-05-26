"""Coding-agent sample — Agent + SandboxToolset wired to one of two backends.

Demonstrates the frozen sandbox API (spec § 16.3): any SandboxRunner drops
into SandboxToolset, which exposes 3 LLM-facing tools (`exec_command` /
`read_file` / `write_file`). The LLM never sees backend-specific names.

Two backends:
- "stub"     — sample-local in-memory dict (offline, no subprocess)
- "localdir" — real host subprocess via agent_kit.contrib.sandbox.runners.LocalDir
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from agent_kit import Agent
from agent_kit.contrib.sandbox import SandboxToolset
from agent_kit.contrib.sandbox.runners import LocalDirRunner

from ._stub import DEFAULT_COMMANDS, StubRunner

Backend = Literal["stub", "localdir"]

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
    backend: Backend = "stub",
    seed_files: dict[str, bytes] | None = None,
    workspace_root: Path | None = None,
) -> Agent:
    """Construct a coding agent.

    backend="stub":
        Uses in-memory StubRunner. `seed_files` populates the dict workspace.
    backend="localdir":
        Uses LocalDirRunner with a real host subprocess. `seed_files` is
        materialized via a workspace_provider that writes them to disk in
        a fresh tmpdir (or `workspace_root` if you pass one explicitly).
    """
    if backend == "stub":
        seed = seed_files if seed_files is not None else _default_seed()
        runner = StubRunner(files=seed, commands=DEFAULT_COMMANDS)
        return Agent(
            name="coding-agent",
            model=model or _default_model(),
            instruction=INSTRUCTION,
            tools=[SandboxToolset(runner)],
            default_max_rounds=12,
        )

    if backend == "localdir":
        seed = seed_files if seed_files is not None else _default_seed()
        # workspace_provider materializes seed files BEFORE the run, then
        # hands the path to LocalDirRunner.setup().
        return Agent(
            name="coding-agent",
            model=model or _default_model(),
            instruction=INSTRUCTION,
            tools=[SandboxToolset(LocalDirRunner(
                command_allowlist=[
                    "ls", "cat", "echo", "python", "python3", "pytest",
                    "grep", "head", "tail", "wc",
                ],
                env_passthrough=("PATH", "HOME"),
            ))],
            workspace_provider=_seed_provider(seed, workspace_root),
            default_max_rounds=12,
        )

    raise ValueError(f"unknown backend: {backend!r}")


def _default_model() -> str:
    return os.environ.get("MODEL", "gemini/gemini-2.5-flash")


def _default_seed() -> dict[str, bytes]:
    return {
        "task.md": b"Read README.md and src/main.py, summarize the project,"
                   b" then write your summary to notes.md.",
        "README.md": b"# Coding Agent Sample\n\n"
                     b"A demo project for the agent-kit sandbox API.\n",
        "src/main.py": b"def hello():\n    return 'world'\n",
    }


def _seed_provider(seed: dict[str, bytes], workspace_root: Path | None):
    """workspace_provider that materializes seed files into a fresh dir."""
    import tempfile

    def provider(_req, run_id):
        root = workspace_root or Path(tempfile.mkdtemp(prefix="coding-agent-"))
        ws = root / run_id if workspace_root else root
        ws.mkdir(parents=True, exist_ok=True)
        for rel, content in seed.items():
            full = ws / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(content)
        return ws

    return provider
