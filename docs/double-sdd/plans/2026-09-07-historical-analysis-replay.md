# Historical Analysis Replay Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an independent saved-analysis viewer with audition and explicit manual relabeling to each Data Management record.

**Architecture:** A dedicated read-only persistence reader loads run metadata, frozen configuration, latest successful saved result and optional audio independently. A historical dialog composes existing result/chart widgets with a private display context and a reusable audio replay panel; Data Management owns one dialog per run and refreshes after committed labels. Playback gains a backward-compatible ownership guard so closing a historical window cannot stop another caller's newer playback.

**Tech Stack:** Python 3.12, PyQt6, existing analysis chart widgets, NumPy/SciPy, existing run-v2 SQLite repositories.

---

## Scope, repository state, and commands

Spec: `docs/double-sdd/specs/2026-09-07-historical-analysis-replay-design.md`.
Main code base at planning: `de793f5` on `master_0811`. Preserve unrelated dirty files in main (DB connection, managed input backend, main.spec and their tests). Implementation occurs sequentially in `.worktrees/historical-analysis-replay` on `codex/historical-analysis-replay`, based on the main HEAD recorded immediately before worktree creation, including these documents. Temporary checkpoints are not permanent project history.

Run every test from the feature worktree. System interpreter and Qt setup (do not install packages or use main's incomplete .venv):

```powershell
$env:PATH = 'D:\Program Files\Python\Python312\Lib\site-packages\PyQt6\Qt6\bin;D:\Program Files\Python\Python312\Lib\site-packages\PyQt6;' + $env:PATH
$env:PYTHONPATH = (Get-Location).Path
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTEST_QT_API = 'pyqt6'
$env:QT_API = 'pyqt6'
$env:QT_QPA_PLATFORM = 'offscreen'
& 'D:\Program Files\Python\Python312\python.exe' -c "import pytest; raise SystemExit(pytest.main(['-q','-p','pytestqt.plugin','tests/run_persistence/test_historical_analysis_reader.py']))"
```

Substitute the exact test path for each step below; run Qt test files in separate processes. Existing unrelated dashboard history viewport/Qt teardown failures were previously reproduced at baseline; investigate new failures, do not expand this feature to repair unrelated ones.

## File responsibilities

- New `app/service/run_persistence/historical_analysis_reader.py`: persistence-only historical snapshot and partial-availability reader; no Qt, analysis execution, or writes.
- `app/services_container.py`: construct reader alongside `run_export_reader` using same runtime/artifact store.
- `app/service/run_scoped_playback/service.py`: optional caller ownership for playback/conditional stop under existing operation gate; preserve unowned public calls and backend.
- `ui/popups/run_audio_replay_dialog.py`: retain old dialog API; factor reusable audition widget/data preparation as needed; scoped playback ownership and asynchronous preparation for the new use.
- New `ui/widgets/historical_analysis_view.py`: private read-only result/chart display using existing ResultModule and configured chart modules, frozen config and private bus. No live bus subscriptions or operational service delegation.
- New `ui/popups/historical_analysis_dialog.py`: metadata, current manual label, saved result view, audition tab/panel, loading/refresh lifecycle, explicit label save and committed signal.
- `ui/modules/data_management.py`: per-row new button; dialog lifecycle keyed by run_id; refresh after successful marking; preserve old column meanings and existing actions.
- Tests: `tests/run_persistence/test_historical_analysis_reader.py`, `tests/run_scoped_playback/test_service_lifecycle.py`, `tests/test_historical_analysis_dialog.py`, `tests/test_data_management_module.py`, and new focused replay widget tests if useful.

## Task 1: Durable historical snapshot reader

**Files:** Create reader + `tests/run_persistence/test_historical_analysis_reader.py`; modify `app/services_container.py`.

- [ ] Write failing tests with a temporary run-v2 DB and real result-artifact store using existing persistence fixtures. Cover: metadata/frozen config; latest successful attempt even after later failure; original saved judgment distinct from manually updated run judgment; missing audio; missing/corrupt result while metadata/audio remain available; missing run; empty Decision chart payload.
- [ ] Run the new test file and confirm meaningful missing-reader failure.
- [ ] Implement a small snapshot data object containing `run`, plain frozen `config`, `workorder`, `review` dict or None, successful attempt identity, original analysis judgment, optional replay audio facts (`run_id`, `source_wav_path`, sample/frame counts, `channel_order`), accepted fault types, and user-facing availability messages. Use explicit fields/dataclasses, not a broad dynamic proxy.
- [ ] Implement `HistoricalAnalysisReader(runtime, result_artifact_store=...).read(run_id)`: `RunRepository.get`; parse stored envelope; `AttemptRepository.latest_successful_for_run` to hydrate existing compressed/inline result; decode `{judgment, result}` preserving saved values; `AudioRepository.get_for_run` independently. Reuse existing snapshot/bridge parsers where they fit without importing operational composition. Missing/corrupt optional attachments produce per-section warnings. Invalid/missing primary run produces a clear reader error. Never use strict export `read_many` as the sole read path, because it requires audio.
- [ ] Wire `services.historical_analysis_reader` adjacent to persistence services. No startup or device changes.
- [ ] Run new reader suite, `tests/run_persistence/test_data_management.py` and focused container construction test where available. Assert marking through existing service leaves result payload and audio bytes unchanged.
- [ ] `git add app/service/run_persistence/historical_analysis_reader.py app/services_container.py tests/run_persistence/test_historical_analysis_reader.py`; `git commit -m "tmp/sdd: historical analysis reader"`.

## Task 2: Playback ownership

**Files:** Modify `app/service/run_scoped_playback/service.py`; extend `tests/run_scoped_playback/test_service_lifecycle.py`.

- [ ] Add failing lifecycle tests for A-owner playback, B takeover (same and different run), legacy/unowned takeover, conditional stop by wrong owner, failed request preserving old owner.
- [ ] Run lifecycle suite to verify RED.
- [ ] Add backward-compatible keyword-only owner identity to play and conditional stop under existing operation gate, as detailed in Task 4 below. Keep ordinary play/stop/shutdown behavior and all contracts/backend unchanged.
- [ ] Run lifecycle, resolution and public API suites to verify GREEN; commit `tmp/sdd: scoped playback ownership`.
- [ ] Pass paired spec/quality review before advancing.

## Task 3: Reusable asynchronous audition panel

**Files:** Modify `ui/popups/run_audio_replay_dialog.py`; create `tests/test_run_audio_replay_panel.py`; add a focused helper module if necessary to keep audio preparation separate from widgets.

- [ ] Add failing tests for reusable embedded panel, channel/range/gain requests, asynchronous audio and selection-STFT preparation, stale selection discard, close during loading/play startup, scoped stop after takeover. Include existing replay-dialog constructor/controls compatibility.
- [ ] Run new panel tests to verify RED.
- [ ] Extract reusable audition controls/panel from current dialog, keeping old dialog constructor and attributes available. Implement background sample/plot preparation and playback start, queued GUI updates, latest-request gating, safe worker retention and owner-conditional cleanup using Task 2 API. Detailed constraints appear in Task 4 below; that task consumes this completed panel rather than reimplementing it.
- [ ] Run `tests/test_run_audio_replay_panel.py` and existing replay cases in `tests/test_data_management_module.py`; verify GREEN; commit `tmp/sdd: reusable asynchronous audition panel`.
- [ ] Pass paired spec/quality review before advancing.

## Task 4: Isolated historical results dialog

**Files:** Create view/dialog and `tests/test_historical_analysis_dialog.py`. Consume reader, playback ownership and audition panel from Tasks 1–3; do not repeat their implementations. The playback and audition bullets below document cross-component integration constraints, already implemented and reviewed in Tasks 2–3.

- [ ] Write failing tests for opening snapshot A, observing saved metrics/AI and configured charts; snapshot B differs; relabel updates current judgment only; save failure; partial availability; refresh reload; delayed old read after refresh/close; no live result mutation or analysis calls. Test playable channel/range/gain mapping and cross-window playback ownership (including same run in old replay and new history window).
- [ ] Run targeted new tests to prove missing behavior.
- [ ] Add backward-compatible keyword-only optional owner identity to playback `play`, and `stop_if_owner(owner, timeout)` using the existing gate. Bind owner to the playback generation actually started; a legacy/unowned replacement invalidates previous owner. Failed requests that leave old generation playing must not transfer ownership. Conditional stop for a different owner is a no-op lifecycle result. Do not change backend, recording, existing request/result contracts, or global stop/shutdown semantics. Adapt reusable replay panel to use unique owner per panel and conditional stop; keep simple legacy test fakes usable without widening production behavior.
- [ ] Reuse audition controls from RunAudioReplayDialog via a widget, retaining original constructor/controls for existing callers. Allow preloaded audio and worker-prepared plot arrays in the new panel. Read/prepare audio off GUI thread; compute selection STFT off GUI thread with newest-request gating. Plot only on GUI thread. Label this as audition waveform/spectrum, separate from saved analysis charts. Disable play/stop while their request is outstanding; close must invalidate late results and stop owned playback when outstanding start finishes. Do not start a second recorder or analysis job.
- [ ] Implement private display context supplying a private QObject bus, frozen config lookup, and `FactoryControllerState`/`FactoryReviewPayload` with `manual_reanalysis_allowed=False`. Normalize JSON channel keys/windows as existing dashboard does. Reuse ResultModule, Stft/Spectrum/Octave/Spl/Loudness/Vibration modules and `result_dashboard_layout` normalization/resolved channel rules. Compose using existing Card/layout style. Avoid copying live-history state machine; if a small extraction of existing display utility is necessary, preserve behavior and add regression tests. No generic forwarding to operational real factory controller.
- [ ] Implement nonmodal HistoricalAnalysisDialog bound to immutable run_id. Metadata and current persisted human status appear above a saved-analysis section (explicitly titled original saved result); audition appears in a separate tab or panel. Add Refresh, allowed label combo, Save Label, Close. Label options match existing GOOD/BAD/frozen fault-type rules; call existing `update_fault_type`, then reload persisted metadata and emit `label_saved(run_id)` only after successful commit. Result and audio payloads are not rewritten by label saves.
- [ ] Use queued worker results with monotonically increasing request number; Qt widgets only on main thread. Keep workers alive until their threads finish even when dialog closes (e.g. app-parented worker registry with queued cleanup). Stale results are discarded. Button errors retain last committed state; missing run disables further label/play actions and displays error. Reading may proceed on open/explicit refresh; it must not start reanalysis.
- [ ] Run new dialog tests and existing `tests/run_scoped_playback/test_service_lifecycle.py`, `tests/run_scoped_playback/test_service_resolution.py`, `tests/run_scoped_playback/test_public_api.py`, existing replay cases in `tests/test_data_management_module.py`. Verify cross-owner close, delayed close, and legacy unowned replacement. Use actual existing chart modules in at least one smoke test, not mocks only.
- [ ] Commit owned task files with `tmp/sdd: historical results and audition dialog`.

## Task 5: Data Management entry, regression, and visual verification

**Files:** Modify `ui/modules/data_management.py`, `tests/test_data_management_module.py`; adjust new dialog tests only for integration evidence as needed.

- [ ] Write failing tests that each row has 「查看分析结果」 tied to exact UUID; duplicate clicks reuse dialog; different run opens separate dialog; close removes correct registry entry; successful label save refreshes list but failed save does not claim success. Preserve existing replay/delete positions logically (replace brittle index literals with named indexes where necessary).
- [ ] Run new tests and verify RED.
- [ ] Add button column and `_analysis_dialogs` registry, construct from current services, use WA_DeleteOnClose and identity-aware cleanup, show/raise/activate existing dialog. Connect saved signal to list refresh, leaving filter/page/selection behavior intact. Use explicit column constants/helpers for affected replay/delete/layout logic; no unrelated data-management refactor. Preserve old replay entry.
- [ ] Run full `tests/test_data_management_module.py`, `tests/run_persistence/test_historical_analysis_reader.py`, `tests/test_historical_analysis_dialog.py` and full `tests/run_scoped_playback` directory (Qt suites isolated). Run `tests/test_result_module.py`, `tests/test_result_dashboard_module.py` and targeted component suites for any shared display code edited.
- [ ] Create a synthetic snapshot through a test fixture with representative SPL/loudness/AI/chart data; render offscreen screenshots of saved results and audition. View screenshots, fix clipping/blank configured charts, validate window defaults and labels. Do not modify user DB or require attached audio devices for visual tests. Save screenshots under ignored `outputs/` within worktree.
- [ ] Verify no analysis/PLC/recording calls made by viewer; inspect scoped diff and `git diff --check`. Commit final task checkpoint.

## Review and completion

- [ ] After Task 1, dispatch independent spec/quality reviewers against its recorded task base and checkpoint HEAD.
- [ ] After Task 2, pass its paired review checkpoint.
- [ ] After Task 3, pass its paired review checkpoint.
- [ ] After Task 4, dispatch and pass paired spec/quality review.
- [ ] After Task 5, dispatch and pass paired spec/quality review.
- [ ] Perform full-range quality review and evidence-based final verification.
- [ ] Use finishing-a-development-branch and report exact branch/worktree and test evidence. No automatic main merge without applicable user authorization; do not discard or commit user's unrelated main edits.

Assess findings against reachable behavior; ignore speculative issues and unnecessary validation.
