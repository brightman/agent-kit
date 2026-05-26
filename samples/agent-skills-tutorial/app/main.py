"""CLI runner for the blog-skills agent.

Usage:
    # one-shot query
    python -m app.main "Review my blog post 'Getting Started with Kubernetes' for SEO"

    # interactive REPL (sessions share prior messages so the model can continue)
    python -m app.main

Environment:
    GOOGLE_API_KEY     — required if model is a Gemini model (via LiteLLM)
    GOOGLE_MODEL       — override default model (default: gemini/gemini-2.5-flash)
"""

from __future__ import annotations

import sys

from agent_kit import Message

from .agent import build_agent

_DEMO_QUERIES = [
    # (id, prompt, what it demonstrates)
    ("1", "I have a blog post titled 'Getting Started with Kubernetes'. "
          "Can you review it for SEO?",
     "Inline skill (seo-checklist) loaded on demand"),
    ("2", "Help me write a short introduction for a blog about Python async "
          "programming. Make it SEO-friendly.",
     "Multi-skill: blog-writer + seo-checklist loaded in parallel"),
    ("3", "Can you use your video-editing skill to create a thumbnail?",
     "Edge case: agent handles a nonexistent skill gracefully"),
    ("4", "OK, then use your content research skill to help me research async Python.",
     "External skill (content-research-writer) with resource loading"),
    ("5", "I need a new skill for reviewing Python code for security vulnerabilities. "
          "Can you create a SKILL.md?",
     "Meta skill: skill-creator generates a new skill on demand"),
]


def _print_event_summary(result) -> None:
    """Print a compact one-line trace of which tools the model actually used."""
    tool_calls = [e for e in result.events if e.kind == "tool_call"]
    if tool_calls:
        names = ", ".join(e.payload["name"] for e in tool_calls)
        print(f"\n[trace] tool calls: {names}")
    print(f"[trace] rounds: {result.rounds_used}, "
          f"cancelled: {result.cancelled}, error: {result.error is not None}")


def one_shot(prompt: str) -> int:
    agent = build_agent()
    result = agent.run_sync(prompt)
    print(result.final_text or "(no final text)")
    _print_event_summary(result)
    return 0 if result.error is None else 1


def interactive() -> int:
    agent = build_agent()
    print("Blog Skills Agent (agent-kit). Type a message; Ctrl-D / `quit` to exit.")
    print("Try the demo prompts:")
    for idx, prompt, what in _DEMO_QUERIES:
        print(f"  {idx}. {prompt}\n     ({what})")
    print()

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
        if line in {n for n, *_ in _DEMO_QUERIES}:
            line = next(p for n, p, _ in _DEMO_QUERIES if n == line)
            print(f">>> {line}")

        result = agent.run_sync(line, prior_messages=history)
        text = result.final_text or "(no final text)"
        print(text)
        _print_event_summary(result)

        history.append(Message(role="user", content=line))
        history.append(Message(role="assistant", content=text))


def main() -> int:
    if len(sys.argv) > 1:
        return one_shot(" ".join(sys.argv[1:]))
    return interactive()


if __name__ == "__main__":
    raise SystemExit(main())
