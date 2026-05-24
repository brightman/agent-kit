"""tests/test_request_aware_schemas.py — spec § 5.4 per-request schema hook.

Coverage:

- BaseToolset.build_schemas_for_request 默认 delegate 到 build_schemas
- ToolsetRouter 接 request → 走 per-request 路径
- ToolsetRouter 不接 request(默认 None)→ 走静态路径
- AgentLoop 每个 run 重建 Router(同一个 toolset 不同 request → 不同 tools)
- 同一个 AgentLoop 跨多 run 时,过滤不同
- Router collision 每个 run 都重新检测,失败 → setup error event
- 静态 toolset 不受影响,行为一致
- baizhi 风格的 per-request 过滤示例(by enabled_skills)
- MCP allow-list 风格的过滤示例(by tenant_id)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_kit.loop import AgentLoop, RunRequest
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.toolset import BaseToolset, ToolCallContext, ToolsetRouter
from agent_kit.types import Event, Message, ToolCall, ToolResult


# ---- helpers ----


class _StaticToolset(BaseToolset):
    """Doesn't override build_schemas_for_request — default delegation kicks in."""

    def __init__(self, name: str, tools: list[str]) -> None:
        self.name = name
        self._tools = tools

    def build_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(name=n, description="", parameters={"type": "object"})
            for n in self._tools
        ]

    async def execute(self, call, ctx):
        return ToolResult(call_id=call.id, content=f"ran {call.name}")


class _DynamicSkillToolset(BaseToolset):
    """Filters its schemas by request.enabled_skills — baizhi pattern."""

    name = "skill_toolset"

    def build_schemas(self) -> list[ToolSchema]:
        return []  # nothing without a request

    def build_schemas_for_request(self, request: RunRequest) -> list[ToolSchema]:
        return [
            ToolSchema(
                name=f"skill_write__{s}",
                description="",
                parameters={"type": "object"},
            )
            for s in request.enabled_skills
        ]

    async def execute(self, call, ctx):
        return ToolResult(call_id=call.id, content=f"wrote {call.name}")


class _TenantAclMcpStyleToolset(BaseToolset):
    """MCP allow-list pattern: tenant -> set of allowed remote tool names."""

    def __init__(self, name: str, all_tools: list[str], acl: dict[str, set[str]]):
        self.name = name
        self._all_tools = all_tools
        self._acl = acl

    def build_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(name=n, description="", parameters={"type": "object"})
            for n in self._all_tools
        ]

    def build_schemas_for_request(self, request: RunRequest) -> list[ToolSchema]:
        full = self.build_schemas()
        allow = self._acl.get(request.tenant_id)
        if allow is None:
            return full
        return [s for s in full if s.name in allow]

    async def execute(self, call, ctx):
        return ToolResult(call_id=call.id, content=f"called {call.name}")


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        tenant_id="t",
        run_id="r",
        skill_name=None,
        cancel=asyncio.Event(),
        workspace=Path("/tmp"),
        storage=Path("/tmp"),
        emit=lambda evt: None,
    )


def _req(**overrides) -> RunRequest:
    base = dict(tenant_id="t", agent_id="a", user_message="x", max_rounds=2)
    base.update(overrides)
    return RunRequest(**base)


# ---- BaseToolset default ----


def test_default_build_schemas_for_request_delegates_to_static() -> None:
    ts = _StaticToolset("s", ["a", "b"])
    req = _req()
    static = [s.name for s in ts.build_schemas()]
    dynamic = [s.name for s in ts.build_schemas_for_request(req)]
    assert static == dynamic == ["a", "b"]


# ---- ToolsetRouter ----


def test_router_no_request_uses_static_path() -> None:
    """Router(toolsets) with no request → uses build_schemas() of each."""
    s = _StaticToolset("s", ["a", "b"])
    r = ToolsetRouter([s])
    assert {x.name for x in r.all_schemas()} == {"a", "b"}


def test_router_with_request_uses_per_request_path() -> None:
    """Router(toolsets, request=...) → uses build_schemas_for_request of each."""
    d = _DynamicSkillToolset()
    req = _req(enabled_skills=["alpha", "beta"])
    r = ToolsetRouter([d], request=req)
    names = {s.name for s in r.all_schemas()}
    assert names == {"skill_write__alpha", "skill_write__beta"}


def test_router_static_toolset_unchanged_with_request() -> None:
    """Static toolset (no override) still works when request is passed."""
    s = _StaticToolset("s", ["a"])
    r = ToolsetRouter([s], request=_req())
    assert [x.name for x in r.all_schemas()] == ["a"]


def test_router_mixed_static_and_dynamic() -> None:
    """Static + dynamic in same registry — both respected."""
    s = _StaticToolset("s", ["static_tool"])
    d = _DynamicSkillToolset()
    req = _req(enabled_skills=["pptx"])
    r = ToolsetRouter([s, d], request=req)
    names = {x.name for x in r.all_schemas()}
    assert names == {"static_tool", "skill_write__pptx"}


