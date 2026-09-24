from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from app.service.run_record_export.contracts import (
    AiExportValue,
    AudioExportFacts,
    CurrentExportFacts,
    PackagedAudio,
    PackagedAudioChannel,
    PackagedCurrent,
    PackagedRunRecord,
    RunDataExportOptions,
    RunDataExportRecord,
    RunDataExportSkippedRecord,
)
from app.service.run_record_export.manifests import (
    build_export_manifest,
    build_record_manifest,
    validate_declared_members,
)
from app.service.run_record_export.naming import (
    audio_member_path,
    current_member_path,
    record_directory,
)


RUN_IDS = (
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000003",
    "00000000-0000-4000-8000-000000000004",
)


def _packaged_record(
    sequence: int,
    selection_index: int,
    *,
    sn: str,
    state: str = "SUCCEEDED",
    judgment: str | None = "PASS",
    audio_status: str = "not_selected",
    current_status: str = "not_selected",
    ai_score: float | None = 0.95,
    ai_label: str | None = "PASS",
) -> PackagedRunRecord:
    record = RunDataExportRecord(
        run_id=RUN_IDS[selection_index],
        selection_index=selection_index,
        accepted_at=f"2026-09-0{sequence}T01:02:03.000000Z",
        terminal_at=f"2026-09-0{sequence}T01:03:03.000000Z",
        sn=sn,
        model="M1",
        state=state,
        terminal_code="completed",
        judgment=judgment,
        judgment_source="AUTOMATIC" if judgment is not None else "NONE",
        fault_type=None,
        audio=AudioExportFacts(None, None, None, None, None, None),
        current=CurrentExportFacts(None, None),
    )
    directory = record_directory(sequence, sn)
    if audio_status == "exported":
        audio = PackagedAudio(
            True,
            "exported",
            None,
            48_000,
            96_000,
            (
                PackagedAudioChannel(
                    1, 0, audio_member_path(directory, source_channel=0)
                ),
                PackagedAudioChannel(
                    2, 2, audio_member_path(directory, source_channel=2)
                ),
            ),
        )
    elif audio_status == "skipped":
        audio = PackagedAudio(True, "skipped", "audio missing", None, None, ())
    else:
        audio = PackagedAudio(False, "not_selected", None, None, None, ())

    if current_status == "exported":
        current = PackagedCurrent(
            True, "exported", None, current_member_path(directory)
        )
    elif current_status == "skipped":
        current = PackagedCurrent(True, "skipped", "current invalid", None)
    else:
        current = PackagedCurrent(False, "not_selected", None, None)

    return PackagedRunRecord(
        sequence=sequence,
        record=record,
        ai=AiExportValue(ai_score, ai_label),
        directory=directory,
        audio=audio,
        current=current,
    )


def _decode(payload: bytes) -> dict[str, object]:
    assert not payload.startswith(b"\xef\xbb\xbf")
    return json.loads(payload.decode("utf-8"))


