"""tests/test_mcp.py — Stage 4 MCP integration tests.

Coverage(对照 docs/tech-design.md § 7 全部):

- McpServerConfig 字段校验(空 name / `__` in name / 非法字符 /
  stdio 缺 command / sse 缺 url)
- `${VAR}` 替换:env / secrets / 缺失 → KeyError / secrets 覆盖 env
- 工具命名 `mcp__<server>__<tool>`(spec § 7.4)
- connect()/aclose() 真实链路(in-memory FastMCP):initialize → list_tools
  → call_tool → close
- build_schemas 未 connect → RuntimeError
- execute 未 connect → ToolResult(is_error=True)
- aclose idempotent(重复调不抛)
- connect idempotent(重复调只生效一次)
- isError=True 的 MCP 响应 → ToolResult(is_error=True)
- toolsets_from_configs 批量构造
- 4 lifecycle 用法(per-call / per-run via Runner / per-tenant / global)
- Runner pre-warm:McpToolset.connect() 在 setup 阶段自动被 await
- 连不上 → Runner 报 setup error event
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from agent_kit.loop import RunRequest
from agent_kit.mcp import (
    McpServerConfig,
    McpToolset,
    _substitute,
    _substitute_config,
    toolsets_from_configs,
)
from agent_kit.provider import LlmResponse, ToolSchema
from agent_kit.runner import Runner
from agent_kit.toolset import ToolCallContext, ToolsetRouter
from agent_kit.types import ToolCall


# ---- in-memory FastMCP test server + toolset subclass ----


def _make_server(name: str = "test") -> FastMCP:
    """In-process server with three tools covering happy path / structured /
    error response."""
    srv: FastMCP = FastMCP(name)

    @srv.tool()
    def echo(text: str) -> str:
        """Echo a string."""
        return f"echo:{text}"

    @srv.tool()
    def add(a: int, b: int) -> dict:
        """Return structured sum."""
        return {"sum": a + b}

    @srv.tool()
    def fail() -> str:
        """Raises; FastMCP wraps into isError=True."""
        raise RuntimeError("boom")

    return srv


class _InMemMcpToolset(McpToolset):
    """Test seam: opens memory streams + spawns the FastMCP server task
    instead of a real transport. Same behavior surface as production."""

    def __init__(self, server: FastMCP, name: str = "test", *, tool_filter=None) -> None:
        # The McpServerConfig values are only used for naming/limits, not
        # for the transport (overridden below)
        super().__init__(
            McpServerConfig(name=name, transport="stdio", command=["unused"]),
            tool_filter=tool_filter,
        )
        self._server = server

    async def _open_streams(self, stack: AsyncExitStack):  # type: ignore[override]
        import anyio

        client_streams, server_streams = await stack.enter_async_context(
            create_client_server_memory_streams()
        )
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        # Run the FastMCP server in the background until stack closes
        tg = await stack.enter_async_context(anyio.create_task_group())
        underlying = self._server._mcp_server  # private; same as the SDK helper
        opts = underlying.create_initialization_options()
        tg.start_soon(
            lambda: underlying.run(server_read, server_write, opts,
                                   raise_exceptions=False)
        )
        # Ensure task group cancels on close
        stack.push_async_callback(_cancel_tg, tg)
        return client_read, client_write


async def _cancel_tg(tg) -> None:
    tg.cancel_scope.cancel()


# ---- McpServerConfig validation ----


def test_config_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        McpServerConfig(name="", transport="stdio", command=["x"])


def test_config_rejects_double_underscore_in_name() -> None:
    with pytest.raises(ValueError, match="must not contain '__'"):
        McpServerConfig(name="bad__name", transport="stdio", command=["x"])


def test_config_rejects_uppercase_name() -> None:
    with pytest.raises(ValueError, match="must match"):
        McpServerConfig(name="GitHub", transport="stdio", command=["x"])


def test_config_accepts_baizhi_hyphenated_server_id() -> None:
    cfg = McpServerConfig(name="web-search", transport="http", url="https://example.test/mcp")
    assert cfg.name == "web-search"


def test_config_rejects_stdio_without_command() -> None:
    with pytest.raises(ValueError, match="requires command"):
        McpServerConfig(name="x", transport="stdio")


def test_config_rejects_sse_without_url() -> None:
    with pytest.raises(ValueError, match="requires url"):
        McpServerConfig(name="x", transport="sse")


def test_config_rejects_http_without_url() -> None:
    with pytest.raises(ValueError, match="requires url"):
        McpServerConfig(name="x", transport="http")


def test_config_accepts_stdio() -> None:
    cfg = McpServerConfig(name="gh", transport="stdio", command=["mcp-github"])
    assert cfg.name == "gh"


def test_config_accepts_sse_with_url() -> None:
    cfg = McpServerConfig(name="ws", transport="sse", url="https://x/y")
    assert cfg.transport == "sse"


# ---- ${VAR} substitution ----


def test_substitute_basic_env() -> None:
    assert _substitute("a-${FOO}-b", {"FOO": "bar"}) == "a-bar-b"


def test_substitute_missing_raises() -> None:
    with pytest.raises(KeyError, match="MISSING_VAR"):
        _substitute("${MISSING_VAR}", {})


def test_substitute_no_vars_passthrough() -> None:
    assert _substitute("no vars here", {}) == "no vars here"


def test_substitute_config_command_url_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENV_TOKEN", "from-env")
    cfg = McpServerConfig(
        name="srv",
        transport="http",
        url="https://api.example/${ENV_TOKEN}",
        headers={"Authorization": "Bearer ${SECRET}"},
        env={"NEEDED": "${ENV_TOKEN}-suffix"},
    )
    out = _substitute_config(cfg, {"SECRET": "shh"})
    assert out.url == "https://api.example/from-env"
    assert out.headers["Authorization"] == "Bearer shh"
    assert out.env["NEEDED"] == "from-env-suffix"


def test_substitute_secrets_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "env-version")
    cfg = McpServerConfig(name="srv", transport="http", url="${TOKEN}")
    out = _substitute_config(cfg, {"TOKEN": "secret-version"})
    assert out.url == "secret-version"


def test_mcp_toolset_substitutes_on_construct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """${VAR} substitution happens at __init__, not at connect()."""
    monkeypatch.setenv("CMD", "real-binary")
    ts = McpToolset(
        McpServerConfig(
            name="srv", transport="stdio", command=["${CMD}", "--flag"],
        )
    )
    assert ts._config.command == ["real-binary", "--flag"]


def test_mcp_toolset_missing_var_raises_at_construct() -> None:
    with pytest.raises(KeyError, match="MISSING"):
        McpToolset(
            McpServerConfig(
                name="srv", transport="stdio", command=["${MISSING}"],
            )
        )


# ---- naming ----


def test_toolset_name_prefix() -> None:
    ts = McpToolset(
        McpServerConfig(name="github", transport="stdio", command=["x"])
    )
    assert ts.name == "mcp__github"


# ---- pre-connect behavior ----


def test_build_schemas_before_connect_raises() -> None:
    ts = McpToolset(
        McpServerConfig(name="srv", transport="stdio", command=["x"])
    )
    with pytest.raises(RuntimeError, match="not connected"):
        ts.build_schemas()


@pytest.mark.asyncio
async def test_execute_before_connect_returns_error() -> None:
    ts = McpToolset(
        McpServerConfig(name="srv", transport="stdio", command=["x"])
    )
    ctx = _ctx()
    result = await ts.execute(
        ToolCall(id="c1", name="mcp__srv__anything", arguments={}), ctx
    )
    assert result.is_error
    assert "not connected" in result.content


# ---- in-memory end-to-end ----


@pytest.mark.asyncio
async def test_connect_lists_and_calls_tool() -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")
    try:
        await ts.connect()
        schemas = ts.build_schemas()
        names = {s.name for s in schemas}
        assert names == {"mcp__svc__echo", "mcp__svc__add", "mcp__svc__fail"}

        result = await ts.execute(
            ToolCall(id="c1", name="mcp__svc__echo", arguments={"text": "hi"}),
            _ctx(),
        )
        assert result.is_error is False
        assert "echo:hi" in result.content
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_structured_content_returned_as_json() -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")
    try:
        await ts.connect()
        result = await ts.execute(
            ToolCall(id="c1", name="mcp__svc__add", arguments={"a": 2, "b": 3}),
            _ctx(),
        )
        assert result.is_error is False
        assert '"sum": 5' in result.content
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_mcp_iserror_response_becomes_tool_error() -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")
    try:
        await ts.connect()
        result = await ts.execute(
            ToolCall(id="c1", name="mcp__svc__fail", arguments={}),
            _ctx(),
        )
        assert result.is_error is True
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error() -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")
    try:
        await ts.connect()
        result = await ts.execute(
            ToolCall(id="c1", name="mcp__svc__nonexistent", arguments={}),
            _ctx(),
        )
        assert result.is_error is True
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_execute_wrong_prefix_returns_error() -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")
    try:
        await ts.connect()
        result = await ts.execute(
            ToolCall(id="c1", name="mcp__other__echo", arguments={"text": "x"}),
            _ctx(),
        )
        assert result.is_error
        assert "not owned" in result.content
    finally:
        await ts.aclose()


# ---- idempotency ----


@pytest.mark.asyncio
async def test_connect_idempotent() -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")
    try:
        await ts.connect()
        session_one = ts._session
        await ts.connect()  # second call: no-op
        assert ts._session is session_one
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")
    await ts.connect()
    await ts.aclose()
    await ts.aclose()  # second call: no-op, must not raise
    assert ts._connected is False


@pytest.mark.asyncio
async def test_aclose_before_connect_safe() -> None:
    ts = McpToolset(
        McpServerConfig(name="srv", transport="stdio", command=["x"])
    )
    await ts.aclose()  # never connected, must be safe


# ---- batch helper ----


def test_toolsets_from_configs() -> None:
    cfgs = [
        McpServerConfig(name="a", transport="stdio", command=["x"]),
        McpServerConfig(name="b", transport="stdio", command=["y"]),
    ]
    tss = toolsets_from_configs(cfgs)
    assert [t.name for t in tss] == ["mcp__a", "mcp__b"]
    assert all(isinstance(t, McpToolset) for t in tss)


# ---- 4 lifecycle scenarios (spec § 7.2) ----
#
# These don't all need full Runner end-to-end runs — the point is to show that
# the SAME McpToolset surface supports each lifecycle, with the use site
# controlling who calls connect/aclose.


@pytest.mark.asyncio
async def test_lifecycle_per_call() -> None:
    """per-call: new toolset every execute, close after."""
    srv = _make_server("svc")
    # one server, many toolset instances pointing at it
    for _ in range(3):
        ts = _InMemMcpToolset(srv, name="svc")
        await ts.connect()
        result = await ts.execute(
            ToolCall(id="c", name="mcp__svc__echo", arguments={"text": "hi"}),
            _ctx(),
        )
        assert "echo:hi" in result.content
        await ts.aclose()


@pytest.mark.asyncio
async def test_lifecycle_per_run_via_runner() -> None:
    """per-run: Runner constructs/closes via pre-warm + finally aclose."""
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")

    class _Echoer:
        name = "echoer"
        async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
            # First and only round: ask to call echo, then resolve
            assert tools is not None
            tool_names = {t.name for t in tools}
            assert "mcp__svc__echo" in tool_names
            return LlmResponse(text="ok", tool_calls=[])
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    runner = Runner(_Echoer(), toolsets=[ts])
    result = await runner.run_to_completion(
        RunRequest(agent_id="a", user_message="hi", max_rounds=2)
    )
    assert result.error is None
    # Runner.run finally called loop.aclose → router.aclose → ts.aclose
    assert ts._connected is False


@pytest.mark.asyncio
async def test_lifecycle_per_tenant_dict() -> None:
    """per-tenant: user maintains dict[tenant_id, toolset]; the toolset is
    reused across runs; tenant's own aclose at the end."""
    srv = _make_server("svc")
    pool: dict[str, McpToolset] = {}

    async def get_or_create(tid: str) -> McpToolset:
        if tid not in pool:
            ts = _InMemMcpToolset(srv, name="svc")
            await ts.connect()
            pool[tid] = ts
        return pool[tid]

    ts_a = await get_or_create("user_1")
    ts_a_again = await get_or_create("user_1")
    assert ts_a is ts_a_again
    # cleanup
    for t in pool.values():
        await t.aclose()


