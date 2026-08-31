# Two-Stage Analysis Latency Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the authoritative production PASS/FAIL decision within 0.5 seconds of exact audio freeze and complete compatible charts within 1.5 seconds of the capture-quiet epoch without changing recording bytes, business conclusions, history/detail/export formats, or PLC workflow.

**Architecture:** Split the current monolithic isolated-process execution into a small Decision response and a capture-gated Enrichment response. Keep immutable, identity-bound DSP intermediates inside the child slot; durably commit the Decision first, project it idempotently to UI/PLC, then atomically replace the lightweight result artifact with the full compatible payload when Enrichment finishes. Prewarm every selected AI toolchain in every analysis slot before production admission and measure each boundary through a shown Qt paint acknowledgement.

**Tech Stack:** Python 3.12, PyQt6, NumPy/SciPy, multiprocessing spawn/Pipe, SQLite/Peewee, pytest/pytest-qt, zlib/JSON, existing Resident AI stdio protocol.

---

## Approved inputs

- Spec: `docs/double-sdd/specs/2026-08-31-two-stage-analysis-latency-design.md`
- Baseline commit: the implementation worktree must be created from the commit containing the approved spec.
- Hard SLA host: Windows 11, AMD Ryzen 5 5600GT (6C/12T), 31 GB RAM, NVMe SSD.
- Normal-path SLA: authoritative Decision/UI paint/PLC success within 0.5 s per frozen recording, including while another recording is active; Enrichment is suspended during capture and the complete eligible backlog finishes within 1.5 s after the final capture freeze.
- Correctness gate: exact WAV bytes/format/frame/channel identity; exact judgment, AI pass/label, and every limit conclusion; numeric tolerances from spec §11.2.

## File structure and responsibilities

### New files

- `app/service/analysis/staged_execution.py` — exact staged contracts, immutable Decision envelope, bundle identity/token, Enrichment report, timing fields, byte-budget validation, and sink protocols.
- `app/service/analysis/staged_result_payload.py` — build/validate the lightweight compatible payload and validate that Enrichment keeps the committed judgment/summary identities.
- `app/service/analysis/performance_diagnostics.py` — monotonic stage timer and bounded diagnostic events; no raw audio/payload logging.
- `tests/analysis_scheduler/test_staged_execution_contracts.py` — closed contract and memory-budget tests.
- `tests/analysis_scheduler/test_staged_production_executor.py` — Decision/Enrichment split, accuracy, no duplicate DSP, and lifecycle tests.
- `tests/analysis_scheduler/test_staged_process_executor.py` — child protocol, slot-local bundle store, prewarm, capture gating, backlog, and cleanup tests.
- `tests/run_persistence/test_analysis_enrichment.py` — durable Decision and guarded artifact replacement tests.
- `tests/multi_run_integration/test_two_stage_analysis_projection.py` — coordinator/PLC/history/UI ordering and idempotency tests.
- `tests/performance/test_analysis_latency_budget.py` — opt-in real performance benchmark with machine fingerprint and Qt paint endpoint.
- `scripts/benchmark_two_stage_analysis.py` — reproducible production-shaped benchmark and JSON/console stage report.

### Existing files to modify

- `app/service/analysis/production_executor.py` — separate Decision from Enrichment and return reusable intermediates.
- `app/service/analysis/judgment.py` — expose judgment-only work and accept precomputed channel features.
- `app/service/analysis/dsp.py` — profile-proven batching/vectorization only.
- `app/service/analysis/review_payloads.py` — consume precomputed features and build charts once.
- `app/service/analysis/stft_score.py` — reuse the Decision STFT primitives; preserve full resolution.
- `app/service/analysis/process_executor.py` — staged Pipe protocol, per-slot bundle registry, capture-aware enrichment dispatcher, compressed full transport, and exact cleanup.
- `app/service/ai_resident_runtime.py` — explicit idempotent prewarm/readiness API.
- `app/service/analysis/scheduler/models.py` and `ports.py` — small Decision result and enrichment sink types without putting the full payload through `FrozenResultObject`.
- `app/service/multi_run_integration/production_prerequisites.py` — freeze selected AI toolchains and require every slot ready before production admission.
- `app/service/run_persistence/attempt_repository.py` — publish lightweight Decision artifact, then atomically replace only the same successful attempt with a full artifact.
- `app/service/run_persistence/run_persistence_scheduler_bridge.py` — durable Decision commit point and guarded Enrichment intake.
- `app/service/multi_run_coordinator_mvp/contracts.py` and `coordinator.py` — same-attempt non-terminal enrichment projection and stale fencing.
- `app/service/multi_run_integration/production_composition.py` — wire prewarm, enrichment sink, quiet epochs, and idempotent PLC/history delivery.
- `app/service/multi_run_integration/history_qt_adapter.py` — accept same-revision chart enrichment without creating another terminal card.
- `app/service/plc_result_sequencer.py` — verify/extend attempt-revision idempotency only if current tests prove it is insufficient.
- `ui/modules/result.py` — emit a test-only paint acknowledgement after the authoritative summary is painted.
- `ui/modules/result_dashboard.py` and chart modules (`stft.py`, `spectrum.py`, `octave.py`, `loudness.py`) — preserve summary with empty charts and refresh only matching enrichment.
- Existing tests beside every modified module — regression coverage.
- `scripts/benchmark_analysis_isolation.py` — keep old baseline usable and point users to the staged benchmark.

