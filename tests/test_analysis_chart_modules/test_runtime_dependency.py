from __future__ import annotations

from pathlib import Path

from ._support import REQUIRED_CHART_RUNTIME_DEPENDENCY


def test_requirements_txt_is_utf8_and_lists_chart_runtime_dependency():
    requirements_path = Path(__file__).resolve().parents[2] / "requirements.txt"
    requirements_text = requirements_path.read_text(encoding="utf-8")
    entries = {
        line.split("#", 1)[0].strip().casefold()
        for line in requirements_text.splitlines()
        if line.split("#", 1)[0].strip()
    }

    assert isinstance(requirements_text, str)
    assert REQUIRED_CHART_RUNTIME_DEPENDENCY in entries
