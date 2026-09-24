from datetime import datetime
from io import BytesIO
from pathlib import Path

import openpyxl
import pytest
from openpyxl.worksheet import _writer as openpyxl_writer

from app.service.run_record_export import excel as excel_module
from app.service.run_record_export.contracts import (
    AiExportValue,
    AudioExportFacts,
    CurrentExportFacts,
    PackagedAudio,
    PackagedAudioChannel,
    PackagedCurrent,
    PackagedRunRecord,
    RunDataExportRecord,
)
from app.service.run_record_export.excel import build_excel_workbook
from app.service.run_record_export.naming import record_directory


HEADERS = (
    "序号",
    "检测时间",
    "SN",
    "产品型号",
    "最终结果",
    "判定来源",
    "故障类型",
    "AI评分",
    "AI标签",
    "音频相对路径",
)
RUN_IDS = (
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000003",
    "00000000-0000-4000-8000-000000000004",
)


def _packaged_record(
    sequence: int,
    *,
    accepted_at: str = "2026-09-03T01:02:03.000000Z",
    sn: str = "00123",
    model: str = "M1",
    state: str = "SUCCEEDED",
    judgment: str | None = "PASS",
    judgment_source: str = "AUTOMATIC",
    fault_type: str | None = None,
    ai_score: float | None = 0.875,
    ai_label: str | None = "bearing",
    audio_paths: tuple[str, ...] = (),
) -> PackagedRunRecord:
    record = RunDataExportRecord(
        run_id=RUN_IDS[sequence - 1],
        selection_index=sequence - 1,
        accepted_at=accepted_at,
        terminal_at="2026-09-03T01:03:03.000000Z",
        sn=sn,
        model=model,
        state=state,
        terminal_code="completed",
        judgment=judgment,
        judgment_source=judgment_source,
        fault_type=fault_type,
        audio=AudioExportFacts(None, None, None, None, None, None),
        current=CurrentExportFacts(None, None),
    )
    directory = record_directory(sequence, sn)
    if audio_paths:
        audio = PackagedAudio(
            selected=True,
            status="exported",
            reason=None,
            sample_rate_hz=48_000,
            frame_count=96_000,
            channels=tuple(
                PackagedAudioChannel(index, index - 1, path)
                for index, path in enumerate(audio_paths, start=1)
            ),
        )
    else:
        audio = PackagedAudio(False, "not_selected", None, None, None, ())
    return PackagedRunRecord(
        sequence=sequence,
        record=record,
        ai=AiExportValue(ai_score, ai_label),
        directory=directory,
        audio=audio,
        current=PackagedCurrent(False, "not_selected", None, None),
    )


def _load(records: tuple[PackagedRunRecord, ...]):
    payload = build_excel_workbook(records)
    assert payload
    return openpyxl.load_workbook(
        BytesIO(payload), read_only=False, data_only=False
    )


def test_workbook_has_only_the_approved_sheet_and_exact_columns():
    workbook = _load((_packaged_record(1),))

    assert workbook.sheetnames == ["检测记录"]
    sheet = workbook["检测记录"]
    assert tuple(cell.value for cell in sheet[1]) == HEADERS
    assert sheet.max_column == 10
    assert sheet.max_row == 2


def test_rows_preserve_the_already_allocated_sequence_order():
    workbook = _load(
        (
            _packaged_record(2, sn="second"),
            _packaged_record(1, sn="first"),
        )
    )
    sheet = workbook["检测记录"]

    assert [sheet.cell(row, 1).value for row in (2, 3)] == [2, 1]
    assert [sheet.cell(row, 3).value for row in (2, 3)] == ["second", "first"]


def test_utc_timestamp_is_a_real_shanghai_datetime_cell():
    workbook = _load(
        (_packaged_record(1, accepted_at="2026-09-03T16:02:03.000000Z"),)
    )
    cell = workbook["检测记录"]["B2"]

    assert cell.value == datetime(2026, 9, 4, 0, 2, 3)
    assert cell.data_type == "d"
    assert cell.number_format == "yyyy-mm-dd hh:mm:ss"


def test_user_text_is_literal_and_blank_values_remain_blank():
    workbook = _load(
        (
            _packaged_record(
                1,
                sn="00123",
                model="=1+1",
                fault_type="+fault",
                ai_score=None,
                ai_label="-raw-label",
            ),
            _packaged_record(
                2,
                sn="@serial",
                model="-model",
                fault_type=None,
                ai_score=None,
                ai_label=None,
            ),
        )
    )
    sheet = workbook["检测记录"]

    for coordinate, expected in {
        "C2": "00123",
        "D2": "=1+1",
        "G2": "+fault",
        "I2": "-raw-label",
        "C3": "@serial",
        "D3": "-model",
    }.items():
        cell = sheet[coordinate]
        assert cell.value == expected
        assert cell.data_type == "s"
    assert sheet["G3"].value is None
    assert sheet["H2"].value is None
    assert sheet["I3"].value is None