## Task 1: Closed staged contracts and diagnostic clock

**Files:**
- Create: `app/service/analysis/staged_execution.py`
- Create: `app/service/analysis/staged_result_payload.py`
- Create: `app/service/analysis/performance_diagnostics.py`
- Create: `tests/analysis_scheduler/test_staged_execution_contracts.py`
- Modify: `app/service/analysis/scheduler/__init__.py`

- [ ] **Step 1: Write failing identity and byte-budget tests**

Cover exact types, non-empty identities, finite/non-negative timings, duplicate/unknown stage names, immutable tuple fields, token mismatch, and the hard bundle budget `4 * audio_bytes + 32 MiB`.

```python
def test_bundle_identity_rejects_wrong_content_revision():
    identity = make_identity(content_revision="sha256:one")
    token = AnalysisBundleToken(slot_id=1, bundle_id="bundle-1", identity=identity)
    with pytest.raises(ValueError, match="content revision"):
        token.require_identity(replace(identity, content_revision="sha256:two"))


def test_bundle_budget_is_four_audio_copies_plus_32_mib():
    budget = AnalysisBundleBudget(frame_count=390_285, channel_count=1, dtype_size=4)
    assert budget.max_bytes == 4 * 390_285 * 4 + 32 * 1024 * 1024
```

- [ ] **Step 2: Run the contract tests and verify RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/analysis_scheduler/test_staged_execution_contracts.py -q`

Expected: FAIL because staged contract modules do not exist.

- [ ] **Step 3: Implement the minimal closed contracts**

Use frozen/slots dataclasses and exact enums. The public shape must include:

```python
class AnalysisPhase(str, Enum):
    DECISION = "decision"
    ENRICHMENT = "enrichment"

@dataclass(frozen=True, slots=True)
class AnalysisBundleIdentity:
    run_id: str
    analysis_attempt_id: str
    content_revision: str
    algorithm_revision: str
    config_identity: str
    calibration_identity: str
    channel_mapping_identity: str
    window_start_seconds: float
    window_end_seconds: float

@dataclass(frozen=True, slots=True)
class AnalysisDecisionEnvelope:
    identity: AnalysisBundleIdentity
    judgment: str
    canonical_result_json: str
    summary_payload_json: str
    bundle_token: AnalysisBundleToken
    timings: AnalysisStageTimings

@dataclass(frozen=True, slots=True)
class AnalysisEnrichmentReport:
    identity: AnalysisBundleIdentity
    judgment: str
    canonical_result_json: str
    result_payload_zlib: bytes
    timings: AnalysisStageTimings
