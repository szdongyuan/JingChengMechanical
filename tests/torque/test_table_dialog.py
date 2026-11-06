import threading
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QFileDialog

from app.service.torque.jobs import TorqueJobResult
from app.service.torque.store import TorquePreviewConflict
from app.service.torque.types import (RuleSnapshot, TorqueImportPreview, TorqueImportRow,
                                      TorqueImportChange, TorqueImportError)
from app.service.torque.validation import validate_torque
from tests.torque.test_settings_dialog import services
from tests.torque.test_jobs import job_env, until
from ui.popups.torque_table_dialog import TorqueTableDialog


def preview(errors=()):
    addition = TorqueImportRow(2, 'sn', '000123', validate_torque(2.6))
    overwrite = TorqueImportRow(3, 'model', 'M100', 3.5)
    return TorqueImportPreview(0, (addition, overwrite), (addition,),
                               (TorqueImportChange(overwrite, 2.5),), errors)


def open_preview(dialog, services, monkeypatch, value):
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: ('rules.xlsx', ''))
    dialog.import_button.click()
    assert services.torque_jobs.requests[-1][1:] == ('parse_import', ('rules.xlsx',))
    services.torque_jobs.finish(value)
    return dialog.preview_dialog


def test_search_create_edit_delete_preserve_exact_sn_and_real(services):
    services.torque_store.rules = RuleSnapshot(0, {('sn', '000123'): validate_torque(2.6), ('model', 'M100'): 2.5})
    dialog = TorqueTableDialog(services)
    assert dialog.table.rowCount() == 2
    dialog.search.setText('0001')
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == '000123'
    assert dialog.table.item(0, 2).text() == '2.6'
    dialog.table.selectRow(0)
    dialog.edit_button.click()
    editor = dialog.editor_dialog
    assert editor.match_key.text() == '000123'
    editor.save_button.click()
    assert services.torque_jobs.requests[-1][1:] == ('update_rule', ('sn', '000123', 'sn', '000123', validate_torque(2.6)))
    services.torque_jobs.finish(services.torque_store.rules)
    dialog.table.selectRow(0)
    dialog.delete_button.click()
    assert services.torque_jobs.requests[-1][1:] == ('delete_rule', ('sn', '000123'))
    services.torque_jobs.finish(services.torque_store.rules)
    dialog.add_button.click()
    editor = dialog.editor_dialog
    editor.match_type.setCurrentIndex(1)
    editor.match_key.setText(' 000456 ')
    editor.torque.setText('-2.6')
    editor.save_button.click()
    assert services.torque_jobs.requests[-1][1:] == ('create_rule', ('sn', '000456', validate_torque(-2.6)))
    dialog.close()


@pytest.mark.parametrize('key,value', [('', '2.5'), ('SN', 'NaN'), ('SN', '1e-1000')])
def test_rule_validation_prevents_background_request(services, key, value):
    dialog = TorqueTableDialog(services)
    dialog.add_button.click()
    dialog.editor_dialog.match_key.setText(key)
    dialog.editor_dialog.torque.setText(value)
    dialog.editor_dialog.save_button.click()
    assert not services.torque_jobs.requests
    assert dialog.editor_dialog.error_label.text()
    dialog.close()


@pytest.mark.parametrize('value', [2.6, 1.23456789, 123456789, 1e-40, 3.4028234e38])
def test_unchanged_editor_preserves_float32_value(services, value):
    real = validate_torque(value)
    services.torque_store.rules = RuleSnapshot(0, {('sn', '000123'): real})
    dialog = TorqueTableDialog(services)
    dialog.table.selectRow(0)
    dialog.edit_button.click()
    dialog.editor_dialog.save_button.click()
    assert services.torque_jobs.requests[-1][2][-1] == real
    dialog.close()


def test_template_download_and_cancelled_picker_do_not_write_on_ui(services, monkeypatch):
    dialog = TorqueTableDialog(services)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: ('template.xlsx', ''))
    dialog.template_button.click()
    assert services.torque_jobs.requests[-1][1:] == ('write_template', ('template.xlsx',))
    services.torque_jobs.finish()
    assert '模板已保存' in dialog.status.text()
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: ('', ''))
    dialog.import_button.click()
    assert len(services.torque_jobs.requests) == 1
    dialog.close()


def test_import_preview_shows_old_new_action_rows_and_cancel_does_not_mutate(services, monkeypatch):
    dialog = TorqueTableDialog(services)
    pane = open_preview(dialog, services, monkeypatch, preview())
    cells = [[pane.table.item(r, c).text() for c in range(6)] for r in range(2)]
    assert cells == [['2', 'SN', '000123', '—', '2.6', '新增'], ['3', '型号', 'M100', '2.5', '3.5', '覆盖']]
    assert pane.confirm_button.isEnabled()
    pane.cancel_button.click()
    assert len(services.torque_jobs.requests) == 1
    assert services.torque_store.snapshot().revision == 0
    dialog.close()


