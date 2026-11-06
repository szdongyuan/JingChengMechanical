from dataclasses import FrozenInstanceError

import pytest

from app.service.run_record_export.contracts import (
    AiExportValue,
    AudioExportFacts,
    CurrentExportFacts,
    PackagedAudio,
    PackagedAudioChannel,
    PackagedCurrent,
    PackagedRunRecord,
    RunDataExportCounts,
    RunDataExportOptions,
    RunDataExportReadReport,
    RunDataExportRecord,
    RunDataExportResult,
    RunDataExportSkippedRecord,
)
from app.service.run_record_export.naming import (
    audio_member_path,
    current_member_path,
    excel_judgment_source,
    excel_result,
    manifest_result,
    record_directory,
    sanitize_sn_segment,
)


RUN_ID_1 = "00000000-0000-4000-8000-000000000001"
RUN_ID_2 = "00000000-0000-4000-8000-000000000002"


def _record(*, run_id: str = RUN_ID_1, selection_index: int = 0):
    return RunDataExportRecord(
        run_id=run_id,
        selection_index=selection_index,
        accepted_at="2026-09-03T01:02:03.000000Z",
        terminal_at="2026-09-03T01:03:03.000000Z",
        sn="00123",
        model="M1",
        state="SUCCEEDED",
        terminal_code="completed",
        judgment="PASS",
        judgment_source="AUTOMATIC",
        fault_type=None,
        audio=AudioExportFacts(
            source_wav_path="C:/data/source.wav",
            sample_rate_hz=48_000,
            channel_count=2,
            frame_count=96_000,
            channel_order=(0, 1),
            issue=None,
        ),
        current=CurrentExportFacts(samples=((0.0, 1.0),), issue=None),
    )


def test_default_export_options_match_approved_dialog_defaults():
    options = RunDataExportOptions.defaults()
    assert options == RunDataExportOptions(
        include_excel=True,
        include_audio=True,
        include_database=True,
        include_current_csv=False,
    )
    assert options.output_kind == "zip"


def test_excel_only_routes_to_xlsx():
    options = RunDataExportOptions(True, False, False, False)
    assert options.output_kind == "xlsx"


