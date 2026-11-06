from __future__ import annotations

import os
import re
from types import SimpleNamespace
import unicodedata

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

import ui.modules.ai_tool_management as ai_tool_management_module
from ui.modules.ai_tool_management import AiToolManagementModule


class _SignalStub:
    def __init__(self):
        self._callbacks = []
        self.emissions = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        self.emissions.append((args, kwargs))
        for callback in self._callbacks:
            callback(*args, **kwargs)


class _AiServiceStub:
    _GENERATED_ID_SUFFIXES = (
        "a1b2c3d4",
        "b1c2d3e4",
        "c1d2e3f4",
        "d1e2f3a4",
    )

    def __init__(
        self,
        *,
        toolchains: list[dict] | None = None,
        bindings_by_toolchain: dict[str, list[dict]] | None = None,
        list_bindings_error: Exception | None = None,
        save_error: Exception | None = None,
        delete_error: Exception | None = None,
    ):
        self.toolchains = [dict(toolchain) for toolchain in (toolchains or [])]
        self.bindings_by_toolchain = {
            toolchain_id: [dict(binding) for binding in bindings]
            for toolchain_id, bindings in (bindings_by_toolchain or {}).items()
        }
        self.list_bindings_error = list_bindings_error
        self.save_error = save_error
        self.delete_error = delete_error
        self.saved_requests: list[tuple[str | None, dict]] = []
        self.list_binding_requests: list[str] = []
        self.deleted_toolchain_ids: list[str] = []
        self.cleared_binding_rows: list[dict] = []
        self._generated_id_counters: dict[str, int] = {}

    def list_toolchains(self) -> list[dict]:
        return [dict(toolchain) for toolchain in self.toolchains]

    @classmethod
    def generated_toolchain_prefix(cls, display_name: str) -> str:
        value = display_name.strip().lower()
        value = unicodedata.normalize("NFKD", value)
        value = value.encode("ascii", "ignore").decode("ascii")
        value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
        if not value:
            return "toolchain"
        value = value[:24].strip("_")
        return value or "toolchain"

    def generate_toolchain_id(self, display_name: str) -> str:
        return self.generate_toolchain_id_from_prefix(
            self.generated_toolchain_prefix(display_name)
        )

    def generate_toolchain_id_from_prefix(self, prefix: str) -> str:
        normalized_prefix = self.generated_toolchain_prefix(prefix)
        while True:
            index = self._generated_id_counters.get(normalized_prefix, 0)
            self._generated_id_counters[normalized_prefix] = index + 1
            if index < len(self._GENERATED_ID_SUFFIXES):
                suffix = self._GENERATED_ID_SUFFIXES[index]
            else:
                suffix = f"{index:08x}"
            toolchain_id = f"{normalized_prefix}_{suffix}"
            if self.get_toolchain(toolchain_id) is None:
                return toolchain_id

    def get_toolchain(self, toolchain_id: str):
        for toolchain in self.toolchains:
            if toolchain["toolchain_id"] == toolchain_id:
                return dict(toolchain)
        return None

    def save_toolchain(self, original_toolchain_id: str | None, data: dict) -> str:
        payload = dict(data)
        self.saved_requests.append((original_toolchain_id, payload))
        if self.save_error is not None:
            raise self.save_error

        persisted_payload = {
            key: value for key, value in payload.items() if key != "generated_id_prefix"
        }

        if original_toolchain_id is None:
            self.toolchains.append(persisted_payload)
        else:
            self.toolchains = [
                persisted_payload
                if toolchain["toolchain_id"] == original_toolchain_id
                else toolchain
                for toolchain in self.toolchains
            ]
        return persisted_payload["toolchain_id"]

    def list_toolchain_bindings(self, toolchain_id: str) -> list[dict]:
        self.list_binding_requests.append(toolchain_id)
        if self.list_bindings_error is not None:
            raise self.list_bindings_error
        return [
            dict(binding)
            for binding in self.bindings_by_toolchain.get(toolchain_id, [])
        ]

    def delete_toolchain(self, toolchain_id: str) -> None:
        self.deleted_toolchain_ids.append(toolchain_id)
        if self.delete_error is not None:
            raise self.delete_error
        bindings = self.bindings_by_toolchain.get(toolchain_id, [])
        self.cleared_binding_rows = [
            {
                **binding,
                "enabled": False,
                "toolchain_id": None,
            }
            for binding in bindings
        ]
        self.bindings_by_toolchain[toolchain_id] = []
        self.toolchains = [
            toolchain
            for toolchain in self.toolchains
            if toolchain["toolchain_id"] != toolchain_id
        ]


def _toolchain(toolchain_id: str, **updates) -> dict:
    payload = {
        "toolchain_id": toolchain_id,
        "display_name": f"Toolchain {toolchain_id}",
        "version": "1.0",
        "entrypoint": f"python {toolchain_id}.py",
        "package_dir": os.getcwd(),
        "default_timeout_ms": 30000,
        "enabled": True,
        "description": "",
        "protocol_version": 1,
        "execution_mode": "oneshot_process",
    }
    payload.update(updates)
    return payload


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_module(
    *,
    toolchains: list[dict] | None = None,
    bindings_by_toolchain: dict[str, list[dict]] | None = None,
    list_bindings_error: Exception | None = None,
    save_error: Exception | None = None,
    delete_error: Exception | None = None,
):
    bus = SimpleNamespace(
        ai_toolchains_changed=_SignalStub(),
        status=_SignalStub(),
    )
    service = _AiServiceStub(
        toolchains=toolchains,
        bindings_by_toolchain=bindings_by_toolchain,
        list_bindings_error=list_bindings_error,
        save_error=save_error,
        delete_error=delete_error,
    )
    services = SimpleNamespace(ai_service=service, bus=bus)
    module = AiToolManagementModule(services)
    return module, service, bus


