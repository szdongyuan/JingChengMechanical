# Session Capture Warmup Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every real-time test session discard its first 50ms of audio and current data, expose the first retained sample as 0s, and save a full configured post-warmup duration without changing imported audio, golden samples, or existing saved WAV semantics.

**Architecture:** Keep `AudioRecorder` as a shared raw source that assigns exact absolute sample spans at PortAudio callback receipt. Bind each `TestSession` to one `SessionCaptureWindow`, which owns the attachment, warmup, retained, progress, completion, and freeze boundaries and is the only fan-out to monitoring and live DSP. Timestamp current readings in the same monotonic domain, then filter and rebase them against the resolved audio attachment epoch before publishing or storing them.

**Tech Stack:** Python 3, NumPy, sounddevice/PortAudio, PyQt6 signals and threads, pytest, pytest-qt, unittest.mock.

---

## Execution Preconditions

- Execute this plan in a dedicated `.worktrees/...` worktree using the `subagent-driven-development` skill.
- Use a fresh implementer subagent for each task, followed by parallel spec-compliance and code-quality reviews with the spec gate first.
- Treat `docs/double-sdd/specs/2026-07-14-session-capture-warmup-design.md` as authoritative.
- Do not add a config key, database column, migration, old-file detector, or generic WAV-loader trim.
- Do not modify imported-audio or golden-sample production paths. They are regression-tested only.
- Before each pytest invocation in a fresh PowerShell, set:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTEST_QT_API = "pyqt6"
```

- Establish the worktree baseline before Task 1:

```powershell
python -m pytest tests/test_audio_recorder.py tests/test_test_session.py tests/test_current_meter_service.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_stop_behavior.py tests/test_test_manager/test_user_stop_partial_save.py tests/test_test_manager/test_signal_disconnection.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_review_replay.py tests/test_test_manager/test_imported_audio_analysis.py tests/test_golden_sample_service.py tests/test_golden_sample_import_dialog.py -q
```

Expected: PASS before feature edits. If baseline failures exist, report them and do not disguise them as feature regressions.

## File Map

- Create: `app/service/analysis/session_capture.py`
  - Own the single `SESSION_WARMUP_SECONDS` constant, duration-to-frame rounding, exact raw/retained block value objects, resolved attachment bounds, and the pure `SessionCaptureWindow` state machine.
- Create: `tests/test_session_capture.py`
  - Prove rounding, boundary slicing, rebasing, deduplication, exact target clipping, freeze behavior, and independent late attachments.
- Modify: `app/service/analysis/audio_recorder.py`
  - Assign callback-time absolute sample spans, map PortAudio stream time to monotonic time, resolve pending attachment owners, grow capacity, emit exact raw blocks, extract canonical retained ranges, and release owners.
- Modify: `tests/test_audio_recorder.py`
  - Replace emitted-boundary snapshot assumptions with exact callback-span and pending-owner tests, including in-block attachment, missing `time_info`, and the queued-final-block capacity race.
- Modify: `app/service/analysis/test_session.py`
  - Replace chunk-index elapsed estimation with `SessionCaptureWindow`; route retained blocks to monitor and live display; expose freeze/frame-count/current-epoch state.
- Modify: `tests/test_test_session.py`
  - Verify no warmup leakage, progress at zero during warmup, exact cross-boundary monitor/DSP data, rebased spans, completion, and canonical buffer projection.
- Modify: `app/service/analysis/test_manager.py`
  - Establish and release capture owners, reject stopped shared recorders, remove the monitor raw-block bypass, extract the same frozen canonical range for full/partial paths, and gate current samples.
- Modify: `app/service/current_meter_service.py`
  - Preserve the absolute monotonic completion timestamp for every reading and provide a pure filter/rebase helper.
- Modify: `tests/test_current_meter_service.py`
  - Verify timestamp capture, strict pre-epoch filtering, exact-epoch retention, rebasing, and no synthetic zero point.
- Modify: `tests/test_test_manager/_support.py`
  - Give shared test doubles exact block, attachment, capture extraction, and release behavior.
- Modify: `tests/test_test_manager/test_late_attach_buffer_slice.py`
  - Cover full post-warmup capture, block-internal attachment, tagged first sample, and independent late sessions.
- Modify: `tests/test_test_manager/test_signal_disconnection.py`
  - Assert the session window is the sole raw-recorder subscriber and monitor receives only retained data.
- Modify: `tests/test_test_manager/test_user_stop_partial_save.py`
  - Assert pre-warmup stops save nothing and post-warmup stops save only the canonical retained prefix.
- Modify: `tests/test_test_manager/test_stop_behavior.py`
  - Preserve the existing incomplete-analysis toggle/window-coverage rules on the rebased partial duration.
- Modify: `tests/test_test_manager/test_review_payload_emission.py`
  - Assert live and review current traces use the same gated/rebased set.
- Modify: `tests/test_test_manager/test_review_replay.py`
  - Assert review/replay selection starts at the canonical retained frame and performs no second trim.
- Modify mechanically as required by the final recorder API: `tests/test_test_manager/test_start_inputs_and_resolution.py`, `tests/test_test_manager/test_start_acceptance_and_failures.py`, `tests/test_test_manager/test_max_duration.py`, `tests/test_test_manager/test_sample_rate_mismatch.py`, `tests/test_test_manager/test_shared_recorder.py`
  - Update recorder/session test doubles without changing their existing behavioral assertions.
- Regression-only: `tests/test_test_manager/test_imported_audio_analysis.py`, `tests/test_imported_audio_analysis.py`, `tests/test_golden_sample_service.py`, `tests/test_golden_sample_import_dialog.py`
  - Confirm non-real-time paths are unchanged.

## Shared Contracts

Use these names and meanings across the tasks; do not create a second warmup constant or independently recompute boundaries in manager/save/replay code.

```python
SESSION_WARMUP_SECONDS = 0.05

