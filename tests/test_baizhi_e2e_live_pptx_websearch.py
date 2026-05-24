"""Live Baizhi E2E: LLM + WebSearch MCP + pptx skill + PPT artifact.

This test is intentionally opt-in because it spends real LLM/MCP quota:

    RUN_BAIZHI_E2E=1 .venv/bin/python -m pytest \
      tests/test_baizhi_e2e_live_pptx_websearch.py -q -s

Keys are loaded from baizhi-agent/.baizhi-agent/config/.env and never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import pytest

from agent_kit.contrib.skills import FilesystemSkillRegistry
from agent_kit.loop import RunRequest
from agent_kit.mcp import McpServerConfig, McpToolset
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.runner import Runner
from agent_kit.skill import SkillCatalogToolset
from agent_kit.toolset import BaseToolset, ToolCallContext
from agent_kit.types import Message, ToolCall, ToolResult


BAIZHI_AGENT_ROOT = Path(__file__).resolve().parents[2] / "baizhi-agent"
BAIZHI_ENV_FILE = BAIZHI_AGENT_ROOT / ".baizhi-agent" / "config" / ".env"
BUNDLED_SKILLS_ROOT = BAIZHI_AGENT_ROOT / "bundled_skills"
WEBSEARCH_URL = "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp"
TASK = "深度搜索anthropic 关于AI Native orgnization组织方式的材料，生成一份可以分享的ppt"


T = TypeVar("T")


# Transient = OS / network glitch we expect to clear on next attempt.
# Observed on macOS during live runs:
#   - EADDRNOTAVAIL (Errno 49) when the kernel can't allocate a source port
#   - timeouts during DNS / TLS / first byte
#   - 502 / 503 / 504 from the gateway
# Auth failures (401 / 403) and 4xx user errors are NOT transient — they
# come straight up.
_TRANSIENT_HTTP_STATUS = {429, 502, 503, 504}
_TRANSIENT_OS_ERRNO = {49, 60, 61, 65, 110}  # EADDRNOTAVAIL/ETIMEDOUT/ECONNREFUSED/ENETUNREACH


def _is_transient(exc: BaseException) -> bool:
    """Return True for known-flaky network/OS errors, recursing into
    BaseExceptionGroup so anyio-wrapped MCP failures are recognized too."""
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transient(sub) for sub in exc.exceptions)
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _TRANSIENT_HTTP_STATUS
    if isinstance(exc, urllib.error.URLError):
        # URLError wraps OS errors via .reason
        reason = exc.reason
        if isinstance(reason, OSError) and reason.errno in _TRANSIENT_OS_ERRNO:
            return True
        if isinstance(reason, TimeoutError):
            return True
        return False
    if isinstance(exc, OSError) and exc.errno in _TRANSIENT_OS_ERRNO:
        return True
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    return False


async def _retry_transient(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run `fn` up to `attempts` times, sleeping `base_delay * 2**i` between
    attempts on transient failures. Non-transient = first exception re-raised."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001
            if not _is_transient(exc):
                raise
            last = exc
            if i == attempts - 1:
                break
            await asyncio.sleep(base_delay * (2 ** i))
    assert last is not None
    raise last


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class _MiniMaxAgentKitProvider:
    api_key: str
    model: str
    base_url: str
    max_tokens: int = 4096
    timeout_seconds: float = 120.0

    name = "minimax-live"

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LlmResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_encode_message(m) for m in messages],
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature,
            "reasoning_split": True,
        }
        if tools:
            payload["tools"] = [_encode_tool(t) for t in tools]
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # spec § 4.5:provider 应当自己重试 transient error
        async def _one_call() -> dict[str, Any]:
            def _blocking() -> dict[str, Any]:
                req = urllib.request.Request(
                    url, data=body, headers=headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as r:
                    return json.loads(r.read().decode("utf-8"))

            return await asyncio.to_thread(_blocking)

        raw = await _retry_transient(_one_call, attempts=3, base_delay=0.5)
        duration_ms = max(1, round((time.perf_counter() - started) * 1000))
        choice = raw["choices"][0]
        message = choice["message"]
        usage = raw.get("usage") or {}
        return LlmResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=_decode_tool_calls(message.get("tool_calls") or []),
            usage={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "duration_ms": duration_ms,
            },
            finish_reason=choice.get("finish_reason"),
        )

    async def chat_stream(self, *args: Any, **kwargs: Any):
        raise NotImplementedError


def _encode_message(message: Message) -> dict[str, Any]:
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    return {"role": message.role, "content": message.content}


def _encode_tool(tool: ToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _decode_tool_calls(raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls):
        fn = raw.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {"__raw_arguments__": args}
        calls.append(
            ToolCall(
                id=raw.get("id") or f"call_{index}",
                name=fn.get("name") or "",
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return calls


class _RetryingMcpToolset(McpToolset):
    """McpToolset whose `connect()` retries transient OS / network errors.

    The macOS environment running these tests intermittently fails the
    HTTP transport handshake with `EADDRNOTAVAIL`(Errno 49)wrapped by
    anyio's TaskGroup as an ExceptionGroup; without retry, ~25% of live
    runs flake at setup. spec § 7.2 leaves lifecycle to the use site, so
    putting retry **outside** the SDK in a test-side subclass keeps the
    base McpToolset honest about being a single-shot wrapper.
    """

    async def connect(self) -> None:
        await _retry_transient(super().connect, attempts=3, base_delay=1.0)


class _PptxDeckToolset(BaseToolset):
    name = "pptx_deck_writer"

    def build_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name="create_pptx_deck",
                description=(
                    "Create a real .pptx deck file from researched slide content. "
                    "Call this only after loading the pptx skill and collecting "
                    "WebSearch evidence."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Output filename ending in .pptx.",
                        },
                        "title": {"type": "string"},
                        "subtitle": {"type": "string"},
                        "slides": {
                            "type": "array",
                            "minItems": 5,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "bullets": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "notes": {"type": "string"},
                                },
                                "required": ["title", "bullets"],
                            },
                        },
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "URLs or source names used in research.",
                        },
                    },
                    "required": ["filename", "title", "slides"],
                    "additionalProperties": False,
                },
            )
        ]

    async def execute(self, call: ToolCall, ctx: ToolCallContext) -> ToolResult:
        args = call.arguments or {}
        filename = str(args.get("filename") or "anthropic-ai-native-organization.pptx")
        if not filename.endswith(".pptx"):
            filename += ".pptx"
        if "/" in filename or "\\" in filename:
            return ToolResult(call_id=call.id, content="ERROR: filename must be a basename", is_error=True)
        slides = args.get("slides") or []
        if not isinstance(slides, list) or len(slides) < 3:
            return ToolResult(call_id=call.id, content="ERROR: provide at least 3 slides", is_error=True)
        output_dir = ctx.storage / "e2e-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / filename
        _write_minimal_pptx(
            output_path,
            title=str(args.get("title") or "Anthropic AI Native Organization"),
            subtitle=str(args.get("subtitle") or TASK),
            slides=slides,
            sources=[str(s) for s in (args.get("sources") or [])],
        )
        ctx.run_state["pptx_path"] = str(output_path)
        return ToolResult(
            call_id=call.id,
            content=json.dumps(
                {
                    "pptx_path": str(output_path),
                    "size_bytes": output_path.stat().st_size,
                    "slides": len(slides) + 1,
                },
                ensure_ascii=False,
            ),
        )


def _write_minimal_pptx(
    output_path: Path,
    *,
    title: str,
    subtitle: str,
    slides: list[Any],
    sources: list[str],
) -> None:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    title_slide = prs.slides.add_slide(prs.slide_layouts[6])
    title_box = title_slide.shapes.add_textbox(Inches(0.7), Inches(1.0), Inches(12), Inches(1.2))
    title_tf = title_box.text_frame
    title_tf.text = title
    title_p = title_tf.paragraphs[0]
    title_p.alignment = PP_ALIGN.LEFT
    title_p.runs[0].font.size = Pt(36)
    title_p.runs[0].font.bold = True
    title_p.runs[0].font.color.rgb = RGBColor(31, 41, 55)
    subtitle_box = title_slide.shapes.add_textbox(Inches(0.75), Inches(2.35), Inches(11.5), Inches(1.4))
    subtitle_tf = subtitle_box.text_frame
    subtitle_tf.word_wrap = True
    subtitle_tf.text = subtitle
    subtitle_tf.paragraphs[0].runs[0].font.size = Pt(18)
    subtitle_tf.paragraphs[0].runs[0].font.color.rgb = RGBColor(75, 85, 99)

    for raw_slide in slides:
        if not isinstance(raw_slide, dict):
            continue
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        heading = str(raw_slide.get("title") or "Untitled")[:100]
        bullets = raw_slide.get("bullets") or []
        if not isinstance(bullets, list):
            bullets = [str(bullets)]

        heading_box = slide.shapes.add_textbox(Inches(0.65), Inches(0.45), Inches(12.0), Inches(0.65))
        heading_tf = heading_box.text_frame
        heading_tf.text = heading
        heading_run = heading_tf.paragraphs[0].runs[0]
        heading_run.font.size = Pt(28)
        heading_run.font.bold = True
        heading_run.font.color.rgb = RGBColor(17, 24, 39)

        body_box = slide.shapes.add_textbox(Inches(0.95), Inches(1.55), Inches(11.3), Inches(5.3))
        body_tf = body_box.text_frame
        body_tf.word_wrap = True
        body_tf.clear()
        for index, bullet in enumerate(bullets[:6]):
            paragraph = body_tf.paragraphs[0] if index == 0 else body_tf.add_paragraph()
            paragraph.text = str(bullet)[:220]
            paragraph.level = 0
            paragraph.font.size = Pt(18)
            paragraph.font.color.rgb = RGBColor(31, 41, 55)

    if sources:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        heading_box = slide.shapes.add_textbox(Inches(0.65), Inches(0.45), Inches(12.0), Inches(0.65))
        heading_tf = heading_box.text_frame
        heading_tf.text = "Sources"
        heading_tf.paragraphs[0].runs[0].font.size = Pt(28)
        heading_tf.paragraphs[0].runs[0].font.bold = True
        body_box = slide.shapes.add_textbox(Inches(0.95), Inches(1.4), Inches(11.5), Inches(5.6))
        body_tf = body_box.text_frame
        body_tf.word_wrap = True
        body_tf.clear()
        for index, source in enumerate(sources[:8]):
            paragraph = body_tf.paragraphs[0] if index == 0 else body_tf.add_paragraph()
            paragraph.text = str(source)[:180]
            paragraph.font.size = Pt(12)

    prs.save(output_path)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_llm_websearch_and_pptx_artifact(tmp_path: Path) -> None:
    if os.environ.get("RUN_BAIZHI_E2E") != "1":
        pytest.skip("set RUN_BAIZHI_E2E=1 to spend real LLM/WebSearch quota")
    _load_env_file(BAIZHI_ENV_FILE)
    if not os.environ.get("MINIMAX_API_KEY"):
        pytest.skip("MINIMAX_API_KEY missing in baizhi-agent/.baizhi-agent/config/.env")
    if not os.environ.get("DASHSCOPE_API_KEY"):
        pytest.skip("DASHSCOPE_API_KEY missing in baizhi-agent/.baizhi-agent/config/.env")

    provider = _MiniMaxAgentKitProvider(
        api_key=os.environ["MINIMAX_API_KEY"],
        model=os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7"),
        base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"),
        max_tokens=int(os.environ.get("MINIMAX_MAX_TOKENS", "4096")),
    )
    skill_catalog = SkillCatalogToolset(
        FilesystemSkillRegistry(BUNDLED_SKILLS_ROOT),
        tenant_id="tenant-baizhi-e2e",
    )
    websearch = _RetryingMcpToolset(
        McpServerConfig(
            name="web-search",
            transport="http",
            url=WEBSEARCH_URL,
            headers={"Authorization": "Bearer ${DASHSCOPE_API_KEY}"},
            connect_timeout=60,
        )
    )
    runner = Runner(
        provider,
        toolsets=[skill_catalog, websearch, _PptxDeckToolset()],
        system_prelude=(
            "You are running a live E2E test. You MUST use tools before the final answer. "
            "Required sequence: first call load_skill for pptx, then call "
            "load_skill_resource for pptxgenjs.md, then call the WebSearch MCP tool "
            "to research Anthropic AI-native organization / operating model, then "
            "call create_pptx_deck to write the actual .pptx artifact. "
            "Do not claim completion until create_pptx_deck returns a pptx_path. "
            "Keep the deck concise, 5-8 content slides, Chinese language, with sources."
        ),
        workspace_root=tmp_path / "runs",
        storage_root=tmp_path / "storage",
    )

    result = await runner.run_to_completion(
        RunRequest(
            tenant_id="tenant-baizhi-e2e",
            agent_id="agent-live-deck",
            user_message=TASK,
            enabled_skills=["pptx"],
            max_rounds=12,
            temperature=0.1,
        )
    )

    tool_names = [
        event.payload["name"]
        for event in result.events
        if event.kind == "tool_call"
    ]
    assert "load_skill" in tool_names, tool_names
    assert "load_skill_resource" in tool_names, tool_names
    assert any(name.startswith("mcp__web-search__") for name in tool_names), tool_names
    assert "create_pptx_deck" in tool_names, tool_names

    outputs = list((tmp_path / "storage" / "e2e-output").glob("*.pptx"))
    assert outputs, f"no pptx output produced; final_text={result.final_text!r}"
    pptx_path = outputs[0]
    assert pptx_path.stat().st_size > 1000
    from pptx import Presentation

    reopened = Presentation(pptx_path)
    assert len(reopened.slides) >= 5
    extracted_text = "\n".join(
        shape.text
        for slide in reopened.slides
        for shape in slide.shapes
        if hasattr(shape, "text")
    )
    assert "Anthropic" in extracted_text