@pytest.mark.parametrize(
    "values",
    [
        (True, True, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
    ],
)
def test_every_other_valid_option_combination_routes_to_zip(values):
    assert RunDataExportOptions(*values).output_kind == "zip"


@pytest.mark.parametrize("invalid", [None, 0, 1, "true"])
def test_export_options_require_exact_booleans(invalid):
    with pytest.raises(TypeError, match="exact booleans"):
        RunDataExportOptions(invalid, False, False, True)


def test_export_options_reject_all_false_and_are_immutable():
    with pytest.raises(ValueError, match="at least one"):
        RunDataExportOptions(False, False, False, False)

    options = RunDataExportOptions.defaults()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        options.include_excel = False


def test_ai_value_accepts_finite_numbers_and_preserves_raw_label():
    assert AiExportValue(score=2, label=" PASS ") == AiExportValue(
        score=2.0,
        label=" PASS ",
    )
    for score in (True, float("nan"), float("inf")):
        with pytest.raises((TypeError, ValueError)):
            AiExportValue(score=score, label=None)
    with pytest.raises(ValueError, match="nonblank"):
        AiExportValue(score=None, label=" \t")


def test_projection_values_validate_identity_indexes_and_report_alignment():
    record = _record()
    skipped = RunDataExportSkippedRecord(
        run_id=RUN_ID_2,
        component="primary",
        reason="missing durable row",
        selection_index=1,
    )
    report = RunDataExportReadReport(
        selected_run_ids=(RUN_ID_1, RUN_ID_2),
        records=(record,),
        skipped=(skipped,),
    )
    assert report.records == (record,)

    with pytest.raises(ValueError, match="selection"):
        RunDataExportReadReport(
            selected_run_ids=(RUN_ID_1, RUN_ID_2),
            records=(_record(selection_index=1),),
            skipped=(skipped,),
        )
    with pytest.raises(ValueError, match="UUIDv4"):
        _record(run_id="ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF")
    with pytest.raises(ValueError, match="selection index"):
        _record(selection_index=-1)


def test_read_report_rejects_duplicate_component_skip_for_selection():
    duplicate_audio_skips = (
        RunDataExportSkippedRecord(RUN_ID_1, "audio", "missing", 0),
        RunDataExportSkippedRecord(RUN_ID_1, "audio", "unreadable", 0),
    )
    with pytest.raises(ValueError, match="component"):
        RunDataExportReadReport(
            selected_run_ids=(RUN_ID_1,),
            records=(_record(),),
            skipped=duplicate_audio_skips,
        )


def test_read_report_rejects_attachment_skips_for_primary_skipped_run():
    skips = (
        RunDataExportSkippedRecord(RUN_ID_2, "primary", "missing", 1),
        RunDataExportSkippedRecord(RUN_ID_2, "current", "malformed", 1),
    )
    with pytest.raises(ValueError, match="primary skip"):
        RunDataExportReadReport(
            selected_run_ids=(RUN_ID_1, RUN_ID_2),
            records=(_record(),),
            skipped=skips,
        )


def test_packaged_values_enforce_three_state_attachment_contracts():
    not_selected_audio = PackagedAudio(
        selected=False,
        status="not_selected",
        reason=None,
        sample_rate_hz=None,
        frame_count=None,
        channels=(),
    )
    not_selected_current = PackagedCurrent(
        selected=False,
        status="not_selected",
        reason=None,
        path=None,
    )
    packaged = PackagedRunRecord(
        sequence=1,
        record=_record(),
        ai=AiExportValue(None, None),
        directory="records/000001_00123",
        audio=not_selected_audio,
        current=not_selected_current,
    )
    assert packaged.run is packaged.record

    with pytest.raises(ValueError, match="not_selected"):
        PackagedAudio(
            selected=True,
            status="not_selected",
            reason=None,
            sample_rate_hz=None,
            frame_count=None,
            channels=(),
        )
    with pytest.raises(ValueError, match="exported"):
        PackagedCurrent(
            selected=True,
            status="exported",
            reason=None,
            path=None,
        )


def _not_selected_audio():
    return PackagedAudio(False, "not_selected", None, None, None, ())


def _not_selected_current():
    return PackagedCurrent(False, "not_selected", None, None)


def _exported_audio(*channels: PackagedAudioChannel):
    return PackagedAudio(
        selected=True,
        status="exported",
        reason=None,
        sample_rate_hz=48_000,
        frame_count=96_000,
        channels=tuple(channels),
    )


@pytest.mark.parametrize(
    "directory",
    ["records/000001_00123/nested", "records/000001_wrong-sn"],
)
def test_packaged_record_requires_exact_canonical_directory(directory):
    with pytest.raises(ValueError, match="directory"):
        PackagedRunRecord(
            sequence=1,
            record=_record(),
            ai=AiExportValue(None, None),
            directory=directory,
            audio=_not_selected_audio(),
            current=_not_selected_current(),
        )


@pytest.mark.parametrize(
    "path",
    [
        "records/000002_00123/audio/CH1.wav",
        "records/000001_00123/audio/wrong.wav",
    ],
)
def test_packaged_record_rejects_cross_record_or_wrong_audio_path(path):
    audio = _exported_audio(PackagedAudioChannel(1, 0, path))
    with pytest.raises(ValueError, match="audio.*path"):
        PackagedRunRecord(
            sequence=1,
            record=_record(),
            ai=AiExportValue(None, None),
            directory="records/000001_00123",
            audio=audio,
            current=_not_selected_current(),
        )


def test_packaged_record_rejects_duplicate_audio_source_channel_identity():
    with pytest.raises(ValueError, match="source channel"):
        _exported_audio(
            PackagedAudioChannel(
                1,
                0,
                "records/000001_00123/audio/CH1.wav",
            ),
            PackagedAudioChannel(
                2,
                0,
                "records/000001_00123/audio/CH2.wav",
            ),
        )


@pytest.mark.parametrize(
    "path",
    [
        "records/000002_00123/current.csv",
        "records/000001_00123/current-other.csv",
    ],
)
def test_packaged_record_rejects_current_path_outside_exact_directory(path):
    current = PackagedCurrent(True, "exported", None, path)
    with pytest.raises(ValueError, match="current.*path"):
        PackagedRunRecord(
            sequence=1,
            record=_record(),
            ai=AiExportValue(None, None),
            directory="records/000001_00123",
            audio=_not_selected_audio(),
            current=current,
        )


def test_export_counts_and_result_are_validated_completion_values():
    counts = RunDataExportCounts(
        selected_runs=2,
        exported_runs=1,
        skipped_runs=1,
        audio_runs_emitted=1,
        audio_runs_skipped=0,
        audio_files_emitted=2,
        current_runs_emitted=0,
        current_runs_skipped=1,
    )
    result = RunDataExportResult(
        target_path="C:/exports/runs.zip",
        output_kind="zip",
        counts=counts,
    )
    assert result.path == result.target_path

    with pytest.raises(ValueError, match="sum"):
        RunDataExportCounts(
            selected_runs=2,
            exported_runs=2,
            skipped_runs=1,
            audio_runs_emitted=0,
            audio_runs_skipped=0,
            audio_files_emitted=0,
            current_runs_emitted=0,
            current_runs_skipped=0,
        )


def _counts(**changes):
    values = {
        "selected_runs": 2,
        "exported_runs": 2,
        "skipped_runs": 0,
        "audio_runs_emitted": 0,
        "audio_runs_skipped": 0,
        "audio_files_emitted": 0,
        "current_runs_emitted": 0,
        "current_runs_skipped": 0,
    }
    values.update(changes)
    return RunDataExportCounts(**values)


def test_export_counts_require_a_nonempty_validated_selection():
    with pytest.raises(ValueError, match="selected_runs.*positive"):
        _counts(selected_runs=0, exported_runs=0)


def test_export_counts_reject_audio_files_without_an_emitted_audio_run():
    with pytest.raises(ValueError, match="audio.*files"):
        _counts(audio_files_emitted=1)


def test_export_counts_require_at_least_one_file_per_emitted_audio_run():
    with pytest.raises(ValueError, match="audio.*files"):
        _counts(audio_runs_emitted=2, audio_files_emitted=1)


@pytest.mark.parametrize(
    ("state", "judgment", "expected"),
    [
        ("FAILED", "PASS", "OK"),
        ("CANCELED", "FAIL", "NG"),
        ("NO_RESULT", None, "无结果"),
        ("FAILED", None, "错误"),
        ("CANCELED", None, "错误"),
        ("INTERRUPTED", None, "错误"),
    ],
)
def test_excel_result_uses_approved_precedence(state, judgment, expected):
    assert excel_result(state=state, judgment=judgment) == expected


@pytest.mark.parametrize(
    ("state", "judgment", "expected"),
    [
        ("SUCCEEDED", "PASS", "OK"),
        ("SUCCEEDED", "FAIL", "NG"),
        ("NO_RESULT", None, "NO_RESULT"),
        ("INTERRUPTED", None, "ERROR"),
    ],
)
def test_manifest_result_uses_schema_enum(state, judgment, expected):
    assert manifest_result(state=state, judgment=judgment) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [("AUTOMATIC", "自动"), ("MANUAL", "人工"), ("NONE", "")],
)
def test_excel_judgment_source_is_translated(source, expected):
    assert excel_judgment_source(source) == expected


