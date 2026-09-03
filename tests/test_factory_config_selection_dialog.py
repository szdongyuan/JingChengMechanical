from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QAbstractItemView, QDialog

from app.service.factory_mode_controller import (
    FactoryModeController,
    FactorySelectionApplyResult,
)
from app.service.factory_selection_settings import (
    PendingSelectionPersistenceIndeterminateError,
    PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE,
)
from app.services_container import EventBus, FactoryControllerState
from ui.popups.factory_config_selection_dialog import (
    FactoryConfigSelectionDialog,
)


class _Bus(QObject):
    factory_state_changed = pyqtSignal(object)
    config_changed = pyqtSignal()


class _ConfigService:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows)
        self.error: Exception | None = None

    def list_configs(self) -> list[dict]:
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]


class _FactoryMode:
    def __init__(self, state: FactoryControllerState):
        self.state = state
        self.confirm_calls: list[tuple[int, ...]] = []
        self.next_result: FactorySelectionApplyResult | None = None

    def confirm_selection(self, selected_config_ids) -> FactorySelectionApplyResult:
        selected = tuple(selected_config_ids)
        self.confirm_calls.append(selected)
        if self.next_result is not None:
            return self.next_result
        self.state = replace(
            self.state,
            selected_config_ids=selected,
            selection_ready=True,
            selection_ready_reason=None,
        )
        return FactorySelectionApplyResult("applied", selected)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _services(
    *,
    rows: list[dict] | None = None,
    selected: tuple[int, ...] = (101,),
) -> SimpleNamespace:
    state = FactoryControllerState(
        selected_config_ids=selected,
        run_phase="armed" if selected else "not_ready",
    )
    return SimpleNamespace(
        bus=_Bus(),
        config_service=_ConfigService(
            rows
            if rows is not None
            else [
                {"config_id": 101, "config_name": "配置 A"},
                {"config_id": 202, "config_name": "配置 B"},
            ]
        ),
        factory_mode=_FactoryMode(state),
    )


def _show(dialog: FactoryConfigSelectionDialog, qapp) -> None:
    dialog.show()
    qapp.processEvents()


def _rendered_names(dialog: FactoryConfigSelectionDialog) -> list[str]:
    return [
        dialog.config_selector.itemWidget(dialog.config_selector.item(index)).lbl_name.text()
        for index in range(dialog.config_selector.count())
    ]


def test_dialog_has_expected_title_and_instruction_text(qapp):
    dialog = FactoryConfigSelectionDialog(_services())

    assert dialog.windowTitle() == "配置选择"
    assert dialog.instructions_label.text() == (
        "勾选生产使用的配置，拖拽可调整优先顺序，第一项为首选配置。"
    )


def test_dialog_opens_with_authoritative_selection_snapshot(qapp):
    dialog = FactoryConfigSelectionDialog(_services(selected=(202, 101)))

    assert dialog.config_selector.enabled_config_ids() == (202, 101)
    assert dialog.config_selector.primary_config_id() == 202


def test_dialog_empty_state_keeps_launcher_flow_visible_but_disables_confirm(qapp):
    dialog = FactoryConfigSelectionDialog(_services(rows=[], selected=()))
    _show(dialog, qapp)

    assert dialog.empty_label.text() == "配置列表为空，请先新建配置。"
    assert dialog.empty_label.isVisible()
    assert dialog.confirm_button.isEnabled() is False


def test_dialog_nonempty_rows_allow_empty_draft_to_validate_on_confirm(qapp):
    services = _services(selected=())
    dialog = FactoryConfigSelectionDialog(services)

    assert dialog.confirm_button.isEnabled() is True
    dialog.confirm_button.click()

    assert dialog.notice_label.text() == "请至少选择一个配置。"
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert services.factory_mode.confirm_calls == []


def test_dialog_cancel_discards_draft_without_commit(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)

    dialog.reject()
    dialog.prepare_for_open()

    assert services.factory_mode.confirm_calls == []
    assert dialog.config_selector.enabled_config_ids() == (101,)


@pytest.mark.parametrize("close_action", ["escape", "window_close"])
def test_dialog_escape_and_window_close_discard_without_commit(qapp, close_action):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    _show(dialog, qapp)

    if close_action == "escape":
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    else:
        dialog.close()
    qapp.processEvents()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert services.factory_mode.confirm_calls == []


def test_dialog_confirm_applies_ordered_draft_once(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    dialog.config_selector.move_checked_config(202, before_config_id=101)

    dialog.confirm_button.click()

    assert services.factory_mode.confirm_calls == [(202, 101)]
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_dialog_rejected_result_keeps_draft_and_displays_message(qapp):
    services = _services()
    services.factory_mode.next_result = FactorySelectionApplyResult(
        "rejected",
        (101,),
        "配置保存失败",
    )
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)

    dialog.confirm_button.click()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.config_selector.enabled_config_ids() == (101, 202)
    assert dialog.notice_label.text() == "配置保存失败"


