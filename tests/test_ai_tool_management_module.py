from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

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
    def __init__(
        self,
        *,
        toolchains: list[dict] | None = None,
        save_error: Exception | None = None,
    ):
        self.toolchains = [dict(toolchain) for toolchain in (toolchains or [])]
        self.save_error = save_error
        self.saved_requests: list[tuple[str | None, dict]] = []

    def list_toolchains(self) -> list[dict]:
        return [dict(toolchain) for toolchain in self.toolchains]

    def save_toolchain(self, original_toolchain_id: str | None, data: dict) -> str:
        payload = dict(data)
        self.saved_requests.append((original_toolchain_id, payload))
        if self.save_error is not None:
            raise self.save_error

        if original_toolchain_id is None:
            self.toolchains.append(payload)
        else:
            self.toolchains = [
                payload if toolchain["toolchain_id"] == original_toolchain_id else toolchain
                for toolchain in self.toolchains
            ]
        return payload["toolchain_id"]


def _toolchain(toolchain_id: str, **updates) -> dict:
    payload = {
        "toolchain_id": toolchain_id,
        "display_name": f"Toolchain {toolchain_id}",
        "version": "1.0",
        "entrypoint": f"python {toolchain_id}.py",
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
    save_error: Exception | None = None,
):
    bus = SimpleNamespace(
        ai_toolchains_changed=_SignalStub(),
        status=_SignalStub(),
    )
    service = _AiServiceStub(toolchains=toolchains, save_error=save_error)
    services = SimpleNamespace(ai_service=service, bus=bus)
    module = AiToolManagementModule(services)
    return module, service, bus


def _show(module: AiToolManagementModule, qapp):
    module.show()
    qapp.processEvents()


def test_module_shows_empty_state_when_no_toolchains_exist(qapp):
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    assert module.empty_label.isVisible() is True
    assert module.detail.isVisible() is False
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


def test_selecting_row_populates_editor_and_runtime_helper_text(qapp):
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
            )
        ]
    )
    _show(module, qapp)

    module._select_toolchain("motor-ai")
    qapp.processEvents()

    assert module.ed_toolchain_id.text() == "motor-ai"
    assert module.ed_display_name.text() == "Motor AI"
    assert module.ed_version.text() == "2.0"
    assert module.ed_entrypoint.toPlainText() == "python motor_ai_v2.py"
    assert module.spin_timeout.value() == 45000
    assert module.chk_enabled.isChecked() is False
    assert module.txt_description.toPlainText() == "updated registry entry"
    assert "protocol_version = 1" in module.runtime_helper.text()
    assert "execution_mode = oneshot_process" in module.runtime_helper.text()


def test_large_timeout_value_loads_and_saves_without_clamping(qapp):
    large_timeout_ms = 900001
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


def test_save_new_toolchain_emits_registry_refresh(qapp):
    module, service, bus = _make_module(toolchains=[])
    _show(module, qapp)

    module.btn_new.click()
    module.ed_toolchain_id.setText("motor-ai")
    module.ed_display_name.setText("Motor AI")
    module.ed_version.setText("1.0")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.spin_timeout.setValue(30000)
    module.chk_enabled.setChecked(True)
    module.txt_description.setPlainText("registry entry")

    module.btn_save.click()

    saved_original_id, saved = service.saved_requests[-1]
    assert saved_original_id is None
    assert saved["toolchain_id"] == "motor-ai"
    assert saved["display_name"] == "Motor AI"
    assert bus.ai_toolchains_changed.emissions == [((), {})]
    assert module.toolchain_list.currentItem().data(Qt.ItemDataRole.UserRole) == "motor-ai"
    assert module.empty_label.isVisible() is False
    assert module.detail.isVisible() is True


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


def test_delete_button_is_disabled_for_new_draft_without_selection(qapp):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    module.btn_new.click()
    qapp.processEvents()

    assert module.toolchain_list.currentItem() is None
    assert module.btn_delete.isEnabled() is False


@pytest.mark.parametrize(
    ("setup", "expected_message"),
    [
        ("duplicate", "工具链标识已存在"),
        ("missing_required", "工具链标识、显示名称和入口命令不能为空"),
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

    module.btn_new.click()
    module.ed_toolchain_id.setText("new-ai")
    module.ed_display_name.setText("New AI")
    module.ed_entrypoint.setPlainText("python new_ai.py")
    module.spin_timeout.setValue(30000)

    if setup == "duplicate":
        module.ed_toolchain_id.setText("existing-ai")
    elif setup == "missing_required":
        module.ed_toolchain_id.setText("")
    elif setup == "invalid_timeout":
        module.spin_timeout.setValue(0)

    module.btn_save.click()

    assert warnings == [("保存失败", expected_message)]
    assert service.saved_requests == []