@pytest.mark.parametrize(
    ("sn", "expected"),
    [
        ("", "无SN"),
        (" \t ", "无SN"),
        (" 产品甲 ", "产品甲"),
        ('A<>:"/\\|?*B', "A_B"),
        ("A\x00\x1f\x7fB", "A_B"),
        ("A___B", "A_B"),
        ("SN. . ", "SN"),
        ("con", "_con"),
        ("PrN.txt", "_PrN.txt"),
        ("COM9.log", "_COM9.log"),
        ("lpt1", "_lpt1"),
        ("COM10", "COM10"),
    ],
)
def test_sn_sanitization_follows_exact_order(sn, expected):
    assert sanitize_sn_segment(sn) == expected


def test_sn_sanitization_truncates_to_48_unicode_code_points():
    assert sanitize_sn_segment("测" * 60) == "测" * 48
    assert len(sanitize_sn_segment("测" * 60)) == 48


def test_record_paths_are_posix_safe_and_sequences_make_repeats_unique():
    first = record_directory(1, "SN/1")
    second = record_directory(2, "SN/1")
    assert first == "records/000001_SN_1"
    assert second == "records/000002_SN_1"
    assert audio_member_path(first, source_channel=0) == (
        "records/000001_SN_1/audio/CH1.wav"
    )
    assert current_member_path(first) == "records/000001_SN_1/current.csv"


@pytest.mark.parametrize("sequence", [True, 0, -1, 1_000_000])
def test_record_directory_requires_a_positive_six_digit_sequence(sequence):
    with pytest.raises((TypeError, ValueError), match="sequence"):
        record_directory(sequence, "SN")


def test_member_path_helpers_reject_non_package_paths_and_bad_channels():
    for directory in ("", "/records/000001_SN", "records/../escape", "a\\b"):
        with pytest.raises(ValueError, match="path"):
            current_member_path(directory)
    for channel in (True, -1):
        with pytest.raises((TypeError, ValueError), match="source channel"):
            audio_member_path("records/000001_SN", source_channel=channel)


def test_packaged_audio_channel_validates_position_identity_and_path():
    channel = PackagedAudioChannel(
        position=1,
        source_channel=0,
        path="records/000001_SN/audio/CH1.wav",
    )
    assert channel.position == 1
    with pytest.raises(ValueError, match="path"):
        PackagedAudioChannel(1, 0, "records/../CH1.wav")
