"""`agent_kit.contrib` —— reference 实现集合,可选 import,与主包同代码库管理但
**不**进 `agent_kit.__init__` 默认导出。

定位:
- 主包(`agent_kit/*.py`)只有契约 + 最小机制(spec § 1 边界)
- contrib 放"使用方常需要、但跟主契约耦合不深"的具体实现 —— 比如
  - `FilesystemSkillRegistry`:把 SKILL.md 散在文件系统的 reference 持久层
  - (未来候选)cost / usage 累计 hook
  - (未来候选)LiteLLM provider adapter
  - (未来候选)stdlib logging 桥接 hook

使用方 import 路径明确:`from agent_kit.contrib.skills import FilesystemSkillRegistry`
—— 一眼看出"这是 reference,可换"。
"""
