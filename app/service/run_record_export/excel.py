"""Bounded-memory Excel projection for configurable run exports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from io import BytesIO
import re

import openpyxl
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from app.service.run_record_export.contracts import PackagedRunRecord
from app.service.run_record_export.naming import (
    excel_judgment_source,
    excel_result,
)


_SHEET_NAME = "检测记录"
_HEADERS = (
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
_COLUMN_WIDTHS = (8, 20, 16, 18, 12, 12, 18, 12, 18, 60)
_DATETIME_FORMAT = "yyyy-mm-dd hh:mm:ss"
_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
_OOXML_ILLEGAL_CHARACTERS = re.compile(
    r"[\x00-\x08\x0b-\x0c\x0e-\x1f]"
)

_HEADER_FONT = Font(bold=True)
_HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center")
_BODY_ALIGNMENT = Alignment(vertical="center")
_AUDIO_ALIGNMENT = Alignment(vertical="top", wrap_text=True)
_RESULT_FILLS = {
    "OK": PatternFill(fill_type="solid", fgColor="C6EFCE"),
    "NG": PatternFill(fill_type="solid", fgColor="FFC7CE"),
    "无结果": PatternFill(fill_type="solid", fgColor="FFEB9C"),
    "错误": PatternFill(fill_type="solid", fgColor="FFEB9C"),
}


def build_excel_workbook(
    records: tuple[PackagedRunRecord, ...],
    *,
    cancel_check: Callable[[], None] | None = None,
) -> bytes:
    """Build and structurally validate the one-sheet export workbook."""

    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet(_SHEET_NAME)
    try:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:J{len(records) + 1}"
        for index, width in enumerate(_COLUMN_WIDTHS, start=1):
            column = openpyxl.utils.get_column_letter(index)
            sheet.column_dimensions[column].width = width
        sheet.row_dimensions[1].height = 24

        header_cells = []
        for value in _HEADERS:
            cell = WriteOnlyCell(sheet, value=value)
            cell.font = _HEADER_FONT
            cell.alignment = _HEADER_ALIGNMENT
            header_cells.append(cell)
        sheet.append(header_cells)

        for row_index, packaged in enumerate(records, start=2):
            audio_paths = tuple(
                channel.path for channel in packaged.audio.channels
            )
            result = excel_result(
                state=packaged.record.state,
                judgment=packaged.record.judgment,
            )
            row = [
                _cell(sheet, packaged.sequence),
                _datetime_cell(sheet, packaged.record.accepted_at),
                _text_cell(sheet, packaged.record.sn),
                _text_cell(sheet, packaged.record.model),
                _result_cell(sheet, result),
                _cell(
                    sheet,
                    excel_judgment_source(
                        packaged.record.judgment_source
                    )
                    or None,
                ),
                _text_cell(sheet, packaged.record.fault_type),
                _cell(sheet, packaged.ai.score),
                _text_cell(sheet, packaged.ai.label),
                _audio_cell(sheet, "\n".join(audio_paths) or None),
            ]
            sheet.row_dimensions[row_index].height = min(
                60,
                max(20, 15 * max(1, len(audio_paths))),
            )
            sheet.append(row)
            if cancel_check is not None:
                cancel_check()
    except BaseException:
        try:
            sheet.close()
        finally:
            if sheet._writer is not None:
                sheet._writer.cleanup()
        raise

    stream = BytesIO()
    workbook.save(stream)
    payload = stream.getvalue()
    _validate_workbook(payload, expected_rows=len(records) + 1)
    return payload


def _cell(sheet, value):
    cell = WriteOnlyCell(sheet, value=value)
    cell.alignment = _BODY_ALIGNMENT
    return cell


def _text_cell(sheet, value: str | None):
    literal = _ooxml_literal_text(value)
    cell = _cell(sheet, literal)
    if value is not None:
        cell.data_type = "s"
    return cell


def _ooxml_literal_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _OOXML_ILLEGAL_CHARACTERS.sub("\ufffd", value)


def _datetime_cell(sheet, value: str):
    accepted_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    local_value = accepted_at.astimezone(_SHANGHAI).replace(tzinfo=None)
    cell = _cell(sheet, local_value)
    cell.number_format = _DATETIME_FORMAT
    return cell


def _result_cell(sheet, value: str):
    cell = _cell(sheet, value)
    cell.fill = _RESULT_FILLS[value]
    return cell


def _audio_cell(sheet, value: str | None):
    cell = WriteOnlyCell(sheet, value=value)
    cell.alignment = _AUDIO_ALIGNMENT
    return cell


def _validate_workbook(payload: bytes, *, expected_rows: int) -> None:
    if not payload:
        raise ValueError("Excel workbook is empty")
    workbook = openpyxl.load_workbook(
        BytesIO(payload),
        read_only=True,
        data_only=False,
    )
    try:
        if workbook.sheetnames != [_SHEET_NAME]:
            raise ValueError("Excel workbook has an unexpected worksheet layout")
        sheet = workbook[_SHEET_NAME]
        rows = sheet.iter_rows(values_only=True)
        if tuple(next(rows, ())) != _HEADERS:
            raise ValueError("Excel workbook has unexpected columns")
        if sum(1 for _ in rows) + 1 != expected_rows:
            raise ValueError("Excel workbook has an unexpected row count")
    finally:
        workbook.close()