def test_dialog_indeterminate_result_stays_open_with_critical_message(qapp):
    services = _services()
    critical_message = "严重错误：配置选择持久化状态不确定，请重启软件后重新对账。"
    services.factory_mode.next_result = FactorySelectionApplyResult(
        "indeterminate",
        (101,),
        critical_message,
    )
    dialog = FactoryConfigSelectionDialog(services)

    dialog.confirm_button.click()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.notice_label.text() == critical_message


def test_real_controller_indeterminate_persistence_permanently_locks_dialog(qapp):
    class _IndeterminateSettings:
        @staticmethod
        def save_pending_selected_config_ids(_selected):
            raise PendingSelectionPersistenceIndeterminateError

        @staticmethod
        def load_pending_selected_config_ids():
            return None

    bus = EventBus()
    services = SimpleNamespace(
        bus=bus,
        config_service=_ConfigService(
            [
                {"config_id": 101, "config_name": "配置 A"},
                {"config_id": 202, "config_name": "配置 B"},
            ]
        ),
        production_selection_locked=True,
        factory_selection_settings=_IndeterminateSettings(),
        request_plc_start_signal_resets_for_configs=lambda _selected: None,
    )
    services.factory_mode = FactoryModeController(
        bus,
        services,
        prepared_selected_config_ids=(101,),
        prepared_config_routes=((101, None),),
    )
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)

    dialog.confirm_button.click()

    critical_reason = services.factory_mode.state.start_ready_reason
    assert services.factory_mode.selection_persistence_indeterminate is True
    assert dialog.config_selector.enabled_config_ids() == (101, 202)
    assert dialog.config_selector.count() == 2
    assert dialog.config_selector.is_locked() is True
    assert dialog.config_selector.dragDropMode() == (
        QAbstractItemView.DragDropMode.NoDragDrop
    )
    assert all(
        not dialog.config_selector.itemWidget(
            dialog.config_selector.item(index)
        ).checkbox.isEnabled()
        for index in range(dialog.config_selector.count())
    )
    assert dialog.confirm_button.isEnabled() is False
    assert dialog.lock_notice_label.text() == critical_reason

    dialog.prepare_for_open()

    assert dialog.config_selector.is_locked() is True
    assert dialog.confirm_button.isEnabled() is False
    assert dialog.lock_notice_label.text() == critical_reason


def test_dialog_restart_required_result_displays_message_and_closes(qapp):
    services = _services()
    services.factory_mode.next_result = FactorySelectionApplyResult(
        "restart_required",
        (101,),
        PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE,
    )
    dialog = FactoryConfigSelectionDialog(services)

    dialog.confirm_button.click()

    assert dialog.notice_label.text() == PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.mark.parametrize(
    ("phase", "reason_fragment"),
    [
        ("running", "当前"),
        ("analyzing", "当前"),
        ("reanalyzing", "当前"),
    ],
)
def test_dialog_busy_state_is_read_only_and_explains_lock(
    qapp,
    phase,
    reason_fragment,
):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    busy_state = replace(services.factory_mode.state, run_phase=phase)

    services.bus.factory_state_changed.emit(busy_state)

    assert dialog.config_selector.is_locked() is True
    assert dialog.confirm_button.isEnabled() is False
    assert reason_fragment in dialog.lock_notice_label.text()