def _show(module: AiToolManagementModule, qapp):
    module.show()
    qapp.processEvents()


def _current_toolchain_item_id(module: AiToolManagementModule) -> str | None:
    current = module.toolchain_list.currentItem()
    if current is None:
        return None
    return current.data(Qt.ItemDataRole.UserRole)


def _mock_new_toolchain_name_dialog(
    monkeypatch,
    *,
    value: str = "Motor AI",
    accepted: bool = True,
):
    monkeypatch.setattr(
        ai_tool_management_module,
        "QInputDialog",
        SimpleNamespace(getText=lambda *args, **kwargs: (value, accepted)),
        raising=False,
    )


def _create_draft(
    module: AiToolManagementModule,
    qapp,
    monkeypatch,
    *,
    name: str = "Motor AI",
):
    _mock_new_toolchain_name_dialog(monkeypatch, value=name, accepted=True)
    module.btn_new.click()
    qapp.processEvents()


def test_module_shows_empty_state_when_no_toolchains_exist(qapp):
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    assert module.empty_label.isVisible() is True
    assert module.detail.isVisible() is False
    assert module.btn_delete.isEnabled() is False


def test_new_toolchain_cancel_name_dialog_leaves_empty_state(qapp, monkeypatch):
    _mock_new_toolchain_name_dialog(monkeypatch, value="", accepted=False)
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    module.btn_new.click()

    assert module.toolchain_list.count() == 0
    assert module.empty_label.isVisible() is True
    assert module.detail.isVisible() is False


def test_new_cancel_name_dialog_after_discarding_dirty_row_creates_no_draft(
    qapp,
    monkeypatch,
):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    module.ed_display_name.setText("Dirty Name")
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "discard")
    _mock_new_toolchain_name_dialog(monkeypatch, value="", accepted=False)

    module.btn_new.click()

    assert module.toolchain_list.count() == 1
    assert _current_toolchain_item_id(module) == "motor-ai"
    assert "[未保存]" not in module.toolchain_list.item(0).text()
    assert module.ed_display_name.text() == "Toolchain motor-ai"


def test_new_cancel_name_dialog_after_saving_dirty_row_creates_no_draft(
    qapp,
    monkeypatch,
):
    module, service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    module.ed_display_name.setText("Saved Name")
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "save")
    _mock_new_toolchain_name_dialog(monkeypatch, value="", accepted=False)

    module.btn_new.click()

    assert service.saved_requests[-1][1]["display_name"] == "Saved Name"
    assert module.toolchain_list.count() == 1
    assert _current_toolchain_item_id(module) == "motor-ai"
    assert "[未保存]" not in module.toolchain_list.item(0).text()


def test_new_toolchain_blank_display_name_warns_and_creates_no_draft(
    qapp,
    monkeypatch,
):
    warnings = []
    _mock_new_toolchain_name_dialog(monkeypatch, value="   ", accepted=True)
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    module.btn_new.click()

    assert warnings == [("创建失败", "显示名称不能为空")]
    assert module.toolchain_list.count() == 0
    assert module.detail.isVisible() is False


