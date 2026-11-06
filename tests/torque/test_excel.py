import pytest
from openpyxl import Workbook, load_workbook

from app.service.torque.excel import TorqueImportCancelled, parse_workbook, write_template
from app.service.torque.types import TorqueImportRow


def workbook(tmp_path, rows, headers=('匹配类型', '型号或SN', '扭矩')):
    path = tmp_path / 'rules.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    book.create_sheet('ignored').append(['bad header'])
    book.save(path)
    book.close()
    return path


def test_reordered_headers_whitespace_text_keys_blank_rows_first_sheet(tmp_path):
    result = parse_workbook(workbook(tmp_path, [(2.5, ' 000123 ', ' SN '), (None, None, None), ('-2', '123456789012345678901234', '型号')], (' 扭矩 ', '型号或SN', '匹配类型')))
    assert result.errors == ()
    assert result.rows == (TorqueImportRow(2, 'sn', '000123', 2.5), TorqueImportRow(4, 'model', '123456789012345678901234', -2))


@pytest.mark.parametrize('row', [('SN', 123, 2), ('SN', 'x', True), ('型号', 'x', 'NaN'), ('型号', 'x', '1e-1000'), ('型号', 'x', '1e100'), ('型号', None, 2), ('型号', 'x', None), ('model', 'x', 2), ('SN', '=TEXT(123,"000000")', 2), ('SN', 'x', '=1+1'), ('="SN"', 'x', 2)])
def test_invalid_rows_include_excel_row_number(tmp_path, row):
    result = parse_workbook(workbook(tmp_path, [row]))
    assert result.rows == ()
    assert len(result.errors) == 1
    assert result.errors[0].row_number == 2
    assert result.errors[0].message


def test_duplicate_keys_normalize_and_report_row(tmp_path):
    result = parse_workbook(workbook(tmp_path, [('SN', '000123', 2), ('SN', ' 000123 ', 3)]))
    assert [error.row_number for error in result.errors] == [3]
    assert '重复' in result.errors[0].message


@pytest.mark.parametrize('headers', [('错误', '型号或SN', '扭矩'), ('匹配类型', '匹配类型', '扭矩'), ('匹配类型', '型号或SN')])
def test_wrong_headers_display_error(tmp_path, headers):
    result = parse_workbook(workbook(tmp_path, [], headers))
    assert result.errors[0].row_number == 1
    assert not result.rows


def test_only_xlsx_and_bad_zip_are_displayable_errors(tmp_path):
    for name in ('rules.xls', 'rules.xlsm', 'bad.xlsx'):
        path = tmp_path / name
        path.write_text('invalid file')
        result = parse_workbook(path)
        assert result.errors and not result.rows


def test_template_roundtrips_and_formats_keys_as_text(tmp_path):
    path = tmp_path / 'template.xlsx'
    write_template(path)
    book = load_workbook(path)
    assert len(book.worksheets) == 1
    assert book.active['B2'].number_format == '@'
    assert book.active['B3'].number_format == '@'
    assert book.active['B1'].number_format == '@'
    assert '与 PLC 单位一致' in book.active['C1'].comment.text
    book.close()
    result = parse_workbook(path)
    assert not result.errors
    assert len(result.rows) == 2


def test_cancellation_never_returns_partial_rows(tmp_path):
    path = workbook(tmp_path, [('型号', 'M100', 2), ('SN', '000123', 3)])
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 3
    with pytest.raises(TorqueImportCancelled):
        parse_workbook(path, cancelled=cancelled)
    path.unlink()  # Parser released the workbook file on cancellation.


def test_workbook_without_worksheets_has_displayable_error(tmp_path):
    from zipfile import ZipFile
    from xml.etree import ElementTree
    original = workbook(tmp_path, [])
    path = tmp_path / 'no-sheets.xlsx'
    with ZipFile(original) as source, ZipFile(path, 'w') as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == 'xl/workbook.xml':
                root = ElementTree.fromstring(data)
                sheets = root.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets')
                sheets.clear()
                data = ElementTree.tostring(root)
            target.writestr(item, data)
    result = parse_workbook(path)
    assert not result.rows
    assert result.errors[0].row_number == 1
    assert '没有工作表' in result.errors[0].message
