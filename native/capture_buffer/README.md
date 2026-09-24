# Windows native capture buffer (ABI 1)

Build from a Windows x64 developer machine with Visual Studio C++ and Windows SDK:

```powershell
./scripts/build_capture_buffer.ps1 -WithTests
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/native_capture/test_native_buffer.py -q
```

`-VisualStudioPath` overrides the installed VS root. The script configures the
x64 MSVC environment, compiles with `/W4 /WX /O2 /MT`, and places the shipping
library at `app/native/capture_buffer.dll`. The optional native test driver goes
to `tests/native_capture/capture_test_driver.dll`. Intermediates and the test DLL
are ignored; the production DLL is an application asset. Neither library links
Python or PortAudio. The static C runtime removes a separately deployed VC runtime
dependency. The exported C ABI can be loaded from Python 3.12 or packaged 3.14.

## Buffer and callback contract

There is exactly one callback producer and one serialized consumer. The callback
is PortAudio's Windows x64 `PaStreamCallback` ABI, returning 0 (continue) or 2
(abort). It copies interleaved float32 bytes, frame count, status flags, all three
PortAudio times, native entry time and sequence into a preallocated ring.
PortAudio status is retained without reinterpretation. Null input, zero frames,
and frames exceeding the configured maximum abort with sticky error 2; exhausted
slots abort with sticky error 1. Unread samples are never overwritten. Stopping
aborts explicitly without manufacturing a sample block.

Allocation is checked before multiplication can overflow. The total allocation,
including slot metadata, must not exceed 512 MiB. All data/metadata pages are
touched before capture starts. The default slot count is
`ceil(sample_rate * 10 / block_frames)`, providing ten seconds for the configured
callback block size. Smaller variable blocks use one slot apiece; sustained
smaller callbacks therefore reduce the available duration. Frames larger than
the configured block size are a fault, so production must request a fixed size.

The producer uses Windows Interlocked publication (full memory barriers), bounded
copying and native QPC reads. It does not enter Python, allocate, log, acquire a
consumer lock, wait, or perform analysis. The first callback applies
`THREAD_PRIORITY_HIGHEST` to its own thread, without changing process priority.
Priority status is 0 (not attempted), 1 (applied), or 2 (failed).

## Python integration and lifecycle

```python
buffer = NativeCaptureBuffer(sample_rate=48000, channels=2, block_frames=512)
callback, userdata = buffer.cffi_callback(sd._ffi)
# Pass callback/userdata to sd._StreamBase(kind='input', wrap_callback=None, ...).
# Retain buffer strongly for the complete stream and pump lifetime.
buffer.arm()  # Before starting the producer.
block = buffer.peek()
if block is not None and downstream_is_ready:
    downstream_accept(block.samples, block.entry_ns, block.status)
    buffer.release()
```

`peek()` returns the same copied `NativeCaptureBlock` until `release()`. It keeps
the native slot occupied so a pump can apply backpressure before removing audio.
Copied arrays remain valid after release/close. Native callers of `cb_peek` receive
a pointer lease instead, valid only until `cb_release`.

`snapshot()` exposes successful `sequence`, released `consumed`, native
`heartbeat_ns`, sticky `error`, `priority_applied`, slot `capacity`, `armed`, and
`stop_requested`. Heartbeat advances on native callback entry even when Python is
stalled. These values are individually atomic, not a transactional snapshot;
consumed is read before sequence to make backlog subtraction conservative.

Shutdown order: request_stop(), stop/close and definitively quiesce the external
producer, stop/join the pump within its owning lifecycle deadline, release any
lease, producer_stopped(), close(). `producer_stopped()` is an explicit assertion
by the caller that future callbacks are impossible; the native active-call count
alone cannot prove this between callbacks. `close()` refuses armed/active
producers and outstanding leases. Failed or timed-out upstream shutdown must
retain the owner rather than free its memory. The binding never silently frees
in a destructor. A stopped buffer cannot be rearmed; create a new generation.
Stream lifecycle deadlines and retaining owners on failed shutdown belong to
`app/service/native_capture_stream.py` and the existing Resident lifecycle owner.

## Clock domain

