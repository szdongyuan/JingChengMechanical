"""Fail-closed evidence for the frozen final aggregate inputs."""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterable


COMPOSITE_TIP = "9659a287e0497160098942e514942ff73e2c214a"
COMPOSITE_BASE = "7756733062b4f820f3ff1fb0c56505d6629eca63"
COMPOSITE_CONTRACT = "83d20a446f8f41c45d1a97d7ce25adb8f620c385"
COMPOSITE_DEPENDENCIES = (
    "e8946cec6ea7c23971545104fbc64c37365d50cc",
    "1e585053da861aa8f6c20cd2a265286e97411e87",
    "6a7edc8a4e2f7d97beb3b70e82330ec4b913d827",
    "eba4b55733ae6eab91992577e8b1eb30d16377f8",
    "c5e6e75ecda81d36f5f709c3db3cbf458cfc749f",
    "73cb0d57b493c76a22422296d348b2447ce49a7c",
)
FINAL_INTEGRATION_BASE = "b453621ef629610a32e79c442a928d610bd46d2a"
UI_TIP = "22ea1b3ee4b96f3705ce698593fc945f1919e13f"
PLC_SEQUENCER_HEAD = "eba4b55733ae6eab91992577e8b1eb30d16377f8"
SELECTED_WINDOW_HEAD = "c5e6e75ecda81d36f5f709c3db3cbf458cfc749f"
PLAYBACK_TIP = "456b6bb71e50cc43ff3d45d550f0a41f2fb050db"
LOW_LATENCY_TIP = "7db52ac9805cd2380e9d512114925ae0648461d4"
FINAL_DEPENDENCIES = (
    UI_TIP,
    COMPOSITE_TIP,
    PLAYBACK_TIP,
    LOW_LATENCY_TIP,
)
FINAL_UI_MERGE = "5d26b71fe27ed8b0fb5d73eadbc07153328fab6f"
FINAL_COMPOSITE_MERGE = "804254149ff48035ecd2e07b7ceb74dc0f883ff2"
FINAL_PLAYBACK_MERGE = "0550b33cc1a6fb4c7052a5e3fc4fb74291952eaf"
FINAL_LOW_LATENCY_MERGE = "b13b3e0038c2955c4aaffedf7f6d87312c52b775"
PRODUCTION_PREREQUISITE_BASE = "aa5186d626edf96afff5cafe4b522606260b1096"
PRODUCTION_RUN_FACTS_TIP = "6fc223bab4c17936ddce563134d4b2ac79fae35b"
PRODUCTION_RUN_FACTS_MERGE = "bfffe2560c442bb73be969297804c2d657ce324f"
PRODUCTION_HARDWARE_ANALYSIS_TIP = (
    "67eb92acd7e0a88cb0dcc7e862befe3ab28d394d"
)
PRODUCTION_HARDWARE_ANALYSIS_MERGE = (
    "49d54bc3cccc93215072ecba86eb6b6d251da8f8"
)
PRODUCTION_COMPOSITION_TIP = "a5adfca65b92301d4ce898d85d47405172ca00e7"
PRODUCTION_COMPOSITION_MERGE = "a425975ff7665111c8a974882da289947d76786a"
TASK13_RESULT_OPERATIONS_TIP = "b55a0b590a858d64c5d1a4eac5d1bf21662f8bd3"
TASK13_RESULT_OPERATIONS_MERGE = "2970ae272c2ff61c1e84dfe78a8831dfbec6d0ea"
FINAL_ACCEPTANCE_EXPORT_MERGE = "604c97e9ed72a9334b4d77791fd4a9aa46ee893b"
FINAL_ACCEPTANCE_EXPORT_BASE = "2970ae272c2ff61c1e84dfe78a8831dfbec6d0ea"
FINAL_ACCEPTANCE_EXPORT_TIP = "a61e581c619f21416e31ca72c8435d05c5878067"
FINAL_REVIEWED_MERGES = (
    (FINAL_UI_MERGE, FINAL_INTEGRATION_BASE, UI_TIP),
    (FINAL_COMPOSITE_MERGE, FINAL_UI_MERGE, COMPOSITE_TIP),
    (FINAL_PLAYBACK_MERGE, FINAL_COMPOSITE_MERGE, PLAYBACK_TIP),
    (FINAL_LOW_LATENCY_MERGE, FINAL_PLAYBACK_MERGE, LOW_LATENCY_TIP),
    (
        PRODUCTION_RUN_FACTS_MERGE,
        PRODUCTION_PREREQUISITE_BASE,
        PRODUCTION_RUN_FACTS_TIP,
    ),
    (
        PRODUCTION_HARDWARE_ANALYSIS_MERGE,
        PRODUCTION_RUN_FACTS_MERGE,
        PRODUCTION_HARDWARE_ANALYSIS_TIP,
    ),
    (
        PRODUCTION_COMPOSITION_MERGE,
        PRODUCTION_HARDWARE_ANALYSIS_MERGE,
        PRODUCTION_COMPOSITION_TIP,
    ),
    (
        TASK13_RESULT_OPERATIONS_MERGE,
        PRODUCTION_COMPOSITION_MERGE,
        TASK13_RESULT_OPERATIONS_TIP,
    ),
    (
        FINAL_ACCEPTANCE_EXPORT_MERGE,
        FINAL_ACCEPTANCE_EXPORT_BASE,
        FINAL_ACCEPTANCE_EXPORT_TIP,
    ),
)
CORE_PATH = (
    "docs/double-sdd/specs/"
    "2026-07-25-multi-run-audio-testing-core-design.md"
)
CORE_BLOB = "3352bb47f2fd89a585839b6bc07f96901ac03f7c"

