from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping


def ai_subprocess_creation_flags() -> int:
    if os.name == "nt":
        # Explicit idle also preserves the existing idle analysis parent's class.
        return subprocess.CREATE_NO_WINDOW | subprocess.IDLE_PRIORITY_CLASS
    return 0


def ai_subprocess_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Limit numerical workers before child imports without mutating the parent."""
    child_environment = dict(os.environ if environment is None else environment)
    for variable in (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        child_environment[variable] = "1"
    return child_environment
