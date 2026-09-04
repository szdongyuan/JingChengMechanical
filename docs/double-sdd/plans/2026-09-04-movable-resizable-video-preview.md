# Movable and Resizable Video Preview Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a page-owned video preview overlay that can be moved, resized from its top-right corner, reset, and restored after restart without changing the camera lifecycle or rearranging the rest of the test page.

**Architecture:** Introduce a video-specific `VideoPreviewOverlay` widget that owns header controls, pointer interaction, geometry normalization, and `QSettings` persistence. Keep `VideoModule` unchanged and replace its layout-owned shared `Card` on the test page with a fixed-height placeholder plus a direct-child overlay positioned over that placeholder.

**Tech Stack:** Python 3.12, PyQt6 widgets/events/QSettings, pytest, Qt offscreen tests

---

## File Structure

- Create `ui/widgets/video_preview_overlay.py`: video-only floating card, persistence, geometry model, move/resize/reset interactions, and parent/anchor event synchronization.
- Create `tests/test_video_preview_overlay.py`: focused widget tests for geometry, persistence, failures, z-order, and pointer interactions.
- Modify `app/main_window.py`: replace the layout-owned video `Card` with a 270 px placeholder and page-owned `VideoPreviewOverlay` while preserving `self.video` shutdown registration.
- Create `tests/test_video_preview_integration.py`: focused test-page composition and camera-lifecycle integration coverage.
- Do not modify `ui/modules/video.py`, `ui/widgets/cards.py`, `main.spec`, or unrelated existing files.

## Preflight Baseline

- [ ] **Record the baseline before implementation**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_module.py tests\test_main_window.py -q
```

At the recorded main-worktree base this produces `28 passed, 1 failed`. The existing unrelated failure is `tests/test_main_window.py::test_main_window_registers_process_plc_shutdown_after_module_construction`, which expects a PLC shutdown registration no longer present in `build_hardware_page`. Do not change PLC code or that test as part of this feature. All new feature tests and `tests/test_video_module.py` must pass; the final regression run must not introduce any additional failures.

### Task 1: Geometry Model and Persistence

**Files:**
- Create: `ui/widgets/video_preview_overlay.py`
- Create: `tests/test_video_preview_overlay.py`

- [ ] **Step 1: Write the failing construction and default-geometry tests**

Create a session-scoped offscreen `QApplication` fixture and a helper that gives the overlay a real parent and anchor:

```python
def _build_overlay(qapp, settings):
    page = QWidget()
    page.resize(1000, 700)
    anchor = QWidget(page)
    anchor.setGeometry(0, 430, 420, 270)
    body = QWidget()
    overlay = VideoPreviewOverlay(page, body, anchor, settings=settings)
    page.show()
    qapp.processEvents()
    overlay.sync_geometry()
    return page, anchor, body, overlay


def test_default_geometry_follows_fixed_anchor(qapp, settings):
    page, anchor, body, overlay = _build_overlay(qapp, settings)
    assert overlay.geometry() == QRect(0, 430, 420, 270)
    assert body.parentWidget() is overlay
    assert overlay.is_customized is False
```

Also add a test that moves/resizes the anchor, calls `sync_geometry()`, and expects a non-customized overlay to follow it.

- [ ] **Step 2: Run the construction test and verify it fails**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_preview_overlay.py::test_default_geometry_follows_fixed_anchor -q
```

Expected: FAIL because `ui.widgets.video_preview_overlay` does not exist.

- [ ] **Step 3: Implement the card shell and two-geometry model**

Create `VideoPreviewOverlay(QFrame)` with these stable public test seams:

```python
class VideoPreviewOverlay(QFrame):
    SETTINGS_CUSTOMIZED = "ui/video_preview_overlay/customized"
    SETTINGS_GEOMETRY = "ui/video_preview_overlay/geometry"
    NORMAL_MINIMUM = QSize(320, 270)
    SUPPORTED_PAGE_MINIMUM = QSize(200, 80)

    def __init__(self, parent: QWidget, body: QWidget, anchor: QWidget,
                 *, settings=None): ...

    @property
    def is_customized(self) -> bool: ...

    @property
    def requested_geometry(self) -> QRect | None: ...

    def sync_geometry(self) -> None: ...

    def reset_geometry(self) -> None: ...
```

Use `QSettings("JingChen", "JingChenMechanical")` only when no settings object is injected. Set the frame object name to `Card`, create a zero-margin body layout beneath a header placeholder, and parent the supplied body through the layout.