def test_dialog_pending_start_state_is_read_only(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    pending_state = replace(
        services.factory_mode.state,
        start_ready=False,
        start_ready_reason="启动请求处理中",
    )

    services.bus.factory_state_changed.emit(pending_state)

    assert dialog.config_selector.is_locked() is True
    assert dialog.confirm_button.isEnabled() is False
    assert dialog.lock_notice_label.text() == "启动请求处理中"


def test_phase_only_state_changes_preserve_the_draft(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)

    services.bus.factory_state_changed.emit(
        replace(services.factory_mode.state, run_phase="running")
    )
    services.bus.factory_state_changed.emit(
        replace(services.factory_mode.state, run_phase="armed")
    )

    assert dialog.config_selector.enabled_config_ids() == (101, 202)
    assert dialog.config_selector.is_locked() is False


def test_external_selection_change_rebuilds_and_warns(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    external_state = replace(
        services.factory_mode.state,
        selected_config_ids=(202,),
    )

    services.bus.factory_state_changed.emit(external_state)

    assert dialog.config_selector.enabled_config_ids() == (202,)
    assert dialog.notice_label.text() == "配置选择已更新，请重新确认。"


def test_config_refresh_adds_imported_and_renamed_rows_while_open(qapp):
    services = _services(rows=[{"config_id": 101, "config_name": "旧名称"}])
    dialog = FactoryConfigSelectionDialog(services)
    services.config_service.rows = [
        {"config_id": 101, "config_name": "已重命名"},
        {"config_id": 202, "config_name": "新导入配置"},
    ]

    services.bus.config_changed.emit()

    assert dialog.config_selector.count() == 2
    assert _rendered_names(dialog) == ["已重命名", "新导入配置"]


def test_config_refresh_prunes_stale_draft_ids_and_warns(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    services.config_service.rows = [
        {"config_id": 202, "config_name": "配置 B"},
    ]

    services.bus.config_changed.emit()

    assert dialog.config_selector.enabled_config_ids() == (202,)
    assert dialog.notice_label.text() == "部分配置已失效，请重新确认。"


def test_prepare_for_open_refreshes_rows_authority_notice_and_lock(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    dialog.notice_label.setText("旧提示")
    services.config_service.rows = [
        {"config_id": 202, "config_name": "新配置名称"},
    ]
    services.factory_mode.state = FactoryControllerState(
        selected_config_ids=(202,),
        run_phase="running",
    )

    dialog.prepare_for_open()

    assert dialog.config_selector.enabled_config_ids() == (202,)
    assert _rendered_names(dialog) == ["新配置名称"]
    assert dialog.config_selector.is_locked() is True
    assert "当前" in dialog.lock_notice_label.text()


def test_config_load_failure_disables_confirm_and_shows_error(qapp):
    services = _services()
    services.config_service.error = RuntimeError("database unavailable")

    dialog = FactoryConfigSelectionDialog(services)

    assert dialog.config_selector.count() == 0
    assert dialog.confirm_button.isEnabled() is False
    assert dialog.notice_label.text() == "配置加载失败"


def test_config_load_failure_does_not_show_normal_empty_list_message(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    _show(dialog, qapp)
    services.config_service.error = RuntimeError("database unavailable")

    services.bus.config_changed.emit()
    qapp.processEvents()

    assert dialog.config_selector.count() == 2
    assert dialog.empty_label.isVisible() is False
    assert dialog.confirm_button.isEnabled() is False
    assert dialog.config_selector.is_locked() is True
    assert dialog.notice_label.text() == "配置加载失败"


def test_edited_draft_survives_load_failure_and_successful_refresh(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    dialog.config_selector.move_checked_config(202, before_config_id=101)
    services.config_service.error = RuntimeError("database unavailable")

    services.bus.config_changed.emit()

    assert dialog.config_selector.enabled_config_ids() == (202, 101)
    assert _rendered_names(dialog) == ["配置 B", "配置 A"]
    assert dialog.config_selector.is_locked() is True
    assert dialog.confirm_button.isEnabled() is False

    services.config_service.error = None
    services.config_service.rows = [
        {"config_id": 101, "config_name": "配置 A 已重命名"},
        {"config_id": 202, "config_name": "配置 B"},
        {"config_id": 303, "config_name": "新导入配置"},
    ]
    services.bus.config_changed.emit()

    assert dialog.config_selector.enabled_config_ids() == (202, 101)
    assert _rendered_names(dialog) == [
        "配置 B",
        "配置 A 已重命名",
        "新导入配置",
    ]
    assert dialog.config_selector.is_locked() is False
    assert dialog.confirm_button.isEnabled() is True
    assert dialog.notice_label.text() == ""


def test_authoritative_change_retries_after_load_failure_on_same_state(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    external_state = replace(
        services.factory_mode.state,
        selected_config_ids=(202,),
    )
    services.factory_mode.state = external_state
    services.config_service.error = RuntimeError("database unavailable")

    services.bus.factory_state_changed.emit(external_state)

    assert dialog.config_selector.enabled_config_ids() == (101, 202)
    assert dialog.notice_label.text() == "配置加载失败"

    services.config_service.error = None
    services.bus.factory_state_changed.emit(external_state)

    assert dialog.config_selector.enabled_config_ids() == (202,)
    assert dialog.notice_label.text() == "配置选择已更新，请重新确认。"
    assert dialog.config_selector.is_locked() is False


def test_locked_selection_conflict_shows_lock_and_conflict_notices(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    external_state = replace(
        services.factory_mode.state,
        selected_config_ids=(202,),
        run_phase="running",
    )

    services.bus.factory_state_changed.emit(external_state)

    assert dialog.config_selector.enabled_config_ids() == (202,)
    assert dialog.config_selector.is_locked() is True
    assert dialog.confirm_button.isEnabled() is False
    assert dialog.lock_notice_label.text() == "当前测试运行中"
    assert dialog.notice_label.text() == "配置选择已更新，请重新确认。"


def test_locked_config_prune_shows_lock_and_prune_notices(qapp):
    services = _services()
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    services.bus.factory_state_changed.emit(
        replace(services.factory_mode.state, run_phase="running")
    )
    services.config_service.rows = [
        {"config_id": 202, "config_name": "配置 B"},
    ]

    services.bus.config_changed.emit()

    assert dialog.config_selector.enabled_config_ids() == (202,)
    assert dialog.config_selector.is_locked() is True
    assert dialog.confirm_button.isEnabled() is False
    assert dialog.lock_notice_label.text() == "当前测试运行中"
    assert dialog.notice_label.text() == "部分配置已失效，请重新确认。"
