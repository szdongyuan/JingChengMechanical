# Offline Audio Import Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move WAV import into Data Management as hardware-independent, precision-preserving, persisted analysis using the existing engine and historical replay UI.

**Architecture:** A bounded offline application service freezes one saved config and import options, publishes an original-byte audio copy, then composes existing scheduler/executor/persistence classes without production hardware prerequisites. Shared WAV metadata/decoding and import-aware mappings connect archive, DSP and replay. Production capture/restart behavior stays unchanged.

**Tech Stack:** Python 3.12, PyQt6, NumPy/SciPy, peewee/SQLite, existing process-isolated analysis and resident AI.

---

## Authority, setup and boundaries

Spec: `docs/double-sdd/specs/2026-09-09-offline-audio-import-design.md` (approved design, including precision extension).

Main observed branch: `master_0811`; source base: `91d16cbfc2e7a5dd472b06753461da36c1c4e255`. Record the actual main HEAD immediately before worktree creation, after spec/plan documentation commits. Feature branch: `codex/offline-audio-import-20260909`. Worktree: `.worktrees/offline-audio-import-20260909`. Do not implement on main. Temporary `tmp/sdd:` task commits are review checkpoints, not permanent integration history.

Main has unrelated dirty video preview, history-card, database-compatibility and recorder-jitter changes. Do not copy/commit/revert them. Do not touch the running GUI or actual `data/*.db`; tests use temporary databases and synthetic WAVs. Use system Python, not an unverified local venv. Main application startup may need its existing dirty DB compatibility changes; that is not an import regression.

Production `main_window.py` restart wiring and `ControlModule` restart fields/methods/signals are protected. Prefer no `main_window.py` edit at all. Do not implement hot configuration reload. Preserve old PCM16 production encoding and DSP scaling.

Run each Qt test file in a separate process if teardown contamination appears. Shared test environment (PowerShell, from worktree):

```powershell
$env:PATH = 'D:\Program Files\Python\Python312\Lib\site-packages\PyQt6\Qt6\bin;D:\Program Files\Python\Python312\Lib\site-packages\PyQt6;' + $env:PATH
$env:PYTHONPATH = (Get-Location).Path
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTEST_QT_API = 'pyqt6'
$env:QT_API = 'pyqt6'
$env:QT_QPA_PLATFORM = 'offscreen'
& 'D:\Program Files\Python\Python312\python.exe' -m pytest -q -p pytestqt.plugin tests/test_import_audio_analysis_dialog.py
```

For all commands below use that interpreter, environment and `-p pytestqt.plugin`; expect PASS after implementation. Use `apply_patch` for edits. Each task: run newly added test RED before code, implement, rerun GREEN, run specified regressions, commit only owned files. Orchestrator reviews each slice with independent spec and quality agents before the next task.

## File responsibilities

- New `app/service/audio_wav.py`: shared RIFF metadata, supported PCM/IEEE format detection, source-appropriate float64 decoding; no GUI/database/runtime orchestration.
- `run_persistence/{audio_publication,audio_service,audio_repository,contracts,schema,database,prepare_database}.py`, `model/audio_table.py`, new `run_persistence/audio_format_migration.py` if needed: truthful byte-preserving archival and known-schema compatibility.
- `run_persistence/{production_run_audio_adapter,run_persistence_scheduler_bridge,analysis_envelope,export_reader,historical_analysis_reader}.py`, `analysis/{scheduler/models,production_executor,process_executor}.py`, `run_scoped_playback/service.py`: precision-aware file consumption and original-column import mapping, retain legacy capture behavior.
- New `app/service/offline_audio_import/{__init__,contracts,snapshot,runtime,service}.py`: typed public submission/status; frozen options; hardware-free existing-component composition; one-job background lifecycle. Small modules, no duplicate DSP/SQL/result format implementations.
- `app/services_container.py`: independent service and playback availability, read-only production busy predicate, shutdown registration and status signal bridge. Only narrowly expose production busy state from composition if no existing public API suffices.
- `ui/modules/data_management.py`, `ui/popups/import_audio_analysis_dialog.py`, `ui/modules/control.py`: toolbar move, shared metadata, async progress, historical result routing and obsolete import-only wiring removal. Existing replay/history controls retain layout; minimal metadata/provenance additions in history dialog/view where necessary.

## Task 1: Shared WAV format authority and precision-preserving archive

**Files:** Create `app/service/audio_wav.py`, `tests/test_audio_wav.py`, `tests/run_persistence/test_audio_format_migration.py`; modify `app/service/run_persistence/audio_publication.py`, `audio_service.py`, `audio_repository.py`, `contracts.py`, `schema.py`, `database.py`, `prepare_database.py`, `model/audio_table.py`; create `app/service/run_persistence/audio_format_migration.py` for the required known-v2 migration; update matching tests in `tests/run_persistence/`.