def test_router_collision_detection_per_request() -> None:
    """Collision check happens against per-request schemas, not static."""

    class _ConflictDynamic(BaseToolset):
        name = "d"
        def build_schemas(self):
            return []  # no conflict at static time
        def build_schemas_for_request(self, request):
            return [ToolSchema(name="x", description="", parameters={"type": "object"})]
        async def execute(self, call, ctx):
            return ToolResult(call_id=call.id, content="")

    static = _StaticToolset("s", ["x"])
    dyn = _ConflictDynamic()
    # No request: no conflict (dynamic returns nothing static-wise)
    ToolsetRouter([static, dyn])
    # With request: dynamic now emits "x" → collision
    with pytest.raises(ValueError, match="tool name collision: 'x'"):
        ToolsetRouter([static, dyn], request=_req())


# ---- AgentLoop per-run rebuild ----


class _Scripted:
    """Provider that records what tools were offered each call."""

    name = "scripted"

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.tool_lists: list[list[str]] = []

    async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        self.tool_lists.append([t.name for t in (tools or [])])
        return self._responses.pop(0)

    async def chat_stream(self, *a, **k):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_agentloop_rebuilds_router_each_run_with_different_schemas() -> None:
    """Same AgentLoop instance, two runs with different enabled_skills → different tools."""
    provider = _Scripted([
        LlmResponse(text="ok", tool_calls=[]),
        LlmResponse(text="ok", tool_calls=[]),
    ])
    dyn = _DynamicSkillToolset()
    loop = AgentLoop(provider, toolsets=[dyn])

    # Run 1: enable two skills
    [e async for e in loop.run(_req(enabled_skills=["a", "b"]), _ctx())]
    # Run 2: enable a different set
    [e async for e in loop.run(_req(enabled_skills=["c"]), _ctx())]

    assert provider.tool_lists[0] == ["skill_write__a", "skill_write__b"]
    assert provider.tool_lists[1] == ["skill_write__c"]


@pytest.mark.asyncio
async def test_agentloop_per_run_collision_yields_setup_error_event() -> None:
    """Dynamic toolset that collides with static one for this request →
    setup error event (caught by per-run Router build)."""

    class _ConflictDynamic(BaseToolset):
        name = "dyn"
        def build_schemas(self):
            return []
        def build_schemas_for_request(self, request):
            return [ToolSchema(name="echo", description="",
                               parameters={"type": "object"})]
        async def execute(self, call, ctx):
            return ToolResult(call_id=call.id, content="")

    provider = _Scripted([LlmResponse(text="never", tool_calls=[])])
    static = _StaticToolset("s", ["echo"])
    loop = AgentLoop(provider, toolsets=[static, _ConflictDynamic()])

    events = [e async for e in loop.run(_req(), _ctx())]
    error_evts = [e for e in events if e.kind == "error"]
    assert len(error_evts) == 1
    assert error_evts[0].payload["stage"] == "setup"
    assert "tool name collision: 'echo'" in error_evts[0].payload["message"]


@pytest.mark.asyncio
async def test_agentloop_aclose_walks_toolsets_directly() -> None:
    """aclose closes toolsets in reverse order (no Router intermediary)."""
    order: list[str] = []

    class _CloseRecorder(BaseToolset):
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = 0
        def build_schemas(self):
            return []
        async def execute(self, call, ctx):
            return ToolResult(call_id=call.id, content="")
        async def aclose(self) -> None:
            self.closed += 1
            order.append(self.name)

    a = _CloseRecorder("a")
    b = _CloseRecorder("b")
    c = _CloseRecorder("c")
    loop = AgentLoop(_Scripted([]), toolsets=[a, b, c])
    await loop.aclose()
    assert order == ["c", "b", "a"]
    assert a.closed == b.closed == c.closed == 1


# ---- multi-tenant MCP-style ACL ----


@pytest.mark.asyncio
async def test_tenant_acl_filters_mcp_style_tools() -> None:
    provider = _Scripted([
        LlmResponse(text="ok", tool_calls=[]),
        LlmResponse(text="ok", tool_calls=[]),
    ])
    acl_ts = _TenantAclMcpStyleToolset(
        "mcp_acl",
        all_tools=["search", "fetch", "summarize"],
        acl={
            "alice": {"search"},                 # only search
            "bob": {"search", "fetch"},         # search + fetch
            # carol: no entry → full set
        },
    )
    loop = AgentLoop(provider, toolsets=[acl_ts])

    [e async for e in loop.run(_req(tenant_id="alice"), _ctx())]
    [e async for e in loop.run(_req(tenant_id="bob"), _ctx())]

    assert provider.tool_lists[0] == ["search"]
    assert set(provider.tool_lists[1]) == {"search", "fetch"}