def _compact(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def test_record_manifest_has_exact_v2_schema_and_compact_utf8_json():
    record = _packaged_record(
        1,
        0,
        sn="序列一",
        audio_status="exported",
        current_status="skipped",
    )
    expected = {
        "schema": "jingcheng-run-export-record/v2",
        "sequence": 1,
        "run": {
            "run_id": RUN_IDS[0],
            "accepted_at": "2026-09-01T01:02:03.000000Z",
            "sn": "序列一",
            "model": "M1",
        },
        "outcome": {
            "result": "OK",
            "judgment": "PASS",
            "judgment_source": "AUTOMATIC",
            "fault_type": None,
        },
        "ai": {"score": 0.95, "label": "PASS"},
        "audio": {
            "selected": True,
            "status": "exported",
            "reason": None,
            "sample_rate_hz": 48_000,
            "frame_count": 96_000,
            "channels": [
                {
                    "position": 1,
                    "source_channel": 0,
                    "path": "records/000001_序列一/audio/CH1.wav",
                },
                {
                    "position": 2,
                    "source_channel": 2,
                    "path": "records/000001_序列一/audio/CH3.wav",
                },
            ],
        },
        "current": {
            "selected": True,
            "status": "skipped",
            "reason": "current invalid",
            "path": None,
        },
    }

    payload = build_record_manifest(record)

    assert _decode(payload) == expected
    assert payload == _compact(expected)


def test_record_manifest_projects_error_outcome_and_nullable_ai():
    record = _packaged_record(
        1,
        0,
        sn="SN",
        state="INTERRUPTED",
        judgment=None,
        ai_score=None,
        ai_label=None,
    )

    manifest = _decode(build_record_manifest(record))

    assert manifest["outcome"] == {
        "result": "ERROR",
        "judgment": None,
        "judgment_source": "NONE",
        "fault_type": None,
    }
    assert manifest["ai"] == {"score": None, "label": None}
    assert manifest["audio"] == {
        "selected": False,
        "status": "not_selected",
        "reason": None,
        "sample_rate_hz": None,
        "frame_count": None,
        "channels": [],
    }
    assert manifest["current"] == {
        "selected": False,
        "status": "not_selected",
        "reason": None,
        "path": None,
    }


def test_root_manifest_has_exact_counts_artifacts_and_deterministic_order():
    # Export order intentionally differs from original selection order.
    first = _packaged_record(
        1,
        2,
        sn="later-selection",
        audio_status="exported",
        current_status="skipped",
    )
    second = _packaged_record(
        2,
        0,
        sn="first-selection",
        audio_status="skipped",
        current_status="exported",
    )
    skips = (
        RunDataExportSkippedRecord(RUN_IDS[3], "primary", "missing", 3),
        RunDataExportSkippedRecord(RUN_IDS[2], "current", "current invalid", 2),
        RunDataExportSkippedRecord(RUN_IDS[1], "primary", "bad primary", 1),
        RunDataExportSkippedRecord(RUN_IDS[0], "audio", "audio missing", 0),
    )
    expected = {
        "schema": "jingcheng-run-export-package/v2",
        "exported_at": "2026-09-03T01:02:03.000000Z",
        "options": {
            "excel": True,
            "audio": True,
            "database": False,
            "current_csv": True,
        },
        "counts": {
            "selected_runs": 4,
            "exported_runs": 2,
            "skipped_runs": 2,
            "audio_runs_emitted": 1,
            "audio_runs_skipped": 1,
            "audio_files_emitted": 2,
            "current_runs_emitted": 1,
            "current_runs_skipped": 1,
        },
        "artifacts": {"excel": "检测记录.xlsx", "database": None},
        "records": [
            {
                "sequence": 1,
                "run_id": RUN_IDS[2],
                "directory": "records/000001_later-selection",
                "manifest": "records/000001_later-selection/manifest.json",
            },
            {
                "sequence": 2,
                "run_id": RUN_IDS[0],
                "directory": "records/000002_first-selection",
                "manifest": "records/000002_first-selection/manifest.json",
            },
        ],
        "skips": [
            {
                "run_id": RUN_IDS[0],
                "component": "audio",
                "reason": "audio missing",
            },
            {
                "run_id": RUN_IDS[1],
                "component": "primary",
                "reason": "bad primary",
            },
            {
                "run_id": RUN_IDS[2],
                "component": "current",
                "reason": "current invalid",
            },
            {"run_id": RUN_IDS[3], "component": "primary", "reason": "missing"},
        ],
    }

    payload = build_export_manifest(
        options=RunDataExportOptions(True, True, False, True),
        exported_at=datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc),
        selected_run_ids=RUN_IDS,
        records=(second, first),
        skips=skips,
        excel_path="检测记录.xlsx",
        database_path=None,
    )

    assert _decode(payload) == expected
    assert payload == _compact(expected)


def test_unselected_component_counts_are_zero():
    record = _packaged_record(1, 0, sn="SN")

    manifest = _decode(
        build_export_manifest(
            options=RunDataExportOptions(True, False, False, False),
            exported_at="2026-09-03T01:02:03.000000Z",
            selected_run_ids=(RUN_IDS[0],),
            records=(record,),
            skips=(),
            excel_path="检测记录.xlsx",
            database_path=None,
        )
    )

    assert manifest["counts"] == {
        "selected_runs": 1,
        "exported_runs": 1,
        "skipped_runs": 0,
        "audio_runs_emitted": 0,
        "audio_runs_skipped": 0,
        "audio_files_emitted": 0,
        "current_runs_emitted": 0,
        "current_runs_skipped": 0,
    }