Entry times are sampled natively from QPC and converted using its frequency.
When Python monotonic and perf-counter implementations agree (Windows Python
3.13+), their clock domain is QPC and the offset is zero. Python 3.12 on this
machine reports GetTickCount64 for monotonic and QPC for perf-counter. The binding
therefore measures a fixed QPC-to-monotonic offset at allocation, before capture.
It exposes `clock_offset_ns` and `clock_uncertainty_ns`; uncertainty includes the
older monotonic clock's reported tick resolution and calibration call duration.
No delayed Python delivery timestamp is used to establish or move capture time.
The production wrapper subtracts that explicit uncertainty from the initial
mapping and future-time ceiling. It never replaces native time with replay time.
A one-time domain calibration does not correct indefinitely accumulated drift.

## Observed verification (2026-09-09)

Environment: Windows x64, Python 3.12.3, installed Visual Studio 18 Community,
Windows SDK 10.0.26100.0. MSVC compilation completed with warnings treated as
errors. Initial RED: native tests failed collection because the binding did not
exist. Stop visibility RED: the targeted lifecycle test failed because
`NativeCaptureStats.stop_requested` did not exist. Both were implemented and
verified GREEN. Final command above: **15 passed in 14.57s**.

The C driver starts a native producer thread and invokes the actual exported
production callback. `ctypes.PyDLL('kernel32').Sleep(2300)` retains the GIL for
more than two seconds while native sequence advances by at least 100 blocks.
After replay, 320 three-channel variable-frame blocks and every native entry
timestamp match the native/generated reference exactly. Two adjoining sample
windows are independently checked against that reference. A separate 600-block
producer/consumer test uses a small ring and Python serialization load to force
wrap while checking every sample and timestamp. The driver independently reads
back `GetThreadPriority` and verifies `THREAD_PRIORITY_HIGHEST` (2).

Tests also cover full-boundary retention, explicit stop/error status, raw
PortAudio times/status, float32 bit patterns including NaN payload/signed zero,
invalid and excessive sizes at both binding and C boundaries, expert CFFI pointer
types, and read/close lifetime. Windows timer resolution means Sleep(8) may take
about 15.6ms, so the progress test does not assume a precise synthetic cadence.

This is synthetic native callback evidence, not microphone/customer-device
capture. It does not prove hard realtime scheduling, packet-device-clock timing,
actual-pool lifecycle behavior (Task 2), or a final packaged EXE build.

## Production integration (Task 2)

Real Windows sounddevice modules use `_StreamBase(kind='input',
wrap_callback=None, callback=CData, userdata=CData)`. Injected fake modules and
non-Windows callers retain the existing InputStream path. The factory/open hooks
keep the wrapper owned before opening PortAudio, preserve open/start failure
stages, and reuse the existing bounded lifecycle control thread.

The above-normal Python pump starts immediately and polls at a bounded 1 ms
idle interval (Windows scheduling may delay this). It checks the explicit
Resident ingress readiness before peeking a native block. A full downstream
queue therefore keeps unread samples in native memory. Legacy callbacks still
fault on ingress overflow. Above-normal Resident drain threads consume the
existing ingress; neither pump nor drain performs native acquisition.

Native sequence/heartbeat reach the owning handle and slot independently of
Python deliveries. Both immediate readiness and recovery observation accept
healthy native progress, retaining the generation and its backlog. Capture
completion can wait for replayed samples. Native allocation, copy, full/status
faults remain explicit; this is finite protection, not unlimited buffering.

Native WASAPI and Windows DirectSound are identified by the resolved
`ResidentStreamSpec.hostapi_name`. Their raw ADC estimates are ignored for
initial origin, faulting and correction. First block
end is native entry minus reported input latency plus block duration, bounded by
native entry and reduced by the fixed clock-domain uncertainty. Later timestamps
advance by exact frame counts; a nominal end exceeding the native-entry ceiling
shifts the origin back without making block ends decrease. Delayed Python pump
arrival never establishes or corrects the timeline. This handles slight fast
device clocks without estimate-only faults, but does not provide full device
clock drift correction or packet-device-clock accuracy. PCM is unchanged.

