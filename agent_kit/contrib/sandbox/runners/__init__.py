"""Reference SandboxRunner implementations (spec § 16.3).

Stages C-D ship `LocalDirRunner` + `SrtRunner`. Stage E will add
`McpSandboxRunner` alongside.
"""

from .localdir import LocalDirRunner
from .srt import SrtRunner

__all__ = ["LocalDirRunner", "SrtRunner"]
