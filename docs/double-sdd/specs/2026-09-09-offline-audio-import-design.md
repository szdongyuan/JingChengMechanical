# 数据管理离线音频导入重构

## Approval and scope

The user approved the following design in this task: move import from Test to Data Management; reuse production analysis algorithms, durable run/result storage and the existing historical analysis/replay/relabel UI; accept any saved configuration independently of production selection, recorder and PLC availability; retain channel mapping, analysis window and calibration options; run configured acoustic and AI analysis in the background with recording priority; preserve original WAV precision, including 24-bit PCM and floating-point WAV, in storage and playback. The restart button and its entire behavior must remain unchanged. The user authorized direct implementation after plan review; do not request another procedural approval for this written transcription of the approved design.

This is one feature, implemented in sequential reviewable slices. Do not refactor recording, PLC, production configuration activation, or application restart as separate improvements.

## Evidence / current defect

- `ui/popups/import_audio_analysis_dialog.py` produces `ImportedAudioAnalysisRequest`.
- `ui/modules/control.py` passes it through `FactoryModeController.analyze_imported_audio` directly to `MultiRunCoordinator.start_imported`, which requires `ImportedStartRequest`. A real call reproduces `accepted=False, reason='invalid imported request'` before any file access.
- The production imported freezer is limited to prepared production configurations and PCM16 WAV, does whole-file hashing synchronously, and does not consume the old dialog's mapping/window/calibration choices.
- The old `analysis/imported_audio.py` temporary review path intentionally skips AI and persistence. Restoring it as a second engine would not satisfy the request.
- Persisted audio loading, analysis references and playback assume int16. The audio table currently permits only `WAV_PCM`; IEEE-float files need a truthful compatible representation, not a misleading tag or silent PCM16 conversion.

## Approach

Use a dedicated offline import application service and background execution lane, assembled independently of hardware-bound production preparation. Reuse `ProductionAnalysisExecutor` (including configured resident AI), the scheduler/persistence boundary and repositories, artifact store, and historical viewer/playback services. Reuse service implementations, not necessarily the same hardware-owned object instance. Do not resurrect temporary unsaved analysis or duplicate DSP/judgment algorithms.

An adapter-only fix is insufficient because it leaves the hardware, format and option mismatches. A separate analysis/result application is rejected because it duplicates the existing engine and historical UI.

## User workflow

1. Data Management toolbar contains **导入音频分析**. Remove this button and import-only wiring from Test/Control; preserve the restart button, its layout usability, signal and implementation exactly.
2. A single import setup dialog chooses one WAV and one saved configuration. Configuration choices come from `ConfigService.list_configs`, not enabled production IDs. No batch import or MP3/FLAC expansion.
3. Show file name, rate, channel count, duration and sample format. Preserve current mapping choices (configuration channel / chart-only / ignore), selected analysis start/duration, and the explicit current microphone calibration checkbox. Defaults retain the existing dialog's configuration window and mapping behavior. Clamp an overlong window to EOF with a visible warning; reject empty windows, duplicate mapped targets and unreadable/nonfinite audio before analysis.
4. Start submits asynchronously. Show meaningful phase text: waiting for production, preparing/saving, analyzing, completing charts, completed or failure. Accept at most one unfinished import in this UI/service; a repeated click cannot create duplicates. Closing the setup/progress UI or changing pages must not destroy an accepted job or its callbacks. Application shutdown owns cancellation/cleanup.
5. After a successful result is persisted, refresh the Data Management list using existing query/selection preservation, and offer/open the existing `HistoricalAnalysisDialog` for that run. The record remains accessible even if filters hide it. Delayed chart enrichment must refresh the opened record by run identity; manual relabeling or switching pages must not lose it.
6. Historical results retain the existing single-page channel/gain/replay controls, original analysis result, current manual status, and relabel rules. Import provenance and **无电流数据** are visible for imported records. Never fabricate zero current, a curve or a current-based PASS.

## Audio and precision contract

- Support the old dialog's uncompressed RIFF/WAVE PCM 8/16/24/32/64-bit and IEEE-float 32/64-bit formats, including the corresponding supported extensible subtypes. Unsupported/compressed or malformed files receive an actionable error. UI probing and worker decoding use one shared format authority.
- Persist a byte-preserving independent copy of the full original WAV, including unmapped/ignored channels and samples outside the analysis window. Do not truncate, reorder, resample, normalize, clip or quantize the archived source; do not rename/move/overwrite the user's source.
- Analysis converts decoded samples to the engine's float64 DSP representation with the appropriate source scale, not via PCM16. PCM24 must retain low bits. Float32/64 must not be integer-scaled. Int64-to-float64 arithmetic has normal float64 rounding, not an int16 intermediate.
- Playback applies channel selection and gain only to playback buffers, followed by the existing output clipping policy. Archive precision and analysis values are unaffected by playback gain. Hardware output precision may differ; there must be no forced PCM16 archive or analysis conversion.
- Legacy captured PCM16 decoding/scale and persistence semantics remain unchanged. Format/generalized import branches must not alter recording callbacks, encoding, sample counts, current collection or production result timing.
- Full-file copy/hash/decode/DSP run off the GUI thread. Stream copy/hash where appropriate rather than duplicating large byte buffers.

## Frozen analysis semantics