@pytest.mark.asyncio
async def test_lifecycle_global_singleton() -> None:
    """global: module-level toolset, idempotent close means it's safe even
    if used by Runner (Runner's aclose flips _connected; re-connecting via
    explicit connect() is supported)."""
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")
    await ts.connect()
    # Runner closes the same instance
    await ts.aclose()
    assert ts._connected is False
    # User can re-connect
    await ts.connect()
    assert ts._connected is True
    await ts.aclose()


# ---- Runner pre-warm integration ----


@pytest.mark.asyncio
async def test_runner_prewarms_mcp_toolset(tmp_path) -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")

    class _Probe:
        name = "probe"
        captured_tools: list[ToolSchema] | None = None
        async def chat(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
            _Probe.captured_tools = list(tools) if tools else []
            return LlmResponse(text="ok", tool_calls=[])
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    runner = Runner(_Probe(), toolsets=[ts], workspace_root=tmp_path / "ws")
    result = await runner.run_to_completion(
        RunRequest(agent_id="a", user_message="hi", max_rounds=2)
    )
    assert result.error is None
    names = {t.name for t in (_Probe.captured_tools or [])}
    assert {"mcp__svc__echo", "mcp__svc__add", "mcp__svc__fail"} <= names
    assert ts._connected is False  # Runner aclose ran


@pytest.mark.asyncio
async def test_runner_prewarm_failure_emits_setup_error(tmp_path) -> None:
    """A toolset whose connect() raises trips Runner setup → error event."""

    class _FlakyToolset(McpToolset):
        async def connect(self) -> None:
            raise RuntimeError("connect blew up")

        def build_schemas(self):
            return []

    flaky = _FlakyToolset(
        McpServerConfig(name="flaky", transport="stdio", command=["x"])
    )

    class _Dummy:
        name = "d"
        async def chat(self, *a, **k):
            return LlmResponse(text="never", tool_calls=[])
        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    runner = Runner(_Dummy(), toolsets=[flaky], workspace_root=tmp_path / "ws")
    events = [e async for e in runner.run(
        RunRequest(agent_id="a", user_message="hi")
    )]
    error_evts = [e for e in events if e.kind == "error"]
    assert len(error_evts) == 1
    assert error_evts[0].payload["stage"] == "setup"
    assert "connect blew up" in error_evts[0].payload["message"]


# ---- tool_filter (spec § 7.5.2) ----


@pytest.mark.asyncio
async def test_tool_filter_default_none_exposes_all() -> None:
    """No filter → all 3 server tools become schemas."""
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc")  # tool_filter=None default
    try:
        await ts.connect()
        names = {s.name for s in ts.build_schemas()}
        assert names == {"mcp__svc__echo", "mcp__svc__add", "mcp__svc__fail"}
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_tool_filter_whitelist_keeps_only_named() -> None:
    """`tool_filter=["echo"]` → only echo exposed; matches REMOTE name (not prefixed)."""
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc", tool_filter=["echo"])
    try:
        await ts.connect()
        names = [s.name for s in ts.build_schemas()]
        assert names == ["mcp__svc__echo"]
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_tool_filter_whitelist_multiple_remote_names() -> None:
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc", tool_filter=["echo", "add"])
    try:
        await ts.connect()
        names = {s.name for s in ts.build_schemas()}
        assert names == {"mcp__svc__echo", "mcp__svc__add"}
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_tool_filter_unknown_names_silently_excluded() -> None:
    """Unknown filter entries don't error — just nothing matches them.

    Rationale: catalog can change; failing whole toolset just because one
    filtered name is gone would be more disruptive than helpful.
    """
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc", tool_filter=["echo", "does_not_exist"])
    try:
        await ts.connect()
        names = {s.name for s in ts.build_schemas()}
        assert names == {"mcp__svc__echo"}
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_tool_filter_empty_list_exposes_nothing() -> None:
    """`tool_filter=[]` is an explicit "block everything" — useful for a
    debugging toggle ("disable this MCP without removing it from toolsets")."""
    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc", tool_filter=[])
    try:
        await ts.connect()
        assert ts.build_schemas() == []
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_tool_filter_callable_predicate() -> None:
    """`tool_filter=lambda s: ...` for arbitrary filtering."""
    srv = _make_server("svc")
    # Keep tools whose remote name is exactly 3 letters
    ts = _InMemMcpToolset(
        srv, name="svc",
        tool_filter=lambda s: len(s.name.split("__", 2)[-1]) == 3,
    )
    try:
        await ts.connect()
        names = {s.name for s in ts.build_schemas()}
        assert names == {"mcp__svc__add"}  # only "add" is 3 letters
    finally:
        await ts.aclose()


@pytest.mark.asyncio
async def test_tool_filter_excludes_from_router_path() -> None:
    """Filtered-out tool isn't routed to the toolset (Router doesn't know it).

    Defense-in-depth: even if the LLM hallucinates `mcp__svc__fail` after we
    filtered to ["echo"], the Router never registered `fail`, so `execute`
    wouldn't get called for it. Verified end-to-end here via Router.
    """
    from agent_kit.toolset import ToolsetRouter

    srv = _make_server("svc")
    ts = _InMemMcpToolset(srv, name="svc", tool_filter=["echo"])
    try:
        await ts.connect()
        router = ToolsetRouter([ts])
        registered_names = {s.name for s in router.all_schemas()}
        assert registered_names == {"mcp__svc__echo"}
        # Try to execute a filtered-out tool → router returns "unknown tool"
        r = await router.execute(
            ToolCall(id="c1", name="mcp__svc__fail", arguments={}),
            _ctx(),
        )
        assert r.is_error
        assert "unknown tool" in r.content
    finally:
        await ts.aclose()


# ---- convenience factories (spec § 7.5.3) ----


def test_factory_http_equivalent_to_ctor() -> None:
    """`McpToolset.http(...)` builds the same shape as the McpServerConfig ctor path."""
    by_factory = McpToolset.http(
        "brave", url="https://brave.com/mcp",
        headers={"X-Key": "v"}, connect_timeout=15.0,
    )
    by_ctor = McpToolset(
        McpServerConfig(
            name="brave", transport="http", url="https://brave.com/mcp",
            headers={"X-Key": "v"}, connect_timeout=15.0,
        )
    )
    # 对 caller 可见的字段一致;internal stack / session 不比较
    assert by_factory.name == by_ctor.name == "mcp__brave"
    assert by_factory._config == by_ctor._config


def test_factory_stdio() -> None:
    ts = McpToolset.stdio("github", command=["mcp-github"],
                          env={"GH_TOKEN": "secret"})
    assert ts.name == "mcp__github"
    assert ts._config.transport == "stdio"
    assert ts._config.command == ["mcp-github"]
    assert ts._config.env == {"GH_TOKEN": "secret"}


def test_factory_sse() -> None:
    ts = McpToolset.sse("ws", url="https://x.test/sse",
                        headers={"Auth": "Bearer x"})
    assert ts.name == "mcp__ws"
    assert ts._config.transport == "sse"
    assert ts._config.headers == {"Auth": "Bearer x"}


def test_factory_propagates_tool_filter_and_secrets() -> None:
    """`secrets` + `tool_filter` pass through to ctor unchanged."""
    ts = McpToolset.http(
        "x", url="https://api/mcp/${TOKEN}",
        secrets={"TOKEN": "abc"},
        tool_filter=["search", "fetch"],
    )
    # ${VAR} substitution happened (secrets winning over env)
    assert ts._config.url == "https://api/mcp/abc"
    # tool_filter stored as frozenset(behavior verified separately;
    # we just check the set contents here)
    assert set(ts._tool_filter or []) == {"search", "fetch"}


def test_factory_validation_propagates() -> None:
    """Factories run the same McpServerConfig validation —
    bad server names / missing required args raise the same errors."""
    import pytest
    with pytest.raises(ValueError, match="must not contain '__'"):
        McpToolset.http("bad__name", url="https://x")
    # http without url is impossible via factory (url is required kwarg)
    # — Python TypeError, not our ValueError. Test stdio mistake instead:
    with pytest.raises(TypeError):
        McpToolset.stdio("x")           # missing command kwarg


# ---- helpers ----


def _ctx() -> ToolCallContext:
    import asyncio
    from pathlib import Path

    return ToolCallContext(
        run_id="r", skill_name=None,
        cancel=asyncio.Event(),
        workspace=Path("/tmp"), storage=Path("/tmp"),
        emit=lambda evt: None,
    )
