from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

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
    module.txt_description.setPlainText("saved description   ")
    qapp.processEvents()

    assert module._is_dirty() is True

    module.btn_save.click()
    qapp.processEvents()

    assert service.saved_requests[-1][1]["display_name"] == "Motor AI"
    assert service.saved_requests[-1][1]["entrypoint"] == "python motor_ai.py"
    assert service.saved_requests[-1][1]["description"] == "saved description"
    assert module.ed_display_name.text() == "Motor AI"
    assert module.ed_entrypoint.toPlainText() == "python motor_ai.py"
    assert module.txt_description.toPlainText() == "saved description"
    assert module._editor_snapshot["display_name"] == "Motor AI"
    assert module._editor_snapshot["entrypoint"] == "python motor_ai.py"
    assert module._editor_snapshot["description"] == "saved description"

    module.ed_version.setText("2.0")
    qapp.processEvents()
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "discard")

    assert module.confirm_leave() is True
    assert module.ed_display_name.text() == "Motor AI"
    assert module.ed_entrypoint.toPlainText() == "python motor_ai.py"
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
                    "score_threshold": 0.8,
                },
                {
                    "config_id": 2,
                    "config_name": "Cfg Two",
                    "toolchain_id": "motor-ai",
                    "enabled": False,
                    "timeout_ms": 4000,
                    "params_json": '{"mode":"b"}',
                    "score_threshold": None,
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
            "score_threshold": 0.8,
        },
        {
            "config_id": 2,
            "config_name": "Cfg Two",
            "toolchain_id": None,
            "enabled": False,
            "timeout_ms": 4000,
            "params_json": '{"mode":"b"}',
            "score_threshold": None,
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
