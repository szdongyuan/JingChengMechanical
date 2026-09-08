from __future__ import annotations

import os
import subprocess


def ai_subprocess_creation_flags() -> int:
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW
    return 0
