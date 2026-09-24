# Resident Audio Recovery and Callback Isolation Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate false and sticky `resident_unavailable` failures by isolating the PortAudio callback, making Resident Pool the only stream-lifecycle owner, and adding recoverable full-generation replacement without changing software operation or output contracts.

**Architecture:** Each Resident generation owns its callback, preallocated ingress, Ring, drain, native handle, and generation token. Pool alone decides health and replacement. A captured start may park on one shared recovery future for at most one five-second budget while retaining admission capacity but freeing the captured-start worker.

**Tech Stack:** Python 3.12, NumPy, sounddevice/PortAudio, threading, PyQt6 integration, pytest, existing SQLite persistence.

---

## Fixed acceptance boundaries

- Preserve PLC/manual operations, UI workflow, configuration, database data/schema, WAV/result formats, public coordinator categories, start capacity `2`, analysis workers `2`, and capture capacity `6`.
- Do not migrate to `app/service/analysis/resident_audio`.
- A healthy generation never performs recovery lifecycle work.
- A stale generation may continue the current trigger only when the original generation resumes within the observation window and Ring continuity still covers the original trigger.
- A replacement generation never fabricates the current recording: the current trigger fails as `resident_unavailable/generation_replaced_for_trigger`; the next independent trigger can succeed.
- One abnormal start gets one monotonic budget of at most five seconds. Recovery may continue in the background afterward.
- Analysis, display, save, PLC publication, and database load must not run on or block the PortAudio callback.
- Keep safety checks at external/device/generation/memory/time/resource boundaries; remove only duplicated internal checks proven redundant by tests.

## Baseline

- Approved spec: `docs/double-sdd/specs/2026-08-28-resident-audio-recovery-and-callback-isolation-design.md`.
- Spec commit: `882ecaf`.
- Current focused baseline command:

```powershell
python -m pytest tests/resident_audio_mvp tests/multi_run_coordinator_mvp/test_managed_resident_capture_port.py -q
```

- Recorded result: `329 passed, 2 failed`. Both failures are stale same-commit test expectations from `dc58b0b`: Ring byte-boundary setup and existing `latency="high"` SoundDevice kwargs.
- This host currently cannot import PyQt6 `QtCore` because its DLL fails to load. Non-Qt gates run locally; Qt production gates must be run on a working target environment and cannot be reported as passed here.

## Task 1: Establish a green baseline and stable recovery contracts

**Files:**
- Modify: `tests/resident_audio_mvp/test_contracts_startup.py`
- Modify: `tests/resident_audio_mvp/test_sounddevice_backend.py`
- Create: `app/service/analysis/resident_audio_mvp/diagnostics.py`
- Modify: `app/service/analysis/resident_audio_mvp/contracts.py`
- Modify: `app/service/analysis/resident_audio_mvp/__init__.py`
- Create: `tests/resident_audio_mvp/test_recovery_contracts.py`
- Modify: `tests/resident_audio_mvp/test_stress_and_handoff.py`

- [ ] Reproduce the two baseline failures, then fix tests only: derive the exact Ring boundary from `MAX_RING_BYTES_PER_STREAM` and include the already-shipped `latency="high"` kwarg.
- [ ] Add immutable internal contracts for lifecycle status (`GENERATION_CONTINUOUS`, `GENERATION_REPLACED`, `RECOVERY_PENDING`, `RECOVERY_FAILED`, `LIFECYCLE_CANCELLED`), bounded stage/reason diagnostics, authenticated readiness tickets, and one-shot futures.
- [ ] Keep only these contracts public. Continue forbidding imports from the large alternate Resident implementation and keep Pool/slot/Ring/recovery internals private.
- [ ] Test one-shot settlement, ticket ownership, bounded diagnostic text, and the concrete reasons used by this fix: stale heartbeat, callback/PortAudio overflow, continuity loss, backend fault, endpoint resolution, open/start/first-sample, generation replacement, orphan cap, closing, and invariant failure. Do not create exhaustive theoretical combinations that production cannot emit.

Verify:

```powershell
python -m pytest tests/resident_audio_mvp/test_recovery_contracts.py tests/resident_audio_mvp/test_stress_and_handoff.py::test_public_exports_and_static_import_boundary -q
python -m pytest tests/resident_audio_mvp tests/multi_run_coordinator_mvp/test_managed_resident_capture_port.py -q
```