```

Keep the lightweight canonical root exactly `{"judgment": ..., "result": ...}` with the existing required result fields and `chart_payload={}`. Add a validator that full enrichment cannot change judgment, run/attempt identity, summary conclusion, channel conclusion, or input revision.

- [ ] **Step 4: Implement bounded timing diagnostics**

`StageTimer` must use injected `perf_counter_ns`, record only integer nanoseconds/byte counts/ids, reject negative deltas, and emit one bounded immutable event per named boundary. No log path may accept samples, numpy arrays, request JSON, or result JSON.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/analysis_scheduler/test_staged_execution_contracts.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the task checkpoint**

```powershell
git add app/service/analysis/staged_execution.py app/service/analysis/staged_result_payload.py app/service/analysis/performance_diagnostics.py app/service/analysis/scheduler/__init__.py tests/analysis_scheduler/test_staged_execution_contracts.py
git commit -m "tmp/sdd: define staged analysis contracts"
```

## Task 2: Decision/Enrichment DSP split with accuracy parity

**Files:**
- Create: `tests/analysis_scheduler/test_staged_production_executor.py`
- Modify: `app/service/analysis/production_executor.py`
- Modify: `app/service/analysis/judgment.py`
- Modify: `app/service/analysis/dsp.py`
- Modify: `app/service/analysis/review_payloads.py`
- Modify: `app/service/analysis/stft_score.py`
- Modify: `tests/analysis_scheduler/test_production_executor.py`

- [ ] **Step 1: Add a golden parity fixture and failing split tests**

Reuse the existing production executor job/audio factories. For one-channel 44.1 kHz, two-channel 48 kHz, near-limit, no-AI, and fake-Resident-AI cases assert:

- `execute_decision()` returns judgment, summary, channel conclusions, and a bundle, but no full chart matrix.
- `execute_enrichment(bundle)` returns the current full compatible payload.
- legacy monolithic result vs enriched result meets spec §11.2 tolerances and exact verdict/limit equality.
- a spy proves calibration, loudness trace, SPL trace, FFT/STFT primitives are not recomputed in Enrichment.
- arrays in the bundle are read-only and total `nbytes` is within budget.

```python
decision = executor.execute_decision(job, audio, token)
assert decision.judgment == legacy_judgment
assert json.loads(decision.canonical_result_json)["result"]["chart_payload"] == {}
enriched = executor.execute_enrichment(decision.bundle_token)
assert_conclusions_equal(enriched, legacy)
assert_chart_payload_close(enriched, legacy, tolerances=SPEC_TOLERANCES)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/analysis_scheduler/test_staged_production_executor.py tests/analysis_scheduler/test_production_executor.py -q`

Expected: FAIL because staged executor methods and reusable features do not exist.

- [ ] **Step 3: Extract typed per-channel intermediate results**

Introduce private, focused dataclasses in `production_executor.py` or a small adjacent module only if the file becomes unwieldy. Store calibrated window views and required derived arrays once; mark arrays read-only. Do not store a second full audio copy. Keep frozen config/calibration/mapping hashes in the bundle identity.

Use this explicit per-attempt bundle inventory and budget. Let `F` be analysis-window frames across all selected channels, `C` selected channels, `B = nfft // 2 + 1`, `T = 1 + floor((F/C - nfft) / hop)` when a complete STFT window exists, `S` the configured SPL trace sample count, `L` the loudness trace sample count, and `O` the octave center count:

| Stored array | dtype/shape | Maximum owning bytes |
|---|---|---:|
| source analysis-window view | read-only view of loader-owned audio | `0` additional |
| calibrated channel signals | float64, total `F` values | `8 * F` |
| SPL time/value traces | two float64 arrays per channel | `16 * S * C` |
| loudness time/value traces | two float64 arrays per channel | `16 * L * C` |
| FFT frequency axis + channel magnitudes | float64 | `8 * B + 8 * B * C` |
| STFT time/frequency axes | float64 | `8 * T + 8 * B` |
| STFT magnitude matrices reused by score/chart | float64, `C * B * T` | `8 * C * B * T` |
| octave centers + channel levels | float64 | `8 * O + 8 * O * C` |
| scalar conclusions/identity metadata | closed dataclasses/tuples | `<= 256 KiB` |

Temporary filter/percentile work arrays must be released before bundle publication and are not bundle-owned. The sum of actual `nbytes` plus a measured metadata allowance must satisfy both the table and the hard attempt cap `4 * audio_bytes + 32 MiB`; reject publication on either violation. Add a test that enumerates every owning ndarray field so a later unbudgeted array fails the contract test.

- [ ] **Step 4: Split judgment-only work from chart-only work**

Change `JudgmentEngine` so Decision computes every scalar/boolean needed for the authoritative conclusion and returns reusable channel features. `build_chart_payload()` and `_attach_stft_payload()` must accept those features and avoid re-running the same DSP. Keep the existing `execute()` as a compatibility wrapper used by legacy tests:

```python
def execute(self, job, audio, cancel_token):
    decision = self.execute_decision(job, audio, cancel_token)
    return self.execute_enrichment(decision.bundle_token)
```

- [ ] **Step 5: Batch only measured hotspots**

Profile before editing `dsp.py`/`stft_score.py`. Make one-variable changes to loudness windows, percentile spans, and filter batches. Preserve configured sample/window resolution. After each change run parity tests; revert any optimization that changes a discrete conclusion or exceeds tolerances.

- [ ] **Step 6: Move diagnostic JSON copies out of Decision**

Build the AI request in memory, call the already-Resident runtime, and queue pretty `request.json`/`response.json` writes for Enrichment. Preserve their eventual content and location. Decision must not perform these file writes.

