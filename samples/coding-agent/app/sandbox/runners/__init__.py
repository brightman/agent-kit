"""Reference SandboxRunner implementations.

Stage B ships only `StubRunner` (in-memory). Stage C-E add LocalDir / SRT / MCP.
"""

from .stub import DEFAULT_COMMANDS, StubRunner

__all__ = ["DEFAULT_COMMANDS", "StubRunner"]