Implement these geometry helpers as focused methods:

```python
def _default_geometry(self) -> QRect:
    top_left = self._anchor.mapTo(self.parentWidget(), QPoint(0, 0))
    return QRect(top_left, self._anchor.size())

def _effective_geometry(self, requested: QRect) -> QRect:
    # Empty parent: return an empty rect without raising.
    # Parent below 200x80: fill parent.rect().
    # Otherwise clamp width/height to both NORMAL_MINIMUM and available size,
    # then clamp x/y so the whole effective rect stays inside parent.rect().
```

Keep `_requested_geometry` separate from the displayed `geometry()`. Parent-induced clamping must never overwrite `_requested_geometry`. `sync_geometry()` selects the anchor rect in default mode or requested rect in customized mode, applies effective geometry, shows the overlay when appropriate, and finishes with `raise_()`.

- [ ] **Step 4: Write failing persistence, reset, and parent-resize tests**

Use a temporary INI store for success paths:

```python
@pytest.fixture
def settings(tmp_path):
    value = QSettings(str(tmp_path / "video-preview.ini"), QSettings.Format.IniFormat)
    value.clear()
    return value
```

Add exact behavioral tests for:

- `customized=true` plus a valid `QRect` restores custom geometry;
- partial state, malformed `customized` values, wrong-type geometry, empty geometry, and non-positive geometry fall back to default, set `is_customized` false, and best-effort remove both keys by clearing the complete `ui/video_preview_overlay` group;
- shrinking the page clamps only effective geometry and expanding restores the original requested geometry;
- a parent smaller than 200×80 makes the overlay fill the parent, then restores requested/default geometry after re-expansion;
- `reset_geometry()` immediately follows the anchor and persists `customized=false` before removing geometry;
- a settings read/sync exception and a read returning `QSettings.Status.AccessError` both fall back to a usable default overlay;
- a custom-geometry `setValue()` exception, custom-geometry `sync()` exception, and custom-geometry sync returning `AccessError` each leave `_commit_user_geometry()` usable for a subsequent current-session geometry change even though restart persistence is unavailable;
- a reset `customized=false` write failure leaves the current session in default mode without claiming restart persistence;
- a successful `customized=false` sync followed by a geometry `remove()` exception or cleanup `AccessError` still prevents stale geometry from restoring on restart;
- a fully successful reset removes the geometry key and restores default mode.

Expose a test-only helper on the widget for establishing a user choice without synthesizing mouse input yet:

```python
def _commit_user_geometry(self, rect: QRect) -> None:
    self._customized = True
    self._requested_geometry = QRect(rect)
    self.sync_geometry()
    self._save_custom_geometry()
```