- [ ] **Step 7: Run parity and legacy tests**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/analysis_scheduler/test_staged_production_executor.py tests/analysis_scheduler/test_production_executor.py tests/test_stft_score.py tests/test_review_payloads.py -q`

If a named file is absent, use `rg --files tests | rg "stft_score|review_payload"` and run the actual matching files; record the exact command in the task checkpoint.

Expected: PASS with exact conclusions and tolerance-compliant arrays.

- [ ] **Step 8: Commit the task checkpoint**

```powershell
git add app/service/analysis/production_executor.py app/service/analysis/judgment.py app/service/analysis/dsp.py app/service/analysis/review_payloads.py app/service/analysis/stft_score.py tests/analysis_scheduler/test_staged_production_executor.py tests/analysis_scheduler/test_production_executor.py
git commit -m "tmp/sdd: split decision and enrichment DSP"
```

## Task 3: Resident prewarm and staged isolated-process protocol

**Files:**
- Create: `tests/analysis_scheduler/test_staged_process_executor.py`
- Modify: `app/service/ai_resident_runtime.py`
- Modify: `app/service/analysis/process_executor.py`
- Modify: `app/service/analysis/scheduler/models.py`
- Modify: `app/service/analysis/scheduler/ports.py`
- Modify: `app/service/analysis/scheduler/__init__.py`
- Modify: `app/service/multi_run_integration/production_prerequisites.py`
- Modify: `tests/test_ai_resident_runtime.py`
- Modify: `tests/analysis_scheduler/test_process_executor_transport.py`
- Modify: `tests/multi_run_integration/test_production_prerequisites.py`

- [ ] **Step 1: Write failing Resident prewarm tests**

Assert `prewarm(toolchain snapshot)` is idempotent, creates one cached process without a business analysis request, verifies readiness, invalidates on exit/revision change, and never silently substitutes a revision. Also prove that unexpected Resident exit immediately revokes `READY(revision)`, schedules exactly one background restart/readiness handshake, and republishes readiness only after the handshake succeeds.

- [ ] **Step 2: Write failing child protocol and bundle-store tests**

Protocol commands must be closed tuples/messages: `warm`, `decision`, `enrich`, `discard`, `stop`. Cover:

- Decision response returns small canonical JSON plus opaque token, not full chart chunks.
- Multiple Decision bundles can reside in one child while the slot handles later decisions.
- Enrichment is routed to the owning slot/token and only runs while `capture_active()` is false plus the 250 ms quiet fence.
- capture becoming active stops new Enrichment admission but does not block Decision.
- six bundles respect the byte cap and are released on success, cancel, retry exhaustion, child death, and shutdown.
- compressed Enrichment transport crosses once; parent does not reconstruct a million-node `FrozenResultObject`.
- if Resident exits after audio freeze but before Decision commit, that attempt fails with the existing execution-failed semantics and never switches to another revision; only later, newly admitted recordings may use the restarted ready instance.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/test_ai_resident_runtime.py tests/analysis_scheduler/test_staged_process_executor.py tests/analysis_scheduler/test_process_executor_transport.py -q`

Expected: new tests fail.

- [ ] **Step 4: Add explicit Resident readiness**

Add an idempotent `prewarm()`/`readiness()` API that uses the same cache key `(toolchain_id, execution_revision, entrypoint, package_dir)`. Do not fake a normal request id or create AI execution rows. Reader thread/process cleanup must remain the existing authority. A process-exit callback/health poll revokes readiness and coalesces one background restart per key; the restart performs the same handshake before republishing ready. An in-flight frozen attempt keeps its exact snapshot and fails if that instance dies—it cannot attach to the restarted instance or a different revision.

- [ ] **Step 5: Replace single execute response with a staged child loop**

Child-side pseudocode:

```python
bundles: dict[str, _StoredBundle] = {}
while True:
    command = connection.recv()
    if command.kind == "decision":
        decision, bundle = executor.execute_decision(...)
        bundles[bundle.token.bundle_id] = bounded(bundle)
        connection.send(("decision", pack_small(decision)))
    elif command.kind == "enrich":
        report = executor.execute_enrichment(require_owned(command.token, bundles))
        connection.send(("enrichment", pack_compressed(report)))
        release(command.token)
```

Parent slots need a condition/busy flag rather than a queue that cannot acquire a specific owner slot. Record `attempt_id -> (slot, token)`. A dedicated bounded enrichment dispatcher selects only quiet-eligible tokens and calls an injected `AnalysisEnrichmentSink`.

- [ ] **Step 6: Preserve Decision priority during active capture**

Remove `_wait_for_capture_quiet()` from the Decision send/receive path. Keep process/thread below-normal priority and single-threaded numerical backends. Apply the existing 250 ms fence only before Enrichment compute/large transport/fsync projection.