COORDINATOR_PATH = "app/service/multi_run_coordinator_mvp/coordinator.py"
CONTRACTS_PATH = "app/service/multi_run_coordinator_mvp/contracts.py"
PORTS_PATH = "app/service/multi_run_coordinator_mvp/ports.py"
RESIDENT_PATH = (
    "app/service/multi_run_coordinator_mvp/managed_resident_capture_port.py"
)
SEQUENCER_PATH = "app/service/plc_result_sequencer.py"
PLAYBACK_CONTRACTS_PATH = "app/service/run_scoped_playback/contracts.py"
PLAYBACK_SERVICE_PATH = "app/service/run_scoped_playback/service.py"
PLAYBACK_PUBLIC_PATH = "app/service/run_scoped_playback/__init__.py"


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        text=True,
        encoding="utf-8",
    ).strip()


def _source(tip: str, path: str) -> str:
    return _git("show", f"{tip}:{path}")


def _tree(tip: str, path: str) -> ast.Module:
    return ast.parse(_source(tip, path), filename=f"{tip}:{path}")


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    node = next(
        (
            candidate
            for candidate in tree.body
            if isinstance(candidate, ast.ClassDef) and candidate.name == name
        ),
        None,
    )
    assert node is not None, f"missing pinned class: {name}"
    return node


def _method(node: ast.ClassDef, name: str) -> ast.FunctionDef:
    result = next(
        (
            candidate
            for candidate in node.body
            if isinstance(candidate, ast.FunctionDef) and candidate.name == name
        ),
        None,
    )
    assert result is not None, f"missing pinned method: {node.name}.{name}"
    return result


def _annotation(node: ast.arg | ast.FunctionDef) -> str:
    annotation = node.annotation if isinstance(node, ast.arg) else node.returns
    assert annotation is not None
    return ast.unparse(annotation)


def _assert_ancestor(ancestor: str, descendant: str) -> None:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
    )
    assert result.returncode == 0, f"{ancestor} is not an ancestor of {descendant}"


def _assert_public_exports(source: str, expected: Iterable[str]) -> None:
    tree = ast.parse(source)
    assignment = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        ),
        None,
    )
    assert assignment is not None, "missing pinned __all__"
    assert isinstance(assignment.value, (ast.List, ast.Tuple))
    exports = {
        element.value
        for element in assignment.value.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }
    assert set(expected) <= exports


def test_frozen_composite_has_exact_base_merge_base_range_and_core_blob() -> None:
    assert _git("cat-file", "-t", COMPOSITE_TIP) == "commit"
    assert _git("cat-file", "-t", COMPOSITE_BASE) == "commit"
    assert _git("merge-base", COMPOSITE_BASE, COMPOSITE_TIP) == COMPOSITE_BASE
    assert _git("rev-list", "--count", f"{COMPOSITE_BASE}..{COMPOSITE_TIP}") == "95"
    assert len(_git("diff", "--name-only", COMPOSITE_BASE, COMPOSITE_TIP).splitlines()) == 31
    assert _git("rev-parse", f"{COMPOSITE_TIP}:{CORE_PATH}") == CORE_BLOB