def frames_for_duration(sample_rate: float, duration_sec: float) -> int:
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("sample_rate must be positive and finite")
    if not math.isfinite(duration_sec) or duration_sec < 0:
        raise ValueError("duration_sec must be non-negative and finite")
    return int(math.ceil(sample_rate * duration_sec))

@dataclass(frozen=True)
class CapturedAudioBlock:
    data: np.ndarray
    sample_start: int
    sample_end: int
    monotonic_start: float | None
    monotonic_end: float | None

@dataclass(frozen=True)
class ResolvedCaptureBounds:
    attach_sample: int
    attach_monotonic: float
    warmup_samples: int
    target_samples: int

    @property
    def retained_start(self) -> int:
        return self.attach_sample + self.warmup_samples

    @property
    def retained_end(self) -> int:
        return self.retained_start + self.target_samples

@dataclass(frozen=True)
class RetainedAudioBlock:
    data: np.ndarray
    sample_start: int  # rebased, inclusive
    sample_end: int    # rebased, exclusive
```

`RecorderAttachment` is a thread-safe handle with immutable request metadata and an atomically published `ResolvedCaptureBounds | None`. Pending and resolved handles both own recorder capacity until `release_capture()` is called. `SessionCaptureWindow` reads the handle resolution but never mutates recorder ownership. `CaptureNotAcceptedError` is the typed result of an atomic request against a recorder that has already committed to terminal completion/stop; callers must replace that recorder rather than attach a handle that can never resolve.

---

### Task 1: Pure Session Capture Window

**Files:**
- Create: `app/service/analysis/session_capture.py`
- Create: `tests/test_session_capture.py`

- [ ] **Step 1: Write failing duration-rounding tests**

Add parameterized tests showing ceiling behavior rather than truncation:

```python
@pytest.mark.parametrize(
    ("sample_rate", "duration", "expected"),
    [(44100, 0.05, 2205), (48000, 0.05, 2400), (44101, 0.05, 2206), (10, 0.21, 3)],
)
def test_frames_for_duration_never_undershoots(sample_rate, duration, expected):
    assert frames_for_duration(sample_rate, duration) == expected
```

Also reject zero/non-finite sample rates and negative/non-finite durations.

- [ ] **Step 2: Run the rounding tests and verify RED**

```powershell
python -m pytest tests/test_session_capture.py -q
```

Expected: collection/import failure because `session_capture.py` does not exist.

- [ ] **Step 3: Implement the constant, frame conversion, and value objects**

Create `app/service/analysis/session_capture.py` with the contracts above. In `CapturedAudioBlock.__post_init__`, validate that `data.shape[0] == sample_end - sample_start`, spans are non-negative, and the block is at least one-dimensional. Store callback-owned copies as C-contiguous `float32` in the recorder; the pure type itself must not perform a second data copy.

Implement a small `RecorderAttachment` handle with these read APIs:

```python
@property
def resolution(self) -> ResolvedCaptureBounds | None: ...

@property
def released(self) -> bool: ...

def current_epoch(self) -> float | None:
    resolved = self.resolution
    if resolved is None:
        return None
    return resolved.attach_monotonic + SESSION_WARMUP_SECONDS
