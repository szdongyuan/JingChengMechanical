# AI Tool Management Generated ID And Draft Creation Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AI toolchain creation ask only for a display name, create a visible unsaved draft row immediately, and generate a stable random-suffixed internal `toolchain_id` instead of letting operators edit it manually.

**Architecture:** Add AI-service-owned generated-ID helpers and generated-draft create retry support first, then update the AI tool management UI to consume that service contract. The UI keeps `toolchain_id` visible as read-only diagnostic information, stores one active draft payload in memory, injects that draft into the left list, and preserves existing unsaved-change semantics.

**Tech Stack:** Python, PyQt6, peewee, pytest

---

## File Structure

- `app/service/ai_service.py`
  - Add deterministic display-name prefix normalization.
  - Add `generate_toolchain_id(display_name)` and `generate_toolchain_id_from_prefix(prefix)`.
  - Add generated-draft create retry behavior inside `save_toolchain()` for `original_toolchain_id is None` requests containing `generated_id_prefix`.

- `ui/modules/ai_tool_management.py`
  - Replace editable `工具链标识` editor with read-only `内部标识`.
  - Change `新建` to prompt for display name first.
  - Add one active in-memory draft row that appears at the top of the left list and is selected.
  - Keep delete disabled for unsaved drafts.
  - Regenerate a draft ID before save if the generated ID becomes occupied.

- `tests/test_ai_service.py`
  - Add service-level tests for generated-ID normalization, random suffix, collision retry, immutable-prefix regeneration, and create-time retry.

- `tests/test_ai_tool_management_module.py`
  - Update existing UI tests that used editable `ed_toolchain_id`.
  - Add new draft-flow tests for prompt/cancel/confirm/list/save/unsaved-change behavior.

---

### Task 1: AI Service Generated Toolchain IDs

**Files:**
- Modify: `app/service/ai_service.py`
- Modify: `tests/test_ai_service.py`

- [ ] **Step 1: Write failing tests for generated ID format and normalization**

Add tests to `tests/test_ai_service.py` near the existing `AiService` toolchain save tests:

```python
def test_generate_toolchain_id_uses_ascii_prefix_and_random_suffix(ai_service, monkeypatch):
    monkeypatch.setattr("app.service.ai_service.secrets.token_hex", lambda n: "a1b2c3d4")

    generated = ai_service.generate_toolchain_id("  Motor AI ++  ")

    assert generated == "motor_ai_a1b2c3d4"
    assert len(generated) <= 33
    assert generated.isascii()


def test_generate_toolchain_id_falls_back_for_non_ascii_display_name(ai_service, monkeypatch):
    monkeypatch.setattr("app.service.ai_service.secrets.token_hex", lambda n: "11112222")

    generated = ai_service.generate_toolchain_id("电机异音")

    assert generated == "toolchain_11112222"
```

- [ ] **Step 2: Run service tests and verify the new tests fail**

Run: `pytest tests/test_ai_service.py -q`

Expected: FAIL because `AiService.generate_toolchain_id()` does not exist yet.

- [ ] **Step 3: Implement prefix normalization and ID generation**

In `app/service/ai_service.py`:

- Add imports:

```python
import re
import secrets
import unicodedata
```

- Add constants near the existing AI constants:

```python
_GENERATED_TOOLCHAIN_PREFIX_FALLBACK = "toolchain"
_GENERATED_TOOLCHAIN_PREFIX_MAX_LEN = 24
_GENERATED_TOOLCHAIN_RANDOM_HEX_LEN = 8
```

- Add helpers on `AiService`:

```python
    @classmethod
    def generated_toolchain_prefix(cls, display_name: str) -> str:
        value = display_name.strip().lower()
        value = unicodedata.normalize("NFKD", value)
        value = value.encode("ascii", "ignore").decode("ascii")
        value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
        if not value:
            value = _GENERATED_TOOLCHAIN_PREFIX_FALLBACK
        return value[:_GENERATED_TOOLCHAIN_PREFIX_MAX_LEN].strip("_") or _GENERATED_TOOLCHAIN_PREFIX_FALLBACK

    def generate_toolchain_id_from_prefix(self, prefix: str) -> str:
        normalized_prefix = self.generated_toolchain_prefix(prefix)
        while True:
            suffix = secrets.token_hex(_GENERATED_TOOLCHAIN_RANDOM_HEX_LEN // 2)
            toolchain_id = f"{normalized_prefix}_{suffix}"
            if self.get_toolchain(toolchain_id) is None:
                return toolchain_id

    def generate_toolchain_id(self, display_name: str) -> str:
        return self.generate_toolchain_id_from_prefix(
            self.generated_toolchain_prefix(display_name)
        )
```

Note: `token_hex(4)` returns 8 hex characters.

- [ ] **Step 4: Write failing tests for persisted collision regeneration**

Add tests:

```python
def test_generate_toolchain_id_regenerates_when_persisted_id_collides(ai_service, monkeypatch, tmp_path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    AiToolchainTable.create(
        toolchain_id="motor_ai_aaaabbbb",
        display_name="Existing",
        version="1.0",
        entrypoint="python main.py",
        package_dir=str(package_dir),
        protocol_version=1,
        execution_mode="resident_process",
        default_timeout_ms=30000,
        enabled=True,
        description="",
    )
    suffixes = iter(["aaaabbbb", "ccccdddd"])
    monkeypatch.setattr("app.service.ai_service.secrets.token_hex", lambda n: next(suffixes))

    assert ai_service.generate_toolchain_id("Motor AI") == "motor_ai_ccccdddd"
```

- [ ] **Step 5: Run service tests and verify collision tests pass after implementation**

Run: `pytest tests/test_ai_service.py -q`

Expected: PASS for the new ID generation tests and no regressions.

- [ ] **Step 6: Write failing tests for generated-draft create retry**

Add a test that simulates a create-time collision after normalization but before insert:

```python
def test_save_generated_draft_retries_once_when_create_id_collides(ai_service, monkeypatch, tmp_path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    original_create = AiToolchainTable.create
    calls = {"count": 0}
    from peewee import IntegrityError

    def create_with_one_collision(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise IntegrityError("UNIQUE constraint failed: ai_toolchain_table.toolchain_id")
        return original_create(**kwargs)

    suffixes = iter(["11112222"])
    monkeypatch.setattr("app.service.ai_service.secrets.token_hex", lambda n: next(suffixes))
    monkeypatch.setattr(AiToolchainTable, "create", create_with_one_collision)

    saved_id = ai_service.save_toolchain(
        None,
        _toolchain_payload(
            toolchain_id="motor_ai_aaaabbbb",
            display_name="Motor AI",
            package_dir=str(package_dir),
            generated_id_prefix="motor_ai",
        ),
    )

    assert saved_id == "motor_ai_11112222"
    assert calls["count"] == 2
```

Also add a test for immutable prefix regeneration:

```python
def test_generate_toolchain_id_from_prefix_reuses_original_prefix(ai_service, monkeypatch):
    monkeypatch.setattr("app.service.ai_service.secrets.token_hex", lambda n: "9999aaaa")

    assert ai_service.generate_toolchain_id_from_prefix("motor_ai") == "motor_ai_9999aaaa"
```

Add a non-retry guard test to ensure only generated-draft `toolchain_id` collisions retry:

