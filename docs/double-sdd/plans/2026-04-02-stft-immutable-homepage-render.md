# STFT Immutable Homepage Render Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop redundant STFT heatmap redraws when the homepage revisits or state transitions resolve to the same unchanged run/channel image.

**Architecture:** Add a monotonic render revision to each `_StftCacheEntry` and let `StftModule` remember the last rendered `(config_id, run_id, channel_index, revision)` signature. Keep the current refresh boundaries and overlay updates, but make repeated refreshes idempotent unless the key changes, new STFT data arrives, or the heatmap image was explicitly cleared.

**Tech Stack:** Python 3.12, PyQt6, NumPy, pytest

---

**Execution Context:** Commands below assume the worker is already running from the dedicated worktree root created by the `subagent-driven-development` setup step.

## File Structure

- Modify: `ui/modules/stft.py`
- Modify: `tests/test_stft_module/test_homepage_performance.py`
- Modify: `tests/test_main_window.py`

### Task 1: Add Cache Render Revision Semantics

**Files:**
- Modify: `ui/modules/stft.py`
- Modify: `tests/test_stft_module/test_homepage_performance.py`

- [ ] **Step 1: Write failing cache-revision tests**

Add two tests near the existing `_StftCacheEntry` coverage in `tests/test_stft_module/test_homepage_performance.py`:

```python
def test_stft_cache_entry_increments_render_revision_only_for_valid_frames():
    cache_entry = stft_module._StftCacheEntry()

    assert cache_entry.render_revision == 0
    assert cache_entry.append_frame(_make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)))
    assert cache_entry.render_revision == 1
    assert cache_entry.append_frame(_make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)))
    assert cache_entry.render_revision == 2


def test_stft_cache_entry_rejects_mismatched_frequency_frame_without_revision_advance():
    cache_entry = stft_module._StftCacheEntry()
    assert cache_entry.append_frame(_make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)))

    assert cache_entry.render_revision == 1
    assert cache_entry.append_frame(
        _make_stft_frame(
            time=0.5,
            magnitudes=(3.0, 4.0),
            frequencies=(125.0, 250.0),
        )
    ) is False
    assert cache_entry.render_revision == 1
```

- [ ] **Step 2: Run the new cache-revision tests to confirm they fail**

Run:

```powershell
pytest tests/test_stft_module/test_homepage_performance.py -k "render_revision" -q
```

Expected: FAIL with missing `render_revision` support or equivalent assertion failure.

- [ ] **Step 3: Implement the minimal cache revision support**

Update `_StftCacheEntry` in `ui/modules/stft.py`:

```python
@dataclass
class _StftCacheEntry:
    render_revision: int = 0

    def append_frame(self, frame: StftFrame) -> bool:
        ...
        self._append_magnitude_column(magnitudes)
        self.times_sec.append(float(getattr(frame, "time", 0.0)))
        self.render_revision += 1
        return True
```

Implementation notes:

- initialize `render_revision` at `0`
- increment it only after a valid frame has been appended successfully
- do not advance it on rejected frames or no-op validation failures
- keep the existing matrix-storage behavior unchanged

- [ ] **Step 4: Re-run the cache-revision tests**

Run:

```powershell
pytest tests/test_stft_module/test_homepage_performance.py -k "render_revision" -q
```

Expected: PASS

- [ ] **Step 5: Commit Task 1**

```bash
git add ui/modules/stft.py tests/test_stft_module/test_homepage_performance.py
git commit -m "feat(stft): track cached render revisions"
```

### Task 2: Make STFT Refresh Idempotent For Unchanged Signatures

**Files:**
- Modify: `ui/modules/stft.py`
- Modify: `tests/test_stft_module/test_homepage_performance.py`

- [ ] **Step 1: Write failing redraw-gating tests**

Add tests in `tests/test_stft_module/test_homepage_performance.py` covering unchanged-signature refreshes:

```python
def test_refresh_selected_display_channel_skips_redraw_when_same_signature_is_visible(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="running",
            trigger_source="manual",
        )
    )
    for frame in (
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
        _make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)),
    ):
        services.bus.stft_updated.emit(101, "run-101", 0, frame)
    qapp.processEvents()

    redraw_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.canvas.heatmap,
        "set_image",
        lambda **kwargs: redraw_calls.append(kwargs),
    )

    module.refresh_selected_display_channel()

    assert redraw_calls == []


def test_review_metadata_churn_skips_redraw_for_same_run_without_new_frames(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            judgment_payload={"overall": "合格"},
        ),
    )
    qapp.processEvents()

    redraw_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.canvas.heatmap,
        "set_image",
        lambda **kwargs: redraw_calls.append(kwargs),
    )

    services.bus.factory_review_changed.emit(
        _make_review_payload(
            run_id="run-101",
            judgment_payload=None,
        )
    )
    qapp.processEvents()

    assert redraw_calls == []
    assert module.selection_box.isVisible() is True


def test_review_and_reanalysis_state_churn_skip_redraw_for_unchanged_signature(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    payload = _make_review_payload(
        run_id="run-101",
        judgment_payload={"overall": "合格"},
    )
    _emit_reviewing(module, services, payload)
    qapp.processEvents()

    redraw_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.canvas.heatmap,
        "set_image",
        lambda **kwargs: redraw_calls.append(kwargs),
    )

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reanalyzing",
            trigger_source="manual",
        )
    )
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="manual",
        )
    )
    qapp.processEvents()

    assert redraw_calls == []
```