```

Keep mutation methods private/package-internal (`_resolve_once`, `_mark_released`) so only `AudioRecorder` can resolve or release the owner. `_resolve_once` must reject a second, different resolution.

- [ ] **Step 4: Run the rounding/value-object tests and verify GREEN**

```powershell
python -m pytest tests/test_session_capture.py -q
```

Expected: the initial tests pass.

- [ ] **Step 5: Write failing window-boundary tests**

Build attachments with known resolved bounds and tagged arrays. Required cases:

- a block entirely before `retained_start` returns `None`;
- a block ending exactly at `retained_start` returns `None`;
- a block crossing `retained_start` returns only its valid suffix as rebased `[0, n)`;
- a block beginning exactly at `retained_start` is retained in full;
- a block crossing `retained_end` is clipped exactly at `target_samples`;
- repeated/overlapping delivery never emits the same absolute sample twice;
- adjacent differently sized blocks produce continuous rebased spans with no gap;
- after target completion or `freeze()`, later blocks return `None`;
- two windows attached at different absolute samples trim independently.

Representative assertion:

```python
block = CapturedAudioBlock(
    data=np.arange(8, dtype=np.float32).reshape(-1, 1),
    sample_start=102,
    sample_end=110,
    monotonic_start=1.02,
    monotonic_end=1.10,
)
retained = window.accept(block)  # retained_start == 105
np.testing.assert_array_equal(retained.data[:, 0], [3, 4, 5, 6, 7])
assert (retained.sample_start, retained.sample_end) == (0, 5)
```

Add a gap-invariant test: if the window has emitted through absolute sample 110 and the next candidate begins at 112, `accept()` raises a clear `ValueError` rather than silently compressing time.

- [ ] **Step 6: Run boundary tests and verify RED**

```powershell
python -m pytest tests/test_session_capture.py -q
```

Expected: failures because `SessionCaptureWindow` is absent.

- [ ] **Step 7: Implement `SessionCaptureWindow` minimally**

The constructor accepts `attachment`, `sample_rate`, and `record_duration`, calculates `target_samples` once, and verifies it matches the attachment request metadata after resolution. Implement:

```python
class SessionCaptureWindow:
    def accept(self, block: CapturedAudioBlock) -> RetainedAudioBlock | None: ...
    def freeze(self) -> int: ...                 # idempotent retained frame count
    @property
    def retained_frames(self) -> int: ...
    @property
    def target_samples(self) -> int: ...
    @property
    def complete(self) -> bool: ...
    @property
    def absolute_retained_end(self) -> int | None: ...
    @property
    def current_epoch(self) -> float | None: ...
```

Protect every mutable window field with one private `threading.RLock`. `accept()` and `freeze()` must linearize on that lock: either a block is accepted entirely before freeze, or freeze wins and that block contributes no frames. `accept()` must calculate absolute slice offsets from the block span, clip against both resolved bounds and the last emitted absolute sample, and derive rebased spans by subtracting `retained_start`. It must not concatenate a session buffer, invoke DSP, or know about Qt.

Add a deterministic two-thread test using events/barriers: pause one call immediately before it acquires the window lock, let `freeze()` win, resume `accept()`, and assert the returned value is `None` and the frozen/retained count never changes. Add the inverse ordering and assert a block linearized before freeze is included in the frozen count.

- [ ] **Step 8: Run Task 1 tests and verify GREEN**

```powershell
python -m pytest tests/test_session_capture.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```powershell
git add app/service/analysis/session_capture.py tests/test_session_capture.py
git commit -m "feat: add session capture window"
```

---

### Task 2: Sample-Exact Recorder Attachments and Capacity Ownership

**Files:**
- Modify: `app/service/analysis/audio_recorder.py`
- Modify: `tests/test_audio_recorder.py`

Keep the old `chunk_ready`/snapshot path working during this task so the existing manager remains green. Add the exact-block/capture-owner path alongside it; Task 3 migrates production subscribers and Task 5 removes the legacy path.

- [ ] **Step 1: Write failing callback-span and ADC-time tests**

Add tests around a new `captured_block_ready = pyqtSignal(object)` signal and inject a deterministic `monotonic_func` into `AudioRecorder`.

For usable PortAudio times, use:

```python
time_info = SimpleNamespace(inputBufferAdcTime=10.000, currentTime=10.020)
callback_monotonic = 100.020
```

At `sample_rate=1000` with 20 frames, assert the block maps to monotonic `[100.000, 100.020)` and receives the next non-overlapping absolute sample span. Call two callbacks before draining and assert spans are `[0, 20)` and `[20, 40)`, proving assignment occurs at callback receipt rather than queue drain.

Also assert `_feed_captured_block()` and `_emit_captured_block()` preserve the exact partial block instead of reconstructing an overlapping nominal tail.

- [ ] **Step 2: Run exact-block tests and verify RED**

```powershell
python -m pytest tests/test_audio_recorder.py -q
```

Expected: failures because exact captured blocks and callback-time spans do not exist.

- [ ] **Step 3: Implement callback-time block capture**

In `AudioRecorder`:

- add a monotonic clock dependency defaulting to `time.monotonic`;
- add `_capture_sample_cursor`, separate from the background `_write_pos`;
- under `_state_lock` in `_sd_callback`, reserve `[cursor, cursor + len(indata))` exactly once;
- map stream time with:

```python
block_monotonic_start = callback_monotonic + (
    float(time_info.inputBufferAdcTime) - float(time_info.currentTime)
)
block_monotonic_end = block_monotonic_start + frame_count / sample_rate
```

- treat missing attributes, exceptions, NaN, or infinity as unusable timing;
- queue one `CapturedAudioBlock` containing `indata.copy()` and its exact span;
- write by the block's absolute span and emit the same exact block on `captured_block_ready`.

Do not infer a start time using `callback_monotonic - block_duration` in the unusable-timing path.

- [ ] **Step 4: Run exact-block tests and verify GREEN**

```powershell
python -m pytest tests/test_audio_recorder.py -q
```

Expected: the new exact-block tests and existing legacy tests pass.