```python
def test_save_explicit_create_id_collision_without_generated_prefix_does_not_retry(ai_service, monkeypatch, tmp_path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    from peewee import IntegrityError
    calls = {"count": 0}

    def always_raise_id_integrity(**_kwargs):
        calls["count"] += 1
        raise IntegrityError("UNIQUE constraint failed: ai_toolchain_table.toolchain_id")

    monkeypatch.setattr(AiToolchainTable, "create", always_raise_id_integrity)

    with pytest.raises(IntegrityError, match="toolchain_id"):
        ai_service.save_toolchain(
            None,
            _toolchain_payload(
                toolchain_id="motor_ai_aaaabbbb",
                display_name="Motor AI",
                package_dir=str(package_dir),
            ),
        )

    assert calls["count"] == 1


def test_save_generated_draft_does_not_retry_non_id_integrity_errors(ai_service, monkeypatch, tmp_path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    from peewee import IntegrityError

    calls = {"count": 0}

    def always_raise_non_id_integrity(**_kwargs):
        calls["count"] += 1
        raise IntegrityError("NOT NULL constraint failed: ai_toolchain_table.display_name")

    monkeypatch.setattr(AiToolchainTable, "create", always_raise_non_id_integrity)

    with pytest.raises(IntegrityError, match="display_name"):
        ai_service.save_toolchain(
            None,
            _toolchain_payload(
                toolchain_id="motor_ai_aaaabbbb",
                display_name="Motor AI",
                package_dir=str(package_dir),
                generated_id_prefix="motor_ai",
            ),
        )

    assert calls["count"] == 1
```

- [ ] **Step 7: Implement generated-draft create retry**

In `_normalize_toolchain_payload()`:

- Accept optional `generated_id_prefix` only as normalized metadata in the returned dict when present and non-empty.
- Normalize it with `generated_toolchain_prefix()`.
- Do not persist `generated_id_prefix` to `AiToolchainTable`.

In `save_toolchain()` create branch:

```python
if original_toolchain_id is None:
    try:
        row = AiToolchainTable.create(...)
    except Exception as exc:
        if generated_id_prefix is not available or this is not a toolchain_id uniqueness collision:
            raise
        normalized["toolchain_id"] = self.generate_toolchain_id_from_prefix(generated_id_prefix)
        row = AiToolchainTable.create(...)
    return row.toolchain_id
```

Use peewee `IntegrityError` for uniqueness conflicts, but discriminate the error message or database exception details so retry is limited to `toolchain_id` primary-key/unique collisions. Retry at most once. Preserve existing behavior for imports/tests that create explicit IDs without `generated_id_prefix`, and do not retry unrelated integrity failures such as missing required fields.

- [ ] **Step 8: Run service tests**

Run: `pytest tests/test_ai_service.py -q`

Expected: PASS

- [ ] **Step 9: Commit Task 1**

```bash
git add app/service/ai_service.py tests/test_ai_service.py
git commit -m "feat: generate AI toolchain internal ids"
```

---

### Task 2: AI Tool Management Draft Creation UI

**Files:**
- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_tool_management_module.py`

- [ ] **Step 1: Update test stub for service-generated IDs**

In `tests/test_ai_tool_management_module.py`, extend `_AiServiceStub` with:

```python
def generated_toolchain_prefix(self, display_name: str) -> str:
    ...

def generate_toolchain_id(self, display_name: str) -> str:
    ...

def generate_toolchain_id_from_prefix(self, prefix: str) -> str:
    ...

def get_toolchain(self, toolchain_id: str):
    ...
```

The stub may return deterministic IDs such as `motor_ai_a1b2c3d4` for predictable UI assertions.

- [ ] **Step 2: Write failing tests for create prompt and cancel behavior**

Replace or update `test_new_toolchain_defaults_execution_mode_to_resident_process` and related create tests:

```python
def test_new_toolchain_cancel_name_dialog_leaves_empty_state(qapp, monkeypatch):
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QInputDialog.getText",
        lambda *args, **kwargs: ("", False),
    )
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    module.btn_new.click()

    assert module.toolchain_list.count() == 0
    assert module.empty_label.isVisible() is True
    assert module.detail.isVisible() is False