- [ ] Add synthetic RIFF fixtures covering PCM8/16/24/32/64, float32/64 and supported extensible forms. Shared probe and decoder return the same rate/channel/frame/format facts; low PCM24 bits and sub-int16-LSB floating samples survive float64 decoding. Test malformed/empty/nonfinite/unsupported input with actionable errors, not speculative validation layers.
- [ ] Add archive tests: publish PCM24/float WAV through real `AudioPublication`/`AudioPersistenceService`, assert `saved.read_bytes() == source.read_bytes()`, all columns/frames remain, file format is truthful. Capture PCM16 behavior/verification remains strict. Add v2 migration fixture containing run/audio/attempt/manual label and assert all rows, FK links, artifact paths survive with a consistent backup; unknown schema stays rejected, repeat open is idempotent.
- [ ] Run RED: `python -m pytest -q tests/test_audio_wav.py tests/run_persistence/test_audio_format_migration.py tests/run_persistence/test_audio_publication.py tests/run_persistence/test_audio_service.py`. Capture the concrete missing capability failure before changing code.
- [ ] Implement shared metadata/decoding by extracting/reusing old WAV knowledge and SciPy conversion logic. Keep archive copy byte-for-byte, verify using shared header facts and complete declared data rather than re-encoding. Add `WAV_IEEE_FLOAT` (or equally explicit stable tag) beside `WAV_PCM`; retain schema columns where possible, bump known schema version and transactional backup-first v2 table-constraint migration. Never rewrite user audio or pretend IEEE float is PCM. Use consistent SQLite backup API, preserve indices/foreign keys, commit migration only after validation.
- [ ] Run GREEN and regressions: `python -m pytest -q tests/test_audio_wav.py tests/run_persistence/test_audio_format_migration.py tests/run_persistence/test_audio_publication.py tests/run_persistence/test_audio_service.py tests/run_persistence/test_audio_repository.py tests/run_persistence/test_contracts.py tests/run_persistence/test_database_lifecycle.py tests/run_persistence/test_prepare_database.py`.
- [ ] Commit owned paths: `git commit -m "tmp/sdd: preserve imported WAV precision in archive"`; report tests and public helper contracts for Task 2.

## Task 2: Precision-aware analysis/history/replay and explicit import channel mapping

**Files:** Modify `app/service/run_persistence/production_run_audio_adapter.py`, `run_persistence_scheduler_bridge.py`, `analysis_envelope.py`, `export_reader.py`, `historical_analysis_reader.py`, `app/service/analysis/scheduler/models.py`, `app/service/analysis/production_executor.py`, `process_executor.py` only if transport needs extension, `app/service/run_scoped_playback/service.py`; add `tests/analysis_scheduler/test_imported_precision_analysis.py`, `tests/run_scoped_playback/test_imported_precision.py`; extend relevant `tests/run_persistence/` and `tests/analysis_scheduler/` tests.

- [ ] Add RED tests using real archived PCM24/float files and real audio adapter/reference/ProductionAnalysisExecutor. Verify low-level tone measurements against direct float64 input. Validate no int16 intermediate; old captured PCM16 remains on its exact legacy scale. Exercise process transport with a high-precision source, not just direct executor.
- [ ] Add explicit import mapping fixture: original WAV columns contain different tones; map a column to a nonmatching logical channel, one chart-only, one ignored; preserve original full archive. Assert configured target limits/calibration/AI mapping, chart-only verdict exclusion and ignored-chart exclusion. Playback selects original columns rather than assuming config index equals file position. Include window/calibration on/off and default CH1 history replay (original file column 0), gains still -60..35 dB, default20.
- [ ] Implement compatible frozen import mapping/provenance metadata, preferably versioned mapping if v1 cannot express sparse mapping. Retain v1 readers. Do not redefine `channel_order` dishonestly or insert ignored channels into judgment. Allow engine mapping subset only for explicit import snapshots; keep production validations intact. Adapt shared executor chart/judgment config selection using existing import shaping logic, not a second judgment engine. DSP uses source rate, existing import golden resampling behavior and explicit unavailable outcomes; no current collector or fake current.
- [ ] Generalize imported references/loader output with truthful dtype and source scaling. Derive file sample format from shared WAV metadata and validate against persisted facts. Playback/history/export readers accept new format and use original WAV columns for explicit imports; legacy captured paths are unchanged. Backend playback output may convert to its required float buffer; archive/DSP must not quantize.
- [ ] Run GREEN: `python -m pytest -q tests/analysis_scheduler/test_imported_precision_analysis.py tests/run_scoped_playback/test_imported_precision.py tests/run_persistence/test_analysis_envelope.py tests/run_persistence/test_production_run_audio_adapter.py tests/run_persistence/test_historical_analysis_reader.py tests/run_persistence/test_export_reader.py`.
- [ ] Run legacy regressions in separate commands: `python -m pytest -q tests/analysis_scheduler/test_production_executor.py tests/analysis_scheduler/test_staged_production_executor.py tests/analysis_scheduler/test_process_executor_transport.py`; `python -m pytest -q tests/run_scoped_playback`; `python -m pytest -q tests/run_persistence/test_scheduler_bridge_end_to_end.py`.
- [ ] Commit: `git commit -m "tmp/sdd: support imported precision and channel mapping downstream"`; document actual helper/mapping contracts for Task 3.