- [ ] **Step 5: Write failing attachment-resolution tests**

Add `request_capture(record_duration: float) -> RecorderAttachment`, using the injected monotonic clock at request registration. Required tests:

1. Request before a timed block starts: attach at block start.
2. Request exactly inside a timed block: use `ceil((request-start)*rate)`, and verify the first accepted attach sample is never earlier than the request.
3. Request exactly at block end: attach at `block_sample_end`.
4. Request later than a block's ADC end even though the callback enters after the request: remain pending and resolve on a later block.
5. Missing/unusable `time_info`: exclude the entire current block, resolve at `block_sample_end`, set `attach_monotonic=max(request_time, callback_monotonic)`, and log one degradation warning.
6. Concurrent request/callback locking: a handle resolves once and sample/time fields come from the same block.

Use tagged block values so the in-block test proves excluded samples cannot reappear in `get_capture_buffer()`.

- [ ] **Step 6: Run attachment tests and verify RED**

```powershell
python -m pytest tests/test_audio_recorder.py -q
```

Expected: failures because pending attachment owners do not exist.

- [ ] **Step 7: Implement pending attachment resolution**

Under the recorder state lock:

- register each accepted request immediately in `_pending_capture_owners` with `warmup_samples=frames_for_duration(rate, SESSION_WARMUP_SECONDS)` and `target_samples=frames_for_duration(rate, record_duration)`;
- resolve only requests already registered when the callback acquires the lock;
- for timed blocks, resolve only when `request_time <= block_monotonic_end` and use the specified ceil/clamp formula;
- for untimed blocks, resolve at the block end with the conservative monotonic value;
- move resolved owners to `_active_capture_owners` and ensure backing capacity through `bounds.retained_end`;
- publish the immutable resolution on the handle before the block is queued/emitted.

Do not implement correctness as `can_accept_capture()` followed by `request_capture()`. `request_capture()` itself must acquire `_state_lock`, atomically verify that capture acceptance is still open, and either register the pending capacity owner before releasing the lock or raise `CaptureNotAcceptedError`. A recorder that has never started but is not stopped remains attachable.

The run loop must commit terminal capture completion under that same lock: once the completion predicate is true, atomically set `_accepting_captures = False` before leaving the lock/stream loop. `stop()` and terminal error paths close acceptance under the same lock. Therefore exactly one side wins a completion/request race:

- request wins: the pending owner is visible and completion is no longer eligible;
- completion wins: acceptance is closed and the request is rejected without creating a handle.

- [ ] **Step 8: Write failing capacity-owner and extraction tests**

Required cases:

- pending owners force `_capture_complete()` false even when the old fixed buffer is full;
- callback blocks arriving while an owner is pending grow the raw buffer rather than truncate;
- after resolution, completion waits through `retained_end`;
- two resolved owners use the latest `retained_end`;
- releasing one owner does not stop/truncate the other;
- releasing a pending owner removes the completion hold;
- `get_capture_buffer(handle, retained_frames=n)` returns exactly `[retained_start, retained_start+n)` and caps at both written data and target;
- unresolved/released handles return an empty `(0, n_channels)` copy or raise the single documented misuse error consistently;
- the "old capacity reached + final block queued + new pending owner" race does not emit `recording_done` before the pending owner resolves and receives its full range.
- deterministic completion/request interleavings use a barrier around the shared state lock: when request registration wins, completion remains open and the owner resolves; when terminal completion commits first, `request_capture()` raises `CaptureNotAcceptedError` and no pending/active owner is created.

- [ ] **Step 9: Implement ownership-aware capacity and completion**

Replace fixed-buffer fullness as the new capture path's completion criterion:

```python
def _capture_complete_locked(self) -> bool:
    if self._pending_capture_owners:
        return False
    if self._active_capture_owners:
        required_end = max(
            owner.resolution.retained_end
            for owner in self._active_capture_owners.values()
        )
        return self._write_pos >= required_end
    return self._legacy_buffer_full_locked()  # removed in Task 5
```

Evaluate this predicate only inside an atomic `_commit_capture_completion_locked()` operation that closes `_accepting_captures` when it returns true. Grow before every exact block write and when a resolution raises the required end. Add idempotent `release_capture(handle)`. Never shrink/move existing raw samples.

- [ ] **Step 10: Run recorder and pure-window tests**

