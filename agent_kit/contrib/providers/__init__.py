"""`agent_kit.contrib.providers` —— LlmProvider reference 实现集合。

Provider 是 SDK 主契约的 plug-in 点(`agent_kit.LlmProvider` Protocol),
SDK 自己不内置具体 provider —— 但 reference impl 在这里,装 extras 后可用:

- `litellm`:`pip install "agent-kit[litellm]"` → `agent_kit.contrib.providers.litellm.LiteLlm`
  包 LiteLLM gateway 的 100+ provider(gemini / anthropic / openai / minimax / ...)

未来候选(待真消费者驱动):
- `openai`:直连 OpenAI SDK,无中间层
- `anthropic`:直连 Anthropic SDK
- `gemini`:直连 google-generativeai

> 跟主包的关系:`Agent(model="<litellm-route>")` 的字符串 model 会试图
> `from agent_kit.contrib.providers.litellm import LiteLlm`,失败给清晰
> ImportError 提示装 extras。
"""
