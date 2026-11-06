from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.service.multi_run_coordinator_mvp import MultiRunCoordinator


PACKAGE = Path("app/service/multi_run_coordinator_mvp")


def test_exact_shutdown_public_signatures() -> None:
    assert str(inspect.signature(MultiRunCoordinator.stop_all)) == (
        "(self, timeout_seconds: 'float') -> 'StopReport'"
    )
    assert str(inspect.signature(MultiRunCoordinator.begin_shutdown)) == (
        "(self) -> 'None'"
    )
    assert str(inspect.signature(MultiRunCoordinator.finish_shutdown)) == (
        "(self, deadline_monotonic: 'float') -> 'CoordinatorShutdownReport'"
    )


def test_coordinator_has_one_rlock_and_one_condition_bound_to_it() -> None:
    tree = ast.parse((PACKAGE / "coordinator.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert sum(node.func.attr == "RLock" for node in calls) == 1
    conditions = [node for node in calls if node.func.attr == "Condition"]
    assert len(conditions) == 1
    assert len(conditions[0].args) == 1
    assert isinstance(conditions[0].args[0], ast.Attribute)
    assert conditions[0].args[0].attr == "_lock"


def test_production_scope_has_no_forbidden_ui_or_lifecycle_mechanisms() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py")
    ).lower()
    for forbidden in (
        "pyside",
        "pyqt",
        "services_container",
        "main_window",
        "factory_mode_controller",
        "threading.local",
        "blockingqueuedconnection",
        "historypublishresult",
        "plcsequenceslot",
        "event bus",
        "event_bus",
    ):
        assert forbidden not in source


def test_coordinator_never_closes_bridge_or_dependency_owners() -> None:
    source = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
    for forbidden_call in (
        "self._scheduler.shutdown",
        "self._scheduler.close",
        "self._resident.close",
        "self._persistence.close",
        "self._plc.close",
    ):
        assert forbidden_call not in source


def test_coordinator_has_no_concurrent_finisher_wait_or_synthetic_report() -> None:
    source = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
    assert "while self._shutdown_finish_in_progress" not in source
    assert "finish_shutdown final pass is still in progress" not in source


def test_no_private_resident_access_or_duplicate_persistence_writer() -> None:
    source = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
    for forbidden in (
        "resident._",
        "managed_input._",
        "create_queued_attempt",
        "mark_attempt_running",
        "commit_reanalysis_terminal",
        "cancel_reanalysis_attempt",
    ):
        assert forbidden not in source