```powershell
python -m pytest tests/test_session_capture.py tests/test_audio_recorder.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit Task 2**

```powershell
git add app/service/analysis/audio_recorder.py tests/test_audio_recorder.py
git commit -m "feat: add sample-exact recorder attachments"
```

---

### Task 3: Gate Monitoring, Live Analysis, Progress, Saving, and Stop Paths

**Files:**
- Modify: `app/service/analysis/test_session.py`
- Modify: `app/service/analysis/test_manager.py`
- Modify: `tests/test_test_session.py`
- Modify: `tests/test_test_manager/_support.py`
- Modify: `tests/test_test_manager/test_late_attach_buffer_slice.py`
- Modify: `tests/test_test_manager/test_signal_disconnection.py`
- Modify: `tests/test_test_manager/test_user_stop_partial_save.py`
- Modify: `tests/test_test_manager/test_stop_behavior.py`
- Modify as needed: the recorder/session doubles listed in the File Map

- [ ] **Step 1: Write failing `TestSession` warmup-routing tests**

Replace direct ndarray/chunk-index test inputs with `CapturedAudioBlock` fixtures. Cover:

- unresolved attachment: buffer the exact raw block but publish no monitor/DSP data and emit only progress `0.0`;
- pre-retained block: no monitor feed, no live-display submission, progress remains `0.0`;
- crossing block: monitor and display receive only the valid suffix, with rebased `[0, n)` metadata;
- monitor feed happens before live-display submission for each retained block;
- adjacent retained blocks use exact sample-derived elapsed values, including a non-round sample rate;
- target-crossing block is clipped, elapsed is exactly `target_samples / sample_rate`, and done callback runs once;
- after completion/freeze, no later shared-recorder block reaches monitor or DSP.
- a block-handler/stop interleaving is linearizable: if retained fan-out wins, monitor/DSP finish before freeze returns and those frames are saved; if freeze wins, the block reaches neither monitor nor DSP and cannot extend the saved range.
- callback-versus-bind interleavings never lose or reorder a block: callback-first appends before bind drains it, while bind-first completes the handoff before the callback processes directly.
- after a rejected recorder is replaced, a delayed callback carrying the rejected connection generation is ignored even if its absolute span overlaps the new recorder's sample axis.

Use a 100Hz fixture (`warmup_samples=5`) with tagged samples to make the boundary visible.

- [ ] **Step 2: Run session tests and verify RED**

```powershell
python -m pytest tests/test_test_session.py -q
```

Expected: the new tests fail because `TestSession` still estimates elapsed from chunk indices and has no session window.

- [ ] **Step 3: Implement the single session fan-out**

In `TestSession`:

- retain `prepare_for_attach(source_token)`, but buffer `CapturedAudioBlock` objects received between signal connection and handle binding for that exact source generation;
- make `_on_captured_block(block, source_token=...)` reject any token other than the active generation before inspecting pending/window state;
- change `attach_to_recorder(..., source_token=...)` to validate the same active generation, bind the returned `RecorderAttachment`, create one `SessionCaptureWindow`, then drain buffered exact blocks in arrival order;
- replace `_processed_sample_boundary_since_attach` and `_process_chunk` with window-driven processing;
- add `set_monitor_feed(callback | None)` and invoke it only with `RetainedAudioBlock.data`;
- submit the same retained data to `LiveDisplayDispatcher` with the rebased exact span;
- emit `recording_progress(config_id, 0.0)` while unresolved/pre-warmup and exact retained elapsed afterward;
- expose `capture_attachment`, `captured_frame_count`, `current_epoch`, and idempotent `freeze_capture()`;
- keep `capture_recorder_buffer()` limited to selected-channel projection. It must never trim warmup frames.

Add a session-level private `threading.RLock` around the entire attachment/routing state machine: `prepare_for_attach()` generation change and queue reset; `_on_captured_block()` generation check and pending append; `attach_to_recorder()` bind and ordered pending drain; window `accept()`; monitor feed; live-display submission; progress update; and the decision to mark completion. `freeze_capture()` takes the same lock before calling the window's atomic `freeze()`. Invoke the manager completion callback after releasing the session lock. This creates one atomic ordered handoff: no callback can append after bind has taken/drained the pending list, and no direct callback can overtake that drain. It also guarantees that no monitor/DSP publication can occur after `freeze_capture()` returns while avoiding a reentrant manager-cleanup deadlock.

Use deterministic event/barrier tests for both lock orderings. For callback-first, pause after pending append, start bind, then release and assert the queued block is drained once. For bind-first, pause bind while it owns the lock, start callback, release bind, and assert the callback processes once after all older pending blocks. Do not use sleeps as synchronization.

Delete the old chunk-index/max-chunk-size elapsed state once its tests are migrated.

- [ ] **Step 4: Run session and window tests and verify GREEN**

```powershell
python -m pytest tests/test_session_capture.py tests/test_test_session.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing manager start/routing tests**

In `test_signal_disconnection.py` and `test_late_attach_buffer_slice.py`, assert:

- manager connects only one stored token-capturing wrapper (whose sole target is `session._on_captured_block`) to `recorder.captured_block_ready`;
- `AudioMonitor.feed` is not connected to any raw-recorder signal;
- the session's retained fan-out supplies monitor data before live display;
- a shared recorder is reused only when its atomic `request_capture()` succeeds;
- a stopped/terminal recorder rejects atomically, is discarded, and is replaced before the manager retries once;
- a deterministic race where terminal completion commits between shared-recorder lookup and owner request cannot strand a pending session on the old recorder;
- connection-before-request is preserved: `prepare_for_attach`, connect exact signal, request owner, bind handle;
- every recorder connection captures a unique opaque `source_token`; replacing a rejected recorder rotates the token and clears the rejected generation's pending blocks before connecting the replacement;
- the immediate `test_started`/factory acceptance behavior remains unchanged while first progress is zero.

