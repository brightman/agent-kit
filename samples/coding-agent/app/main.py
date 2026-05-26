"""CLI for the coding-agent sample.

Usage:
    # one-shot, default backend (stub, no subprocess)
    PYTHONPATH=../.. python -m app.main "Read task.md and do as told."

    # one-shot with real LocalDir backend (real subprocess, real fs)
    PYTHONPATH=../.. python -m app.main --backend localdir \\
        "Read task.md, run python on src/main.py, and summarize"

    # interactive REPL
    PYTHONPATH=../.. python -m app.main [--backend stub|localdir]

Env:
    MODEL              override default model (default: gemini/gemini-2.5-flash)
    GOOGLE_API_KEY     for Gemini (LiteLLM picks it up)
"""

from __future__ import annotations

import argparse

from agent_kit import Message

from .agent import Backend, build_agent


def _trace(result) -> None:
    tool_calls = [e for e in result.events if e.kind == "tool_call"]
    if tool_calls:
        names = ", ".join(e.payload["name"].split("__")[-1] for e in tool_calls)
        print(f"[trace] tools: {names}")
    print(
        f"[trace] rounds={result.rounds_used} "
        f"cancelled={result.cancelled} error={result.error is not None}"
    )


def one_shot(prompt: str, backend: Backend) -> int:
    agent = build_agent(backend=backend)
    result = agent.run_sync(prompt)
    print(result.final_text or "(no final text)")
    _trace(result)
    return 0 if result.error is None else 1


def interactive(backend: Backend) -> int:
    agent = build_agent(backend=backend)
    print(f"Coding Agent (agent-kit, backend={backend}).")
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
    p = argparse.ArgumentParser(description="agent-kit coding-agent sample")
    p.add_argument(
        "--backend",
        choices=("stub", "localdir"),
        default="stub",
        help="sandbox backend: 'stub' (in-memory, default) or 'localdir' "
             "(real host subprocess via LocalDirRunner)",
    )
    p.add_argument("prompt", nargs="*", help="One-shot prompt (omit for REPL).")
    args = p.parse_args()

    if args.prompt:
        return one_shot(" ".join(args.prompt), args.backend)
    return interactive(args.backend)


if __name__ == "__main__":
    raise SystemExit(main())
