from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
LEGACY_TEST_MANAGER = "app/service/analysis/test_manager.py"
LEGACY_AUDIO_RECORDER = "app/service/analysis/audio_recorder.py"
LEGACY_AUDIO_DATA_SERVICE = "app/service/audio_data_service.py"
LEGACY_PLC_SERVICE = "app/service/plc_service.py"
FACTORY_MODE = "app/service/factory_mode_controller.py"
SERVICES_CONTAINER = "app/services_container.py"
MAIN_WINDOW = "app/main_window.py"
MAIN = "main.py"
DATA_MANAGEMENT = "ui/modules/data_management.py"

HISTORY_SCOPE = frozenset(
    {
        "app/service/multi_run_integration/history_qt_adapter.py",
        "app/service/result_history.py",
        "ui/modules/result_dashboard.py",
    }
)
EXPORT_SCOPE = frozenset(
    {
        "app/service/run_persistence/export_reader.py",
        "app/service/run_record_export/archive.py",
        "app/service/run_record_export/contracts.py",
        "app/service/run_record_export/__init__.py",
        "ui/modules/data_management.py",
    }
)
PLC_SCOPE = frozenset(
    {
        "app/service/multi_run_integration/plc_dispatch.py",
        "app/service/plc_result_sequencer.py",
        "ui/modules/plc_settings.py",
    }
)

# Existing chart widgets are mounted only behind ResultDashboard's private
# selected-card projection bus. Their legacy-shaped slots are not global
# production review authorities.
PRIVATE_SELECTED_CARD_PROJECTION = frozenset(
    {
        "ui/modules/result_dashboard.py",
        "ui/modules/loudness.py",
        "ui/modules/octave.py",
        "ui/modules/result.py",
        "ui/modules/spectrum.py",
        "ui/modules/spl.py",
        "ui/modules/stft.py",
        "ui/modules/vibration.py",
    }
)

OLD_RUNTIME_CALLBACKS = frozenset(
    {
        "factory_review_changed",
        "factory_runtime_start_accepted",
        "factory_runtime_analyzing_entered",
        "factory_runtime_auto_analysis_succeeded",
        "factory_runtime_auto_analysis_no_result",
        "factory_runtime_auto_analysis_error",
        "factory_runtime_auto_analysis_cleanup_settled",
        "factory_runtime_stopped",
        "factory_runtime_aborted",
        "factory_runtime_reanalysis_completed",
    }
)


@dataclass(frozen=True)
class SourceUnit:
    relative_path: str
    source: str
    tree: ast.Module