- Freeze the chosen configuration, effective channel mapping, analysis interval, calibration, golden references and AI toolchain at submission/preparation before analysis. User edits after submission do not change that job. Do not modify saved configurations or production selection.
- Preserve a clear distinction between original WAV column and logical configuration channel. Mapped channels use the target's limits/calibration/golden/AI settings. Chart-only channels are visible but excluded from verdict/AI constraints. Ignored channels are excluded from analysis/charts but remain in archived audio and may be replayed as original WAV channels.
- The persisted snapshot records enough mapping/window/format/provenance facts to reproduce the displayed result and choose the correct original WAV column in history. Extend existing envelope/mapping readers compatibly when needed; do not encode a sparse mapping as a misleading dense one.
- Use the source sample rate. Keep existing import window and golden runtime resampling rules; configured analyses that cannot run at that rate report a meaningful unavailable/error result rather than silently switching the sample rate or fabricating PASS. Execute enabled AI using the existing runtime; missing/unusable configured models are not silently skipped.
- Import has no live current collector, PLC start/finish/result write, workorder mutation, capture reservation, or fake hardware identity. Use durable `IMPORTED`/`IMPORT` provenance and existing automatic/manual result semantics.

## Runtime / recording priority

- Import service is available when the configuration/run databases and required software analysis dependencies are usable, even if production preparation failed due to missing input hardware, PLC, no enabled configs or pending production selection restart.
- Maintain a bounded background import lane and lazy reusable analysis resources. Do not initialize every saved configuration's models at startup. Do not share mutable frozen config/toolchain state with a live production run.
- While recordings or production analysis/save work are active, defer starting expensive import phases and show waiting status. Use existing capture/quiet-state information through a read-only predicate and the existing process-based analysis machinery; do not block, pause or cancel production work.
- If production begins during an import phase, keep production admission unblocked; prevent subsequent heavy import stages/enrichment from starting until quiet. Do not claim preemptive mid-DSP suspension. Verify actual recording overlap/sample continuity in addition to deterministic admission tests. No 1.5s import SLA; the prior production result target must not be weakened.
- Shutdown stops accepting imports and safely retires the current worker/scheduler/executor/playback resources using existing shutdown ownership. No late UI access or indefinitely ACTIVE durable record. Normal user page/dialog closure does not cancel an accepted import.

## Persistence and errors

- Use existing run/audio/attempt repositories and result artifact store. A verified archived file precedes analysis; result/history must not reference the user's movable source as its playback authority.
- Preaccept errors create no successful record. Postaccept save/analysis failures persist the existing appropriate failed/no-result terminal state and a useful reason. Report stage and cause rather than swallowing exceptions into a generic warning. Keep cleanup limited to this job's owned unpublished staging files.
- Successful runs remain viewable, playable, exportable and relabelable after application restart or moving/removing the original external file. The ordinary relabel API updates human judgment only, never recomputes or overwrites original results.
- Extend format storage truthfully for IEEE float. If schema changes are necessary, migrate recognized existing v2 run databases transactionally with a SQLite-consistent backup and preserve rows, IDs, foreign keys, attempts, manual labels and artifact paths. Fresh databases use the new format. Unknown/corrupt schemas are still rejected. Do not migrate the user's actual database during development tests; exercise copies/temporary fixtures.

## Compatibility / Migration

- Backward compatibility: **required** for existing captured/imported records, original audio bytes, configured DSP semantics, production operation and public historical UI behavior; **partial** for the obsolete import entry (it intentionally moves).
- Protected surfaces: production recording/PLC/current workflows; restart button and behavior; enabled config state; captured PCM16 sample scaling; historical records, labels and artifacts; existing single-page replay controls/style; source files; all unrelated dirty main-worktree changes.
- Allowed breakage: removal of Test-page import button and unused import-only controller route; no external API/CLI compatibility layer for that obsolete in-process route is required.
- Migration strategy: minimal compatible envelope/format extension and, only if required, backed-up transactional known-schema migration; never destructive reinitialization or bulk audio conversion.

## Verification / acceptance

1. Real old-request rejection reproducer is replaced by a Data Management-to-real-offline-service integration test, not just a mock controller assertion.
2. PCM16, low-amplitude PCM24, float32 and float64 files round-trip byte-identically; all advertised PCM widths have decode tests. A tone below one PCM16 LSB survives analysis and playback input conversion. Old PCM16 tests stay green.
3. A WAV with distinct channel tones proves reordered/sparse logical mapping, chart-only/ignored verdict exclusion, original-column playback, start/duration and calibration on/off. Source archive stays full and unchanged.
4. Hardware/PLC services that raise if called, no enabled production config and an arbitrary saved config still yield a stored acoustic/AI result through real repositories and engine (AI process fixture may be deterministic). Missing model produces a truthful error.
5. Persist -> close/reopen runtime -> `HistoricalAnalysisReader` -> existing dialog -> replay -> relabel integration succeeds for high-precision formats; export/delete of the imported record uses existing ownership rules and never deletes the original external file.
6. Deferred result/charts and manual label overlap retain the correct run's final charts and saved measurements.
7. GUI click returns promptly while worker is blocked; waiting/phase/errors are visible; page/dialog closure and app shutdown are tested. Production-active state blocks import heavy-stage admission, never production admission. Recording-overlap regression verifies sample continuity/current collection remain unaffected.
8. Test-page restart signal/button tests stay unchanged and pass. Run targeted persistence, analysis executor/process, playback, history/data-management/control and recording regressions in separate Qt processes where necessary.

## Review policy

Reviewers provide evidence, not instructions. Address demonstrated regressions and missing approved behavior. Reject theoretical races, redundant layers of validation and unrelated redesign without a concrete current risk, as explicitly requested by the user.