def test_new_toolchain_creates_selected_unsaved_draft_row(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")

    assert module.toolchain_list.count() == 1
    assert _current_toolchain_item_id(module) == "motor_ai_a1b2c3d4"
    assert module.toolchain_list.item(0).text().endswith("[未保存]")
    assert module.ed_display_name.text() == "Motor AI"
    assert module.lb_toolchain_id_value.text() == "motor_ai_a1b2c3d4"
    assert module.combo_execution_mode.currentData() == "resident_process"
    assert module.btn_delete.isEnabled() is False


def test_left_list_sorts_rows_and_marks_disabled_toolchains(qapp):
    module, _service, _bus = _make_module(
        toolchains=[
            _toolchain(
                "z-ai",
                display_name="Zeta AI",
                version="9.0",
                enabled=False,
            ),
            _toolchain(
                "a-ai",
                display_name="Alpha AI",
                version="1.2",
                enabled=True,
            ),
        ]
    )
    _show(module, qapp)

    item_ids = [
        module.toolchain_list.item(index).data(Qt.ItemDataRole.UserRole)
        for index in range(module.toolchain_list.count())
    ]
    item_texts = [
        module.toolchain_list.item(index).text()
        for index in range(module.toolchain_list.count())
    ]

    assert item_ids == ["a-ai", "z-ai"]
    assert "Alpha AI" in item_texts[0]
    assert "1.2" in item_texts[0]
    assert "Zeta AI" in item_texts[1]
    assert "9.0" in item_texts[1]
    assert item_texts[1].endswith("[禁用]")


def test_package_dir_row_uses_shared_fixed_single_line_height(qapp, monkeypatch):
    class TallBrowseButton(QPushButton):
        def sizeHint(self):
            size = super().sizeHint()
            if self.text() == "浏览":
                return QSize(size.width(), size.height() + 12)
            return size

    monkeypatch.setattr(ai_tool_management_module, "QPushButton", TallBrowseButton)

    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    layout = module.package_dir_row.layout()
    editor_index = layout.indexOf(module.ed_package_dir)
    button_index = layout.indexOf(module.btn_browse_package_dir)
    assert editor_index >= 0
    assert button_index >= 0
    expected_alignment = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignTop
    assert layout.itemAt(editor_index).alignment() & expected_alignment
    assert layout.itemAt(button_index).alignment() & expected_alignment
    assert module.package_dir_row.sizePolicy().verticalPolicy().name == "Fixed"
    assert (
        module.btn_browse_package_dir.height()
        >= module.btn_browse_package_dir.sizeHint().height()
    )
    assert module.ed_package_dir.height() == module.btn_browse_package_dir.height()
    assert module.package_dir_row.height() == module.ed_package_dir.height()


def test_timeout_row_uses_fixed_single_line_height_and_preserves_ms_label(qapp):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    assert module.timeout_row.sizePolicy().verticalPolicy().name == "Fixed"
    assert module.timeout_row.height() >= module.spin_timeout.sizeHint().height()
    assert module.timeout_row.layout().indexOf(module.spin_timeout) >= 0
    assert module.timeout_row.layout().indexOf(module.lb_timeout_unit) >= 0
    assert module.lb_timeout_unit.text() == "ms"
    assert module.lb_timeout_unit.isVisible() is True
    assert module.spin_timeout.text() == "30000"


def test_detail_pane_wraps_form_in_scroll_area_and_keeps_save_row_outside(qapp):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    assert hasattr(module, "detail_scroll")
    assert module.detail_scroll.widgetResizable() is True
    assert module.detail_scroll.widget() is not None
    assert module.detail_scroll.frameShape().name == "NoFrame"
    assert module.detail_scroll.parentWidget() == module.detail

    save_row = module.btn_save.parentWidget()
    detail_layout = module.detail.layout()
    assert save_row.parentWidget() == module.detail
    assert detail_layout.indexOf(module.detail_scroll) >= 0
    assert detail_layout.indexOf(save_row) > detail_layout.indexOf(module.detail_scroll)
    assert save_row.parentWidget() != module.detail_scroll.widget()


def test_tall_detail_pane_keeps_form_host_near_compact_height(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.resize(1280, 1400)
    module.show()
    qapp.processEvents()

    form_host = module._form_host
    assert form_host.height() < module.detail.height()
    assert form_host.height() <= form_host.sizeHint().height() + 40


def test_selecting_row_populates_execution_mode_and_timeout_unit(qapp):
    module, _service, _bus = _make_module(
        toolchains=[
            _toolchain(
                "motor-ai",
                display_name="Motor AI",
                version="2.0",
                entrypoint="python motor_ai_v2.py",
                default_timeout_ms=45000,
                enabled=False,
                description="updated registry entry",
                execution_mode="oneshot_process",
                params_json='{"mode":"v2"}',
            )
        ]
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    assert module.lb_toolchain_id_value.text() == "motor-ai"
    assert module.ed_display_name.text() == "Motor AI"
    assert module.ed_version.text() == "2.0"
    assert module.ed_entrypoint.toPlainText() == "python motor_ai_v2.py"
    assert module.ed_package_dir.text() == os.getcwd()
    assert module.spin_timeout.value() == 45000
    assert module.spin_timeout.text() == "45000"
    assert module.spin_timeout.placeholderText() == ""
    assert module.spin_timeout.alignment() == Qt.AlignmentFlag.AlignLeft
    assert module.timeout_row.layout().indexOf(module.spin_timeout) >= 0
    assert module.timeout_row.layout().indexOf(module.lb_timeout_unit) >= 0
    assert module.lb_timeout_unit.text() == "ms"
    assert module.lb_timeout_unit.isVisible() is True
    assert module.chk_enabled.isChecked() is False
    assert module.txt_description.toPlainText() == "updated registry entry"
    assert module.txt_params_json.toPlainText() == '{"mode":"v2"}'
    assert module.combo_execution_mode.currentData() == "oneshot_process"


def test_runtime_helper_copy_is_removed(qapp):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    labels_text = [label.text() for label in module.findChildren(QLabel)]

    assert not hasattr(module, "runtime_helper")
    assert not any("运行时说明" in text for text in labels_text)
    assert not any("protocol_version" in text for text in labels_text)
    assert not any("resident_process" in text for text in labels_text)
    assert not any("oneshot_process" in text for text in labels_text)


def test_internal_toolchain_id_is_not_shown_to_user(qapp):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    labels_text = [label.text() for label in module.findChildren(QLabel)]

    assert "内部标识" not in labels_text
    assert module.lb_toolchain_id_value.parentWidget() is None


def test_package_dir_browse_populates_editor_and_marks_dirty(qapp, tmp_path, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    selected_dir = tmp_path / "selected-package"
    selected_dir.mkdir()
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QFileDialog.getExistingDirectory",
        lambda *args, **kwargs: str(selected_dir),
    )

    module.btn_browse_package_dir.click()
    qapp.processEvents()

    assert module.ed_package_dir.text() == str(selected_dir)
    assert module._is_dirty() is True


def test_save_persists_json_params(qapp, monkeypatch):
    module, service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.ed_package_dir.setText(os.getcwd())
    module.txt_params_json.setPlainText('{"threshold":0.7}')

    module.btn_save.click()

    assert service.saved_requests[-1][1]["params_json"] == '{"threshold":0.7}'


def test_invalid_json_params_block_save(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )
    module, service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.ed_package_dir.setText(os.getcwd())
    module.txt_params_json.setPlainText("[]")

    module.btn_save.click()

    assert service.saved_requests == []
    assert warnings[0][0] in {"保存失败", "淇濆瓨澶辫触"}
    assert "参数(JSON)" in warnings[0][1]


def test_save_persists_selected_execution_mode(qapp, monkeypatch):
    module, service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.ed_package_dir.setText(os.getcwd())
    module.combo_execution_mode.setCurrentIndex(
        module.combo_execution_mode.findData("oneshot_process")
    )

    assert module._collect_save_payload()["package_dir"] == os.getcwd()
    module.btn_save.click()

    assert service.saved_requests[-1][1]["execution_mode"] == "oneshot_process"
    assert service.saved_requests[-1][1]["package_dir"] == os.getcwd()
    assert service.saved_requests[-1][1]["toolchain_id"] == "motor_ai_a1b2c3d4"
    assert service.saved_requests[-1][1]["generated_id_prefix"] == "motor_ai"


def test_large_timeout_value_loads_and_saves_without_clamping(qapp):
    large_timeout_ms = 5_000_000_000
    module, service, bus = _make_module(
        toolchains=[
            _toolchain(
                "motor-ai",
                default_timeout_ms=large_timeout_ms,
            )
        ]
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    assert module.spin_timeout.value() == large_timeout_ms

    module.btn_save.click()

    saved_original_id, saved = service.saved_requests[-1]
    assert saved_original_id == "motor-ai"
    assert saved["default_timeout_ms"] == large_timeout_ms
    assert bus.ai_toolchains_changed.emissions == [((), {})]


def test_left_list_labels_include_toolchain_id_for_duplicate_name_version_rows(qapp):
    module, _service, _bus = _make_module(
        toolchains=[
            _toolchain(
                "motor-ai-a",
                display_name="Motor AI",
                version="1.0",
            ),
            _toolchain(
                "motor-ai-b",
                display_name="Motor AI",
                version="1.0",
            ),
        ]
    )
    _show(module, qapp)

    item_texts = [
        module.toolchain_list.item(index).text()
        for index in range(module.toolchain_list.count())
    ]

    assert len(item_texts) == 2
    assert item_texts[0] != item_texts[1]
    assert "Motor AI" in item_texts[0]
    assert "1.0" in item_texts[0]
    assert "motor-ai-a" in item_texts[0]
    assert "Motor AI" in item_texts[1]
    assert "1.0" in item_texts[1]
    assert "motor-ai-b" in item_texts[1]


def test_save_new_toolchain_emits_registry_refresh(qapp, monkeypatch):
    module, service, bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_version.setText("1.0")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.ed_package_dir.setText(os.getcwd())
    module.spin_timeout.setValue(30000)
    module.chk_enabled.setChecked(True)
    module.txt_description.setPlainText("registry entry")

    module.btn_save.click()

    saved_original_id, saved = service.saved_requests[-1]
    assert saved_original_id is None
    assert saved["toolchain_id"] == "motor_ai_a1b2c3d4"
    assert saved["display_name"] == "Motor AI"
    assert saved["generated_id_prefix"] == "motor_ai"
    assert saved["package_dir"] == os.getcwd()
    assert bus.ai_toolchains_changed.emissions == [((), {})]
    assert (
        module.toolchain_list.currentItem().data(Qt.ItemDataRole.UserRole)
        == "motor_ai_a1b2c3d4"
    )
    assert module.empty_label.isVisible() is False
    assert module.detail.isVisible() is True


def test_draft_display_name_edit_updates_left_list_label(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[_toolchain("persisted-ai")])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_display_name.setText("Updated Motor AI")
    module.ed_version.setText("2.0")
    qapp.processEvents()

    assert module.toolchain_list.item(0).data(Qt.ItemDataRole.UserRole) == "motor_ai_a1b2c3d4"
    assert (
        module.toolchain_list.item(0).text()
        == "Updated Motor AI (2.0) - motor_ai_a1b2c3d4 [未保存]"
    )
    assert module.toolchain_list.item(1).data(Qt.ItemDataRole.UserRole) == "persisted-ai"


def test_empty_draft_display_name_uses_placeholder_in_left_list(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_display_name.setText("")
    qapp.processEvents()

    assert (
        module.toolchain_list.item(0).text()
        == "未命名工具链 (-) - motor_ai_a1b2c3d4 [未保存]"
    )


def test_discarding_unsaved_draft_removes_draft_row(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[_toolchain("persisted-ai")])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    actions = []
    monkeypatch.setattr(
        module,
        "_prompt_unsaved_changes",
        lambda action: actions.append(action) or "discard",
    )

    module._select_toolchain("persisted-ai")
    qapp.processEvents()

    assert actions == ["switch"]
    assert module.toolchain_list.count() == 1
    assert _current_toolchain_item_id(module) == "persisted-ai"
    assert "[未保存]" not in module.toolchain_list.item(0).text()
    assert module.ed_display_name.text() == "Toolchain persisted-ai"


def test_canceling_unsaved_draft_switch_keeps_draft_row(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[_toolchain("persisted-ai")])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "cancel")

    module._select_toolchain("persisted-ai")
    qapp.processEvents()

    assert module.toolchain_list.count() == 2
    assert _current_toolchain_item_id(module) == "motor_ai_a1b2c3d4"
    assert module.toolchain_list.item(0).text().endswith("[未保存]")
    assert module.ed_display_name.text() == "Motor AI"


@pytest.mark.parametrize(
    ("dialog_value", "accepted", "expected_warnings"),
    [
        ("", False, []),
        ("   ", True, [("创建失败", "显示名称不能为空")]),
    ],
)
def test_new_name_dialog_cancel_or_blank_after_discarding_active_draft_keeps_discard_state(
    qapp,
    monkeypatch,
    dialog_value,
    accepted,
    expected_warnings,
):
    warnings = []
    module, _service, _bus = _make_module(
        toolchains=[
            _toolchain("alpha-ai"),
            _toolchain("zeta-ai"),
        ]
    )
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "discard")
    _mock_new_toolchain_name_dialog(
        monkeypatch,
        value=dialog_value,
        accepted=accepted,
    )
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )

    module.btn_new.click()
    qapp.processEvents()

    assert warnings == expected_warnings
    assert module.toolchain_list.count() == 2
    assert _current_toolchain_item_id(module) is None
    assert module._current_toolchain_id is None
    assert module._draft_payload is None
    assert module._is_new_draft is False
    assert module.detail.isVisible() is False
    assert module.empty_label.isVisible() is False
    assert module.ed_display_name.text() == ""
    assert all(
        "[未保存]" not in module.toolchain_list.item(index).text()
        for index in range(module.toolchain_list.count())
    )


def test_saving_draft_persists_generated_id(qapp, monkeypatch):
    module, service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.ed_package_dir.setText(os.getcwd())
    module.btn_save.click()
    qapp.processEvents()

    assert service.saved_requests[-1][1]["toolchain_id"] == "motor_ai_a1b2c3d4"
    assert service.saved_requests[-1][1]["generated_id_prefix"] == "motor_ai"
    assert service.toolchains[0]["toolchain_id"] == "motor_ai_a1b2c3d4"
    assert "[未保存]" not in module.toolchain_list.item(0).text()
    assert module.lb_toolchain_id_value.text() == "motor_ai_a1b2c3d4"
    assert module._is_new_draft is False


def test_saving_draft_regenerates_when_generated_id_is_already_taken(
    qapp,
    monkeypatch,
):
    module, service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_display_name.setText("Motor AI Renamed")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.ed_package_dir.setText(os.getcwd())
    service.toolchains.append(_toolchain("motor_ai_a1b2c3d4", display_name="Occupied"))

    module.btn_save.click()
    qapp.processEvents()

    assert service.saved_requests[-1][1]["toolchain_id"] == "motor_ai_b1c2d3e4"
    assert service.saved_requests[-1][1]["generated_id_prefix"] == "motor_ai"
    assert _current_toolchain_item_id(module) == "motor_ai_b1c2d3e4"
    assert module.lb_toolchain_id_value.text() == "motor_ai_b1c2d3e4"


def test_edit_save_updates_existing_row_without_creating_duplicate(qapp):
    module, service, bus = _make_module(
        toolchains=[
            _toolchain(
                "motor-ai",
                display_name="Motor AI",
                version="1.0",
                enabled=True,
            )
        ]
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    module.ed_display_name.setText("Motor AI V2")
    module.ed_version.setText("2.0")
    module.chk_enabled.setChecked(False)

    module.btn_save.click()

    saved_original_id, saved = service.saved_requests[-1]
    assert saved_original_id == "motor-ai"
    assert saved["display_name"] == "Motor AI V2"
    assert saved["enabled"] is False
    assert len(service.toolchains) == 1
    assert service.toolchains[0]["toolchain_id"] == "motor-ai"
    assert service.toolchains[0]["display_name"] == "Motor AI V2"
    assert bus.ai_toolchains_changed.emissions == [((), {})]
    assert module.toolchain_list.item(0).text().endswith("[禁用]")


def test_save_normalizes_editor_snapshot_and_discard_restores_persisted_row(
    qapp,
    monkeypatch,
):
    module, service, _bus = _make_module(
        toolchains=[
            _toolchain(
                "motor-ai",
                display_name="Motor AI",
                entrypoint="python motor_ai.py",
                description="saved description",
            )
        ]
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    module.ed_display_name.setText("Motor AI   ")
    module.ed_entrypoint.setPlainText("python motor_ai.py   ")
    module.ed_package_dir.setText(f"  {os.getcwd()}  ")
    module.txt_description.setPlainText("saved description   ")
    qapp.processEvents()

    assert module._is_dirty() is True

    module.btn_save.click()
    qapp.processEvents()

    assert service.saved_requests[-1][1]["display_name"] == "Motor AI"
    assert service.saved_requests[-1][1]["entrypoint"] == "python motor_ai.py"
    assert service.saved_requests[-1][1]["package_dir"] == os.getcwd()
    assert service.saved_requests[-1][1]["description"] == "saved description"
    assert module.ed_display_name.text() == "Motor AI"
    assert module.ed_entrypoint.toPlainText() == "python motor_ai.py"
    assert module.ed_package_dir.text() == os.getcwd()
    assert module.txt_description.toPlainText() == "saved description"
    assert module._editor_snapshot["display_name"] == "Motor AI"
    assert module._editor_snapshot["entrypoint"] == "python motor_ai.py"
    assert module._editor_snapshot["package_dir"] == os.getcwd()
    assert module._editor_snapshot["description"] == "saved description"

    module.ed_version.setText("2.0")
    qapp.processEvents()
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "discard")

    assert module.confirm_leave() is True
    assert module.ed_display_name.text() == "Motor AI"
    assert module.ed_entrypoint.toPlainText() == "python motor_ai.py"
    assert module.ed_package_dir.text() == os.getcwd()
    assert module.ed_version.text() == "1.0"
    assert module.txt_description.toPlainText() == "saved description"


def test_edit_enabled_persists_false_then_true_and_emits_refresh(qapp):
    module, service, bus = _make_module(
        toolchains=[
            _toolchain(
                "motor-ai",
                enabled=True,
            )
        ]
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    module.chk_enabled.setChecked(False)
    module.btn_save.click()

    module.chk_enabled.setChecked(True)
    module.btn_save.click()

    assert [saved["enabled"] for _original_id, saved in service.saved_requests[-2:]] == [
        False,
        True,
    ]
    assert bus.ai_toolchains_changed.emissions == [
        ((), {}),
        ((), {}),
    ]
    assert module.toolchain_list.item(0).text().endswith("[禁用]") is False


def test_delete_button_is_enabled_when_row_is_selected(qapp):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    assert module.toolchain_list.currentItem() is not None
    assert module.btn_delete.isEnabled() is True


def test_delete_button_is_disabled_for_selected_new_draft(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")

    assert module.toolchain_list.currentItem() is not None
    assert module.btn_delete.isEnabled() is False


@pytest.mark.parametrize(
    ("setup", "expected_message"),
    [
        ("missing_required", "工具链标识、显示名称和入口命令不能为空"),
        ("missing_package_dir", "AI 包目录不能为空"),
        ("invalid_timeout", "默认超时必须大于 0"),
    ],
)
def test_local_validation_shows_warning_before_service_write(
    qapp,
    monkeypatch,
    setup,
    expected_message,
):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )
    module, service, _bus = _make_module(toolchains=[_toolchain("existing-ai")])
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="New AI")
    module.ed_entrypoint.setPlainText("python new_ai.py")
    module.ed_package_dir.setText(os.getcwd())
    module.spin_timeout.setValue(30000)

    if setup == "missing_required":
        module.ed_display_name.setText("")
    elif setup == "missing_package_dir":
        module.ed_package_dir.setText("")
    elif setup == "invalid_timeout":
        module.spin_timeout.setValue(0)

    module.btn_save.click()

    assert warnings == [("保存失败", expected_message)]
    assert service.saved_requests == []


def test_busy_save_error_shows_user_warning(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )
    module, service, _bus = _make_module(
        toolchains=[],
        save_error=RuntimeError("Resident toolchain is busy"),
    )
    _show(module, qapp)

    _create_draft(module, qapp, monkeypatch, name="Motor AI")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.ed_package_dir.setText(os.getcwd())
    module.combo_execution_mode.setCurrentIndex(
        module.combo_execution_mode.findData("resident_process")
    )

    module.btn_save.click()

    assert service.saved_requests == [
        (
            None,
            {
                "toolchain_id": "motor_ai_a1b2c3d4",
                "display_name": "Motor AI",
                "version": "",
                "entrypoint": "python motor_ai.py",
                "package_dir": os.getcwd(),
                "default_timeout_ms": 30000,
                "enabled": True,
                "description": "",
                "execution_mode": "resident_process",
                "params_json": "{}",
                "generated_id_prefix": "motor_ai",
            },
        )
    ]
    assert warnings == [("保存失败", "Resident toolchain is busy")]


def test_busy_delete_error_shows_user_warning(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.question",
        lambda *args: args[3].Yes,
    )
    module, service, _bus = _make_module(
        toolchains=[_toolchain("motor-ai")],
        delete_error=RuntimeError("Resident toolchain is busy"),
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    module.btn_delete.click()

    assert service.deleted_toolchain_ids == ["motor-ai"]
    assert warnings == [("删除失败", "Resident toolchain is busy")]


def test_delete_dirty_row_prompts_unsaved_changes_before_delete_confirmation(
    qapp,
    monkeypatch,
):
    module, service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()
    module.ed_display_name.setText("Edited Name")
    qapp.processEvents()

    flow = []
    monkeypatch.setattr(
        module,
        "_prompt_unsaved_changes",
        lambda action: flow.append(("dirty", action)) or "discard",
    )
    monkeypatch.setattr(
        module,
        "_confirm_delete",
        lambda *args, **kwargs: flow.append(("delete", None)) or False,
    )

    module.btn_delete.click()

    assert flow == [("dirty", "delete"), ("delete", None)]
    assert service.deleted_toolchain_ids == []


def test_row_switch_cancel_keeps_current_dirty_editor(qapp, monkeypatch):
    module, _service, _bus = _make_module(
        toolchains=[_toolchain("a"), _toolchain("b")]
    )
    _show(module, qapp)

    module._select_toolchain("a")
    qapp.processEvents()
    module.ed_display_name.setText("Unsaved")
    qapp.processEvents()
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "cancel")

    module._select_toolchain("b")
    qapp.processEvents()

    assert module._current_toolchain_id == "a"
    assert _current_toolchain_item_id(module) == "a"
    assert module.ed_display_name.text() == "Unsaved"


def test_new_while_dirty_cancel_keeps_current_editor(qapp, monkeypatch):
    module, _service, _bus = _make_module(
        toolchains=[_toolchain("a"), _toolchain("b")]
    )
    _show(module, qapp)

    module._select_toolchain("a")
    qapp.processEvents()
    module.ed_display_name.setText("Unsaved")
    qapp.processEvents()
    calls = []
    monkeypatch.setattr(
        module,
        "_prompt_unsaved_changes",
        lambda action: calls.append(action) or "cancel",
    )

    module.btn_new.click()
    qapp.processEvents()

    assert calls == ["new"]
    assert module._current_toolchain_id == "a"
    assert module._is_new_draft is False
    assert _current_toolchain_item_id(module) == "a"
    assert module.ed_display_name.text() == "Unsaved"


def test_confirm_leave_returns_false_when_unsaved_prompt_cancels(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()
    module.ed_display_name.setText("Unsaved")
    qapp.processEvents()
    actions = []
    monkeypatch.setattr(
        module,
        "_prompt_unsaved_changes",
        lambda action: actions.append(action) or "cancel",
    )

    result = module.confirm_leave()

    assert result is False
    assert actions == ["leave"]
    assert module.ed_display_name.text() == "Unsaved"
    assert module._current_toolchain_id == "motor-ai"


def test_confirm_leave_whitespace_only_edit_still_prompts_unsaved_changes(
    qapp,
    monkeypatch,
):
    module, _service, _bus = _make_module(
        toolchains=[_toolchain("motor-ai", display_name="Motor AI")]
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()
    module.ed_display_name.setText("Motor AI   ")
    qapp.processEvents()
    actions = []
    monkeypatch.setattr(
        module,
        "_prompt_unsaved_changes",
        lambda action: actions.append(action) or "cancel",
    )

    result = module.confirm_leave()

    assert result is False
    assert actions == ["leave"]
    assert module.ed_display_name.text() == "Motor AI   "
    assert module._current_toolchain_id == "motor-ai"


def test_delete_confirmation_dialog_includes_identity_binding_names_count_and_warning(
    qapp,
    monkeypatch,
):
    question_calls = []
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.question",
        lambda *args: question_calls.append(args[1:3])
        or QMessageBox.StandardButton.No,
    )
    module, service, _bus = _make_module(
        toolchains=[
            _toolchain(
                "motor-ai",
                display_name="Motor AI",
                version="2.0",
            )
        ],
        bindings_by_toolchain={
            "motor-ai": [
                {"config_id": 7, "config_name": "Cfg Alpha"},
                {"config_id": 9, "config_name": "Cfg Beta"},
            ]
        },
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    module.btn_delete.click()

    assert service.list_binding_requests == ["motor-ai"]
    assert len(question_calls) == 1
    title, text = question_calls[0]
    assert title == "确认删除"
    assert "Motor AI" in text
    assert "motor-ai" in text
    assert "2" in text
    assert "Cfg Alpha" in text
    assert "Cfg Beta" in text
    assert "解除绑定" in text
    assert service.deleted_toolchain_ids == []


def test_delete_binding_lookup_failure_shows_warning_and_keeps_state(
    qapp,
    monkeypatch,
):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )
    module, service, _bus = _make_module(
        toolchains=[
            _toolchain("motor-ai", display_name="Motor AI"),
            _toolchain("other-ai", display_name="Other AI"),
        ],
        list_bindings_error=RuntimeError("无法加载工具链绑定"),
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    module.btn_delete.click()
    qapp.processEvents()

    assert service.list_binding_requests == ["motor-ai"]
    assert warnings == [("删除失败", "无法加载工具链绑定")]
    assert service.deleted_toolchain_ids == []
    assert module._current_toolchain_id == "motor-ai"
    assert _current_toolchain_item_id(module) == "motor-ai"
    assert module.ed_display_name.text() == "Motor AI"
    assert module.toolchain_list.count() == 2


def test_delete_clears_bindings_and_emits_ai_toolchains_changed(qapp, monkeypatch):
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.question",
        lambda *args: args[3].Yes,
    )
    module, service, bus = _make_module(
        toolchains=[
            _toolchain("motor-ai"),
            _toolchain("other-ai"),
        ],
        bindings_by_toolchain={
            "motor-ai": [
                {
                    "config_id": 1,
                    "config_name": "Cfg One",
                    "toolchain_id": "motor-ai",
                    "enabled": True,
                    "timeout_ms": 3000,
                    "params_json": '{"mode":"a"}',
                },
                {
                    "config_id": 2,
                    "config_name": "Cfg Two",
                    "toolchain_id": "motor-ai",
                    "enabled": False,
                    "timeout_ms": 4000,
                    "params_json": '{"mode":"b"}',
                },
            ]
        },
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    module.btn_delete.click()
    qapp.processEvents()

    assert service.deleted_toolchain_ids == ["motor-ai"]
    assert service.list_binding_requests == ["motor-ai"]
    assert service.cleared_binding_rows == [
        {
            "config_id": 1,
            "config_name": "Cfg One",
            "toolchain_id": None,
            "enabled": False,
            "timeout_ms": 3000,
            "params_json": '{"mode":"a"}',
        },
        {
            "config_id": 2,
            "config_name": "Cfg Two",
            "toolchain_id": None,
            "enabled": False,
            "timeout_ms": 4000,
            "params_json": '{"mode":"b"}',
        },
    ]
    assert bus.ai_toolchains_changed.emissions == [((), {})]
    assert _current_toolchain_item_id(module) == "other-ai"
    assert service.bindings_by_toolchain["motor-ai"] == []


@pytest.mark.parametrize(
    "pending_action",
    ["switch", "delete", "leave"],
)
def test_save_failure_during_unsaved_prompt_blocks_pending_action_and_keeps_editor_visible(
    qapp,
    monkeypatch,
    pending_action,
):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )
    module, service, _bus = _make_module(
        toolchains=[_toolchain("a"), _toolchain("b")]
    )
    _show(module, qapp)

    module._select_toolchain("a")
    qapp.processEvents()
    module.ed_display_name.setText("")
    qapp.processEvents()
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "save")
    delete_calls = []
    monkeypatch.setattr(
        module,
        "_confirm_delete",
        lambda *args, **kwargs: delete_calls.append("confirm") or True,
    )

    if pending_action == "switch":
        result = None
        module._select_toolchain("b")
        qapp.processEvents()
    elif pending_action == "delete":
        result = None
        module.btn_delete.click()
        qapp.processEvents()
    else:
        result = module.confirm_leave()

    if pending_action == "leave":
        assert result is False
    else:
        assert result is None
    assert warnings == [("保存失败", "工具链标识、显示名称和入口命令不能为空")]
    assert service.saved_requests == []
    assert service.deleted_toolchain_ids == []
    assert delete_calls == []
    assert module.detail.isVisible() is True
    assert module._current_toolchain_id == "a"
    assert _current_toolchain_item_id(module) == "a"
    assert module.ed_display_name.text() == ""

@pytest.mark.parametrize('value', [None, 0, 1, 0.12345678901234568])
def test_optional_threshold_load_save_clear(qapp, value):
    import json
    row = _toolchain('tc', params_json=json.dumps({'decision_threshold': value, 'mode': 'keep'}))
    module, service, _ = _make_module(toolchains=[row])
    module._load_toolchain(row)
    assert module.ed_decision_threshold.text() == ('' if value is None else str(value))
    assert json.loads(module.txt_params_json.toPlainText()) == {'mode': 'keep'}
    assert not module._is_dirty()
    module.ed_decision_threshold.setText('0.12345678901234568')
    assert module._is_dirty() is (value != 0.12345678901234568)
    assert module._save_current_editor()
    assert json.loads(service.saved_requests[-1][1]['params_json']) == {'mode': 'keep', 'decision_threshold': 0.12345678901234568}
    assert not module._is_dirty()
    module.ed_decision_threshold.clear()
    assert module._save_current_editor()
    assert json.loads(service.saved_requests[-1][1]['params_json']) == {'mode': 'keep'}

@pytest.mark.parametrize('text', ['bad', 'true', 'NaN', 'Infinity', '-0.1', '1.1'])
def test_optional_threshold_invalid_save_is_warning(qapp, monkeypatch, text):
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args[-1]))
    row = _toolchain('tc')
    module, service, _ = _make_module(toolchains=[row])
    module._load_toolchain(row)
    module.ed_decision_threshold.setText(text)
    assert not module._save_current_editor()
    assert not service.saved_requests
    assert 'decision_threshold' in warnings[-1]
    module._restore_snapshot()
    assert module.ed_decision_threshold.text() == ''
    assert not module._is_dirty()

def test_threshold_raw_json_conflict(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args[-1]))
    row = _toolchain('tc')
    module, service, _ = _make_module(toolchains=[row])
    module._load_toolchain(row)
    module.txt_params_json.setPlainText('{"decision_threshold":0.6}')
    assert not module._save_current_editor()
    assert not service.saved_requests
    assert '专用' in warnings[-1]

