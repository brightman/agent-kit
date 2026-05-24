"""Baizhi asset integration smoke test.

This test exercises the handoff the Baizhi product needs:

- the real pptx SKILL.md bundle from baizhi-agent is exposed through
  agent-kit's skill catalog;
- the Baizhi WebSearch MCP server id (`web-search`) is usable by agent-kit;
- an agent loop can load the pptx skill, search via MCP, and finish the
  "shareable PPT" task.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from agent_kit.contrib.skills import FilesystemSkillRegistry
from agent_kit.loop import RunRequest
from agent_kit.mcp import McpServerConfig, McpToolset
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.runner import Runner
from agent_kit.skill import SkillCatalogToolset
from agent_kit.types import Message, ToolCall


BAIZHI_AGENT_ROOT = Path(__file__).resolve().parents[2] / "baizhi-agent"
BUNDLED_SKILLS_ROOT = BAIZHI_AGENT_ROOT / "bundled_skills"
WEBSEARCH_URL = "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp"
TASK = "深度搜索anthropic 关于AI Native orgnization组织方式的材料，生成一份可以分享的ppt"


class _InMemoryMcpToolset(McpToolset):
    def __init__(self, server: FastMCP, config: McpServerConfig) -> None:
        super().__init__(config, secrets={"DASHSCOPE_API_KEY": "test-token"})
        self._server = server

    async def _open_streams(self, stack: AsyncExitStack):  # type: ignore[override]
        client_streams, server_streams = await stack.enter_async_context(
            create_client_server_memory_streams()
        )
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        task_group = await stack.enter_async_context(anyio.create_task_group())
        underlying = self._server._mcp_server
        opts = underlying.create_initialization_options()
        task_group.start_soon(
            lambda: underlying.run(
                server_read, server_write, opts, raise_exceptions=False
            )
        )
        stack.push_async_callback(_cancel_task_group, task_group)
        return client_read, client_write


async def _cancel_task_group(task_group: Any) -> None:
    task_group.cancel_scope.cancel()


def _make_websearch_server() -> FastMCP:
    server = FastMCP("web-search")

    @server.tool()
    def search(query: str) -> str:
        """Search Anthropic AI-native organization material."""
        assert "Anthropic" in query
        return (
            "Anthropic source pack: AI-native organizations use small empowered "
            "teams, written operating principles, model-assisted research and "
            "review loops, evals for quality, and human oversight for deployment."
        )

    return server


class _DeckProvider:
    name = "scripted-deck-agent"

    def __init__(self) -> None:
        self.calls: list[tuple[list[Message], list[ToolSchema] | None]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LlmResponse:
        self.calls.append((list(messages), list(tools) if tools else None))
        round_index = len(self.calls) - 1
        if round_index == 0:
            assert TASK in messages[-1].content
            assert _tool_names(tools) >= {
                "load_skill",
                "load_skill_resource",
                "mcp__web-search__search",
            }
            return LlmResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        id="load-pptx",
                        name="load_skill",
                        arguments={"name": "pptx"},
                    )
                ],
            )
        if round_index == 1:
            assert any("PPTX Skill" in m.content for m in messages)
            return LlmResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        id="load-pptx-guide",
                        name="load_skill_resource",
                        arguments={"name": "pptx", "path": "pptxgenjs.md"},
                    )
                ],
            )
        if round_index == 2:
            assert any("pptxgenjs" in m.content.lower() for m in messages)
            return LlmResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        id="websearch-anthropic",
                        name="mcp__web-search__search",
                        arguments={
                            "query": (
                                "Anthropic AI Native organization operating "
                                "model org design teams evals"
                            )
                        },
                    )
                ],
            )
        if round_index == 3:
            assert any("Anthropic source pack" in m.content for m in messages)
            return LlmResponse(
                text=(
                    "已完成可分享 PPT 的研究素材与结构: "
                    "anthropic-ai-native-organization.pptx"
                ),
                tool_calls=[],
            )
        raise RuntimeError("scripted provider exhausted")

    async def chat_stream(self, *args: Any, **kwargs: Any):
        raise NotImplementedError


def _tool_names(tools: list[ToolSchema] | None) -> set[str]:
    return {tool.name for tool in tools or []}


@pytest.mark.asyncio
async def test_agent_loop_uses_baizhi_pptx_skill_and_websearch_mcp(
    tmp_path: Path,
) -> None:
    provider = _DeckProvider()
    skill_catalog = SkillCatalogToolset(
        FilesystemSkillRegistry(BUNDLED_SKILLS_ROOT), tenant_id="tenant-baizhi"
    )
    websearch = _InMemoryMcpToolset(
        _make_websearch_server(),
        McpServerConfig(
            name="web-search",
            transport="http",
            url=WEBSEARCH_URL,
            headers={"Authorization": "Bearer ${DASHSCOPE_API_KEY}"},
        ),
    )
    runner = Runner(
        provider,
        toolsets=[skill_catalog, websearch],
        workspace_root=tmp_path / "runs",
    )

    result = await runner.run_to_completion(
        RunRequest(
            tenant_id="tenant-baizhi",
            agent_id="agent-deck",
            user_message=TASK,
            enabled_skills=["pptx"],
            max_rounds=6,
        )
    )

    assert result.final_text is not None
    assert "anthropic-ai-native-organization.pptx" in result.final_text
    assert result.rounds_used == 4
    assert any(
        event.kind == "tool_call"
        and event.payload["name"] == "mcp__web-search__search"
        for event in result.events
    )