def test_frozen_composite_contains_contract_and_all_six_approved_dependencies() -> None:
    _assert_ancestor(COMPOSITE_CONTRACT, COMPOSITE_TIP)
    for dependency in COMPOSITE_DEPENDENCIES:
        _assert_ancestor(dependency, COMPOSITE_TIP)

    assert PLC_SEQUENCER_HEAD in COMPOSITE_DEPENDENCIES
    assert SELECTED_WINDOW_HEAD in COMPOSITE_DEPENDENCIES


def test_frozen_history_public_api_is_exact() -> None:
    ports = _tree(COMPOSITE_TIP, PORTS_PATH)
    publish = _method(_class(ports, "ResultHistoryEventSink"), "publish")

    assert [argument.arg for argument in publish.args.args] == ["self", "event"]
    assert _annotation(publish.args.args[1]) == "ProjectedHistoryEvent"
    assert _annotation(publish) == "None"
    assert _class(_tree(COMPOSITE_TIP, CONTRACTS_PATH), "ProjectedHistoryEvent")
    _assert_public_exports(
        _source(
            COMPOSITE_TIP,
            "app/service/multi_run_coordinator_mvp/__init__.py",
        ),
        ("ProjectedHistoryEvent", "ResultHistoryEventSink"),
    )


def test_frozen_plc_dispatch_callable_api_is_exact() -> None:
    sequencer = _class(_tree(COMPOSITE_TIP, SEQUENCER_PATH), "PlcResultSequencer")
    init = _method(sequencer, "__init__")
    keyword_only = {argument.arg: argument for argument in init.args.kwonlyargs}

    assert _annotation(keyword_only["write_result"]) == (
        "Callable[[int, Mapping[str, PlcScalar]], None]"
    )
    assert _annotation(keyword_only["write_testing_state"]) == (
        "Callable[[int, bool], None]"
    )