Expected: all pass; focused baseline becomes `331 passed` before new tests are counted.

Commit:

```powershell
git add tests/resident_audio_mvp/test_contracts_startup.py tests/resident_audio_mvp/test_sounddevice_backend.py app/service/analysis/resident_audio_mvp/diagnostics.py app/service/analysis/resident_audio_mvp/contracts.py app/service/analysis/resident_audio_mvp/__init__.py tests/resident_audio_mvp/test_recovery_contracts.py tests/resident_audio_mvp/test_stress_and_handoff.py
git commit -m "feat: define resident recovery contracts"
```

## Task 2: Isolate the callback and make native cleanup bounded

**Files:**
- Create: `app/service/analysis/resident_audio_mvp/ingress.py`
- Create: `tests/resident_audio_mvp/test_ingress.py`
- Create: `tests/resident_audio_mvp/test_callback_isolation.py`
- Create: `tests/resident_audio_mvp/test_backend_orphans.py`
- Modify: `app/service/analysis/resident_audio_mvp/stream.py`
- Modify: `app/service/analysis/resident_audio_mvp/ring.py`
- Modify: `app/service/analysis/resident_audio_mvp/pool.py`
- Modify: `app/service/analysis/resident_audio_mvp/sounddevice_backend.py`
- Modify: `tests/resident_audio_mvp/fakes.py`
- Modify: `tests/resident_audio_mvp/test_ring_capture.py`
- Modify: `tests/resident_audio_mvp/test_cancel_fault_shutdown.py`
- Modify: `tests/resident_audio_mvp/test_sounddevice_backend.py`

- [ ] First write failing tests proving the current callback directly reaches Ring/Pool/display work and that a poisoned callback cannot be made healthy by replacing only the raw handle.
- [ ] Add a preallocated SPSC ingress with at least 40 production blocks (two seconds at 50 ms). Callback work is limited to generation/shape/status bounds, `np.copyto`, sequence/heartbeat update, and signal. It may not wait on locks/Conditions/Futures or call Ring, display, logging, coordinator, save, analysis, or lifecycle operations.
- [ ] Move Ring append, capture-owner progress, and drain-side observer work to the per-generation drain. Heartbeat advances when ingress accepts a block, before downstream work. Preserve trigger mapping and exactly-once capture terminal races.
- [ ] In the SoundDevice backend, serialize native lifecycle calls on each handle's control worker and represent `CALL_RUNNING`, `CALL_TIMED_OUT`, `CLOSE_RETRYABLE`, and `CLOSED`. A caller returns at its deadline; a timed-out call receives no second command. Cleanup release requires actual close evidence, not elapsed time, worker exit, or callback stop.
- [ ] Convert stale callback `STOP` to native `CallbackStop`. Forward real `on_fault` to Pool; contain OS-boundary diagnostic errors without a silent `except Exception: pass`.

Verify:

```powershell
python -m pytest tests/resident_audio_mvp/test_ingress.py tests/resident_audio_mvp/test_callback_isolation.py tests/resident_audio_mvp/test_backend_orphans.py tests/resident_audio_mvp/test_sounddevice_backend.py tests/resident_audio_mvp/test_ring_capture.py tests/resident_audio_mvp/test_cancel_fault_shutdown.py -q
```

Expected: all pass, including a short six-stream callback timing test with p99 <= 5 ms and max <= 10 ms.

Commit:

```powershell
git add app/service/analysis/resident_audio_mvp/ingress.py app/service/analysis/resident_audio_mvp/stream.py app/service/analysis/resident_audio_mvp/ring.py app/service/analysis/resident_audio_mvp/pool.py app/service/analysis/resident_audio_mvp/sounddevice_backend.py tests/resident_audio_mvp/test_ingress.py tests/resident_audio_mvp/test_callback_isolation.py tests/resident_audio_mvp/test_backend_orphans.py tests/resident_audio_mvp/fakes.py tests/resident_audio_mvp/test_ring_capture.py tests/resident_audio_mvp/test_cancel_fault_shutdown.py tests/resident_audio_mvp/test_sounddevice_backend.py
git commit -m "refactor: isolate resident callback and native cleanup"
```