- [ ] **Step 6: Run manager start/routing tests and verify RED**

```powershell
python -m pytest tests/test_test_manager/test_signal_disconnection.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_start_inputs_and_resolution.py tests/test_test_manager/test_shared_recorder.py -q
```

Expected: failures because manager still uses the raw monitor bypass and legacy snapshot.

- [ ] **Step 7: Migrate manager startup to capture owners**

In `TestManager.start()`:

1. select the shared recorder candidate, then set up the session and monitor and give the monitor feed callback to the session;
2. create a unique opaque `source_token`, call `session.prepare_for_attach(source_token)`, and connect the candidate signal through a stored callback that supplies that exact token to `session._on_captured_block` before requesting ownership;
3. call the candidate's atomic `request_capture(record_duration=record_duration)` without a separate check/reserve gap;
4. on `CaptureNotAcceptedError`, disconnect the candidate callback, discard the recorder from `_shared_recorders`, create a new recorder and a new token, call `prepare_for_attach(new_token)` to atomically clear old pending blocks, connect a new token-capturing callback, and retry the atomic request once; propagate/clean up if the new recorder also rejects;
5. bind the successfully returned exact handle with the matching source token;
6. connect recorder errors and monitor errors, register runtime dictionaries, then start current collection/monitor/recorder as today.

Store the actual token-capturing callback on the session for exact disconnection. A queued/in-flight old callback still carries the old token and must be ignored after the generation rotates; never identify sources from sample indices alone.

Do not connect `recording_done` to each session. A session completes only when its window has accepted `target_samples`. Preserve all existing cleanup if any startup step raises; release a handle that was already requested.

- [ ] **Step 8: Write failing canonical-buffer and stop tests**

Add/replace manager tests proving:

- a configured 5s session saves exactly `ceil(rate * 5)` retained frames after physically accepting warmup plus target;
- a tagged sample immediately before `retained_start` is absent and the first saved frame is the tagged boundary sample;
- late sessions on one recorder each discard their own 50ms and receive full target length;
- completing/stopping one owner does not truncate another;
- `_on_session_done` freezes and extracts `get_capture_buffer(handle, retained_frames=target)` once, then projects channels, saves, judges, charts, review, and replay from that canonical buffer;
- stop before the first retained frame does not call `_save_wav`, `AudioDataTable.create`, judgment, review, or replay setup;
- stop after retained data exists saves exactly the retained prefix;
- stop-triggered analysis still requires both `allow_incomplete_recording_analysis` and coverage of `analysis_window_offset_ms + analysis_duration` on the rebased duration;
- save failure/projection failure still execute all cleanup and release ownership.

- [ ] **Step 9: Implement one freeze/extract helper and use it everywhere**

Add one manager helper used by `_on_session_done`, `_capture_partial_stop_buffer`, and non-factory `_stop_one`:

```python
def _freeze_and_capture_session_buffer(self, session, recorder) -> np.ndarray:
    retained_frames = session.freeze_capture()
    if retained_frames <= 0:
        return np.empty((0, recorder._n_channels), dtype=np.float32)
    raw = recorder.get_capture_buffer(
        session.capture_attachment,
        retained_frames=retained_frames,
    )
    return self._capture_session_recorder_buffer(session, raw, recorder._sample_rate)
```

Use the actual recorder channel count safely rather than assuming a hardcoded width. A zero-frame result is a normal pre-warmup stop, not an analysis error.

Add an idempotent `_release_session_capture(session, recorder)` and call it on full completion, manual/factory stop, errors, and failed startup. Freeze before disconnecting; disconnect/freeze prevents queued late signals from extending the saved range. Keep the existing incomplete-analysis eligibility function and feed it `captured_frame_count / sample_rate`.

- [ ] **Step 10: Run all Task 3 tests**

```powershell
python -m pytest tests/test_session_capture.py tests/test_audio_recorder.py tests/test_test_session.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_signal_disconnection.py tests/test_test_manager/test_user_stop_partial_save.py tests/test_test_manager/test_stop_behavior.py tests/test_test_manager/test_start_inputs_and_resolution.py tests/test_test_manager/test_start_acceptance_and_failures.py tests/test_test_manager/test_shared_recorder.py tests/test_test_manager/test_max_duration.py tests/test_test_manager/test_sample_rate_mismatch.py -q
```

Expected: PASS. Confirm no production connection to the legacy raw `chunk_ready` signal remains:

```powershell
rg -n "chunk_ready\.connect|chunk_ready\.disconnect" app
```

Expected: no matches.

- [ ] **Step 11: Commit Task 3**

```powershell
git add app/service/analysis/test_session.py app/service/analysis/test_manager.py tests/test_test_session.py tests/test_test_manager
git commit -m "feat: gate realtime audio by session warmup"
```

---

### Task 4: Gate and Rebase Current-Meter Data to the Audio Epoch