- [ ] **Step 7: Freeze and prewarm selected bindings before admission**

Extend prepared prerequisites with the unique selected `AiToolchainSnapshot`s. `build_analysis_executor()` passes them to every slot; `start()` returns ready only after DSP and every selected revision are warm. On revision change, new production activation cannot freeze the new binding until all slots report ready. Existing frozen attempts keep their revision.

Wire readiness revocation back to the production prerequisite gate. While background restart is in progress, reject/hold only new starts needing that revision using the existing prerequisite-not-ready path; do not stop healthy recordings or rewrite already-frozen attempts.

- [ ] **Step 8: Run protocol, prewarm, and prerequisite tests**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/test_ai_resident_runtime.py tests/analysis_scheduler/test_staged_process_executor.py tests/analysis_scheduler/test_process_executor_transport.py tests/multi_run_integration/test_production_prerequisites.py -q`

Expected: PASS; no steady Decision spawns an AI process.

- [ ] **Step 9: Commit the task checkpoint**

```powershell
git add app/service/ai_resident_runtime.py app/service/analysis/process_executor.py app/service/analysis/scheduler app/service/multi_run_integration/production_prerequisites.py tests/test_ai_resident_runtime.py tests/analysis_scheduler/test_staged_process_executor.py tests/analysis_scheduler/test_process_executor_transport.py tests/multi_run_integration/test_production_prerequisites.py
git commit -m "tmp/sdd: stage isolated analysis and prewarm AI"
```

## Task 4: Durable authoritative Decision and atomic Enrichment replacement

**Files:**
- Create: `tests/run_persistence/test_analysis_enrichment.py`
- Modify: `app/service/run_persistence/attempt_repository.py`
- Modify: `app/service/run_persistence/run_persistence_scheduler_bridge.py`
- Modify: `app/service/run_persistence/analysis_result_artifact.py`
- Modify: `app/service/run_persistence/__init__.py`
- Modify: `tests/run_persistence/test_scheduler_bridge_contracts.py`
- Modify: `tests/run_persistence/test_scheduler_bridge_lifecycle.py`
- Modify: `tests/run_persistence/test_scheduler_bridge_end_to_end.py`

- [ ] **Step 1: Write failing lightweight terminal tests**

Assert the Decision payload is canonical, has the current root topology and required summary/channel fields, and uses an empty existing `chart_payload`. A successful attempt must commit once and return the same terminal report on duplicate identical outcome; conflicting judgment with the same attempt id must be rejected.

- [ ] **Step 2: Write failing artifact replacement tests**

Cover:

- replacement only when attempt state is `SUCCEEDED`, attempt/run/content identity and judgment match, and current stored payload is the expected lightweight revision;
- publish full compressed artifact first, atomically update the DB reference in a short transaction, then settle/remove the old lightweight artifact;
- DB rollback removes the unreferenced new artifact and retains the old one;
- duplicate same digest is idempotent; stale/late/canceled/reanalysis-mismatched reports are ignored;
- readers see either the old valid lightweight payload or new complete payload, never a broken reference.

- [ ] **Step 3: Run persistence tests and verify RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/run_persistence/test_analysis_enrichment.py tests/run_persistence/test_scheduler_bridge_contracts.py tests/run_persistence/test_scheduler_bridge_lifecycle.py tests/run_persistence/test_scheduler_bridge_end_to_end.py -q`

Expected: new replacement API is missing.

- [ ] **Step 4: Implement guarded repository replacement**

Add a method with an explicit expected lightweight content revision/hash; do not broaden the existing terminal state transition:

```python
def replace_succeeded_result(
    self, *, attempt_id: str, run_id: str, judgment: Judgment,
    expected_payload_revision: str, result_payload_json: str,
    result_payload_zlib: bytes, now: str,
) -> TransactionResult[bool]: ...
```

Artifact publication remains atomic (`temporary -> fsync -> replace`). The SQL update must include attempt id, run id, state `SUCCEEDED`, judgment, and expected current reference in its WHERE clause.

- [ ] **Step 5: Add bridge Enrichment intake without a second terminal**

`AnalysisPersistenceBoundary.accept_enrichment(report)` validates the full canonical payload at the persistence boundary, calls guarded replacement, and emits an immutable enrichment-committed event. It must not call `finish_running_and_project`, change terminal time, create another PLC action, or resurrect a retired context.

- [ ] **Step 6: Run persistence suites**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/run_persistence/test_analysis_enrichment.py tests/run_persistence/test_scheduler_bridge_contracts.py tests/run_persistence/test_scheduler_bridge_lifecycle.py tests/run_persistence/test_scheduler_bridge_end_to_end.py tests/run_persistence/test_analysis_result_artifact.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the task checkpoint**

