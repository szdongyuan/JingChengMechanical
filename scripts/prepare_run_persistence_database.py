"""Release entrypoint for preparing a new run-persistence database."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import argparse
import subprocess


_SCHEMA_TEST_COMMAND = [
    "python",
    "-m",
    "pytest",
    "tests/run_persistence/test_schema.py",
    "-q",
]


def main(
    target_path: str | Path,
    *,
    run_command: Callable[..., object] | None = None,
    prepare_database: Callable[[str | Path], Path] | None = None,
) -> int:
    command_runner = subprocess.run if run_command is None else run_command
    result = command_runner(_SCHEMA_TEST_COMMAND, check=False)
    return_code = int(result.returncode)
    if return_code != 0:
        return return_code

    if prepare_database is None:
        from app.service.run_persistence.prepare_database import (
            prepare_run_persistence_database,
        )

        prepare_database = prepare_run_persistence_database
    prepare_database(target_path)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_path", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main(_parse_args().target_path))