## Task 3: Hardware-independent offline import runtime and application service

**Files:** Create `app/service/offline_audio_import/__init__.py`, `contracts.py`, `snapshot.py`, `runtime.py`, `service.py`, `tests/offline_audio_import/test_snapshot.py`, `test_service.py`, `test_integration.py`; modify `app/services_container.py`; narrowly extend `app/service/multi_run_integration/production_composition.py` only for a read-only busy query if required; matching container tests.

- [ ] Add RED tests for submit returning promptly while preparation is blocked, one unfinished job capacity, waiting when production busy, stable run identity/status notifications and safe shutdown. Use raising fakes for recorder/PLC/config-selection mutation and prove none are called. No enabled production configs, failed hardware preparation and an arbitrary saved config must still permit import. Freeze options at submission so config edits during waiting do not change results.
- [ ] Implement snapshot preparation from `ConfigService.load_config`, calibration service and frozen golden/toolchain helpers without `ProductionPrerequisites.prepare`/hardware selection leases. Freeze original file metadata/identity, import mapping/window/calibration, explicit absent current and configured AI. Validate configured software dependencies normally; never silently skip enabled AI. Reuse same-config AI resources where safe rather than reload on every import.
- [ ] Compose existing `AudioPersistenceService`, `RunRepository`, `AttemptRepository`, `AnalysisPersistenceBoundary`, `RunPersistenceSchedulerBridge`, `AnalysisScheduler` and `ProcessIsolatedAnalysisExecutor(process_count=1)` in an independent lazy lane. The last two may be wrapped, not replaced with new analysis algorithms. Use existing acceptance/finalization repository methods, artifact store and automatic/manual result representation. Ensure postaccept failure terminalizes correctly and enrichment persists by attempt identity.
- [ ] Wait for read-only production capture/save/analysis busy state before expensive import phases; wire existing quiet/enrichment gating. Production can start during import without being denied. Bound phases/resources and cancel waiting on shutdown; do not attempt unsafe thread suspension or hold production locks during copy/DSP. Per-source original copy/hash/decode stays in worker. Provide status events with run_id and monotonic generation/phase, useful errors, and an explicit charts-ready update.
- [ ] Wire service independent of hardware-bound production activation in `ServicesContainer`, and ensure existing historical playback service is available offline (reuse active production playback when available, otherwise hardware-free read-side composition with lazy output backend). Register correct independent shutdown owner; do not close shared DB or playback twice. Do not mutate `ingress_open` or production unavailable/restart states to fake offline availability.
- [ ] Add real pipeline integration: temp DB + PCM24/float WAV + saved config -> accepted run -> archived audio -> shared executor -> persisted measurements/AI result -> historical reader. Deterministic AI fixture runs through configured engine interface; missing model gives truthful failure. Delete/move only test-owned source afterwards and replay archived audio. Relabel preserves original result/artifact.
- [ ] Run GREEN: `python -m pytest -q tests/offline_audio_import`; run `python -m pytest -q tests/multi_run_integration/test_production_composition.py` and the affected `ServicesContainer` test files individually. Do not require physical hardware or actual data DB.
- [ ] Commit: `git commit -m "tmp/sdd: add offline persisted audio import service"`; report wiring, shutdown and production-priority evidence.

## Task 4: Move entry to Data Management and connect progress/history UI

**Files:** Modify `ui/modules/data_management.py`, `ui/popups/import_audio_analysis_dialog.py`, `ui/modules/control.py`; remove only obsolete import-specific route in `app/service/factory_mode_controller.py` if unused; minimally modify `ui/popups/historical_analysis_dialog.py` / `ui/widgets/historical_analysis_view.py` for import provenance/no-current display and enrichment refresh. Tests: `tests/test_data_management_module.py`, `tests/test_import_audio_analysis_dialog.py`, `tests/test_control_module.py`, `tests/test_historical_analysis_dialog.py`; add `tests/offline_audio_import/test_ui_integration.py`.