def _valid_package():
    record = _packaged_record(
        1,
        0,
        sn="SN",
        audio_status="exported",
        current_status="exported",
    )
    root = build_export_manifest(
        options=RunDataExportOptions(True, True, False, True),
        exported_at="2026-09-03T01:02:03.000000Z",
        selected_run_ids=(RUN_IDS[0],),
        records=(record,),
        skips=(),
        excel_path="检测记录.xlsx",
        database_path=None,
    )
    manifest_path = f"{record.directory}/manifest.json"
    record_manifests = {manifest_path: build_record_manifest(record)}
    members = {
        "export_manifest.json",
        "检测记录.xlsx",
        manifest_path,
        *(channel.path for channel in record.audio.channels),
        record.current.path,
    }
    return root, record_manifests, members


def test_declared_members_accepts_exact_package_closure():
    root, record_manifests, members = _valid_package()

    validate_declared_members(
        root_manifest=root,
        record_manifests=record_manifests,
        members=members,
    )


def test_declared_members_rejects_undeclared_attachment():
    root, record_manifests, members = _valid_package()
    members.add("records/000001_SN/audio/CH99.wav")

    with pytest.raises(ValueError, match="undeclared"):
        validate_declared_members(
            root_manifest=root,
            record_manifests=record_manifests,
            members=members,
        )


def test_declared_members_rejects_path_to_absent_member():
    root, record_manifests, members = _valid_package()
    members.remove("records/000001_SN/audio/CH1.wav")

    with pytest.raises(ValueError, match="missing"):
        validate_declared_members(
            root_manifest=root,
            record_manifests=record_manifests,
            members=members,
        )


def test_declared_members_rejects_duplicate_declared_member_paths():
    root, record_manifests, members = _valid_package()
    path = next(iter(record_manifests))
    document = _decode(record_manifests[path])
    document["audio"]["channels"][1]["path"] = document["audio"][
        "channels"
    ][0]["path"]
    record_manifests[path] = _compact(document)

    with pytest.raises(ValueError, match="duplicate"):
        validate_declared_members(
            root_manifest=root,
            record_manifests=record_manifests,
            members=members,
        )


def test_declared_members_rejects_traversal_segments():
    root, record_manifests, members = _valid_package()
    document = _decode(root)
    document["artifacts"]["excel"] = "../检测记录.xlsx"

    with pytest.raises(ValueError, match="path"):
        validate_declared_members(
            root_manifest=_compact(document),
            record_manifests=record_manifests,
            members=members,
        )


def test_declared_members_rejects_status_path_contradiction():
    root, record_manifests, members = _valid_package()
    path = next(iter(record_manifests))
    document = _decode(record_manifests[path])
    document["current"]["status"] = "skipped"

    with pytest.raises(ValueError, match="current"):
        validate_declared_members(
            root_manifest=root,
            record_manifests={path: _compact(document)},
            members=members,
        )


def test_declared_members_rejects_count_mismatch():
    root, record_manifests, members = _valid_package()
    document = _decode(root)
    document["counts"]["audio_files_emitted"] = 99

    with pytest.raises(ValueError, match="count"):
        validate_declared_members(
            root_manifest=_compact(document),
            record_manifests=record_manifests,
            members=members,
        )


def test_declared_members_rejects_manifest_missing_from_root_index():
    root, record_manifests, members = _valid_package()
    document = _decode(root)
    document["records"] = []

    with pytest.raises(ValueError, match="manifest"):
        validate_declared_members(
            root_manifest=_compact(document),
            record_manifests=record_manifests,
            members=members,
        )


@pytest.mark.parametrize(
    "bad_timestamp",
    [
        "2026-09-03T01:02:03Z",
        "2026-09-03T01:02:03.000000+00:00",
        "2026-09-03T09:02:03.000000Z+08:00",
    ],
)
def test_builders_reject_noncanonical_utc_timestamps(bad_timestamp: str):
    record = _packaged_record(1, 0, sn="SN")
    object.__setattr__(record.record, "accepted_at", bad_timestamp)

    with pytest.raises(ValueError, match="timestamp"):
        build_record_manifest(record)


def test_export_builder_rejects_artifact_option_contradiction():
    record = _packaged_record(1, 0, sn="SN")

    with pytest.raises(ValueError, match="excel"):
        build_export_manifest(
            options=RunDataExportOptions(True, False, False, False),
            exported_at="2026-09-03T01:02:03.000000Z",
            selected_run_ids=(RUN_IDS[0],),
            records=(record,),
            skips=(),
            excel_path=None,
            database_path=None,
        )