def test_frozen_run_playback_request_and_service_api_are_exact() -> None:
    contracts = _tree(PLAYBACK_TIP, PLAYBACK_CONTRACTS_PATH)
    request = _class(contracts, "RunPlaybackRequest")
    fields = {
        node.target.id: ast.unparse(node.annotation)
        for node in request.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert fields == {
        "run_id": "str",
        "display_channel_index": "int",
        "window_start_seconds": "float",
        "window_end_seconds": "float",
    }

    service = _class(_tree(PLAYBACK_TIP, PLAYBACK_SERVICE_PATH), "RunScopedPlaybackService")
    expected_methods = {
        "play": (["self", "request"], ["RunPlaybackRequest"], "PlaybackResult"),
        "stop": (
            ["self", "timeout_seconds"],
            ["float"],
            "PlaybackLifecycleReport",
        ),
        "join": (
            ["self", "timeout_seconds"],
            ["float"],
            "PlaybackLifecycleReport",
        ),
        "shutdown": (
            ["self", "timeout_seconds"],
            ["float"],
            "PlaybackLifecycleReport",
        ),
    }
    for name, (argument_names, annotations, return_annotation) in expected_methods.items():
        pinned = _method(service, name)
        assert [argument.arg for argument in pinned.args.args] == argument_names
        assert [_annotation(argument) for argument in pinned.args.args[1:]] == annotations
        assert _annotation(pinned) == return_annotation

    _assert_public_exports(
        _source(PLAYBACK_TIP, PLAYBACK_PUBLIC_PATH),
        (
            "RunPlaybackRequest",
            "RunScopedPlaybackService",
            "PlaybackResult",
            "PlaybackLifecycleReport",
        ),
    )


def test_export_handoff_pins_coordinator_constructor_and_direct_save_start() -> None:
    coordinator = _class(_tree(COMPOSITE_TIP, COORDINATOR_PATH), "MultiRunCoordinator")
    init_args = [argument.arg for argument in _method(coordinator, "__init__").args.args]
    assert init_args == [
        "self",
        "captured_freezer",
        "imported_freezer",
        "resident",
        "persistence",
        "save_dispatcher",
        "scheduler",
        "history_projector",
        "history_sink",
        "plc",
        "diagnostics",
        "save_job_id_factory",
        "wall_clock",
        "monotonic_clock",
    ]

    process = _method(coordinator, "_process_capture_outcome")
    process_calls = {
        node.func.attr
        for node in ast.walk(process)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_begin_save" in process_calls


def test_export_handoff_pins_freezers_and_persistence_port() -> None:
    ports = _tree(COMPOSITE_TIP, PORTS_PATH)
    for freezer_name, request_type, result_type in (
        ("CapturedRunFreezer", "CapturedStartRequest", "FrozenCapturedRun"),
        ("ImportedRunFreezer", "ImportedStartRequest", "FrozenImportedRun"),
    ):
        freeze = _method(_class(ports, freezer_name), "freeze")
        assert [argument.arg for argument in freeze.args.args] == ["self", "request"]
        assert _annotation(freeze.args.args[1]) == request_type
        assert _annotation(freeze) == result_type

    persistence = _class(ports, "MultiRunPersistencePort")
    persistence_methods = [
        node.name for node in persistence.body if isinstance(node, ast.FunctionDef)
    ]
    assert persistence_methods == [
        "create_active",
        "commit_initial_terminal",
        "apply_manual_judgment",
        "load_result_control_facts",
    ]


def test_export_handoff_pins_captured_audio_and_resident_mapping_seam() -> None:
    audio_value = _class(_tree(COMPOSITE_TIP, CONTRACTS_PATH), "CapturedAudioValue")
    audio_fields = [
        node.target.id
        for node in audio_value.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert audio_fields == [
        "source_identity",
        "sample_rate_hz",
        "channel_count",
        "frame_count",
        "format",
        "interleaved_bytes",
    ]

    resident = _class(_tree(COMPOSITE_TIP, RESIDENT_PATH), "ManagedResidentCapturePort")
    mapper = _method(resident, "_map_completed_audio")
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "channel_indices"
        for node in ast.walk(mapper)
    )
    constructors = [
        node
        for node in ast.walk(mapper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CapturedAudioValue"
    ]
    assert len(constructors) == 1
    assert "channel_indices" not in {
        keyword.arg for keyword in constructors[0].keywords
    }


def test_final_tip_has_ordered_dependency_merges_and_preserved_core_blob() -> None:
    final_tip = _git("rev-parse", "HEAD")
    _assert_ancestor(FINAL_INTEGRATION_BASE, final_tip)

    rows = [
        line.split()
        for line in _git(
            "rev-list",
            "--first-parent",
            "--parents",
            f"{FINAL_INTEGRATION_BASE}..{final_tip}",
        ).splitlines()
    ]
    assert all(len(row) <= 3 for row in rows), "octopus merge in final lane"
    merge_rows = [row for row in rows if len(row) == 3]
    assert [tuple(row) for row in merge_rows] == list(
        reversed(FINAL_REVIEWED_MERGES)
    )

    merges_by_dependency = {row[2]: row for row in merge_rows}
    ui_merge = merges_by_dependency[UI_TIP]
    composite_merge = merges_by_dependency[COMPOSITE_TIP]
    playback_merge = merges_by_dependency[PLAYBACK_TIP]
    low_latency_merge = merges_by_dependency[LOW_LATENCY_TIP]

    assert ui_merge[1] == FINAL_INTEGRATION_BASE
    assert composite_merge[1] == ui_merge[0]
    assert playback_merge[1] == composite_merge[0]
    assert low_latency_merge[1] == playback_merge[0]

    for dependency in FINAL_DEPENDENCIES:
        _assert_ancestor(dependency, final_tip)
    assert _git("merge-base", LOW_LATENCY_TIP, final_tip) == LOW_LATENCY_TIP
    assert _git("rev-parse", f"{final_tip}:{CORE_PATH}") == CORE_BLOB

    final_lane_second_parents = {row[2] for row in merge_rows}
    for contained_dependency in (PLC_SEQUENCER_HEAD, SELECTED_WINDOW_HEAD):
        _assert_ancestor(contained_dependency, COMPOSITE_TIP)
        assert contained_dependency not in final_lane_second_parents