## Task 3: Implement the complete Pool-owned recovery state machine

This remains one task intentionally: foreground replacement, orphan ownership, retry, and shutdown must share one Pool authority. Splitting them into separately shippable owners would recreate the bug class.

**Files:**
- Create: `app/service/analysis/resident_audio_mvp/recovery.py`
- Create: `app/service/managed_input_endpoint_resolver.py`
- Create: `tests/resident_audio_mvp/test_recovery.py`
- Create: `tests/test_managed_input_endpoint_resolver.py`
- Modify: `app/service/analysis/resident_audio_mvp/pool.py`
- Modify: `app/service/analysis/resident_audio_mvp/stream.py`
- Modify: `app/service/analysis/resident_audio_mvp/contracts.py`
- Modify: `app/service/managed_input_contracts.py`
- Modify: `app/service/managed_input_sounddevice_backend.py`
- Modify: `app/service/managed_input_resident_adapter.py`
- Modify: `tests/test_managed_input_sounddevice_backend.py`
- Modify: `tests/test_managed_input_resident_adapter.py`
- Modify: `tests/resident_audio_mvp/test_cancel_fault_shutdown.py`

- [ ] Write deterministic fake-clock tests for the real failure modes: healthy immediate path; stale original generation resumes continuously; explicit fault/overflow/continuity gap; full new callback/ingress/Ring/handle generation; replacement makes current claim fail and next claim succeed; five-second foreground deadline with later background success; same-hardware coalescing; other-hardware isolation; stale callback rejection; close unconfirmed; endpoint index change/missing/ambiguous; candidate open/start/first-sample failures; and shutdown.
- [ ] Pool stores the only desired/active generation state and one shared lifecycle future per hardware. A merely stale generation gets at most one second within the caller's five-second budget. Fault/overflow latches once and initiates recovery without waiting for another user start.
- [ ] Replacement order is exact: detach old generation under a short lock; retire outside the lock; record unresolved native cleanup separately; freshly resolve the exact endpoint; create a completely new generation; start and await first ingress; publish only if desired revision still matches. If it no longer matches because of configuration revision or shutdown, retire the unpublished candidate through the same bounded close/orphan path before settling it as superseded. Claims created before publication settle as `generation_replaced_for_trigger`.
- [ ] When callback overflow, continuity loss, or backend fault invalidates a live generation, atomically latch the fault and give every affected active capture owner exactly one explicit failed terminal outcome before requesting recovery outside the Pool lock. A completed immutable capture that won the existing terminal race remains successful; no owner is migrated to the replacement generation or left pending.
- [ ] Recovery endpoint resolution re-enumerates every candidate open. Use a unique stable UID plus compatible descriptor when available; otherwise require one exact normalized `(name, host API, default rate, input channels)` match. Never select by similar/name-only/first device and never write config/database state.
- [ ] Continue per-hardware background retries at `1, 2, 5, 10, 30, 30...` seconds while the exact desired endpoint remains configured. New starts join the same future. Successful first ingress resets backoff.
- [ ] Track unresolved native cleanup outside active generations, capped at two per hardware and twelve globally. At a cap, create no new handle/worker; cleanup continues and a released slot wakes one retry. `CLOSE_RETRYABLE` reuses its control worker; `CALL_TIMED_OUT` is only observed until it returns.
- [ ] Shutdown uses one deadline: stop admission/retries, invalidate generations, settle owners, join drains, request cleanup for eligible handles/orphans, reconcile late completions, and return the existing clean/non-clean report with exact unresolved hardware evidence.
- [ ] Adapter passes frozen endpoint identity and resolver into Pool but owns no heartbeat, recovery thread, or replacement registry. Startup matching/calibration/capture-target behavior stays unchanged.

Verify:

```powershell
python -m pytest tests/resident_audio_mvp/test_recovery.py tests/resident_audio_mvp/test_backend_orphans.py tests/resident_audio_mvp/test_cancel_fault_shutdown.py tests/test_managed_input_endpoint_resolver.py tests/test_managed_input_sounddevice_backend.py tests/test_managed_input_resident_adapter.py -q
```

Expected: all pass. Test the stated failure modes, not every mathematically possible state permutation.

Commit:

```powershell
git add app/service/analysis/resident_audio_mvp/recovery.py app/service/analysis/resident_audio_mvp/pool.py app/service/analysis/resident_audio_mvp/stream.py app/service/analysis/resident_audio_mvp/contracts.py app/service/managed_input_endpoint_resolver.py app/service/managed_input_contracts.py app/service/managed_input_sounddevice_backend.py app/service/managed_input_resident_adapter.py tests/resident_audio_mvp/test_recovery.py tests/resident_audio_mvp/test_cancel_fault_shutdown.py tests/test_managed_input_endpoint_resolver.py tests/test_managed_input_sounddevice_backend.py tests/test_managed_input_resident_adapter.py
git commit -m "feat: recover complete resident generations"
```

## Task 4: Make managed Resident reserve two-phase and generation-pinned

**Files:**
- Create: `tests/multi_run_coordinator_mvp/test_resident_recovery_continuation.py`
- Modify: `app/service/multi_run_coordinator_mvp/contracts.py`
- Modify: `app/service/multi_run_coordinator_mvp/ports.py`
- Modify: `app/service/multi_run_coordinator_mvp/__init__.py`
- Modify: `app/service/multi_run_coordinator_mvp/managed_resident_capture_port.py`
- Modify: `tests/multi_run_coordinator_mvp/test_managed_resident_capture_port.py`

- [ ] Add `begin_reserve(run_id, hardware_id, trigger_ns, deadline_ns)` and authenticated `resume_reserve(claim)` internal contracts. Results are immediate reserved/failure or pending; a continuation is not a second public reserve.
- [ ] Under the port lock check lifecycle, route, duplicate run, and existing capacity six, then create one capacity-owning claim. Release the lock before Pool readiness/reserve. Cancellation, close, success, failure, and continuation exception release capacity exactly once.
- [ ] On continuous readiness create a binding pinned to that Pool generation. Do not map the trigger here. Existing `start_capture()` still maps it only after durable ACTIVE, preserving EXPIRED/TIMELINE_GAP/future-invalid terminal behavior.
- [ ] Map replacement/recovery/endpoint/orphan failures to the existing `RESIDENT_UNAVAILABLE` category with precise detail. Lifecycle cancellation lets existing coordinator close/cancel ownership win.
- [ ] Test immediate, pending/resume, replacement, recovery failure, capacity, duplicate run, cancellation/close, forged claim, and—most importantly—prove a five-second Pool wait occurs outside the port lock while another hardware reserve/cancel enters.

Verify:

```powershell
python -m pytest tests/multi_run_coordinator_mvp/test_resident_recovery_continuation.py tests/multi_run_coordinator_mvp/test_managed_resident_capture_port.py -q
```

Commit:

```powershell
git add app/service/multi_run_coordinator_mvp/contracts.py app/service/multi_run_coordinator_mvp/ports.py app/service/multi_run_coordinator_mvp/__init__.py app/service/multi_run_coordinator_mvp/managed_resident_capture_port.py tests/multi_run_coordinator_mvp/test_resident_recovery_continuation.py tests/multi_run_coordinator_mvp/test_managed_resident_capture_port.py
git commit -m "feat: add two-phase resident reserve"
```

## Task 5: Park recovery waits without consuming captured-start workers

**Files:**
- Create: `app/service/captured_start_dispatcher.py`
- Create: `tests/test_captured_start_dispatcher_core.py`
- Modify: `app/service/multi_run_integration/captured_start_dispatcher.py`
- Modify: `app/service/multi_run_coordinator_mvp/contracts.py`
- Modify: `app/service/multi_run_coordinator_mvp/coordinator.py`
- Modify: `app/services_container.py`
- Modify: `tests/multi_run_coordinator_mvp/test_resident_recovery_continuation.py`
- Modify: `tests/multi_run_coordinator_mvp/test_initial_runs.py`
- Modify: `tests/multi_run_coordinator_mvp/test_lifecycle.py`

