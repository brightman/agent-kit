"""baizhi-sdk: minimal agent loop + skill + MCP SDK.

公开接口固定在本模块导出;子模块视为 SDK 内部组织,不保证稳定性。
"""

from __future__ import annotations

__version__ = "0.0.0"

# 待 stub 实现到位后,从这里 re-export 出 Runner / Event / Message / RunRequest 等
__all__: list[str] = []
