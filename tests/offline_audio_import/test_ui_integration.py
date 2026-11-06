"""Real Data Management entry through the owned import worker and saved viewer."""
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QObject, pyqtSignal, QCoreApplication, QEvent
from PyQt6.QtWidgets import QDialogButtonBox

from app.service.config_service import ConfigService
from app.service.offline_audio_import import ImportPhase, ImportStatus
from app.service.offline_audio_import.snapshot import SnapshotPreparer
from app.service.run_persistence import RunRepository
from app.service.run_persistence.data_management import RunDataManagementService
from app.service.run_persistence.historical_analysis_reader import HistoricalAnalysisReader
from model.test_config_table import TestConfigTable as ConfigTable
from model.channel_config_table import ChannelConfigTable
from ui.modules.data_management import DataManagementModule
from tests.offline_audio_import.test_integration import make_service


class Bus(QObject):
    offline_import_status = pyqtSignal(object)
    run_records_deleted = pyqtSignal(object)


@pytest.fixture
def setup_ui(tmp_path, qtbot):
    bus = Bus()
    busy = threading.Event()
    phases = []
    defer_charts = [False]
    def notify(status):
        phases.append(status.phase)
        if defer_charts[0] and status.phase is ImportPhase.ANALYZING:
            busy.set()
        bus.offline_import_status.emit(status)
    service, db, artifacts, request = make_service(tmp_path, busy=busy.is_set, sink=notify)
    cfg = ConfigTable.create(config_name='Offline saved', selected_channels='[7]', analysis_duration=1.)
    ChannelConfigTable.create(config=cfg, channel_index=7, spl_weighting='Z')
    configs = ConfigService(None)
    freezer = SnapshotPreparer(config_service=configs, calibration_loader=lambda _: {}, toolchain_loader=lambda _: None)
    service._freeze, service._prepare = freezer.freeze, freezer.prepare
    services = SimpleNamespace(bus=bus, config_service=configs, offline_audio_import_service=service,
        historical_playback=None, historical_analysis_reader=HistoricalAnalysisReader(db, result_artifact_store=artifacts),
        run_data_management_service=RunDataManagementService(db), factory_mode=SimpleNamespace(
            state=SimpleNamespace(selected_config_ids=(), run_phase='running')), production_unavailable_reason='No input device')
    module = DataManagementModule(services)
    yield SimpleNamespace(module=module, service=service, db=db, request=request, config_id=cfg.config_id,
        busy=busy, bus=bus, phases=phases, defer_charts=defer_charts)
    busy.clear()
    for dialog in list(module._analysis_dialogs.values()):
        if not sip.isdeleted(dialog):
            dialog.close()
    if not sip.isdeleted(module):
        module.close()
        module.deleteLater()
    assert service.close()
    from ui.popups.historical_analysis_dialog import _ACTIVE_READERS
    qtbot.waitUntil(lambda: not _ACTIVE_READERS, timeout=5000)
    db.close_after_upstream_stopped()


def submit_from_button(ctx):
    ctx.module.btn_import_audio.click()
    dialog = ctx.module._import_dialog
    assert dialog is not None
    index = dialog._config_selector.findData(ctx.config_id)
    assert index >= 0
    dialog._config_selector.setCurrentIndex(index)
    dialog.load_wav_path(ctx.request.source_path)
    assert 'WAV_IEEE_FLOAT' in dialog._format_value.text()
    assert dialog._mic_calibration_checkbox.isChecked()
    combo = dialog._mapping_combos[0]
    combo.setCurrentIndex(next(index for index in range(combo.count()) if combo.itemData(index) == ('config', 7)))
    dialog._buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    assert ctx.service.status is not None
    return ctx.service.status.run_id