- [ ] Extract the dispatcher into a Qt-independent module while retaining the old import path as a re-export. Ordinary `Callable[[], None]` remains compatible.
- [ ] Add `COMPLETE`/`PARKED` work disposition. A parked item keeps its admission permit (so the third start is still rejected at capacity two) but releases the worker. Future settlement or deadline requeues the authenticated continuation on the same lane without acquiring another permit. Completion/cancel/close releases it exactly once.
- [ ] Add only one coordinator continuation point around Resident reserve. Initial work freezes/registers once, begins reserve, stores the claim, leaves no external action in flight, and parks. Resume runs on a captured-start worker, reconciles cancel/shutdown first, resumes the same claim, and then rejoins the unchanged ACTIVE/register/start pipeline.
- [ ] `ServiceContainer` returns the coordinator disposition from the captured-start work closure rather than discarding it. Request construction, lane keys, PLC/button entry, and rejection signaling remain unchanged.
- [ ] Test one parked hardware plus one healthy hardware, same-lane resume, two permits/two workers, third admission rejection, close/resume race, cancel/shutdown priority, and original trigger preservation. Avoid exhaustive race permutations after exactly-once ownership is demonstrated in both orderings for the critical close/resume case.

Verify:

```powershell
python -m pytest tests/test_captured_start_dispatcher_core.py tests/multi_run_coordinator_mvp/test_resident_recovery_continuation.py tests/multi_run_coordinator_mvp/test_initial_runs.py tests/multi_run_coordinator_mvp/test_lifecycle.py -q
```

Commit:

```powershell
git add app/service/captured_start_dispatcher.py app/service/multi_run_integration/captured_start_dispatcher.py app/service/multi_run_coordinator_mvp/contracts.py app/service/multi_run_coordinator_mvp/coordinator.py app/services_container.py tests/test_captured_start_dispatcher_core.py tests/multi_run_coordinator_mvp/test_resident_recovery_continuation.py tests/multi_run_coordinator_mvp/test_initial_runs.py tests/multi_run_coordinator_mvp/test_lifecycle.py
git commit -m "feat: park captured starts during resident recovery"
```

## Task 6: Remove production dual ownership and isolate live display

**Files:**
- Create: `tests/multi_run_integration/test_resident_recovery_production.py`
- Modify: `app/service/multi_run_integration/production_composition.py`
- Modify: `tests/multi_run_integration/test_production_composition.py`
- Modify: `tests/multi_run_integration/test_composition_and_ingress.py`

- [ ] Replace tests for `_LiveDisplayAudioBackend.capture_ready()` raw-handle rebuild with tests that production has exactly one lifecycle owner and real backend `on_fault` reaches Pool.
- [ ] Delete the outer heartbeat/generation/handle registry and its rebuild wrapper. Pass the passive managed SoundDevice backend and recovery resolver directly into the managed Resident composition.
- [ ] Feed live display only from the Resident drain through a fixed four-block best-effort queue. Full queue drops the oldest display block and records a diagnostic; it cannot delay callback heartbeat, Ring, or capture owners. Display failure does not fault the generation.
- [ ] Test PLC/manual entry behavior, unchanged stream/config mapping, callback-to-drain-to-display thread boundary, slow display, backend fault, shutdown, and no reuse of a poisoned raw handle.

Verify on a Qt-capable environment:

```powershell
python -m pytest tests/multi_run_integration/test_resident_recovery_production.py tests/multi_run_integration/test_production_composition.py tests/multi_run_integration/test_composition_and_ingress.py tests/multi_run_integration/test_captured_start_dispatcher.py -q
```

On this host, record the exact Qt DLL collection failure; do not claim this command passed.

Commit:

```powershell
git add app/service/multi_run_integration/production_composition.py tests/multi_run_integration/test_resident_recovery_production.py tests/multi_run_integration/test_production_composition.py tests/multi_run_integration/test_composition_and_ingress.py
git commit -m "refactor: make pool sole resident lifecycle owner"
```

## Task 7: Remove masking checks and run targeted concurrency/compatibility gates

**Files:**
- Create: `tests/resident_audio_mvp/test_recovery_stress.py`
- Modify only as required: touched Resident/port modules and their focused tests
- Modify: `tests/multi_run_coordinator_mvp/test_composite_end_to_end.py`
- Modify: `tests/multi_run_integration/test_resident_recovery_production.py`
- Modify: `tests/multi_run_integration/test_stop_reuse.py`