def test_new_cancel_name_dialog_after_discarding_dirty_row_creates_no_draft(qapp, monkeypatch):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)
    module.ed_display_name.setText("Dirty Name")
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "discard")
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QInputDialog.getText",
        lambda *args, **kwargs: ("", False),
    )

    module.btn_new.click()

    assert module.toolchain_list.count() == 1
    assert _current_toolchain_item_id(module) == "motor-ai"
    assert "[未保存]" not in module.toolchain_list.item(0).text()
    assert module.ed_display_name.text() == "Toolchain motor-ai"


def test_new_cancel_name_dialog_after_saving_dirty_row_creates_no_draft(qapp, monkeypatch):
    module, service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)
    module.ed_display_name.setText("Saved Name")
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "save")
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QInputDialog.getText",
        lambda *args, **kwargs: ("", False),
    )

    module.btn_new.click()

    assert service.saved_requests[-1][1]["display_name"] == "Saved Name"
    assert module.toolchain_list.count() == 1
    assert _current_toolchain_item_id(module) == "motor-ai"
    assert "[未保存]" not in module.toolchain_list.item(0).text()


def test_new_toolchain_blank_display_name_warns_and_creates_no_draft(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QInputDialog.getText",
        lambda *args, **kwargs: ("   ", True),
    )
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
    monkeypatch.setattr(
        "ui.modules.ai_tool_management.QInputDialog.getText",
        lambda *args, **kwargs: ("Motor AI", True),
    )
    module, _service, _bus = _make_module(toolchains=[])
    _show(module, qapp)

    module.btn_new.click()
    qapp.processEvents()

    assert module.toolchain_list.count() == 1
    assert _current_toolchain_item_id(module) == "motor_ai_a1b2c3d4"
    assert "[未保存]" in module.toolchain_list.item(0).text()
    assert module.ed_display_name.text() == "Motor AI"
    assert module.lb_toolchain_id_value.text() == "motor_ai_a1b2c3d4"
    assert module.combo_execution_mode.currentData() == "resident_process"
```

- [ ] **Step 3: Run UI tests and verify they fail**

Run: `pytest tests/test_ai_tool_management_module.py -q`

Expected: FAIL because new flow and read-only internal ID UI do not exist yet.

- [ ] **Step 4: Replace editable toolchain ID field with read-only internal ID display**

In `ui/modules/ai_tool_management.py`:

- Add `QInputDialog` import.
- Remove editable `self.ed_toolchain_id = QLineEdit()` row.
- Replace with:

```python
self.lb_toolchain_id_value = QLabel("-")
self.lb_toolchain_id_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
form_lay.addRow("内部标识", self.lb_toolchain_id_value)
```

- Update signal connections to remove `self.ed_toolchain_id.textChanged`.
- Update `_set_editor_payload()` to set `self.lb_toolchain_id_value`.
- Update `_collect_payload()` to read `toolchain_id` from current state/draft payload rather than from a line edit.

- [ ] **Step 5: Implement draft payload/list behavior**

Add state:

```python
self._draft_payload: dict | None = None
```

Draft rules:

- `_refresh_list()` clears and rebuilds persisted rows, but inserts `_draft_payload` first when present.
- Draft item data uses generated `toolchain_id`.
- Draft label uses `_format_list_text(row)` plus ` [未保存]`.
- `_toolchain_rows` should include the draft row or `_on_item_changed()` should explicitly detect `_draft_payload`.
- Selecting the draft loads `_draft_payload`.
- `_is_new_draft` remains `True` while editing the unsaved draft.
- `_update_action_state()` disables delete when `_is_new_draft` is true.

New helper shape:

```python
def _build_draft_payload(self, display_name: str) -> dict:
    prefix = self.services.ai_service.generated_toolchain_prefix(display_name)
    toolchain_id = self.services.ai_service.generate_toolchain_id_from_prefix(prefix)
    payload = self._empty_draft_payload()
    payload.update(
        {
            "toolchain_id": toolchain_id,
            "generated_id_prefix": prefix,
            "display_name": display_name,
        }
    )
    return payload
