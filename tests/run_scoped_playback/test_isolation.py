from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOT = ROOT / "app" / "service" / "run_scoped_playback"

_LEAF_MODULE = "app.service.run_scoped_playback"
_ALLOWED_APPLICATION_IMPORTS = {
    "app.service.analysis.scheduler": frozenset(
        {
            "AudioStorageKind",
            "CancellationToken",
            "LoadedAudio",
            "PersistedAudioReference",
        }
    ),
    "app.service.run_persistence": frozenset(
        {
            "AudioRecord",
            "RunRecord",
            "decode_analysis_envelope",
            "validate_utc_timestamp",
            "validate_uuid4",
        }
    ),
}


def _find_forbidden_application_imports(source: str) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _LEAF_MODULE or alias.name.startswith(
                    f"{_LEAF_MODULE}."
                ):
                    continue
                if alias.name == "app" or alias.name.startswith("app."):
                    violations.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = _LEAF_MODULE.split(".")
                retained_count = len(package_parts) - node.level + 1
                if retained_count <= 0:
                    resolved_module = "<beyond-top-level>"
                else:
                    resolved_parts = package_parts[:retained_count]
                    if node.module:
                        resolved_parts.extend(node.module.split("."))
                    resolved_module = ".".join(resolved_parts)
                if resolved_module == _LEAF_MODULE or resolved_module.startswith(
                    f"{_LEAF_MODULE}."
                ):
                    continue
                violations.extend(
                    f"{resolved_module}:{alias.name}" for alias in node.names
                )
                continue
            module = node.module or ""
            if module == _LEAF_MODULE or module.startswith(
                f"{_LEAF_MODULE}."
            ):
                continue
            if module in _ALLOWED_APPLICATION_IMPORTS:
                allowed_names = _ALLOWED_APPLICATION_IMPORTS[module]
                for alias in node.names:
                    if alias.name not in allowed_names:
                        violations.append(f"{module}:{alias.name}")
            elif module == "app" or module.startswith("app."):
                violations.extend(
                    f"{module}:{alias.name}" for alias in node.names
                )
    return violations


def test_production_leaf_uses_only_exact_application_import_seam() -> None:
    violations = {
        path.name: found
        for path in sorted(PRODUCTION_ROOT.glob("*.py"))
        if (
            found := _find_forbidden_application_imports(
                path.read_text(encoding="utf-8")
            )
        )
    }

    assert violations == {}


def test_application_import_checker_rejects_new_modules_and_symbols() -> None:
    source = """
from .contracts import PlaybackCode
import app.service.run_scoped_playback.backend
from app.service.run_persistence import AudioRecord
from app.service.analysis.scheduler import AudioStorageKind, SchedulerCapacity
from app.services_container import services
import app.service.plc_service
from ...services_container import services
from ..plc_service import PLCService
from ..analysis.scheduler import SchedulerCapacity
import json
"""

    assert _find_forbidden_application_imports(source) == [
        "app.service.analysis.scheduler:SchedulerCapacity",
        "app.services_container:services",
        "app.service.plc_service",
        "app.services_container:services",
        "app.service.plc_service:PLCService",
        "app.service.analysis.scheduler:SchedulerCapacity",
    ]