- [ ] Audit only the touched lifecycle paths. Remove repeated exact-type/shape/state checks after an owning boundary has already produced a private trusted value. Do not remove configuration/device/callback memory/Ring/generation/resource/shutdown safety boundaries.
- [ ] Replace broad `except Exception: False/pass` in the touched paths with structured stage/reason conversion. The generic Chinese “设备未返回采样” text is valid only for actual first-sample timeout, not open/start/close/invariant failures.
- [ ] Run the approved six-stream 60-second callback workload. Under concurrent two-second analysis occupancy, slow display, slow PLC sink, and 100,000-row-equivalent history load, assert callback p99 <= 5 ms, max <= 10 ms, and normal ingress has no overflow. A separate deliberate drain stall over two seconds must fault explicitly as `ingress_overflow`.
- [ ] While analysis/display/reporting is busy, exercise healthy starts and assert Resident reserve p99 <= 100 ms/max <= 250 ms with original trigger cursors. One five-second parked recovery must not delay another healthy hardware's worker. Keep this test deterministic and measure the Resident segment only.
- [ ] Run compatibility fixtures for: PLC/manual timestamp preservation, existing `resident_unavailable` mapping, capacity 2/2/6, read-only existing schema, PCM16 WAV publication, result payload, and history projection. No database migration or UI/PLC/analysis behavior change is allowed.

Verify:

```powershell
python -m pytest tests/resident_audio_mvp tests/test_managed_input_endpoint_resolver.py tests/test_managed_input_resident_adapter.py tests/test_managed_input_sounddevice_backend.py tests/test_captured_start_dispatcher_core.py tests/multi_run_coordinator_mvp -q
python -m pytest tests/resident_audio_mvp/test_recovery_stress.py -q
python -m pytest tests/test_factory_mode_controller/test_pending_start_lifecycle.py::test_manual_start_uses_nonblocking_service_submission_when_available tests/test_factory_mode_controller/test_pending_start_lifecycle.py::test_plc_and_scanner_preserve_producer_timestamp_without_resampling tests/run_persistence/test_schema.py::test_schema_validation_is_read_only_and_preserves_rows_and_file_bytes tests/run_persistence/test_production_run_audio_adapter.py::test_captured_publication_is_pcm16_no_replace_and_commits_one_audio_row tests/analysis_scheduler/test_production_executor.py::test_executor_success_payload_has_exact_closed_judgment_result_root -q
```

Expected: all available non-Qt tests pass with recorded counts and timing. No theoretical test is added unless it protects an ownership or compatibility failure reachable in this implementation.

Commit:

```powershell
git add app/service/analysis/resident_audio_mvp app/service/multi_run_coordinator_mvp/managed_resident_capture_port.py tests/resident_audio_mvp/test_recovery_stress.py tests/resident_audio_mvp/test_recovery_contracts.py tests/resident_audio_mvp/test_stress_and_handoff.py tests/multi_run_coordinator_mvp/test_composite_end_to_end.py tests/multi_run_integration/test_resident_recovery_production.py tests/multi_run_integration/test_stop_reuse.py
git commit -m "test: prove resident recovery isolation"
```

## Task 8: Final scope review and completion evidence

- [ ] Run `git diff --check 882ecaf..HEAD`, inspect the complete diff, and confirm no unrelated database/query/UI/PLC/analysis changes, no imports from the large alternate Resident, and no global PortAudio terminate recovery.
- [ ] Run the Task 7 non-Qt suite and stress test again. Run the Task 6 Qt suite on a working environment; if unavailable, report it explicitly as environment-blocked rather than passed.
- [ ] Use the `code-review` skill for one spec-compliance gate and one engineering-quality gate over `882ecaf..HEAD`. Apply only findings that are relevant, reachable, and proportionate to the approved behavior; document rejected theoretical findings instead of expanding scope.
- [ ] Use `verification-before-completion` before claiming success. Report exact tests, measured callback/reserve latency, recovery outcomes, preserved compatibility, and any remaining environment/hardware validation.
- [ ] Recommended field check when the real PLC/hardware rig is available: alternate PLC/manual starts while analysis/display/reporting is active, inject one software handle fault, confirm current trigger is not shifted, confirm next trigger succeeds without restarting, and retain generation/continuity diagnostics. This is not falsely marked passed when the rig is unavailable.
