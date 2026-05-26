"""CLI for the coding-agent sample.

Usage:
    # one-shot
    PYTHONPATH=../.. python -m app.main "Read task.md and do as told."

    # interactive REPL
    PYTHONPATH=../.. python -m app.main

Env:
    MODEL              override default model (default: gemini/gemini-2.5-flash)
    GOOGLE_API_KEY     for Gemini (LiteLLM picks it up)
"""

from __future__ import annotations

import sys

from agent_kit import Message

from .agent import build_agent


def _trace(result) -> None:
    tool_calls = [e for e in result.events if e.kind == "tool_call"]
    if tool_calls:
        names = ", ".join(e.payload["name"].split("__")[-1] for e in tool_calls)
        print(f"[trace] tools: {names}")
    print(
        f"[trace] rounds={result.rounds_used} "
        f"cancelled={result.cancelled} error={result.error is not None}"
    )


def one_shot(prompt: str) -> int:
    agent = build_agent()
    result = agent.run_sync(prompt)
    print(result.final_text or "(no final text)")
    _trace(result)
    return 0 if result.error is None else 1


def interactive() -> int:
    agent = build_agent()
    print("Coding Agent (agent-kit). Workspace is in-memory (StubRunner).")
    print("Type a message; Ctrl-D / `quit` to exit.\n")
    history: list[Message] = []
    while True:
        try:
            line = input(">>> ").strip()
        except EOFError:
            print()
            return 0
        if not line:
            continue
        if line.lower() in {"quit", "exit", ":q"}:
            return 0
        result = agent.run_sync(line, prior_messages=history)
        text = result.final_text or "(no final text)"
        print(text)
        _trace(result)
        history.append(Message(role="user", content=line))
        history.append(Message(role="assistant", content=text))


def main() -> int:
    if len(sys.argv) > 1:
        return one_shot(" ".join(sys.argv[1:]))
    return interactive()


if __name__ == "__main__":
    raise SystemExit(main())