- [ ] Add RED tests clicking actual Data Management import entry, selecting any saved config, showing format via shared probe, preserving mapping/window/calibration defaults, submitting to typed offline service, progressing across waiting/save/analysis/chart completion, and opening the existing history window by accepted run ID. Explicitly assert an overlong window clamps to EOF with a visible warning, an empty window is rejected, and duplicate mapped targets are rejected in both UI and service boundary. No old-controller mock-only success test counts as integration.
- [ ] Remove Control's import button/method/enable wiring and obsolete import-only imports; retain `btn_restart`, `restart_intent`, `_request_restart`, `set_restart_pending` and main-window connections untouched. Place surviving restart button naturally without redesign. Replace obsolete button-presence tests with absence/moved-entry tests; keep existing restart click/dedup tests unchanged.
- [ ] Reuse existing setup dialog structure but shared metadata reader; list all configs independently of production selection. Wire start/progress to nonblocking service notifications via Qt signals. GUI files only do cheap metadata or worker-dispatched reads, never whole-file hashing/DSP. Disable duplicate submit until final completion, restore on failure.
- [ ] On committed result refresh table via existing query-preserving path and show/reuse `HistoricalAnalysisDialog`. Record-specific enrichment notifications refresh that window even if user relabels or changes page. Show import origin and absent current text without modifying recorded result status. Keep single-page replay UI unchanged in layout.
- [ ] Test dialog/page closure while waiting/working, late notification after destruction, failure visibility, result-before-chart followed by relabel and chart completion. Accepted job continues until completion or app shutdown. Tests distinguish original automatic verdict from latest human label.
- [ ] Run each GREEN file in its own Qt process: `python -m pytest -q -p pytestqt.plugin tests/test_data_management_module.py`; similarly `tests/test_import_audio_analysis_dialog.py`, `tests/test_control_module.py`, `tests/test_historical_analysis_dialog.py`, `tests/offline_audio_import/test_ui_integration.py`.
- [ ] Commit: `git commit -m "tmp/sdd: move import into data management and reuse history UI"`.

## Task 5: End-to-end verification and regression closure

**Files:** Add `tests/offline_audio_import/test_end_to_end.py`, `test_recording_priority.py`; modify only feature-owned files when a reproduced integration defect requires it; retain reviewable checkpoints. Verification notes in `docs/double-sdd/plans/2026-09-09-offline-audio-import-verification.md`.

- [ ] Add missing RED end-to-end coverage using temporary v2 database migrated to current, multi-channel precision WAV, actual service/scheduler/persistence/executor and history reader. Assert archived bytes, AI/acoustic measurement, mapping, window, calibration, no-current, source independence, relabel and export/delete ownership. Close the first service/runtime completely, reopen the database in a fresh runtime, then read via HistoricalAnalysisReader, open the existing history dialog, replay the archived high-precision WAV and relabel; assert original results and bytes survive this simulated application restart. Use deterministic device/AI boundary fakes only where physical hardware is unavailable, not fake whole analysis success.
- [ ] Exercise recording overlap with real ring/capture path and controlled backend: import already active then recording starts, and recording already active then import queues. Assert exact requested frame count/continuity and current trace behavior; import never delays admission via its own gate. Run timing as diagnostic, do not claim production-PC SLA from this development PC.
- [ ] Fix only demonstrated integration failures; run all new suites, all `tests/run_persistence` and `tests/run_scoped_playback`, affected analysis scheduler suites, production composition/current meter, Control/Data Management/history tests and `tests/resident_audio_mvp/test_ring_capture.py`. Isolate Qt test files to avoid baseline teardown contamination. Record exact counts, commands and any independently reproduced baseline failures.
- [ ] Render a test-only Data Management/import/history screenshot with synthetic data if useful; never start real recording or alter user's runtime/database. Check relevant UI text/overflow and default channel/gain.
- [ ] Commit verification and fixes: `git commit -m "tmp/sdd: verify offline import end to end"`. Orchestrator runs paired slice review then full-range quality review and `verification-before-completion`.

## Completion and integration

- [ ] Spec review passes; approved design transcription committed without unrelated changes.
- [ ] Full plan review passes; execution worktree/base recorded and baseline tests pass.
- [ ] Each task passes independent spec/quality review; confirmed issues fixed, theoretical/non-scope feedback explicitly rejected with reason.
- [ ] Full implementation quality review and final fresh verification pass.
- [ ] Invoke `finishing-a-development-branch`. Report exact feature worktree location and whether main has been integrated. Do not claim deployment or merge unless performed. If integration is authorized, squash only feature delta, preserving every unrelated main edit and actual data files; do not retain temporary checkpoint history.