Implementation notes:

- seed the module once so the initial render already happened before spying on `set_image`
- exercise:
  - `refresh_selected_display_channel()` with the same visible key/revision
  - a same-run `factory_review_changed` update with unchanged STFT data
  - a `reviewing -> reanalyzing -> reviewing` transition for the same run without new STFT frames
- keep the existing `test_refresh_selected_display_channel_restores_cached_image_after_heatmap_clear(...)` green as the restore-after-clear case

- [ ] **Step 2: Run the redraw-gating tests to confirm they fail**

Run:

```powershell
pytest tests/test_stft_module/test_homepage_performance.py -k "skips_redraw or state_churn or metadata_churn" -q
```

Expected: FAIL because `StftModule` still calls `set_image(...)` for unchanged cached content.

- [ ] **Step 3: Implement last-rendered signature gating in `StftModule`**

Update `ui/modules/stft.py` around the render path (`_refresh_cached_rendering`, `_resolve_render_target`, `_render_payload`, `_clear_rendering`):

```python
self._last_render_signature: tuple[tuple[int, str, int], int] | None = None

def _resolve_render_target(self) -> tuple[tuple[int, str, int], _StftCacheEntry, dict] | None:
    ...

def _render_payload(...):
    render_signature = (cache_key, cache_entry.render_revision)
    if (
        render_signature == self._last_render_signature
        and self.canvas.heatmap.has_image()
    ):
        self._render_cache_key = cache_key
        return
    ...
    self._last_render_signature = render_signature
```

Implementation notes:

- keep `MainWindow._on_main_tab_changed(...)` unchanged; the idempotence belongs in `StftModule`
- short-circuit redraw only when all three conditions hold:
  - same cache key
  - same cache revision
  - heatmap still has a current image
- invalidate the last-rendered signature on `_clear_rendering()` and `_on_factory_display_reset(...)`
- when the widget was cleared manually, allow cached data to restore exactly once on refresh
- preserve the current live-refresh scheduling semantics when new frames actually advance the revision

- [ ] **Step 4: Run the full STFT homepage-performance file**

Run:

```powershell
pytest tests/test_stft_module/test_homepage_performance.py -q
```

Expected: PASS

- [ ] **Step 5: Commit Task 2**

```bash
git add ui/modules/stft.py tests/test_stft_module/test_homepage_performance.py
git commit -m "feat(stft): skip redraws for unchanged cached images"
```

### Task 3: Lock The Main-Window Homepage Reentry Regression

**Files:**
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Write a failing main-window reentry no-redraw test**

Add a regression near the existing page-reentry coverage in `tests/test_main_window.py`:

```python
def test_main_window_page_reentry_does_not_redraw_visible_unchanged_stft(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(
            config_id=101,
            run_id="run-101",
            trigger_source="manual",
        )
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        1,
        _make_stft_frame(time=0.0, magnitudes=(9.0, 10.0)),
    )
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    redraw_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        window.stft.canvas.heatmap,
        "set_image",
        lambda **kwargs: redraw_calls.append(kwargs),
    )

    window.tab.setCurrentIndex(1)
    qapp.processEvents()
    window.tab.setCurrentIndex(0)
    qapp.processEvents()

    assert redraw_calls == []
    assert window.stft.current_render_cache_key() == (101, "run-101", 1)
```

Implementation notes:

- do not clear `window.stft.canvas.heatmap` in this new test; that case is already covered by `test_main_window_page_reentry_refreshes_stft_from_existing_cache(...)`
- keep the existing restore-after-clear test unchanged so both behaviors are covered:
  - visible image stays put without redraw
  - cleared image restores from cache on reentry

- [ ] **Step 2: Run the page-reentry tests to confirm the new one fails first**

Run:

```powershell
pytest tests/test_main_window.py -k "page_reentry_refreshes_stft_from_existing_cache or page_reentry_does_not_redraw_visible_unchanged_stft" -q
```

Expected: FAIL because reentry currently redraws the already visible STFT image.

- [ ] **Step 3: Re-run the same page-reentry tests after Task 2 and confirm they pass**

Run:

```powershell
pytest tests/test_main_window.py -k "page_reentry_refreshes_stft_from_existing_cache or page_reentry_does_not_redraw_visible_unchanged_stft" -q
```

Expected: PASS

- [ ] **Step 4: Run the focused regression suite**

Run:

```powershell
pytest tests/test_stft_module tests/test_main_window.py -q
```

Expected: PASS

- [ ] **Step 5: Run the full suite**

Run:

```powershell
pytest tests -q
```

Expected: PASS

- [ ] **Step 6: Commit Task 3**

```bash
git add tests/test_main_window.py
git commit -m "test(main-window): lock stft reentry redraw behavior"
```