Stop requests native abort, obtains confirmed PortAudio quiescence, and joins
the pump for at most 200 ms per attempt. Caller deadlines remain enforced by the
existing control owner. Failed/timed-out cleanup retains the buffer and read
lease. A pump timeout or failed close permits a later retry. A genuine PortAudio
stop failure quarantines the generation until process shutdown: ASIO can mark
the stream stopped/inactive even when driver stop fails before callback
completion, so these flags and later `paStreamIsStopped` responses cannot prove
quiescence. Only an initial `PortAudioError` carrying `paStreamIsStopped`, with
stopped/inactive evidence and no earlier stop failure, is accepted as benign
callback-abort cleanup. Quarantined cleanup remains bounded, reports failure,
retains armed native memory and rejects restart. A deliberate restart of a
confirmed stopped generation closes the old stream and buffer,
creates a fresh generation, and resets timing. The wrapper requests explicit
stop/close error reporting; it retains a failed-close pointer for retry because
sounddevice clears its pointer before raising. Partial failed opens with a NULL
PortAudio output pointer can free their unarmed buffer safely. See the upstream
[PortAudio lifecycle implementation](https://github.com/PortAudio/portaudio/blob/v19.7.0/src/common/pa_front.c#L1268)
and [ASIO stop failure path](https://github.com/PortAudio/portaudio/blob/v19.7.0/src/hostapi/asio/pa_asio.cpp#L3175),
plus the installed sounddevice 0.5.5 expert stream implementation.

Usable audio timeline history is now ten seconds, capped by physical storage
minus one boundary block for smaller test rings. Metadata retention and trigger
expiry use the same frame budget. PLC current lookback is ten seconds within
the existing 4096-reading cap. Replay uses only readings actually observed;
readings never obtained during a blocked PLC poll cannot be reconstructed.

`main.spec` includes `app/native/capture_buffer.dll` at the same packaged path.
Missing native support fails clearly on real Windows production streams without
a Python-callback fallback. The DLL was not changed in Task 2; existing compiled
production DLL and native test harness were used for all integration tests.

### Task 2 test evidence

All commands use the main `.venv/Scripts/python.exe` above, from the isolated
worktree. RED/GREEN cycles were observed for ten-second audio/current retention,
production CData registration and WASAPI native timing, actual-pool native
liveness/backpressure, fixed non-WASAPI clock uncertainty, package inclusion,
strict lifecycle error reporting, failed-open/close pointer cleanup, and correct
open-stage diagnostics. Additional characterization checks cover bounded pump
join, restart timeout, missing DLL failure and Windows thread priority readback.

`tests/native_capture/test_native_integration.py` has 24 tests. Actual-pool tests
hold the GIL using PyDLL Sleep(2300), gate Python replay, retain the same generation
through both immediate readiness and a forced recovery observation, then block
Resident drain until its smaller ingress fills. Native backlog remains retained
without overflow. Two adjoining 100-frame, two-channel captures exactly match
the native generated reference; already-observed PLC readings retain their
relative offsets to the native audio origin. A simulated slightly-fast clock
advances 200,000 variable-estimate timing steps past the first corrective shift.
GetThreadPriority confirms above-normal pump and Resident drain priorities.
Cleanup fixtures verify no new capture/pump/lifecycle thread remains and restore
the pytest caller's priority after synchronous native callback invocation.

Focused checks: native tests plus backend tests and the reserve latency stress
case passed **61 tests in 28.82 s**; the final coupled actual-pool/current replay
checks passed **2 tests in 6.66 s**. Earlier broad runs passed 675 and 681 tests,
each failing the same fake reserve-latency case: its single startup callback
became stale against wall time. Current and preserved-baseline isolated reruns
both passed; this was not labeled a proven baseline failure. The fixture now
uses its existing controlled fake clock for pool health, leaving real latency
measurement, all 1000 operations, the five-second park and thresholds unchanged;
the corrected isolated case passed in 6.49 s. Final broad result is recorded below.
Final broad selection: **682 passed in 103.96 s**, including the corrected
reserve-latency fixture, compiled native tests, Resident recovery/lifecycle,
managed backend/adapter, current history and coordinator integration.
An actual sounddevice expert-class failure before instance pointer allocation
also passed (1 test in 0.44 s), confirming its class-level NULL pointer lets the
unarmed buffer be disposed without calling PortAudio close.

Stop-failure safety regression: the focused selector
`-k 'failed_stop_flags or callback_abort_stream'` first produced **3 failed,
1 passed in 2.30 s** because host errors and unrelated exceptions with cached
stopped/inactive flags incorrectly reported successful close. After the fix it
passed **4 tests in 2.15 s**. The tests use actual sounddevice `PortAudioError`
argument shapes, repeat cleanup, reject restart, and verify retained armed memory
and fault reporting. Intentional process-lifetime quarantine runs in child
processes without manufacturing successful shutdown to clean up test threads.
The benign callback-abort case still closes normally.

Final focused stop-fix verification: **59 passed in 9.42 s**:

```powershell
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/native_capture/test_native_integration.py tests/resident_audio_mvp/test_sounddevice_backend.py tests/resident_audio_mvp/test_backend_orphans.py tests/test_managed_input_sounddevice_backend.py -q
```

Reproduce the broad integration selection:

```powershell
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/native_capture tests/resident_audio_mvp tests/test_managed_input_sounddevice_backend.py tests/test_managed_input_resident_adapter.py tests/test_current_meter_service.py tests/multi_run_coordinator_mvp/test_managed_resident_capture_port.py tests/multi_run_coordinator_mvp/test_resident_recovery_continuation.py tests/multi_run_integration/test_resident_recovery_production.py -q
```

Narrow RED commands used the same interpreter with
`-m pytest tests/native_capture/test_native_integration.py -q`, and selectors
`-k 'uncertainty or priority or restart_timeout'`,
`-k 'error_reporting or output_pointer or original_pointer'`, and
`-k lifecycle_errors`. Failures respectively exposed missing ten-second history,
Python callback registration/ADC-based origin, unavailable native pool liveness
and readiness, a future coarse-clock timestamp, missing package asset, ignored
stop/close errors, partial-open/failed-close ownership, and open errors labeled
as start errors. Each was re-run GREEN after its corresponding implementation.

These are synthetic production-integration tests. No microphone/customer device
was opened, and no final EXE was built or verified in this task.

## Resource scheduling (Task 3)

Both external AI launch paths use `CREATE_NO_WINDOW | IDLE_PRIORITY_CLASS` on
Windows. Explicit idle ensures a child of the already-idle analysis worker is
never promoted. A shared copied environment sets `OMP_NUM_THREADS`,
`OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, and `NUMEXPR_NUM_THREADS` to `1` before
launch. Other environment entries, protocol arguments, working directories,
output pipes and hidden-window behavior remain intact. These library settings
do not control custom threads a toolchain explicitly creates.

The dedicated PLC owner applies `THREAD_PRIORITY_ABOVE_NORMAL` before its first
connection attempt and uses Qt `PreciseTimer` for polling. The configured poll
and reconnect intervals remain unchanged; timer precision cannot eliminate
Windows scheduling, GIL, network or PLC response delays.

Observed RED: PLC's actual Windows thread readback was normal (`0`) with a
coarse timer; AI creation flags were only `0x08000000` and the actual child
priority class was normal (`32`). Environment tests failed because neither
launch supplied the numerical limits. Focused RED commands are below; their
test cases passed in the final GREEN selections after implementation:

```powershell
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/test_plc_service_scheduling.py -q
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/test_ai_subprocess.py -q
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/test_ai_subprocess.py tests/test_ai_service.py tests/test_ai_resident_runtime.py -q -k 'child_environment or preserves_io_and_uses_no_console or preserves_pipes_and_uses_no_console'
```

Real one-shot and resident toolchain probes from both normal (`32`) and idle
(`64`) parents read back child priority `64`, no console, all four numerical
limits `1`, preserved PATH/system/custom environment values, exact arguments
and working directory, and working stdout/stderr (plus resident stdin).
An actual QThread verifies PLC priority `1`, PreciseTimer, a configured 137 ms
poll and 2345 ms reconnect, for successful and failed connection setup.
The test's persistent QApplication also allows later widget suites to run;
an initial QCoreApplication fixture caused a combined-suite teardown abort and
was corrected before the successful combined verification.

Final AI helper/service/runtime selection: **132 passed in 22.43 s**. Additional
AI preflight/validation/integration and PLC/current selection: **147 passed in
7.42 s**, with four existing model collection warnings:

```powershell
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/test_ai_subprocess.py tests/test_ai_service.py tests/test_ai_resident_runtime.py -q
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/test_ai_service_preflight.py tests/test_ai_service_validation.py tests/test_ai_service_integration.py tests/test_plc_service_scheduling.py tests/test_plc_service_current.py tests/test_plc_service_shutdown.py tests/test_plc_service_workorder.py tests/test_current_meter_service.py tests/test_stft_module/test_plc_current_live_path.py -q
```

Native exact-sample/GIL-stall and CPU-contention evidence remains synthetic,
using the compiled production callback and test driver described above. No
physical microphone/PLC customer hardware or final EXE was tested in Task 3.

The broad native/Resident/backend/current/coordinator command from Task 2 was
rerun after the scheduling changes: **687 passed in 106.93 s**.

An additional full-volume acceptance test opens two concurrent synthetic
stations at nominal 48 kHz with two channels each. Each station completes two
immediately adjoining 384,000-frame (eight-second) captures. A PyDLL Sleep(2300)
holds the GIL near the first handoff; both native producers advance by at least
100 blocks during the pause. Every sample in all four capture outputs and each
of their four 96,000-frame (two-second) phases matches the generated reference.
Start cursors are exactly 0 and 384,000; neither station restarts or reports an
ingress overflow. Native producer priority reads back as highest (`2`).

The existing compiled harness supplies 1,504 variable-size blocks of
512/511/510 frames per station, with Sleep(10) between blocks and no producer
backpressure. The focused test passed **1 test in 23.96 s**. This is realistic
PCM volume through actual capture outputs, but the Windows synthetic sleep
cadence takes more wall time than the nominal sixteen seconds of PCM and is
not a physical 48 kHz device-clock timing measurement. Shipping C/DLL sources
and binaries were unchanged. This added characterization test required no
production implementation change and was validated separately from the broad
selection:

```powershell
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/native_capture/test_native_integration.py -q -k two_station_48khz
```

## DirectSound estimated-clock regression (2026-09-10)

Native DirectSound now shares the existing WASAPI frame timeline. The same
predicate selects callback-entry normalization and timeline mapping, so the
fixed clock uncertainty is applied once at the initial origin and correction
ceiling. ASIO, MME and non-native callbacks retain their existing policies.
No threshold was enlarged and no samples, shipping C/DLL or test driver changed.
PortAudio v19.7 DirectSound computes its ADC estimate from processing time and
queued input bytes in
[`TimeSlice`](https://github.com/PortAudio/portaudio/blob/v19.7.0/src/hostapi/dsound/pa_win_ds.c);
estimate-only variation is not evidence of missing samples.

The exact 44,100 Hz, 512-frame regression reproduced
`raw_end_ns=2465589111525477`, `expected_end_ns=2465589137607250`,
`delta_ns=-26081773`, `tolerance_ns=23219954`: the second continuous block
returned STOP before the fix and CONTINUE afterward. Every stereo sample is
checked. The initial origin uses native entry and reported latency despite a
five-second-later Python delivery clock; initial and corrected times retain
exactly one 16 ms uncertainty margin. Explicit driver overflow still returns
STOP. Separate production-pump tests preserve actual input-overflow flags,
native full-buffer error 1 and invalid-block error 2, with retirement and clean
shutdown. ASIO/MME and non-native host cases still reject the raw deviation.

A new actual Resident-pool test configures DirectSound at 44,100 Hz with a
512-frame maximum and the unchanged C driver's 512/511/510-frame blocks. Only
estimated ADC/current-time metadata is supplied at the Python pump boundary;
native entry, native sample generation and PCM remain untouched. PyDLL
Sleep(2300) holds the GIL while the producer advances at least 100 blocks.
Replay is then held until native backlog exceeds Resident ingress capacity;
with drain blocked, the pump retains excess native blocks without overflow.
The same generation completes two adjoining 44,100-frame stereo captures,
each sample matching the C reference. Captures use the ring's existing ceil
mapping with floored within-block trigger durations (within 1 ns of nominal
one-second boundaries). Native producer timestamp readback matches the origin,
and replay begins over two seconds after that block's native entry.

Commands from the isolated worktree use the main virtual environment:

```powershell
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/native_capture/test_native_integration.py -q -k reported_44100
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/native_capture/test_native_integration.py -q -k 'directsound or estimated_clock'
& 'C:/Users/DY/Documents/ChatGPT/New project/.venv/Scripts/python.exe' -m pytest tests/native_capture tests/test_managed_input_sounddevice_backend.py tests/test_managed_input_timeline_integrity.py tests/resident_audio_mvp/test_ring_capture.py -q
```

Observed RED: exact regression **1 failed, 5 passed in 0.61 s**; completed
DirectSound/clock selector **3 failed, 9 passed in 3.81 s**, exposing the reported
deviation, ADC-based origin and actual-pool estimate fault. Observed GREEN:
the same DirectSound/clock selector **12 passed in 3.67 s**.
The full planned native/backend/timeline/ring selection passed **129 tests in
50.50 s**, including the existing GIL-stall tests and two-station full-volume
48 kHz capture test.

These are synthetic production-path checks, not live microphone or customer
hardware validation. No running application was stopped or restarted and no DB
configuration was changed. An already-running Python process needs a later
restart to load the source update.