**Files:**
- Modify: `app/service/current_meter_service.py`
- Modify: `app/service/analysis/test_manager.py`
- Modify: `tests/test_current_meter_service.py`
- Modify: `tests/test_test_manager/test_review_payload_emission.py`
- Modify: `tests/test_test_manager/test_stop_behavior.py`

- [ ] **Step 1: Write failing current timestamp/rebase unit tests**

Extend the value object without changing payload fields:

```python
@dataclass(frozen=True)
class CurrentSample:
    time_sec: float
    current_a: float
    monotonic_time: float | None = None
```

Add tests that `_poll_once()` calls the clock once after a successful Modbus read and stores that value in `monotonic_time`, while keeping `time_sec` relative to collector start for standalone callers.

Add pure helper tests:

```python
rebased = rebase_current_samples(
    [
        CurrentSample(0.01, 1.0, 10.049),
        CurrentSample(0.02, 2.0, 10.050),
        CurrentSample(0.08, 3.0, 10.125),
    ],
    current_epoch=10.050,
)
assert [(s.time_sec, s.current_a) for s in rebased] == [
    (pytest.approx(0.0), 2.0),
    (pytest.approx(0.075), 3.0),
]
```

Also test unsorted input retains acquisition order, samples without an absolute timestamp are excluded from this real-time gate, and an empty result stays empty (no fabricated zero point).

- [ ] **Step 2: Run current service tests and verify RED**

```powershell
python -m pytest tests/test_current_meter_service.py -q
```

Expected: failures because absolute completion timestamps and rebasing do not exist.

- [ ] **Step 3: Implement absolute timestamp preservation and pure rebasing**

Capture `completed_monotonic = self._time()` only after a valid value is decoded. Create the sample with both relative `time_sec=max(0, completed_monotonic - self._start_time)` and absolute `monotonic_time=completed_monotonic`.

Implement `rebase_current_samples(samples, *, current_epoch)` without sorting or interpolation. Filter with strict `< current_epoch`; an exact-epoch reading is retained at 0.0. Return new immutable samples whose public `time_sec` is rebased and whose absolute timestamp remains available for diagnostics.

- [ ] **Step 4: Run current service tests and verify GREEN**

```powershell
python -m pytest tests/test_current_meter_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing manager current-gate tests**

In `test_review_payload_emission.py`, use a session attachment that starts unresolved and later resolves. Assert:

- collection can start immediately, but a callback before resolution emits no `current_trace_updated`;
- after resolution, `current_epoch = resolved_attach_monotonic + 0.05` (not collector start time and not callback delivery time);
- the first post-resolution live update filters the complete snapshot, so an eligible reading buffered before that callback is not lost;
- pre-epoch readings never enter live payloads;
- the exact-epoch and later readings are rebased and the first point may naturally be greater than zero;
- `_stop_current_collection` stores the same gated set later used by review/chart payloads;
- stopping while attachment is unresolved or before any retained audio clears all staged readings;
- completing one shared-recorder session does not reuse another session's current epoch.

- [ ] **Step 6: Run manager current tests and verify RED**

```powershell
python -m pytest tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_stop_behavior.py -q
```

Expected: new assertions fail because raw collector-relative samples are currently published/stored.

- [ ] **Step 7: Implement one manager current gate**

Add a helper that takes `config_id`, a raw snapshot, and optionally the session:

```python
def _effective_current_samples(self, config_id, raw_samples, *, session=None):
    session = session or self._sessions.get(config_id)
    epoch = getattr(session, "current_epoch", None)
    epoch = epoch() if callable(epoch) else epoch
    if epoch is None:
        return []
    return rebase_current_samples(raw_samples, current_epoch=epoch)
```

Use it in all three places:

- live sample callback before `_emit_current_trace_update`;
- `_current_trace_payload` while a collector is active;
- `_stop_current_collection`, storing only the effective samples.

Pass the session explicitly during cleanup before it is removed from `_sessions`. If `session.freeze_capture()` reports zero retained audio, clear current samples even if a clock-edge current reading exists. Do not add a timer or synthetic current sample at 0s.

- [ ] **Step 8: Run Task 4 tests**

```powershell
python -m pytest tests/test_current_meter_service.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_stop_behavior.py tests/test_test_manager/test_late_attach_buffer_slice.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 4**

```powershell
git add app/service/current_meter_service.py app/service/analysis/test_manager.py tests/test_current_meter_service.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_stop_behavior.py
git commit -m "feat: align current trace with session warmup"
```

---

### Task 5: Remove Legacy Raw Bypass and Prove End-to-End Non-Regression

**Files:**
- Modify: `app/service/analysis/audio_recorder.py`
- Modify: `tests/test_audio_recorder.py`
- Modify mechanically: recorder/session doubles listed in the File Map
- Modify: `tests/test_test_manager/test_late_attach_buffer_slice.py`
- Modify: `tests/test_test_manager/test_review_replay.py`
- Regression-only: `tests/test_test_manager/test_imported_audio_analysis.py`, `tests/test_imported_audio_analysis.py`, `tests/test_golden_sample_service.py`, `tests/test_golden_sample_import_dialog.py`

