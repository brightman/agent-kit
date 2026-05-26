"""Reference SandboxRunner implementations (spec § 16.3).

Stage C ships `LocalDirRunner`. Stage D-E will add `SrtRunner` and
`McpSandboxRunner` alongside.
"""

from .localdir import LocalDirRunner

__all__ = ["LocalDirRunner"]