- [ ] **Step 5: Run the persistence tests and verify they fail**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_preview_overlay.py -q
```

Expected: FAIL on the unimplemented persistence and resize cases.

- [ ] **Step 6: Implement robust settings and lifecycle synchronization**

Implement `_load_state()`, `_save_custom_geometry()`, and `_persist_default_state()` using the two full key paths. Every operation must call `sync()` and treat exceptions or a non-`NoError` status as failure. On invalid reads, set default mode and best-effort clear the complete `ui/video_preview_overlay` group (for example `beginGroup("ui/video_preview_overlay")`, `remove("")`, `endGroup()`, `sync()`), so both malformed `customized` and `geometry` values are removed. A user reset is intentionally different: first write and synchronize `customized=false`, then remove geometry and synchronize again, preserving the reset tombstone when geometry cleanup fails.

Use a programmable fake settings object for failure tests so read, `setValue`, first/second `sync`, group-clear, and `remove` failures are asserted independently. In Task 1, call `_commit_user_geometry()` again after each failed custom save and assert effective/requested geometry still changes; persistence errors must not disable the geometry state model. Pointer-level post-failure interaction is added in Task 2 after pointer handling exists.

Install event filters on the parent page and anchor. Coalesce `Show`, `Resize`, `Move`, and `LayoutRequest` reactions with `QTimer.singleShot(0, self.sync_geometry)`, and remove filters safely when watched objects are destroyed. `showEvent()` and each synchronization must call `raise_()`.

- [ ] **Step 7: Run Task 1 tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_preview_overlay.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the Task 1 checkpoint**

```powershell
git add -- ui/widgets/video_preview_overlay.py tests/test_video_preview_overlay.py
git commit -m "tmp/sdd: add video overlay geometry persistence"
```

### Task 2: Move, Top-Right Resize, Reset, and Z-Order Interactions

**Files:**
- Modify: `ui/widgets/video_preview_overlay.py`
- Modify: `tests/test_video_preview_overlay.py`

- [ ] **Step 1: Write failing movement and control-isolation tests**

Add Qt pointer tests using the exposed `overlay.title_bar`, `overlay.reset_button`, and `overlay.resize_handle` widgets:

```python
def test_dragging_title_moves_and_persists_overlay(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    start = overlay.pos()
    QTest.mousePress(overlay.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(80, 12))
    QTest.mouseMove(overlay.title_bar, QPoint(150, 52))
    QTest.mouseRelease(overlay.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(150, 52))
    qapp.processEvents()
    assert overlay.pos() != start
    assert overlay.is_customized is True
    assert settings.value(VideoPreviewOverlay.SETTINGS_CUSTOMIZED, type=bool) is True
```

Add cases that drag beyond all four page edges and assert the full effective geometry remains within `page.rect()`. Click/drag the reset button and resize handle and assert neither begins title movement. Verify clicking reset returns to the current anchor and clears customized state.

- [ ] **Step 2: Write failing top-right resize tests**

Simulate a drag on `resize_handle` and assert:

- the left edge and bottom edge remain fixed;
- dragging right/up increases width/height;
- normal pages enforce 320×270 minimum and parent maximum;
- if the overlay was temporarily clamped by a parent resize, a subsequent user interaction starts from effective geometry and replaces the old request geometry;
- mouse release persists the new request geometry.
- dragging beyond the parent's right edge caps `right()` at `page.rect().right()` without changing `left()` or `bottom()`;
- dragging beyond the parent's top edge caps `top()` at `page.rect().top()` without changing `left()` or `bottom()`.

Expected core assertion:

```python
before = QRect(overlay.geometry())
# drag handle by (+120, -80)
after = overlay.geometry()
assert after.left() == before.left()
assert after.bottom() == before.bottom()
assert after.width() == before.width() + 120
assert after.height() == before.height() + 80
```

- [ ] **Step 3: Write failing first-show and resynchronization z-order tests**

Add a sibling cover widget after overlay construction, trigger `show()`, `sync_geometry()`, a parent resize, a title-bar press, and a resize-handle press, then use `page.childAt()` at an overlay-only point. Assert the hit widget is the overlay or one of its descendants after each trigger. The press-time cases explicitly verify `raise_()` happens as movement or resizing begins. Add a hidden-page resize/show test and assert geometry is synchronized when the page becomes visible again.

Add a settings call-count test for both move and resize: clear the fake's call log after press, send multiple mouse-move events, and assert no persistence calls occurred; after one release, assert exactly one logical custom save (`customized`, `geometry`, and one successful `sync`) occurred. Also verify a save failure does not prevent a second pointer interaction from changing geometry.

- [ ] **Step 4: Run interaction tests and verify they fail**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_preview_overlay.py -q
```

Expected: FAIL because move/resize controls and event handlers are not implemented.

- [ ] **Step 5: Implement the header and pointer state machine**

Build a card-style header with `QLabel("视频预览")`, compact `QPushButton("重置")`, and an 18×18 top-right `_ResizeHandle`. Give the title label `WA_TransparentForMouseEvents`; keep the button and handle as independent children so they cannot initiate movement. Use `SizeAllCursor` for the title bar and `SizeBDiagCursor` for the top-right handle.

Implement separate move and resize state:

```python
def _begin_move(self, global_pos: QPoint) -> None: ...
def _update_move(self, global_pos: QPoint) -> None: ...
def _finish_move(self) -> None: ...
def _begin_resize(self, global_pos: QPoint) -> None: ...
def _update_resize(self, global_pos: QPoint) -> None: ...
def _finish_resize(self) -> None: ...
```

Both `_begin_move()` and `_begin_resize()` call `raise_()` before recording interaction state. Movement uses `effective topLeft + pointer delta` and `_effective_geometry()`. Top-right resizing starts from the current effective rect, holds `left()` and `bottom()` fixed, applies horizontal delta to width and inverse vertical delta to height, then uses a dedicated `_effective_top_right_resize()` helper. That helper clamps the moving top and right edges against the parent before deriving width and height; it must not reuse generic x/y clamping in a way that moves the fixed left or bottom edges. Mouse-move handlers update only in-memory/effective geometry and never call settings. On either mouse release, the final effective rect becomes the new requested rect, customized mode becomes true, and `_save_custom_geometry()` is called exactly once. If an interaction starts while parent-clamped, it intentionally replaces the older request.

Override the resize handle `paintEvent()` to draw three short diagonal grip strokes at the top-right; keep the visual implementation local to this file. Reposition and raise the handle from the overlay `resizeEvent()`.

- [ ] **Step 6: Run Task 2 tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_preview_overlay.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the Task 2 checkpoint**

```powershell
git add -- ui/widgets/video_preview_overlay.py tests/test_video_preview_overlay.py
git commit -m "tmp/sdd: add video overlay move resize controls"
```

### Task 3: Test-Page Integration and Regression Verification

**Files:**
- Modify: `app/main_window.py`
- Create: `tests/test_video_preview_integration.py`

- [ ] **Step 1: Write the failing test-page composition test**

Monkeypatch `WorkOrderModule`, `ControlModule`, `TodayStatisticsModule`, `VideoModule`, and `ResultDashboardModule` with minimal `QWidget` stubs. Provide service stubs for `register_independent_shutdown_resource`, `attach_committed_history_observer`, and `attach_result_dashboard`. Monkeypatch `main_window.VideoPreviewOverlay` with a factory that returns the real class using a temporary INI `QSettings`.

Call `MainWindow.build_test_page()` on a Qt-initialized `MainWindow` instance and assert:

```python
assert window.video_placeholder.height() == 270
assert window.video_placeholder.parentWidget() is not page
assert window.video_card.parentWidget() is page
assert window.video_card.body is window.video
assert registered_shutdown_callbacks == [window.video.stop_camera]
```

Show and resize the page, process events, and assert the overlay default rect maps exactly from the placeholder into page coordinates. Commit a larger custom overlay rect and assert the placeholder, left column, and right result widget geometries do not change, proving the overlay covers instead of rearranging layout.

- [ ] **Step 2: Run the integration test and verify it fails**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_preview_integration.py -q
```

Expected: FAIL because `MainWindow` still creates a normal `Card` and has no placeholder or `VideoPreviewOverlay` import.

- [ ] **Step 3: Integrate the overlay into `build_test_page()`**

Import `VideoPreviewOverlay`. Preserve creation of `self.video` and immediate registration of `self.video.stop_camera`. Replace:

```python
self.video_card = Card("视频预览", self.video)
self.video_card.setMinimumHeight(270)
left_lay.addWidget(self.video_card)
```

with a `self.video_placeholder = QWidget()` fixed to 270 px height and added to `left_lay`. After both the left and main widgets have been added to `content_lay`, create:

```python
self.video_card = VideoPreviewOverlay(page, self.video, self.video_placeholder)
QTimer.singleShot(0, self.video_card.sync_geometry)
```

Keep the public `self.video_card` attribute for compatibility. Do not add the overlay to any layout. Do not modify widths, stretch factors, or any non-video card.

- [ ] **Step 4: Run focused feature and existing video tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_preview_overlay.py tests\test_video_preview_integration.py tests\test_video_module.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the main-window regression comparison**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_main_window.py -q
```

Expected at the recorded base: the same single unrelated PLC assertion failure and no new failures. If the baseline failure has independently disappeared in the isolated worktree, expect the file to pass instead; do not edit PLC behavior for this feature.

- [ ] **Step 6: Run a broader non-destructive regression suite**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests\test_video_preview_overlay.py tests\test_video_preview_integration.py tests\test_video_module.py tests\test_main_window.py -q
```

Expected: all feature/video tests pass and no regression beyond the recorded unrelated PLC failure.

- [ ] **Step 7: Commit the Task 3 checkpoint**

```powershell
git add -- app/main_window.py tests/test_video_preview_integration.py
git commit -m "tmp/sdd: integrate floating video preview"
```

## Final Review Notes

- [ ] Review Task 1 and Task 2 changes against the spec even though they share the new widget and test files.
- [ ] Verify no implementation change appears in `ui/modules/video.py`, `ui/widgets/cards.py`, `main.spec`, or unrelated user-owned files.
- [ ] Confirm the final full-range quality review explicitly compares test output to the recorded baseline failure.
- [ ] Squash temporary `tmp/sdd:` commits onto the recorded main-worktree base during final integration as required by `subagent-driven-development`.