@dataclass(frozen=True, order=True)
class Finding:
    relative_path: str
    line: int
    rule: str
    detail: str

    def render(self) -> str:
        return f"{self.relative_path}:{self.line}: {self.rule}: {self.detail}"


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@lru_cache(maxsize=1)
def _production_units() -> tuple[SourceUnit, ...]:
    paths = [ROOT / MAIN]
    for directory in ("app", "ui", "base", "model", "consts"):
        paths.extend((ROOT / directory).rglob("*.py"))
    return tuple(
        SourceUnit(
            relative_path=_relative(path),
            source=path.read_text(encoding="utf-8"),
            tree=ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
        for path in sorted(set(paths))
    )


def _unit(relative_path: str) -> SourceUnit:
    return next(
        unit
        for unit in _production_units()
        if unit.relative_path == relative_path
    )


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _imported_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
    return aliases


def _called_symbol(node: ast.Call, aliases: dict[str, str]) -> str | None:
    dotted = _dotted_name(node.func)
    if dotted is None:
        return None
    if "." not in dotted:
        return aliases.get(dotted, dotted).rsplit(".", 1)[-1]
    return dotted.rsplit(".", 1)[-1]


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _enclosing_function(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _identifiers(tree: ast.AST) -> Iterable[tuple[ast.AST, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node, node.id
        elif isinstance(node, ast.Attribute):
            yield node, node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node, node.name


def _assert_no_findings(findings: Iterable[Finding]) -> None:
    ordered = sorted(set(findings))
    assert not ordered, "\n" + "\n".join(item.render() for item in ordered)


def _call_findings(
    symbols: frozenset[str],
    *,
    allowed_paths: frozenset[str] = frozenset(),
    rule: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for unit in _production_units():
        aliases = _imported_aliases(unit.tree)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call):
                continue
            symbol = _called_symbol(node, aliases)
            if symbol in symbols and unit.relative_path not in allowed_paths:
                findings.append(
                    Finding(unit.relative_path, node.lineno, rule, symbol)
                )
    return findings


def test_audit_classifies_legacy_definitions_separately_from_reachability() -> None:
    expected_classes = {
        (LEGACY_TEST_MANAGER, "TestManager"),
        (LEGACY_AUDIO_RECORDER, "AudioRecorder"),
    }
    actual_classes = {
        (unit.relative_path, node.name)
        for unit in _production_units()
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.ClassDef)
        and node.name in {"TestManager", "AudioRecorder"}
    }
    assert actual_classes == expected_classes

    export_zip_definitions = [
        unit.relative_path
        for unit in _production_units()
        for node in ast.walk(unit.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "export_zip"
    ]
    assert export_zip_definitions == [LEGACY_AUDIO_DATA_SERVICE]

    legacy_plc_definitions = [
        unit.relative_path
        for unit in _production_units()
        for node in ast.walk(unit.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "last_write_test_result_to_plc"
    ]
    assert legacy_plc_definitions == [LEGACY_PLC_SERVICE]

    manual_definition = [
        unit.relative_path
        for unit in _production_units()
        for node in ast.walk(unit.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "apply_manual_judgment"
        and unit.relative_path == FACTORY_MODE
    ]
    assert manual_definition == [FACTORY_MODE]


def test_legacy_manager_and_recorder_have_zero_production_construction() -> None:
    findings = _call_findings(
        frozenset({"TestManager", "AudioRecorder"}),
        allowed_paths=frozenset({LEGACY_TEST_MANAGER}),
        rule="legacy-construction",
    )
    _assert_no_findings(findings)

    aliases = _imported_aliases(_unit(LEGACY_TEST_MANAGER).tree)
    allowed_recorder_constructions = [
        node
        for node in ast.walk(_unit(LEGACY_TEST_MANAGER).tree)
        if isinstance(node, ast.Call)
        and _called_symbol(node, aliases) == "AudioRecorder"
    ]
    assert len(allowed_recorder_constructions) == 1


def test_service_container_and_factory_adapter_have_no_single_run_test_owner() -> None:
    findings: list[Finding] = []
    for unit in _production_units():
        if unit.relative_path == LEGACY_TEST_MANAGER:
            continue
        for node in ast.walk(unit.tree):
            dotted = _dotted_name(node)
            if not isinstance(node, ast.Attribute) or dotted is None:
                continue
            if (
                unit.relative_path == SERVICES_CONTAINER
                and dotted == "self.test"
            ):
                findings.append(
                    Finding(
                        unit.relative_path,
                        node.lineno,
                        "service-container-test-owner",
                        dotted,
                    )
                )
            if dotted.endswith(".services.test") or dotted == "services.test":
                findings.append(
                    Finding(
                        unit.relative_path,
                        node.lineno,
                        "legacy-services-test-access",
                        dotted,
                    )
                )

    factory = _unit(FACTORY_MODE)
    for node, identifier in _identifiers(factory.tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        lowered = identifier.lower()
        if lowered == "_pending_start" or (
            ("active_run" in lowered or "active_config" in lowered)
            and not lowered.startswith("_accepted")
        ):
            findings.append(
                Finding(
                    factory.relative_path,
                    node.lineno,
                    "single-run-factory-state",
                    identifier,
                )
            )
    _assert_no_findings(findings)


def test_main_window_has_no_global_single_review_projection() -> None:
    unit = _unit(MAIN_WINDOW)
    parents = _parent_map(unit.tree)
    findings: list[Finding] = []
    for node in ast.walk(unit.tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _dotted_name(node.args[0]) == "factory_mode"
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "review_payload"
        ):
            findings.append(
                Finding(
                    unit.relative_path,
                    node.lineno,
                    "global-review-source",
                    "getattr(factory_mode, 'review_payload')",
                )
            )
        dotted = _dotted_name(node)
        if not isinstance(node, ast.Attribute) or dotted is None:
            continue
        if dotted == "factory_mode.review_payload":
            findings.append(
                Finding(
                    unit.relative_path,
                    node.lineno,
                    "global-review-source",
                    dotted,
                )
            )
        parent = parents.get(node)
        is_attribute_prefix = (
            isinstance(parent, ast.Attribute) and parent.value is node
        )
        if (
            dotted.startswith("self._review_payload")
            and not is_attribute_prefix
        ):
            findings.append(
                Finding(
                    unit.relative_path,
                    node.lineno,
                    "main-window-global-review",
                    dotted,
                )
            )
        if dotted == "self._factory_state.displayed_config_id":
            findings.append(
                Finding(
                    unit.relative_path,
                    node.lineno,
                    "config-id-active-run-inference",
                    dotted,
                )
            )
    _assert_no_findings(findings)


def test_global_runtime_does_not_connect_emit_or_call_old_callbacks() -> None:
    findings: list[Finding] = []
    allowed = PRIVATE_SELECTED_CARD_PROJECTION | frozenset(
        {LEGACY_TEST_MANAGER, SERVICES_CONTAINER}
    )
    for unit in _production_units():
        if unit.relative_path in allowed:
            continue
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Attribute):
                continue
            dotted = _dotted_name(node)
            if dotted is None or node.attr not in OLD_RUNTIME_CALLBACKS:
                continue
            findings.append(
                Finding(
                    unit.relative_path,
                    node.lineno,
                    "old-runtime-callback",
                    dotted,
                )
            )
    _assert_no_findings(findings)


def test_stft_and_plc_have_no_legacy_calls_or_second_dispatch_path() -> None:
    findings = _call_findings(
        frozenset(
            {
                "replay_selection",
                "can_replay_selection",
                "last_write_test_result_to_plc",
            }
        ),
        allowed_paths=frozenset({LEGACY_TEST_MANAGER}),
        rule="legacy-stft-or-plc-call",
    )

    for unit in _production_units():
        if unit.relative_path in {LEGACY_PLC_SERVICE, LEGACY_TEST_MANAGER}:
            continue
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr in {
                "last_write_test_result_to_plc",
                "write_test_result_sign",
            }:
                findings.append(
                    Finding(
                        unit.relative_path,
                        node.lineno,
                        "legacy-plc-result-wiring",
                        _dotted_name(node) or node.attr,
                    )
                )

    construction_sites: dict[str, list[tuple[str, int]]] = {
        "PlcDispatchPorts": [],
        "PlcResultSequencer": [],
    }
    for unit in _production_units():
        aliases = _imported_aliases(unit.tree)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call):
                continue
            symbol = _called_symbol(node, aliases)
            if symbol in construction_sites:
                construction_sites[symbol].append(
                    (unit.relative_path, node.lineno)
                )
    assert {
        symbol: [relative_path for relative_path, _line in sites]
        for symbol, sites in construction_sites.items()
    } == {
        "PlcDispatchPorts": [SERVICES_CONTAINER],
        "PlcResultSequencer": [SERVICES_CONTAINER],
    }

    for relative_path in PLC_SCOPE:
        unit = _unit(relative_path)
        for node, identifier in _identifiers(unit.tree):
            lowered = identifier.lower()
            if (
                ("pending_testing" in lowered or "testing_pending" in lowered)
                and (
                    "retry" in lowered
                    or "reconcil" in lowered
                    or "state" in lowered
                    or "clear" in lowered
                )
            ):
                findings.append(
                    Finding(
                        relative_path,
                        node.lineno,
                        "pending-testing-reconciliation",
                        identifier,
                    )
                )
    _assert_no_findings(findings)


def test_today_statistics_has_one_committed_terminal_refresh_path() -> None:
    statistics = _unit("ui/modules/today_statistics.py")
    main_window = _unit(MAIN_WINDOW)
    forbidden_signal_accesses = [
        (node.lineno, _dotted_name(node))
        for node in ast.walk(statistics.tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"result_changed", "test_finished"}
    ]
    assert forbidden_signal_accesses == []

    observer_attachments = [
        node.lineno
        for node in ast.walk(main_window.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "attach_committed_history_observer"
    ]
    assert len(observer_attachments) == 1


def test_login_cancel_and_window_close_share_the_application_shutdown_owner() -> None:
    main = _unit(MAIN)
    login_cancel = next(
        node
        for node in ast.walk(main.tree)
        if isinstance(node, ast.FunctionDef) and node.name == "on_login_cancel"
    )
    ordered_calls = [
        _dotted_name(node.value.func)
        for node in login_cancel.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert ordered_calls == [
        "services.shutdown",
        "app.quit",
    ]

    owner_constructions = _call_findings(
        frozenset({"ApplicationShutdownOwner"}),
        rule="application-shutdown-owner-construction",
    )
    assert [
        finding.relative_path for finding in owner_constructions
    ] == [SERVICES_CONTAINER]

    window = _unit(MAIN_WINDOW)
    parents = _parent_map(window.tree)
    forbidden_dependency_shutdowns: list[Finding] = []
    for node in ast.walk(window.tree):
        if not isinstance(node, ast.Call):
            continue
        function = _enclosing_function(node, parents)
        if function is None or function.name != "closeEvent":
            continue
        dotted = _dotted_name(node.func)
        if dotted and dotted.endswith(".shutdown") and dotted not in {
            "self.control.shutdown",
            "self.services.shutdown",
        }:
            forbidden_dependency_shutdowns.append(
                Finding(
                    window.relative_path,
                    node.lineno,
                    "second-shutdown-owner",
                    dotted,
                )
            )
    _assert_no_findings(forbidden_dependency_shutdowns)


def test_manual_judgment_uses_only_selected_run_and_revision_authority() -> None:
    findings = _call_findings(
        frozenset({"apply_manual_judgment"}),
        allowed_paths=frozenset(
            {
                FACTORY_MODE,
                LEGACY_TEST_MANAGER,
                "app/service/multi_run_coordinator_mvp/coordinator.py",
                "app/service/multi_run_integration/production_composition.py",
            }
        ),
        rule="legacy-manual-judgment-call",
    )

    window = _unit(MAIN_WINDOW)
    function = next(
        node
        for node in ast.walk(window.tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_apply_manual_judgment_request"
    )
    forbidden = {
        "displayed_config_id",
        "load_config",
        "review_payload",
        "factory_mode",
    }
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            findings.append(
                Finding(
                    window.relative_path,
                    node.lineno,
                    "manual-global-authority",
                    _dotted_name(node) or node.attr,
                )
            )
    _assert_no_findings(findings)


def test_old_export_is_definition_only_and_simple_export_has_no_side_path() -> None:
    findings = _call_findings(
        frozenset({"export_zip"}),
        allowed_paths=frozenset({LEGACY_AUDIO_DATA_SERVICE}),
        rule="legacy-export-call",
    )
    for unit in _production_units():
        if unit.relative_path == LEGACY_AUDIO_DATA_SERVICE:
            continue
        for node in ast.walk(unit.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "export_zip"
            ):
                findings.append(
                    Finding(
                        unit.relative_path,
                        node.lineno,
                        "dynamic-legacy-export-reachability",
                        "getattr(..., 'export_zip')",
                    )
                )

    forbidden_export_symbols = {
        "AudioDataService",
        "AudioDataTable",
        "FactoryReviewPayload",
        "ResultHistoryCardSnapshot",
        "SqliteDatabase",
    }
    forbidden_export_mechanisms = re.compile(
        r"(?:^|_)(?:resume|repair|retry|recovery|"
        r"partial_success|delivery_receipt|history_card)(?:_|$)",
        re.IGNORECASE,
    )
    alternate_current_settlement = re.compile(
        r"(?:current.*(?:attach|settle|persist|commit|insert|update|apply)|"
        r"(?:attach|settle|persist|commit|insert|update|apply).*current)",
        re.IGNORECASE,
    )
    second_channel_authority = re.compile(
        r"(?:channel.*hardware|hardware.*channel|device_id|"
        r"fallback.*(?:channel|hardware|device))",
        re.IGNORECASE,
    )
    allowed_channel_mapping_names = {
        "_validate_channel_mapping",
        "channel_mapping",
    }
    for relative_path in EXPORT_SCOPE:
        unit = _unit(relative_path)
        for node, identifier in _identifiers(unit.tree):
            if identifier in forbidden_export_symbols:
                findings.append(
                    Finding(
                        relative_path,
                        node.lineno,
                        "old-export-fact",
                        identifier,
                    )
                )
            if forbidden_export_mechanisms.search(identifier):
                findings.append(
                    Finding(
                        relative_path,
                        node.lineno,
                        "excluded-export-subsystem",
                        identifier,
                    )
                )
            if alternate_current_settlement.search(identifier):
                findings.append(
                    Finding(
                        relative_path,
                        node.lineno,
                        "alternate-current-settlement",
                        identifier,
                    )
                )
            if second_channel_authority.search(identifier):
                findings.append(
                    Finding(
                        relative_path,
                        node.lineno,
                        "second-channel-authority",
                        identifier,
                    )
                )
            lowered = identifier.lower()
            if (
                "channel" in lowered
                and "map" in lowered
                and identifier not in allowed_channel_mapping_names
            ):
                findings.append(
                    Finding(
                        relative_path,
                        node.lineno,
                        "second-channel-mapping",
                        identifier,
                    )
                )

    data_management = _unit("ui/modules/data_management.py")
    export_action = next(
        node
        for node in ast.walk(data_management.tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_export_selected_rows"
    )
    action_calls = {
        _dotted_name(node.func)
        for node in ast.walk(export_action)
        if isinstance(node, ast.Call)
    }
    assert "self.services.run_archive_service.export" in action_calls
    assert all(
        call is None
        or not any(
            token in call.lower()
            for token in ("audio_data", "review", "history_card", "current")
        )
        for call in action_calls
    )
    _assert_no_findings(findings)


def test_data_management_uses_run_v2_authority_and_archive_export_only() -> None:
    unit = _unit(DATA_MANAGEMENT)
    aliases = _imported_aliases(unit.tree)
    calls = [
        (_dotted_name(node.func), _called_symbol(node, aliases), node.lineno)
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call)
    ]
    forbidden = {
        "list_durable",
        "list_records",
        "list_records_page",
        "delete_record",
        "export_zip",
    }
    findings = [
        Finding(DATA_MANAGEMENT, line, "legacy-data-management-call", dotted or symbol)
        for dotted, symbol, line in calls
        if symbol in forbidden
    ]
    legacy_service_references = [
        Finding(
            DATA_MANAGEMENT,
            node.lineno,
            "legacy-data-management-service",
            "audio_data_service",
        )
        for node, identifier in _identifiers(unit.tree)
        if identifier == "audio_data_service"
    ]

    _assert_no_findings([*findings, *legacy_service_references])
    assert sum(
        dotted == "self.services.run_archive_service.export"
        for dotted, _symbol, _line in calls
    ) == 1


def test_history_delivery_is_one_way_queued_apply_once_without_receipts() -> None:
    findings: list[Finding] = []
    forbidden_exact = {
        "HistoryPublishStatus",
        "HistoryPublishResult",
        "completion_ack",
        "delivery_receipt",
        "apply_ack",
        "history_mailbox",
        "history_ledger",
        "history_outbox",
        "retry_history",
        "replay_history",
    }
    for relative_path in HISTORY_SCOPE:
        unit = _unit(relative_path)
        for node, identifier in _identifiers(unit.tree):
            if (
                identifier in forbidden_exact
                or identifier == "BlockingQueuedConnection"
            ):
                findings.append(
                    Finding(
                        relative_path,
                        node.lineno,
                        "history-ack-retry-or-second-path",
                        identifier,
                    )
                )

    history_store_apply_calls: list[tuple[str, str | None]] = []
    for unit in _production_units():
        parents = _parent_map(unit.tree)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted_name(node.func)
            if dotted not in {
                "self._history_store.apply",
                "self.result_history_store.apply",
                "result_history_store.apply",
            }:
                continue
            function = _enclosing_function(node, parents)
            history_store_apply_calls.append(
                (
                    unit.relative_path,
                    None if function is None else function.name,
                )
            )
    assert history_store_apply_calls == [
        ("ui/modules/result_dashboard.py", "apply_history_event")
    ]

    history_adapter_constructions = _call_findings(
        frozenset({"HistoryQtAdapter"}),
        rule="history-adapter-construction",
    )
    assert [
        finding.relative_path for finding in history_adapter_constructions
    ] == [SERVICES_CONTAINER]
    _assert_no_findings(findings)


def test_audit_uses_ast_not_comments_docs_or_negative_test_strings() -> None:
    source = '''
# TestManager() and services.test.export_zip() are prohibited examples.
TEXT = "AudioRecorder(); app.quit(); HistoryPublishResult"
from legacy import TestManager as LegacyManager

LegacyManager()
'''
    tree = ast.parse(source)
    aliases = _imported_aliases(tree)
    calls = [
        _called_symbol(node, aliases)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    assert calls == ["TestManager"]