- [ ] **Step 1: Write the end-to-end tagged-timeline regression**

Create a low-rate deterministic manager/recorder integration test that simulates:

- 100Hz audio;
- attachment at a known block-internal sample/monotonic instant;
- five warmup frames;
- a ten-frame configured target;
- tagged values before warmup, at retained zero, and at the target end.

Assert one coherent result:

```python
assert session.captured_frame_count == 10
np.testing.assert_array_equal(session._buffer[:, 0], expected_retained_tags)
assert first_live_span == (0, expected_first_block_length)
assert all_live_input_tags == expected_retained_tags
assert monitor_tags == expected_retained_tags
assert saved_wav_frame_count == 10
assert replay_of_0_to_0_03_sec_starts_with == expected_retained_tags[0]
```

Also assert physical absolute coverage ends at `attach_sample + 5 + 10`; completion must not occur at only ten post-attach raw frames.

- [ ] **Step 2: Run the end-to-end test and verify its result**

```powershell
python -m pytest tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_review_replay.py -q
```

Expected: PASS once Tasks 1-4 are correct. If it fails, fix the owning implementation rather than weakening tags or tolerances.

- [ ] **Step 3: Remove transitional recorder APIs and update test doubles**

Delete the legacy raw `chunk_ready` emission, `RecorderAttachSnapshot`, emitted-chunk cursors, chunk-index reconstruction, and legacy `get_buffer_from()` path from `audio_recorder.py`. Rename `captured_block_ready` to `chunk_ready` only if doing so does not obscure that its payload is a `CapturedAudioBlock`; otherwise keep the explicit name. There must be exactly one production raw-block signal and its only production per-session subscriber is `TestSession._on_captured_block`.

Use:

```powershell
rg -n "RecorderAttachSnapshot|attach_session\(|get_buffer_from\(|chunk_index|_emitted_sample_index|_emitted_chunk_index|chunk_ready" app tests/test_audio_recorder.py tests/test_test_manager tests/test_test_session.py
```

Update all test doubles to the final exact capture contract. Do not add production fallback branches solely to support obsolete test doubles.

- [ ] **Step 4: Prove there is no downstream double trim**

Add focused assertions, not new production trimming:

- replay frame selection `start_sec=0` selects `session._buffer[0]`;
- review/chart payload duration is based on `len(session._buffer) / sample_rate`;
- saved WAV playback does not invoke `SessionCaptureWindow`;
- imported WAV analysis creates no recorder/window and consumes its source from original frame 0;
- golden sample service/dialog import keeps original frame 0 and never references `SESSION_WARMUP_SECONDS`.

Run:

```powershell
python -m pytest tests/test_test_manager/test_review_replay.py tests/test_test_manager/test_imported_audio_analysis.py tests/test_imported_audio_analysis.py tests/test_golden_sample_service.py tests/test_golden_sample_import_dialog.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the complete focused feature suite**

```powershell
python -m pytest tests/test_session_capture.py tests/test_audio_recorder.py tests/test_test_session.py tests/test_current_meter_service.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_signal_disconnection.py tests/test_test_manager/test_user_stop_partial_save.py tests/test_test_manager/test_stop_behavior.py tests/test_test_manager/test_start_inputs_and_resolution.py tests/test_test_manager/test_start_acceptance_and_failures.py tests/test_test_manager/test_shared_recorder.py tests/test_test_manager/test_max_duration.py tests/test_test_manager/test_sample_rate_mismatch.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_review_replay.py tests/test_test_manager/test_imported_audio_analysis.py tests/test_imported_audio_analysis.py tests/test_golden_sample_service.py tests/test_golden_sample_import_dialog.py -q
```

Expected: PASS.

- [ ] **Step 6: Run the full test suite**

```powershell
python -m pytest -q
```

Expected: PASS. If unrelated baseline failures exist, rerun the focused suite and document the exact pre-existing failures separately.

- [ ] **Step 7: Run static repository checks**

```powershell
git diff --check
```

Expected: no output and exit code 0.

```powershell
rg -n "SESSION_WARMUP_SECONDS|0\.05" app/service/analysis app/service/current_meter_service.py
```

Expected: the semantic warmup constant is defined once in `session_capture.py`; other `0.05` occurrences are unrelated existing values or should be replaced with the shared constant.

```powershell
git status --short
```

Expected: only intentional tracked feature/test changes plus known user-owned untracked files; do not stage unrelated workspace artifacts.

- [ ] **Step 8: Commit Task 5**

```powershell
git add app/service/analysis/audio_recorder.py tests/test_audio_recorder.py tests/test_test_manager tests/test_test_session.py tests/test_imported_audio_analysis.py tests/test_golden_sample_service.py tests/test_golden_sample_import_dialog.py
git commit -m "test: verify session warmup end to end"
```

- [ ] **Step 9: Apply verification-before-completion**

Before reporting success, use the `verification-before-completion` skill and cite fresh command results for the focused suite, full suite, `git diff --check`, and final scoped `git status`. Do not claim that import/golden/existing-WAV behavior is unchanged without the regression outputs above.
