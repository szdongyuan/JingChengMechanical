from __future__ import annotations

import ast
from pathlib import Path
import re

from app.service.result_dashboard_layout import LAYOUT_VERSION


ROOT = Path(__file__).resolve().parents[1]
HISTORY_SERVICE = ROOT / "app/service/result_history.py"
LAYOUT_SERVICE = ROOT / "app/service/result_dashboard_layout.py"
LAYOUT_EDITOR = ROOT / "ui/widgets/result_dashboard_layout_editor.py"
DASHBOARD = ROOT / "ui/modules/result_dashboard.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_result_history_service_has_no_qt_or_screenshot_cache_types():
    source = _source(HISTORY_SERVICE)
    imported_modules = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert all(not module.startswith("PyQt") for module in imported_modules)
    assert not {"QPixmap", "QImage", "QScreen"}.intersection(source.split())
    assert "screenshot_cache" not in source.lower()


def test_layout_editor_and_dashboard_have_no_obsolete_summary_layout_type():
    for path in (LAYOUT_SERVICE, LAYOUT_EDITOR, DASHBOARD):
        source = _source(path)
        assert "PANEL_RESULT_SUMMARY" not in source
        assert "PANEL_FAMILY_SUMMARY" not in source
        assert "result_summary" not in source


def test_result_dashboard_layout_version_is_two():
    assert LAYOUT_VERSION == 2


def test_dashboard_does_not_connect_or_emit_legacy_signals_on_global_bus():
    source = _source(DASHBOARD)
    legacy_signals = (
        "factory_review_changed",
        "factory_runtime_start_accepted",
        "factory_runtime_analyzing_entered",
    )
    for signal in legacy_signals:
        global_bus_use = re.compile(
            rf"(?:self\.)?(?:_?services)\.bus\.{signal}\.(?:connect|emit)"
        )
        assert global_bus_use.search(source) is None


def test_scope_contains_no_test_only_legacy_or_compatibility_adapter():
    for path in (HISTORY_SERVICE, LAYOUT_SERVICE, LAYOUT_EDITOR, DASHBOARD):
        tree = ast.parse(_source(path))
        definitions = [
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert all("test_only" not in name for name in definitions)
        assert all("legacy" not in name for name in definitions)
        assert all("compat" not in name for name in definitions)
        assert all("migration" not in name for name in definitions)
