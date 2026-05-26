"""Hooks —— Loop 内置切面,使用方可在 LLM / Tool 调用前后插一脚。

## 设计

- **4 个方法**:`before_model` / `after_model` / `before_tool` / `after_tool`
- **基类 + no-op 默认**:子类只覆盖关心的方法
- **Async only**:简化 contract;sync 逻辑用 `asyncio.to_thread` 自己包
- **List 顺序遍历,first-non-None wins**:多个 hook 按注册顺序调用;
  第一个返回非 None 的短路,后续 hook 跳过
- **异常不 swallow**:hook raise → loop 转成 `Event(kind="error", stage="hook")`
  + return(不让错误被吞)

## 短路时也跑后续么?

对 4 个 hook 都一样:**一旦短路就停**。例:

```
before_model: [HookA, HookB, HookC]
  - HookA.before_model → None      # 不短路,继续
  - HookB.before_model → LlmResponse  # 短路!HookC.before_model 不跑
  - 跳过 provider.chat
  - after_model: [HookA, HookB, HookC]
    - 都按顺序跑,可以再短路一次替换 response
```

before_model 和 after_model 互相独立,各自走一遍 first-non-None。

## 跟装饰器模式的选择

**hook 适用**:跨多 toolset / provider 的横切关注点(权限 / 童锁 / quota)
**装饰器适用**:单一 toolset / provider 内聚的关注点(cost tracking、PII redact
                单个 toolset 输出、tool 重试 / 降级)

具体示例见 docs/tech-design.md § 8.7 末尾"装饰器 vs hook 选择指南"。

## 参考实现

- ADK base_agent.py + base_llm_flow.py(callback 字段 + plugin 系统)
- ADK 短路语义:`return non-None` short-circuits,对应我们的合并 Hook 类
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types import Message, ToolCall, ToolResult

if TYPE_CHECKING:
    from .provider import LlmResponse, ToolSchema
    from .toolset import ToolCallContext


class Hook:
    """子类覆盖你需要的方法。所有方法默认 no-op(return None)。"""

    async def before_model(
        self,
        ctx: "ToolCallContext",
        messages: list[Message],
        tools: list["ToolSchema"] | None,
    ) -> "LlmResponse | None":
        """provider.chat 调用前。

        可 mutate messages / tools in place(SDK 不强制 frozen)。
        返回 None → loop 继续正常路径(下一个 hook,然后 provider.chat)
        返回 LlmResponse → **短路**:跳过 provider.chat + 后续 before_model hooks,
            直接用此 response 进 after_model + tool dispatch。
        典型用途:PII redact、prompt cache 注入、quota gate(超额返回合成
            response)、mock 测试。"""
        return None

    async def after_model(
        self,
        ctx: "ToolCallContext",
        response: "LlmResponse",
    ) -> "LlmResponse | None":
        """provider.chat 返回后、tool dispatch 前。

        返回 None → 继续下一个 after_model hook,然后用 response 进 tool dispatch
        返回 LlmResponse → **短路**:用此替换原 response,后续 after_model hook 跳过
        典型用途:输出 validate、内容 rewrite、PII redact LLM 输出、cost 计费
            (副作用为主,通常返回 None)。"""
        return None

    async def before_tool(
        self,
        ctx: "ToolCallContext",
        call: ToolCall,
    ) -> ToolResult | None:
        """单次 toolset.execute 调用前。

        返回 None → 继续下一个 before_tool hook,然后真跑 tool
        返回 ToolResult → **短路**:跳过 tool 执行 + 后续 before_tool hooks,
            直接用此 result(后续 after_tool 仍跑)
        典型用途:权限拒绝 / 童锁 / 缓存命中 / mock 测试。"""
        return None

    async def after_tool(
        self,
        ctx: "ToolCallContext",
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult | None:
        """toolset.execute 返回后。

        返回 None → 继续下一个 after_tool hook,然后用 result 进下一 tool / 下一轮
        返回 ToolResult → **短路**:用此替换原 result,后续 after_tool hook 跳过
        典型用途:tool 输出 PII redact、cache 写入、result rewrite。"""
        return None


__all__ = ["Hook"]