```

- [ ] **Step 6: Implement name prompt in `_on_new()`**

Flow:

```python
def _on_new(self):
    if not self._resolve_pending_editor_action("new"):
        return
    display_name, ok = QInputDialog.getText(self, "新建 AI 工具链", "请输入显示名称：")
    if not ok:
        self._update_action_state()
        return
    display_name = display_name.strip()
    if not display_name:
        QMessageBox.warning(self, "创建失败", "显示名称不能为空")
        return
    self._draft_payload = self._build_draft_payload(display_name)
    self._current_toolchain_id = self._draft_payload["toolchain_id"]
    self._is_new_draft = True
    self._editor_snapshot = self._normalize_editor_payload(self._draft_payload)
    self._refresh_list(selected_toolchain_id=self._current_toolchain_id)
    self._load_draft()
```

- [ ] **Step 7: Update dirty/draft lifecycle behavior**

Required behavior:

- Active draft is dirty even if unchanged after creation.
- Editing draft display name or version updates `_draft_payload` and left-list label live.
- If the draft display name is empty, the left-list label uses `未命名工具链` while still including the generated internal ID.
- `放弃更改` on switch/new/leave removes `_draft_payload`, clears draft selection, and allows the pending action.
- `取消` keeps the draft.
- `保存并继续` saves draft then continues existing action.

Implement focused helpers as needed, for example:

```python
def _remove_draft(self) -> None:
    self._draft_payload = None
    self._is_new_draft = False
    self._current_toolchain_id = None
```

Avoid changing hardware modules or unrelated config UI.

- [ ] **Step 8: Update save behavior for generated draft IDs**

Before saving a new draft:

- If `_is_new_draft` and `self.services.ai_service.get_toolchain(toolchain_id)` returns an existing row, regenerate with `_draft_payload["generated_id_prefix"]`.
- Update the current payload, `_draft_payload`, internal ID label, and selected list item to the regenerated ID.
- Include `generated_id_prefix` in the save payload for new drafts only.
- After save, set `_draft_payload = None`, `_is_new_draft = False`, refresh persisted list, select saved ID, and load persisted row.

- [ ] **Step 9: Update existing tests that set editable toolchain IDs**

Existing UI tests that create new rows should no longer do:

```python
module.ed_toolchain_id.setText("motor-ai")
```

Instead, use the new-name prompt setup or directly seed `_draft_payload` through `btn_new` and then fill the remaining required fields.

Existing saved-row tests should assert:

```python
assert module.lb_toolchain_id_value.text() == "motor-ai"
```

instead of reading `module.ed_toolchain_id`.

- [ ] **Step 10: Add draft lifecycle tests**

Add tests covering:

```python
def test_draft_display_name_edit_updates_left_list_label(...): ...
def test_empty_draft_display_name_uses_placeholder_in_left_list(...): ...
def test_discarding_unsaved_draft_removes_draft_row(...): ...
def test_canceling_unsaved_draft_switch_keeps_draft_row(...): ...
def test_saving_draft_persists_generated_id(...): ...
def test_saving_draft_regenerates_when_generated_id_is_already_taken(...): ...
```

Expected behaviors must match the spec:

- Delete disabled for draft.
- Draft row appears at index `0`.
- Duplicate display names are allowed because `toolchain_id` is unique.

- [ ] **Step 11: Run focused UI tests**

Run: `pytest tests/test_ai_tool_management_module.py -q`

Expected: PASS

- [ ] **Step 12: Run combined AI tests**

Run: `pytest tests/test_ai_service.py tests/test_ai_tool_management_module.py -q`

Expected: PASS

- [ ] **Step 13: Commit Task 2**

```bash
git add ui/modules/ai_tool_management.py tests/test_ai_tool_management_module.py
git commit -m "feat: create AI toolchains as generated-id drafts"
```

---

## Final Verification

After both tasks are complete and reviewed, run from the main worktree after merge:

```powershell
pytest tests/test_ai_service.py tests/test_ai_tool_management_module.py -q
```

Expected: all tests pass.
