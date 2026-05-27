"""Textual TUI driving an agent-kit Agent with live event-stream rendering.

Layout:

    ┌─ agent-tui ────────────────────────────────────────────────────┐
    │ ┌─ Chat ─────────────────┐  ┌─ Events ────────────────────────┐│
    │ │ You: Find me…          │  │ 14:02  round_start  #0          ││
    │ │ Agent: Searching…      │  │ 14:02  llm_request              ││
    │ │ You: Now make a skill… │  │ 14:02  llm_response  1 call(s)  ││
    │ │                        │  │ 14:02  tool_call    mcp__web... ││
    │ │                        │  │ 14:02  tool_result  ok          ││
    │ │                        │  │ 14:02  final_text               ││
    │ └────────────────────────┘  └─────────────────────────────────┘│
    │ > _                                                            │
    ├────────────────────────────────────────────────────────────────┤
    │ [Ctrl-C] quit  [Ctrl-L] clear events                           │
    └────────────────────────────────────────────────────────────────┘

The right pane shows EVERY event as it arrives via
`agent.runner.run(RunRequest)` async-iterator. The left pane shows just
user prompts and the agent's `final_text`.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog

from agent_kit import Message, RunRequest

from .agent import build_agent


class AgentTui(App[None]):
    """Two-pane TUI: chat on the left, live event stream on the right."""

    CSS = """
    Horizontal { height: 1fr; }
    #chat   { width: 1fr; border: round $accent; padding: 0 1; }
    #events { width: 1fr; border: round $warning; padding: 0 1; }
    #input  { dock: bottom; }
    RichLog { background: $surface; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+l", "clear_events", "Clear events"),
    ]

    TITLE = "agent-kit TUI demo"
    SUB_TITLE = "skill-creator + WebSearch MCP"

    def __init__(self) -> None:
        super().__init__()
        self.agent = build_agent()
        self.history: list[Message] = []
        self._busy = False

    # ---- layout ----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            with Horizontal():
                yield RichLog(id="chat", wrap=True, highlight=True, markup=True)
                yield RichLog(id="events", wrap=False, highlight=True, markup=True)
            yield Input(placeholder="Ask the agent…  (Ctrl-C to quit)", id="input")
        yield Footer()

    def on_mount(self) -> None:
        chat = self.query_one("#chat", RichLog)
        chat.write(
            "[bold]Welcome.[/]  Try:\n"
            "  • [italic]Search the web for the latest Python release.[/]\n"
            "  • [italic]Use skill-creator to draft a SKILL.md for code review.[/]\n"
        )
        events = self.query_one("#events", RichLog)
        events.write("[dim]event stream — every agent event lands here live[/]")
        self.query_one("#input", Input).focus()

    # ---- bindings ----

    def action_clear_events(self) -> None:
        self.query_one("#events", RichLog).clear()

    # ---- chat handling ----

    @on(Input.Submitted, "#input")
    async def on_submit(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
            return
        inp = self.query_one("#input", Input)
        inp.value = ""

        chat = self.query_one("#chat", RichLog)
        if self._busy:
            # Agent is mid-loop — queue this message via Agent.send_steering.
            # The loop drains it at the top of the next round and the LLM sees
            # it appended to context. UI shows the queue state immediately.
            self.agent.send_steering(prompt)
            chat.write(
                f"\n[bold cyan]You[/]  [italic dim](queued, will be injected "
                f"next round — agent is mid-loop)[/]  {prompt}"
            )
            return

        chat.write(f"\n[bold cyan]You[/]  {prompt}")
        self._busy = True
        inp.disabled = True
        self.run_worker(self._run_agent(prompt), exclusive=True)

    async def _run_agent(self, prompt: str) -> None:
        """Background worker: drive runner.run() and pipe events to the TUI."""
        chat = self.query_one("#chat", RichLog)
        events = self.query_one("#events", RichLog)

        events.write(f"[dim]── {_now()}  user_prompt[/] {prompt[:60]}")

        # Build the request via Agent — that wires the steering_drain callable
        # so send_steering() above flows through.
        req = self.agent._build_request(
            prompt,
            enabled_skills=[],
            max_rounds=12,
            temperature=None,
            max_tokens=None,
            prior_messages=list(self.history),
            cancel_check=None,
            metadata=None,
        )
        final_text: str | None = None
        try:
            async for evt in self.agent.runner.run(req):
                _render_event(events, evt)
                if evt.kind == "final_text":
                    final_text = evt.payload.get("text", "")
                elif evt.kind == "error":
                    chat.write(
                        f"[bold red]!![/] {evt.payload['stage']}: "
                        f"{evt.payload.get('exc_type','')} "
                        f"{evt.payload.get('message','')[:200]}"
                    )
        except Exception as exc:  # safety net — runner.run shouldn't raise but…
            chat.write(f"[bold red]!![/] worker crashed: {type(exc).__name__}: {exc}")
        finally:
            if final_text:
                chat.write(f"[bold green]Agent[/]  {final_text}")
                self.history.append(Message(role="user", content=prompt))
                self.history.append(Message(role="assistant", content=final_text))
            self._busy = False
            inp = self.query_one("#input", Input)
            inp.disabled = False
            inp.focus()


# ---- event rendering ----


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _render_event(log: RichLog, evt: Any) -> None:
    """One line per event, color-coded by kind, with a tiny payload digest."""
    kind = evt.kind
    payload = evt.payload
    ts = _now()
    color = _COLOR.get(kind, "white")

    summary = _summary(kind, payload)
    log.write(f"[dim]{ts}[/]  [{color}]{kind:<22}[/] {summary}")


_COLOR = {
    "round_start":          "bright_blue",
    "round_end":            "bright_blue",
    "llm_request":          "cyan",
    "llm_response":         "cyan",
    "llm_short_circuited":  "yellow",
    "tool_call":            "magenta",
    "tool_result":          "magenta",
    "tool_short_circuited": "yellow",
    "context_compacted":    "yellow",
    "user_message_added":   "bold cyan",
    "final_text":           "bold green",
    "cancelled":            "yellow",
    "error":                "bold red",
}


def _summary(kind: str, p: dict[str, Any]) -> str:
    if kind == "round_start" or kind == "round_end":
        return f"#{p.get('round', '?')}"
    if kind == "llm_request":
        return f"#{p.get('round','?')}  msgs={p.get('message_count','?')}  tools={p.get('tool_count','?')}"
    if kind == "llm_response":
        tc = p.get("tool_calls") or []
        u = p.get("usage") or {}
        text_preview = (p.get("text") or "")[:40].replace("\n", "↵ ")
        bits = []
        if tc:
            bits.append(f"{len(tc)} tool_call(s)")
        if u.get("prompt_tokens") is not None:
            bits.append(f"in={u['prompt_tokens']}")
        if u.get("completion_tokens") is not None:
            bits.append(f"out={u['completion_tokens']}")
        if text_preview:
            bits.append(f'text="{text_preview}"')
        return "  ".join(bits)
    if kind == "tool_call":
        args = p.get("arguments") or {}
        args_preview = ", ".join(f"{k}={_short(v)}" for k, v in list(args.items())[:3])
        return f"{p.get('name','?')}({args_preview})"
    if kind == "tool_result":
        content = (p.get("content") or "").strip()
        err = "ERR " if p.get("is_error") else ""
        preview = content[:80].replace("\n", "↵ ")
        return f"{err}call={p.get('call_id','?')[:8]}  {preview}"
    if kind in ("llm_short_circuited", "tool_short_circuited"):
        return f"by_hook={p.get('by_hook','?')}"
    if kind == "context_compacted":
        return (f"{p.get('strategy','?')}  "
                f"{p.get('before_tokens','?')}→{p.get('after_tokens','?')} tok")
    if kind == "user_message_added":
        text = (p.get("text") or "").replace("\n", "↵ ")
        return f"[{p.get('source','?')} → round {p.get('round','?')}]  {text[:80]}"
    if kind == "final_text":
        text = (p.get("text") or "").replace("\n", "↵ ")
        return text[:100]
    if kind == "cancelled":
        return f"reason={p.get('reason','?')}  round={p.get('round','?')}"
    if kind == "error":
        return (f"[{p.get('stage','?')}] {p.get('exc_type','')}: "
                f"{(p.get('message') or '')[:100]}")
    return json.dumps(p, default=str)[:120]


def _short(v: Any) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s if len(s) <= 30 else s[:27] + "..."


def main() -> int:
    AgentTui().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