@pytest.mark.parametrize('value', ['0.5', True, [0.5]])
def test_invalid_loaded_threshold_requires_correction(qapp, monkeypatch, value):
    import json
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)
    row = _toolchain('tc', params_json=json.dumps({'decision_threshold': value}))
    module, service, _ = _make_module(toolchains=[row])
    module._load_toolchain(row)
    assert not module._save_current_editor()
    assert not service.saved_requests

@pytest.mark.parametrize('choice', ['save', 'discard', 'cancel'])
def test_threshold_switch_preserves_unsaved_choice(qapp, monkeypatch, choice):
    import json
    module, service, _ = _make_module(toolchains=[_toolchain('a'), _toolchain('b')])
    _show(module, qapp)
    module._select_toolchain('a')
    module.ed_decision_threshold.setText('0.6')
    monkeypatch.setattr(module, '_prompt_unsaved_changes', lambda action: choice)
    module._select_toolchain('b')
    qapp.processEvents()
    if choice == 'cancel':
        assert module._current_toolchain_id == 'a'
        assert module.ed_decision_threshold.text() == '0.6'
        assert module._is_dirty()
    else:
        assert module._current_toolchain_id == 'b'
        assert module.ed_decision_threshold.text() == ''
        module._select_toolchain('a')
        assert module.ed_decision_threshold.text() == ('0.6' if choice == 'save' else '')
        assert not module._is_dirty()
    assert bool(service.saved_requests) is (choice == 'save')