def test_error_rows_block_entire_import_and_scroll(services, monkeypatch, qapp):
    dialog = TorqueTableDialog(services)
    errors = tuple(TorqueImportError(n, '型号或 SN 必须是文本，不能使用数值单元格') for n in range(4, 80))
    pane = open_preview(dialog, services, monkeypatch, preview(errors))
    assert not pane.confirm_button.isEnabled()
    assert pane.table.rowCount() == 78
    assert pane.table.item(2, 0).text() == '4'
    assert '文本' in pane.table.item(2, 5).text()
    pane.resize(640, 420)
    qapp.processEvents()
    assert pane.table.verticalScrollBar().maximum() > 0
    pane.confirm_button.click()
    assert len(services.torque_jobs.requests) == 1
    dialog.close()


def test_external_change_invalidates_preview_and_commit_checks_revision(services, monkeypatch):
    dialog = TorqueTableDialog(services)
    pane = open_preview(dialog, services, monkeypatch, preview())
    services.torque_store.rules = RuleSnapshot(1, {('model', 'M100'): 4.0})
    services.torque_jobs.finished.emit(TorqueJobResult('other', 'upsert_rule', services.torque_store.rules))
    assert not pane.confirm_button.isEnabled()
    assert '重新' in pane.status.text()
    dialog.close()


def test_commit_success_and_conflict_require_new_preview(services, monkeypatch):
    dialog = TorqueTableDialog(services)
    pane = open_preview(dialog, services, monkeypatch, preview())
    pane.confirm_button.click()
    assert services.torque_jobs.requests[-1][1:] == ('commit_import', (preview(),))
    services.torque_jobs.finish(error=TorquePreviewConflict('规则版本已变化'))
    assert not pane.confirm_button.isEnabled()
    assert '重新' in pane.status.text()
    pane.cancel_button.click()
    pane = open_preview(dialog, services, monkeypatch, preview())
    pane.confirm_button.click()
    services.torque_store.rules = RuleSnapshot(1, {('sn', '000123'): validate_torque(2.6)})
    services.torque_jobs.finish(services.torque_store.rules)
    assert not pane.isVisible()
    assert dialog.table.rowCount() == 1
    assert '导入成功' in dialog.status.text()
    dialog.close()


def test_close_cancels_parser_only_and_late_results_are_safe(services, monkeypatch):
    dialog = TorqueTableDialog(services)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: ('rules.xlsx', ''))
    dialog.import_button.click()
    dialog.close()
    assert services.torque_jobs.cancelled == ['1']
    services.torque_jobs.finish(preview())
    assert dialog.preview_dialog is None
    other = TorqueTableDialog(services)
    pane = open_preview(other, services, monkeypatch, preview())
    pane.confirm_button.click()
    other.close()
    assert services.torque_jobs.cancelled == ['1']
    services.torque_jobs.finish(RuleSnapshot(1, {}))


@pytest.mark.parametrize('operation,args,expected', [
    ('delete_rule', ('sn', '000123'), {}),
    ('update_rule', ('sn', '000123', 'sn', '000456', 2.5), {('sn', '000456'): 2.5}),
])
def test_edit_recovers_when_outstanding_mutation_removes_rendered_key(
        job_env, qapp, monkeypatch, operation, args, expected):
    jobs, store, db, results = job_env
    store.create_rule('sn', '000123', 2.5)
    services = SimpleNamespace(torque_jobs=jobs, torque_store=store)
    entered, release, published = threading.Event(), threading.Event(), threading.Event()
    mutation = getattr(store, operation)

    def delayed_mutation(*args):
        entered.set()
        assert release.wait(3)
        result = mutation(*args)
        published.set()
        return result

    monkeypatch.setattr(store, operation, delayed_mutation)
    issuer = TorqueTableDialog(services)
    reopened = None
    try:
        issuer.show()
        assert issuer.submit(operation, *args)
        assert entered.wait(3)
        issuer.close()
        # Reopening must not cancel the application-owned outstanding mutation.
        reopened = TorqueTableDialog(services)
        reopened.show()
        reopened.table.selectRow(0)
        assert reopened.table.item(0, 1).text() == '000123'
        release.set()
        assert published.wait(3)
        assert dict(store.snapshot().values) == expected
        # Do not pump Qt yet: publication is visible before queued UI completion.
        assert results == []
        assert reopened.table.item(0, 1).text() == '000123'
        # Invoke the clicked callback directly so a RED exception is reported by
        # pytest instead of escaping Qt's signal dispatch and aborting the runner.
        reopened.edit_rule()
        assert reopened.editor_dialog is None
        assert '记录已变更或删除' in reopened.status.text()
        assert '重新选择' in reopened.status.text()
        assert reopened.table.rowCount() == len(expected)
        assert not reopened.edit_button.isEnabled()
        until(qapp, lambda: len(results) == 1)
        assert results[0].error is None
        assert not results[0].cancelled
        assert dict(store.snapshot().values) == expected
        assert db.execute_sql('SELECT match_key FROM torque_rule_table WHERE match_key = ?', ('000123',)).fetchall() == []
    finally:
        release.set()
        issuer.close()
        if reopened is not None:
            reopened.close()
