"""Diet sandbox API (Stage B freeze — see spec § 16.3).

Stage C moves this whole subpackage to `agent_kit.contrib.sandbox` unchanged.
"""

from .toolset import SandboxToolset
from .types import ExecResult, SandboxRunner

__all__ = ["ExecResult", "SandboxRunner", "SandboxToolset"]