def test_user_text_replaces_only_ooxml_illegal_characters_and_stays_literal():
    workbook = _load(
        (
            _packaged_record(
                1,
                sn=" 00\x01123 ",
                model="=model\x0braw",
                fault_type="+fault\x0craw",
                ai_label=" raw\x1flabel ",
            ),
        )
    )
    sheet = workbook["检测记录"]

    for coordinate, expected in {
        "C2": " 00\ufffd123 ",
        "D2": "=model\ufffdraw",
        "G2": "+fault\ufffdraw",
        "I2": " raw\ufffdlabel ",
    }.items():
        cell = sheet[coordinate]
        assert cell.value == expected
        assert cell.data_type == "s"


def test_result_source_ai_and_emitted_audio_values_are_projected_exactly():
    audio_paths = (
        "records/000001_00123/audio/CH1.wav",
        "records/000001_00123/audio/CH2.wav",
    )
    workbook = _load(
        (
            _packaged_record(1, audio_paths=audio_paths),
            _packaged_record(
                2,
                state="FAILED",
                judgment="FAIL",
                judgment_source="MANUAL",
                ai_score=-2.5,
                ai_label=" raw ",
            ),
            _packaged_record(
                3,
                state="NO_RESULT",
                judgment=None,
                judgment_source="NONE",
                ai_score=None,
                ai_label=None,
            ),
            _packaged_record(
                4,
                state="INTERRUPTED",
                judgment=None,
                judgment_source="NONE",
                ai_score=None,
                ai_label=None,
            ),
        )
    )
    sheet = workbook["检测记录"]

    assert [sheet.cell(row, 5).value for row in range(2, 6)] == [
        "OK",
        "NG",
        "无结果",
        "错误",
    ]
    assert [sheet.cell(row, 6).value for row in range(2, 6)] == [
        "自动",
        "人工",
        None,
        None,
    ]
    assert sheet["H2"].value == 0.875
    assert sheet["H2"].data_type == "n"
    assert sheet["H3"].value == -2.5
    assert sheet["I3"].value == " raw "
    assert sheet["J2"].value == "\n".join(audio_paths)
    assert sheet["J3"].value is None


def test_presentation_has_filter_freeze_wrap_bounded_widths_and_fills():
    workbook = _load(
        (
            _packaged_record(1),
            _packaged_record(2, state="FAILED", judgment="FAIL"),
            _packaged_record(3, state="NO_RESULT", judgment=None),
            _packaged_record(4, state="CANCELED", judgment=None),
        )
    )
    sheet = workbook["检测记录"]

    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:J5"
    assert all(
        8 <= sheet.column_dimensions[column].width <= 60
        for column in "ABCDEFGHIJ"
    )
    assert all(sheet.cell(row, 10).alignment.wrap_text for row in range(2, 6))
    assert [sheet.cell(row, 5).fill.fgColor.rgb for row in range(2, 6)] == [
        "00C6EFCE",
        "00FFC7CE",
        "00FFEB9C",
        "00FFEB9C",
    ]


def test_cancel_check_runs_exactly_once_after_each_data_row():
    calls: list[int] = []
    records = tuple(_packaged_record(index) for index in range(1, 5))

    build_excel_workbook(records, cancel_check=lambda: calls.append(1))

    assert len(calls) == len(records)


def test_cancellation_after_an_appended_row_cleans_write_only_temp_files(
    monkeypatch,
):
    class ExportCanceled(Exception):
        pass

    tracked_before = set(openpyxl_writer.ALL_TEMP_FILES)
    created_paths: list[Path] = []
    callback_calls = 0
    real_create_temporary_file = openpyxl_writer.create_temporary_file

    def create_temporary_file_spy(*args, **kwargs):
        path = real_create_temporary_file(*args, **kwargs)
        created_paths.append(Path(path))
        return path

    monkeypatch.setattr(
        openpyxl_writer,
        "create_temporary_file",
        create_temporary_file_spy,
    )

    def cancel_after_first_row():
        nonlocal callback_calls
        callback_calls += 1
        raise ExportCanceled

    try:
        with pytest.raises(ExportCanceled):
            build_excel_workbook(
                (_packaged_record(1),),
                cancel_check=cancel_after_first_row,
            )

        newly_tracked = set(openpyxl_writer.ALL_TEMP_FILES) - tracked_before
        assert callback_calls == 1
        assert len(created_paths) == 1
        assert newly_tracked == set()
        assert not any(path.exists() for path in created_paths)
    finally:
        leaked = set(openpyxl_writer.ALL_TEMP_FILES) - tracked_before
        for path in leaked:
            Path(path).unlink(missing_ok=True)
            openpyxl_writer.ALL_TEMP_FILES.remove(path)


def test_builder_uses_write_only_mode_and_reopens_for_structural_validation(
    monkeypatch,
):
    workbook_calls = []
    load_calls = []
    real_workbook = excel_module.openpyxl.Workbook
    real_load_workbook = excel_module.openpyxl.load_workbook

    def workbook_spy(*args, **kwargs):
        workbook_calls.append(kwargs.copy())
        return real_workbook(*args, **kwargs)

    def load_workbook_spy(*args, **kwargs):
        load_calls.append(kwargs.copy())
        return real_load_workbook(*args, **kwargs)

    monkeypatch.setattr(excel_module.openpyxl, "Workbook", workbook_spy)
    monkeypatch.setattr(
        excel_module.openpyxl, "load_workbook", load_workbook_spy
    )

    build_excel_workbook((_packaged_record(1),))

    assert workbook_calls == [{"write_only": True}]
    assert load_calls == [{"read_only": True, "data_only": False}]
