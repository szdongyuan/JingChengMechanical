# Single-page Historical Replay Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the new historical viewer's tabs and audition plots with a compact full-record replay row alongside saved results and manual labels.

**Architecture:** Add a lightweight QWidget in the existing replay module that uses saved audio metadata and existing asynchronous playback/owner cleanup helpers, but never prepares audio plots. Compose it directly into HistoricalAnalysisDialog and preserve the saved-result view, reader, labeling, and legacy replay entry.

**Tech Stack:** Python 3.12, PyQt6, existing RunScopedPlaybackService.

---

Spec: `docs/double-sdd/specs/2026-09-07-single-page-history-replay-design.md` (supersedes old history audition layout requirements).

Continue the existing dedicated `.worktrees/historical-analysis-replay` / `codex/historical-analysis-replay` worktree. Existing main integration anchor is still `master_0811` at `83c4a51b9636db84e7f3f5d8211ba9fa25873379`. Pre-adjustment feature HEAD is `0f4d6aa0fbe4ce00a083a33350c396a78035b656`; record task base after approved docs commit. No new nested worktree, main merge, forced process termination, or device changes.

## One task: Compact replay row and single-page composition

**Files/responsibilities:**

- Modify `ui/popups/run_audio_replay_dialog.py`: a new lightweight single-channel full-run controls class; reuse `_start_worker` and `_stop_owned`, retain legacy RunAudioReplayPanel/RunAudioReplayDialog behavior. Do not subclass old plot panel or hide constructed charts.
- Modify `ui/popups/historical_analysis_dialog.py`: replace QTabWidget/audio page with compact controls in top area and result scroll below; preserve snapshot/read/mark/close behavior.
- Create `tests/test_historical_replay_controls.py`: direct controls tests.
- Update `tests/test_historical_analysis_dialog.py` and `tests/run_persistence/test_historical_analysis_integration.py`: replace old embedded audition assumptions with new single-page/full-run expectations and regenerate screenshot. Update `tests/test_data_management_module.py` only if it references changed historical internals; no DataManagement behavior change intended.

- [x] Write RED tests: no QTabWidget/no embedded old plot panel; saved graphs and label controls visible; channel defaults for (3,0,1) and (3,1); selecting channel/gain produces full-run request; monkeypatch WAV loading and `prepare_replay_audio/plots` to fail if opening compact controls calls them; asynchronous startup, errors, close/refresh during startup and cross-owner replacement.
- [x] Run new focused suite and adapted dialog tests, confirm expected RED failures before implementation.
- [x] Implement lightweight controls with `record`, `playback`, unique owner and closure flag, `channel_combo`, `gain_spin` (-60..35, default20), `play_button` text 重放, and inline status. Populate channels using physical indexes from metadata without loading samples. Request core:

```python
channel = self.channel_combo.currentData()
request = RunPlaybackRequest(
    self._record.run_id, channel, 0.0,
    float(self._record.frame_count) / float(self._record.sample_rate_hz),
    display_channel_indices=(channel,),
    gain_db_override=float(self.gain_spin.value()),
)
```

- [x] Use existing queued worker helper for `play(request, owner=owner)` and owner-conditional close cleanup. Never capture widgets in background closures. While startup pending disable play; late startup after closure still cleans own playback. Expose idempotent `shutdown()` for host. No new backend contracts or separate stop/pause UI.
- [x] Compose single-page host with compact replay row and existing original result view below, removing tabs/extra audition headings. Default selection occurs on new metadata load. Retain 1100x820 default style and existing labels; handle missing audio/services inline without blocking result/mark usage. Call shutdown on refresh/close/reject/delete just as existing host does.
- [x] Run GREEN suites in separate processes: new compact controls, historical dialog, real historical persistence integration, data management, legacy replay panel, playback directory. Keep all existing real result/mark assertions, including original artifact/audio preservation and successful-save-before-close notification. Do not merely drop old lifecycle coverage; adapt it to compact controls.
- [x] Generate/inspect one APP_QSS single-page screenshot from synthetic persisted data showing saved SPL/loudness/AI and graph plus channel/gain/replay/mark controls. Save ignored `outputs/historical-replay-single-page.png`. Verify controls fit and no second page or audition graph exists.
- [x] Run `git diff --check`, inspect scoped diff, commit owned files `tmp/sdd: simplify historical replay to one page`.
- [x] Orchestrator performs paired spec/quality review and independently reruns relevant tests; assess real findings, ignore speculative checks. Because this plan contains one task, that review covers the entire follow-up implementation.
- [x] Record verification and handoff. Keep branch unmerged and existing user program running; report restart is needed to see changed code. Do not launch/close another program unless user requests it.

## Reproducible verification environment

From the feature worktree, substitute the exact suite path for each command (Qt suites remain isolated):

```powershell
$env:PATH = 'D:\Program Files\Python\Python312\Lib\site-packages\PyQt6\Qt6\bin;D:\Program Files\Python\Python312\Lib\site-packages\PyQt6;' + $env:PATH
$env:PYTHONPATH = (Get-Location).Path
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTEST_QT_API = 'pyqt6'
$env:QT_API = 'pyqt6'
$env:QT_QPA_PLATFORM = 'offscreen'
& 'D:\Program Files\Python\Python312\python.exe' -c "import pytest; raise SystemExit(pytest.main(['-q','-p','pytestqt.plugin','tests/test_historical_replay_controls.py']))"
```

No physical audio-device test or full app startup is required for this presentation simplification. Tests exercise playback requests via existing service fakes; backend is unchanged. Original feature's historical review remains valid for unchanged components.

## Completion evidence

Implementation `7725b12` passed paired spec/full-follow-up quality reviews without required fixes. Orchestrator independently ran controls 10, historical dialog 34, real persistence integration 1, Data Management 52, legacy replay panel 15, playback 371: **483 passed**, all exit 0. Single-page APP_QSS screenshot was generated and visually inspected at `outputs/historical-replay-single-page.png`. Only the two UI implementation files and their tests changed; no backend/reader/data changes. Kept existing feature branch unmerged and user process untouched; current running program needs a new validation launch to use this code.