```powershell
git add app/service/run_persistence tests/run_persistence
git commit -m "tmp/sdd: persist decision then replace enrichment artifact"
```

## Task 5: Coordinator, PLC, history, and UI staged projection

**Files:**
- Create: `tests/multi_run_integration/test_two_stage_analysis_projection.py`
- Modify: `app/service/multi_run_coordinator_mvp/contracts.py`
- Modify: `app/service/multi_run_coordinator_mvp/coordinator.py`
- Modify: `app/service/multi_run_integration/history_qt_adapter.py`
- Modify: `app/service/multi_run_integration/production_composition.py`
- Modify: `app/service/plc_result_sequencer.py` only if required by a failing idempotency test
- Modify: `ui/modules/result.py`
- Modify: `ui/modules/result_dashboard.py`
- Modify: `ui/modules/stft.py`
- Modify: `ui/modules/spectrum.py`
- Modify: `ui/modules/octave.py`
- Modify: `ui/modules/loudness.py`
- Modify: `tests/multi_run_integration/test_history_qt_adapter.py`
- Modify: `tests/test_result_module.py`
- Modify: `tests/test_result_dashboard_module.py`
- Modify: `tests/test_result_dashboard_history_projection.py`

- [ ] **Step 1: Write failing commit-order and idempotency tests**

For key `(run_id, attempt_id, terminal_revision, "decision/v1")` prove:

1. durable Decision commit happens before UI/history and PLC publication;
2. identical duplicate publication is ignored/read back; conflicting duplicate is rejected;
3. UI projection failure can be reconstructed from durable history without changing judgment;
4. transient PLC failure uses the existing sequencer retry and never emits a different or duplicate conclusion;
5. Enrichment updates only the same run/attempt/revision and emits no PLC write or terminal history fact.

- [ ] **Step 2: Write failing UI pending/enrichment tests**

Show the authoritative badge and summary with `chart_payload={}`; chart modules retain empty/loading presentation. A matching enrichment refreshes charts without changing the card result or selection. A stale enrichment after reanalysis/selection change is ignored.

Add a `decisionPainted` Qt signal emitted only after the widget has painted the new decision revision:

```python
def paintEvent(self, event):
    super().paintEvent(event)
    if self._pending_paint_revision is not None:
        revision = self._pending_paint_revision
        self._pending_paint_revision = None
        self.decisionPainted.emit(revision, time.perf_counter_ns())
```

- [ ] **Step 3: Run integration/UI tests and verify RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/multi_run_integration/test_two_stage_analysis_projection.py tests/multi_run_integration/test_history_qt_adapter.py tests/test_result_module.py tests/test_result_dashboard_module.py tests/test_result_dashboard_history_projection.py -q`

Expected: staged projection/event types are missing.

- [ ] **Step 4: Add a non-terminal enrichment fact/projection**

The coordinator keeps the existing terminal Decision exactly once. Add a revision-fenced enrichment projection that replaces only `result_payload`/review charts for an already-successful attempt. It cannot alter judgment, phase owner, terminal time, PLC state, manual judgment, or reanalysis ownership.

- [ ] **Step 5: Wire durable-first publication**

In production composition, register the ProcessExecutor Enrichment sink with the persistence boundary. On Decision terminal report: commit DB, then publish history/UI and PLC in existing ordered action machinery. On Enrichment commit: publish only history/UI refresh. Do not claim cross-system atomicity.

- [ ] **Step 6: Preserve pending detail/export behavior**

History/detail readers accept the lightweight valid payload and show summary with empty charts. Existing export action waits on the attempt's enrichment future within its existing timeout and otherwise follows the current artifact-unavailable/export-failed path; never create a partial export format.

- [ ] **Step 7: Run integration/UI suites**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/multi_run_integration/test_two_stage_analysis_projection.py tests/multi_run_integration/test_history_qt_adapter.py tests/multi_run_integration/test_production_composition.py tests/test_result_module.py tests/test_result_dashboard_module.py tests/test_result_dashboard_history_projection.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the task checkpoint**

```powershell
git add app/service/multi_run_coordinator_mvp app/service/multi_run_integration app/service/plc_result_sequencer.py ui/modules tests/multi_run_integration tests/test_result_module.py tests/test_result_dashboard_module.py tests/test_result_dashboard_history_projection.py
git commit -m "tmp/sdd: project decision before chart enrichment"
```

## Task 6: Capture-aware backlog scheduling and recording-integrity gate

**Files:**
- Modify: `app/service/analysis/process_executor.py`
- Modify: `app/service/multi_run_integration/production_composition.py`
- Modify: `tests/analysis_scheduler/test_staged_process_executor.py`
- Modify: `tests/multi_run_integration/test_six_run_pipeline.py`
- Modify: `tests/multi_run_integration/test_failure_matrix.py`
- Modify: `tests/resident_audio_mvp/test_stress_and_handoff.py`
- Modify: `tests/test_managed_input_resident_adapter.py`

- [ ] **Step 1: Write a deterministic six-run quiet-epoch test**

Use fake monotonic clock/capture authority. Freeze six attempts while capture remains active; Decision for each must remain admissible. At the 1->0 capture transition snapshot every `audio_frozen && !charts_visible` attempt, including an attempt whose Decision is still completing. Verify new Enrichment admission waits for the full 250 ms fence and every eligible attempt is scheduled exactly once.

Add four in-flight re-gating cases: capture becomes active (a) after Enrichment compute but before compressed child transport, (b) after transport but before parent decode/validation, (c) before artifact publication/fsync, and (d) before Qt chart projection. Each case must stop at that safe checkpoint, retain one bounded token/result without duplicate DSP, and resume only after the next 250 ms quiet fence.

- [ ] **Step 2: Write recording-integrity stress assertions**

While Decision workers are saturated and Enrichment repeatedly pauses/resumes, compare retained sample arrays/WAV bytes with the no-analysis baseline. Assert exact frame count, sample rate, dtype, channel map, content revision, no Ring overflow/gap, and no analysis/persistence call on callback/Resident drain threads.

- [ ] **Step 3: Run stress tests and verify RED or expose gaps**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/analysis_scheduler/test_staged_process_executor.py tests/multi_run_integration/test_six_run_pipeline.py tests/multi_run_integration/test_failure_matrix.py tests/resident_audio_mvp/test_stress_and_handoff.py tests/test_managed_input_resident_adapter.py -q`