def test_real_entry_waits_then_opens_saved_result_and_enriches_after_relabel(setup_ui, qtbot):
    ctx = setup_ui
    ctx.busy.set()
    ctx.defer_charts[0] = True
    run_id = submit_from_button(ctx)
    qtbot.waitUntil(lambda: '等待' in ctx.module.import_status_label.text())
    assert not ctx.module.btn_import_audio.isEnabled()
    ctx.module.btn_import_audio.click()
    assert ctx.service.status.run_id == run_id
    ctx.busy.clear()
    qtbot.waitUntil(lambda: run_id in ctx.module._analysis_dialogs, timeout=40000)
    history = ctx.module._analysis_dialogs[run_id]
    qtbot.waitUntil(lambda: history.snapshot is not None, timeout=10000)
    assert ctx.service.status.phase is ImportPhase.COMPLETING_CHARTS
    assert history.snapshot.charts_message
    assert '导入' in history.metadata_label.text() and '无电流数据' in history.metadata_label.text()
    assert history.snapshot.review['review']['import']['current_available'] is False
    original = history.snapshot.original_analysis_judgment
    ctx.module.edit_sn.setText('hidden-by-filter')
    ctx.module.btn_search.click()
    query = ctx.module._applied_query
    history.label_combo.setCurrentIndex(2)
    history.save_label_button.click()
    qtbot.waitUntil(lambda: history.snapshot.run.judgment_source == 'MANUAL')
    ctx.module.hide()
    ctx.busy.clear()
    qtbot.waitUntil(lambda: ctx.service.status.phase is ImportPhase.COMPLETED, timeout=40000)
    qtbot.waitUntil(lambda: history.snapshot.charts_message is None, timeout=10000)
    assert ctx.module._analysis_dialogs[run_id] is history
    assert history.snapshot.run.judgment == 'FAIL'
    assert history.snapshot.original_analysis_judgment == original
    assert ctx.module._applied_query == query
    assert ctx.module.btn_import_audio.isEnabled()
    assert {ImportPhase.WAITING, ImportPhase.SAVING, ImportPhase.ANALYZING, ImportPhase.COMPLETING_CHARTS, ImportPhase.COMPLETED} <= set(ctx.phases)


@pytest.mark.parametrize('phase', [ImportPhase.WAITING, ImportPhase.COMPLETING_CHARTS])
def test_close_setup_and_destroy_page_does_not_cancel_job(setup_ui, qtbot, phase):
    ctx = setup_ui
    ctx.module.btn_import_audio.click()
    ctx.module._import_dialog.close()
    assert ctx.service.status is None
    if phase is ImportPhase.WAITING:
        ctx.busy.set()
    else:
        ctx.defer_charts[0] = True
    run_id = submit_from_button(ctx)
    ctx.service.wait_for_phase(phase, 40)
    ctx.module.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(ctx.module)
    ctx.busy.clear()
    qtbot.waitUntil(lambda: ctx.service.status.finished, timeout=40000)
    assert ctx.service.status.phase is ImportPhase.COMPLETED
    assert RunRepository(ctx.db).get(run_id).state == 'SUCCEEDED'


def test_failure_visible_and_stale_deleted_result_does_not_open(setup_ui, qtbot):
    ctx = setup_ui
    def broken(_snapshot):
        raise ValueError('golden read failed')
    ctx.service._prepare = broken
    run_id = submit_from_button(ctx)
    qtbot.waitUntil(lambda: 'golden read failed' in ctx.module.import_status_label.text())
    assert ctx.module.btn_import_audio.isEnabled()
    assert ctx.module._analysis_dialogs == {}


def test_duplicate_targets_and_empty_window_rejected_at_service_boundary(setup_ui):
    from app.service.offline_audio_import import ImportRequest
    from tests.analysis_scheduler.test_imported_precision_analysis import write_tone
    from pathlib import Path
    ctx = setup_ui
    write_tone(Path(ctx.request.source_path), 'float64', columns=2)
    assignments = tuple({'wav_channel_index': column, 'mode': 'config', 'channel_index': 7} for column in range(2))
    invalid = ImportRequest(ctx.request.source_path, ctx.config_id, assignments)
    receipt = ctx.service.submit(invalid)
    assert not receipt.accepted and ('unique' in receipt.error or 'duplicate' in receipt.error)
    empty = replace(invalid, assignments=assignments[:1], start_seconds=0., duration_seconds=0.)
    assert not ctx.service.submit(empty).accepted
    assert ctx.service.status is None


def test_deleted_pending_result_is_not_opened_by_queued_notification(setup_ui, qtbot):
    ctx = setup_ui
    ctx.defer_charts[0] = True
    run_id = submit_from_button(ctx)
    ctx.service.wait_for_phase(ImportPhase.COMPLETING_CHARTS, 40)
    pending = ctx.service.status
    assert ctx.module.services.run_data_management_service.delete_one(run_id).database_committed
    ctx.busy.clear()
    ctx.service.wait_for_phase(ImportPhase.CANCELED, 10)
    ctx.bus.offline_import_status.emit(pending)
    qtbot.waitUntil(lambda: ctx.module.btn_import_audio.isEnabled())
    assert ctx.module._analysis_dialogs == {}
    assert RunRepository(ctx.db).get(run_id) is None
    last = ctx.service.status
    ctx.bus.offline_import_status.emit(ImportStatus(run_id, last.generation - 1,
        ImportPhase.COMPLETING_CHARTS, result_ready=True))
    qtbot.wait(30)
    assert ctx.module._analysis_dialogs == {}
