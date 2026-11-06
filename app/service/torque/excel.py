"""First-sheet XLSX parsing and a round-trippable text-identifier template."""
from pathlib import Path
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.utils.exceptions import InvalidFileException

from app.service.torque.types import TorqueImportError, TorqueImportResult, TorqueImportRow
from app.service.torque.validation import normalize_key, validate_torque


HEADERS = ('匹配类型', '型号或SN', '扭矩')


class TorqueImportCancelled(Exception):
    """Parsing was cancelled; no partial result may be submitted."""


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise TorqueImportCancelled('导入已取消')


def _blank(value):
    return value is None or isinstance(value, str) and not value.strip()


def parse_workbook(path, cancelled=None):
    """Return valid rows and displayable errors, with 1-based Excel row numbers.

    File-level errors use row 1. The caller must pass the entire result to the
    store's preview_import to retain errors and enforce all-or-nothing import.
    """
    _check_cancelled(cancelled)
    path = Path(path)
    if path.suffix.lower() != '.xlsx':
        return TorqueImportResult(errors=(TorqueImportError(1, '仅支持 .xlsx 文件'),))
    book = None
    try:
        book = load_workbook(path, read_only=True, data_only=False)
        if not book.worksheets:
            return TorqueImportResult(errors=(TorqueImportError(1, '工作簿没有工作表'),))
        iterator = book.worksheets[0].iter_rows()
        header = next(iterator, ())
        names = [cell.value.strip() if isinstance(cell.value, str) else cell.value for cell in header]
        if len(names) != 3 or set(names) != set(HEADERS):
            return TorqueImportResult(errors=(TorqueImportError(1, '表头必须为：匹配类型、型号或SN、扭矩'),))
        columns = [names.index(name) for name in HEADERS]
        rows, errors, seen = [], [], {}
        for number, cells in enumerate(iterator, 2):
            _check_cancelled(cancelled)
            if all(_blank(cell.value) for cell in cells):
                continue
            try:
                if any(cell.data_type == 'f' for cell in cells):
                    raise ValueError('不支持公式单元格')
                kind_cell, key_cell, value_cell = [cells[index] for index in columns]
                label = kind_cell.value.strip() if isinstance(kind_cell.value, str) else kind_cell.value
                if label not in ('型号', 'SN'):
                    raise ValueError('匹配类型必须为型号或 SN')
                kind = 'model' if label == '型号' else 'sn'
                key = normalize_key(key_cell.value)
                value = validate_torque(value_cell.value)
                identity = kind, key
                if identity in seen:
                    raise ValueError(f'匹配键重复（首次位于第 {seen[identity]} 行）')
                seen[identity] = number
                rows.append(TorqueImportRow(number, kind, key, value))
            except ValueError as exc:
                errors.append(TorqueImportError(number, str(exc)))
        _check_cancelled(cancelled)
        return TorqueImportResult(tuple(rows), tuple(errors))
    except (OSError, BadZipFile, InvalidFileException, ParseError, KeyError, ValueError) as exc:
        return TorqueImportResult(errors=(TorqueImportError(1, f'无法读取 Excel：{exc}'),))
    finally:
        if book is not None:
            book.close()


def write_template(path):
    path = Path(path)
    if path.suffix.lower() != '.xlsx':
        raise ValueError('仅支持 .xlsx 文件')
    book = Workbook()
    try:
        sheet = book.active
        sheet.title = '扭矩表'
        sheet.append(HEADERS)
        sheet.append(('型号', 'M100', 2.5))
        sheet.append(('SN', 'SN000123', 2.6))
        sheet.column_dimensions['B'].number_format = '@'
        for cell in sheet['B']:
            cell.number_format = '@'
        sheet['C1'].comment = Comment('与 PLC 单位一致；软件不进行单位换算。', 'Torque')
        for column, width in (('A', 16), ('B', 30), ('C', 20)):
            sheet.column_dimensions[column].width = width
        book.save(path)
    finally:
        book.close()