- [ ] **Step 4: Implement the exact eligible-backlog state machine**

Maintain bounded maps keyed by attempt id with `audio_frozen`, `decision_committed`, `charts_visible`, owner slot/token, retry count, and quiet epoch id. The 1->0 transition scans state, not just the queue. A 0->1 transition closes new Enrichment admission immediately. Exactly one retry is allowed after a committed Decision.

Expose a shared `require_quiet_epoch(epoch_id, checkpoint)` guard and call it before each expensive/observable boundary: child compressed transport, parent receive/decode/validation, artifact publish/fsync, and UI chart projection. If the epoch changed, park the already-produced bounded value at its current owner and resume from that checkpoint; never rerun completed DSP or leak a stale chart event.

- [ ] **Step 5: Verify isolation under pressure**

The callback/Resident tests are a hard gate. If the 0.5-second Decision implementation causes any recording regression, fix scheduling/priority/copies; do not loosen assertions or move work into callback.

- [ ] **Step 6: Run the focused stress matrix again**

Run the Step 3 command unchanged.

Expected: PASS with exact recording data and bounded bundle memory.

- [ ] **Step 7: Commit the task checkpoint**

```powershell
git add app/service/analysis/process_executor.py app/service/multi_run_integration/production_composition.py tests/analysis_scheduler/test_staged_process_executor.py tests/multi_run_integration/test_six_run_pipeline.py tests/multi_run_integration/test_failure_matrix.py tests/resident_audio_mvp/test_stress_and_handoff.py tests/test_managed_input_resident_adapter.py
git commit -m "tmp/sdd: gate enrichment behind capture quiet"
```

## Task 7: Reproducible SLA benchmark and final profile-driven tuning

**Files:**
- Create: `scripts/benchmark_two_stage_analysis.py`
- Create: `tests/performance/test_analysis_latency_budget.py`
- Modify: `scripts/benchmark_analysis_isolation.py`
- Modify: `app/service/analysis/performance_diagnostics.py`
- Modify: only files proven hot by the new stage profile

- [ ] **Step 1: Write benchmark output/host-validation tests**

The script must emit machine-readable JSON plus a concise console summary containing:

- hardware/OS/Python/dependency fingerprint and power mode when available;
- input duration/rate/channels/config/toolchain revision;
- audio frozen, Decision scheduling, DSP, AI, DB commit, PLC success, Qt paint, 250 ms fence, Enrichment DSP, canonical JSON bytes, compressed bytes, transport, fsync, chart paint;
- p50/p95/max over at least 30 warm iterations;
- bundle peak bytes and whether a steady Decision spawned an AI process.

