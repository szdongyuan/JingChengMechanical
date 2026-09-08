import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import app.service.ai_subprocess as ai_subprocess


def test_creation_flags_use_create_no_window_on_windows(monkeypatch):
    monkeypatch.setattr(ai_subprocess, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        ai_subprocess,
        "subprocess",
        SimpleNamespace(CREATE_NO_WINDOW=0x08000000),
    )

    assert ai_subprocess.ai_subprocess_creation_flags() == 0x08000000


def test_creation_flags_are_zero_outside_windows(monkeypatch):
    monkeypatch.setattr(ai_subprocess, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(ai_subprocess, "subprocess", SimpleNamespace())

    assert ai_subprocess.ai_subprocess_creation_flags() == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows console semantics only")
def test_create_no_window_child_has_no_console_and_keeps_output_pipes():
    probe = (
        "import ctypes, sys; "
        "print(int(bool(ctypes.windll.kernel32.GetConsoleWindow()))); "
        "print('stderr-ok', file=sys.stderr)"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        creationflags=ai_subprocess.ai_subprocess_creation_flags(),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "0"
    assert result.stderr.strip() == "stderr-ok"