- [ ] **Step 2: Build the shown-widget Qt endpoint**

The hard benchmark must show a non-minimized result window on the named host and wait for `decisionPainted`. Offscreen/hidden mode is allowed only for functional CI and must label the SLA as not evaluated.

- [ ] **Step 3: Run the new benchmark before final tuning**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe scripts\benchmark_two_stage_analysis.py --iterations 30 --audio data\run_audio\右侧_20260831_161050_no-sn.wav --json-output build\analysis-latency.json`

Expected at this step: an honest stage report; it may still fail the target.

- [ ] **Step 4: Tune one measured bottleneck at a time**

Follow `systematic-debugging`: state one hypothesis, make the smallest change, rerun parity/recording tests, then rerun the benchmark. Do not bundle unrelated DSP, transport, UI, or I/O changes. Stop and reassess architecture after three failed distinct fixes.

- [ ] **Step 5: Verify the single-run SLA**

On the named host, 30/30 warm single-channel iterations must satisfy:

- `max(audio_frozen -> max(Qt painted, PLC success)) <= 0.500 s`;
- `max(audio_frozen -> charts painted/full artifact readable) <= 1.500 s`;
- no steady AI process start;
- exact judgment/limit equality and numeric tolerance parity.

- [ ] **Step 6: Verify the six-run capture/quiet SLA**

Run the benchmark's six-run overlap scenario. Each Decision must meet 0.5 s without recording mismatch. The 250 ms fence begins at final freeze, and every eligible chart/artifact must be complete by final freeze + 1.5 s.

- [ ] **Step 7: Verify the required two-channel profile**

Run the 8-second, 48 kHz, two-channel production-shaped scenario for at least 30 warm iterations and record Decision/charts p50, p95, and max plus every stage field. This scenario is required reporting evidence even if the hard single-channel threshold already passes.

- [ ] **Step 8: Run focused performance tests**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest tests/performance/test_analysis_latency_budget.py -q -m performance`

Expected: PASS on the named SLA host; other hosts report SKIP with fingerprint rather than false PASS.

- [ ] **Step 9: Commit the task checkpoint**

```powershell
git add scripts/benchmark_two_stage_analysis.py scripts/benchmark_analysis_isolation.py tests/performance/test_analysis_latency_budget.py app/service/analysis/performance_diagnostics.py
git add -u app/service/analysis app/service/run_persistence app/service/multi_run_integration ui/modules
git commit -m "tmp/sdd: verify staged analysis latency budgets"
```

## Task 8: Full regression, artifact inspection, and documentation closeout

**Files:**
- Modify: `README.md` only if the benchmark command belongs in developer documentation
- Modify: `docs/double-sdd/plans/2026-08-31-two-stage-analysis-latency.md` checkbox state during execution
- No unrelated source edits

- [ ] **Step 1: Run the complete automated suite**

Run: `$env:PYTHONPATH=(Get-Location).Path; .\.venv\Scripts\python.exe -m pytest -q`

Expected: all tests PASS. Record any environment-only skips explicitly.

- [ ] **Step 2: Run fresh single and six-run benchmarks**

Repeat Task 7 Steps 5–8 after the full suite, including the 8-second/48 kHz/two-channel p50/p95/max report; do not reuse earlier output.

- [ ] **Step 3: Inspect compatibility artifacts**

Load at least one pre-change artifact and one new enriched artifact through the production reader. Open history/detail/export paths in tests and compare required topology, chart fields, resolution, judgment, channel mapping, and exported WAV/result contents.

Using the same real production WAV and the same synthetic near-threshold fixtures used for automatic analysis, run imported-audio analysis and manual rejudgment end to end. Assert their final compatible payloads, judgments, limit conclusions, chart resolution, history/detail access, and export behavior remain valid; record timings for information but do not apply the 0.5/1.5-second SLA to these two paths.

- [ ] **Step 4: Inspect repository state**

Run:

```powershell
git status --short
git diff --check HEAD~1..HEAD
git log --oneline --decorate -12
```

Confirm no user-owned `main.spec` change or untracked local tooling was committed.

- [ ] **Step 5: Commit final plan/README bookkeeping if changed**

```powershell
git add README.md docs/double-sdd/plans/2026-08-31-two-stage-analysis-latency.md
git commit -m "tmp/sdd: document staged analysis verification"
```

Skip this commit if neither tracked file changed.

- [ ] **Step 6: Hand off to full-range review**

Dispatch the required whole-range `quality-code-reviewer`, then use `finishing-a-development-branch`. Final integration must squash temporary `tmp/sdd` commits onto the recorded main base; do not preserve temporary commits with a normal merge.
