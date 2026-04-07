# Resident AI Execution Mode And StdIO Protocol

## Summary

The current app only executes AI toolchains as `oneshot_process`: it writes
`request.json`, launches a fresh subprocess, waits for `response.json`, and
then exits. That keeps the contract simple, but it also means every run pays
the process start and model load cost again.

This pass introduces explicit runtime execution modes for registered AI
toolchains:

- `resident_process`
- `oneshot_process`

User-approved direction for this pass:

- `resident_process` becomes the first-class execution mode
- `oneshot_process` remains supported, but only as an explicit optional mode
- the main app must not automatically fall back from `resident_process` to
  `oneshot_process`
- the first resident-process protocol uses `stdin/stdout`
- the first resident-process protocol does not add direct-memory transport,
  shared memory, WebSocket, or raw binary streaming

This spec supersedes the fixed-`execution_mode` assumptions in
`docs/double-sdd/specs/2026-04-07-ai-tool-management-design.md`.

## Problem

### Current Runtime Is Hard-Wired To One-Shot Execution

`AiService` currently normalizes every toolchain row to
`execution_mode = "oneshot_process"` and only has one execution path:

- write `request.json`
- spawn subprocess
- wait for exit
- read `response.json`

There is no real runtime routing by `execution_mode`.

### Model Warmup Cost Dominates Repeat Runs

For AI toolchains that preload PyTorch or similar runtime state, the current
one-shot contract forces repeated:

- Python interpreter startup
- dependency import
- model deserialization
- model warmup

That overhead is paid on every analysis even when the same toolchain and model
are reused many times in one app session.

### The Registry UI Cannot Express Runtime Mode

The current `AI工具管理` page treats `execution_mode` as a fixed helper value.
That no longer matches the desired product model where:

- `resident_process` is the preferred mode
- `oneshot_process` is still available when a toolchain only supports the
  current file-based CLI contract

## Scope

In scope:

- support explicit `execution_mode` values:
  - `resident_process`
  - `oneshot_process`
- make `resident_process` the default mode for new toolchain drafts
- expose `execution_mode` in `AI工具管理`
- route AI execution by the selected toolchain mode
- add a resident-process lifecycle manager inside the main app
- define the first resident-process transport as `stdin/stdout` JSON Lines
- keep `protocol_version` fixed to `1`
- keep current `request.json` persistence for all runs and `response.json`
  persistence whenever a valid logical response exists
- keep artifact output on disk via `artifacts_dir`
- add regression coverage for registry behavior, runtime routing, resident
  transport, and no-fallback semantics

Out of scope:

- WebSocket transport
- shared memory or direct in-memory audio / STFT transfer
- named pipes or socket-based transport
- multiple resident transport protocols in the same pass
- automatic retrofit of external toolchain repositories outside this repo
- automatic fallback from resident to one-shot
- concurrent multiplexing of multiple in-flight requests on one resident
  process
- changing the logical request / response payload schema version
- remote deployment or multi-client AI serving

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - `protocol_version = 1`
  - the logical AI request payload shape already written to `request.json`
  - the logical AI response payload shape already validated from
    `response.json`
  - historical `ai_execution_table` rows
- Allowed breakage:
  - `AI工具管理` no longer treats `execution_mode` as fixed
  - new toolchain drafts default to `resident_process`
  - callers that try to save unsupported execution-mode strings are rejected
  - runtime behavior no longer assumes one-shot execution for every toolchain
- Migration strategy: none; existing rows may keep any currently supported
  value, but this pass adds no compatibility layer, no auto-fallback, and no
  legacy alias values

## Approved Decisions

1. Supported execution modes are exactly:
   - `resident_process`
   - `oneshot_process`
2. `resident_process` is the first-class mode and the default for new
   toolchain drafts.
3. `oneshot_process` remains supported only as an explicit toolchain choice.
4. The main app must never auto-retry a failed `resident_process` request as
   `oneshot_process`.
5. `protocol_version` remains fixed to `1` and is not user-editable.
6. The first resident-process protocol uses `stdin/stdout` with one JSON object
   per line.
7. This pass does not add direct-memory transfer for audio, STFT matrices, or
   other large payloads.
8. The main app continues to persist `request.json` for every AI run and
   persists `response.json` whenever a valid logical AI response payload
   exists, including resident-mode runs.
9. Resident toolchains keep artifact output on disk under the provided
   `artifacts_dir`.
10. `AI工具管理` must expose `execution_mode` as an editable supported-value
    selector.

## Design Summary

Keep the current logical AI request / response schema, but decouple it from the
transport:

- `oneshot_process`
  - keeps the current file-oriented subprocess contract
- `resident_process`
  - launches one long-lived subprocess
  - sends one JSON Lines request message per analysis over `stdin`
  - receives one JSON Lines response message per analysis over `stdout`
  - reuses the live process for later analyses

This gives the app a resident execution path without changing the existing
payload semantics or introducing higher-complexity IPC mechanisms.

## Registry / UI Changes

### Toolchain Model

`AiToolchainTable.execution_mode` stops being normalized to a single fixed
value.

Supported persisted values are:

- `resident_process`
- `oneshot_process`

New toolchain drafts default to `resident_process`.

Unsupported values must be rejected at save time by the service layer even if a
caller bypasses the UI.

### AI Tool Management Page

`AI工具管理` must change from:

- read-only helper text that says `execution_mode = oneshot_process`

to:

- an editable `execution_mode` selector with two supported values

Suggested user-facing labels:

- `resident_process` -> `常驻进程（推荐）`
- `oneshot_process` -> `单次进程`

The helper text should explain:

- `protocol_version = 1` remains fixed
- `resident_process` is recommended for preloaded models
- `oneshot_process` is for toolchains that still use the file-based CLI mode

### Save Validation

Toolchain save behavior must:

- accept only the two supported execution modes
- default missing execution mode in new drafts to `resident_process`
- preserve the user-selected supported value on update
- reject unsupported strings with a user-visible validation error

## Runtime Routing

### Dispatch Decision

`AiService.run_ai_analysis` must route execution by the selected toolchain row's
`execution_mode`.

- `execution_mode == "oneshot_process"`:
  - use the current file-based subprocess contract
- `execution_mode == "resident_process"`:
  - use the resident-process manager and the JSON Lines stdio protocol

No other execution mode is supported in this pass.

### Resident Timeout Contract

Resident mode uses the resolved AI binding timeout as one per-request deadline:

- deadline source: `binding["effective_timeout_ms"]`
- deadline start: after the run has acquired the resident execution lease for
  that `toolchain_id`
- deadline covers:
  - resident process startup when no healthy cached process exists
  - request write to `stdin`
  - wait for exactly one response envelope on `stdout`
- deadline does not cover waiting behind another in-flight resident request,
  because this pass does not queue overlapping requests

If the deadline is exceeded:

- current run gets `module_status = "timeout"`
- cached resident process is terminated and discarded

Resident-process shutdown uses a fixed internal grace period of 2000 ms before
forced termination.

### No Fallback Rule

If a resident-process run fails because of:

- process launch failure
- timeout
- broken pipe
- malformed JSON Lines message
- invalid resident response envelope
- invalid logical response payload

the run fails directly.

The main app must not:

- auto-restart as `oneshot_process`
- silently remap the toolchain to `oneshot_process`
- retry the same request via the one-shot path

## Resident Process Lifecycle

### Ownership

The main app owns resident toolchain processes.

`AiService` must manage a resident-process cache keyed by the persisted
`toolchain_id` string, and must reuse the live process for repeated requests to
that same toolchain id during the app session.

The resident manager must also own one coordination lock per persisted
`toolchain_id`.

### Start / Reuse / Reset

Required behavior:

- lazily start the resident process on the first request for that toolchain
- reuse the same live process for later requests while it remains healthy
- if the cached process exits, breaks protocol, or times out, discard it
- the next request may spawn a fresh resident process for the same toolchain
- hold the per-toolchain execution lease from just before resident process
  selection / spawn until the current run has finished response handling

Rename does not preserve cache identity:

- cached process for old `toolchain_id` stays in the old cache slot until
  invalidated
- later runs for the renamed row use the new `toolchain_id` and therefore a
  fresh resident process

### Registry Mutation Invalidation

Toolchain registry mutations must invalidate affected resident processes.

Required behavior:

- save / rename / delete for a resident toolchain must attempt to acquire the
  same per-toolchain coordination lock in non-blocking mode
- if the lock is unavailable because a resident request is in flight, mutation
  fails fast with a user-visible busy error and no registry mutation
- save of an existing resident toolchain row stops any cached resident process
  for that row before later reuse
- rename stops the old cached resident process; later runs use the new
  toolchain id and a fresh process
- delete stops the cached resident process before or during deletion
- application shutdown stops all cached resident processes

This prevents the app from reusing stale entrypoints or outdated runtime mode
metadata after registry edits.

### In-Flight Concurrency

This pass supports one in-flight request at a time per resident toolchain
process.

Multiple overlapping `run_ai_analysis` calls for the same resident
`toolchain_id` do not queue and do not block waiting for the current run.

Required behavior:

- first run acquires the per-toolchain execution lease and proceeds normally
- second overlapping run for the same resident toolchain fails fast
- fast-fail result:
  - `module_status = "process_error"`
  - `error_summary = "Resident toolchain is busy"`

This pass therefore forbids multiplexing, forbids queuing, and keeps timeout
accounting deterministic.

## Resident StdIO Protocol

### Transport Rules

Resident-process transport rules:

- host launches the configured `entrypoint` command exactly as stored in the
  registry, with no host-appended resident-mode flags or per-request argv
- resident-mode `entrypoint` must therefore already be a self-contained
  long-lived command that starts the stdio protocol loop
- host inherits the parent process environment unchanged
- host inherits the current application working directory unchanged
- host writes one UTF-8 JSON object per line to the child's `stdin`
- child writes one UTF-8 JSON object per line to `stdout`
- child writes human-readable logs only to `stderr`
- host flushes `stdin` immediately after writing each request line
- child flushes `stdout` immediately after writing each protocol response line
- protocol messages must never be mixed with arbitrary non-JSON text on
  `stdout`
- host must continuously drain `stderr` for the lifetime of the resident
  process so the child cannot block on a full stderr pipe
- normal resident protocol messages on `stdout` are not treated as
  `stdout_excerpt` log output

### Resident Stderr Capture

The resident manager must continuously read `stderr` in the background and keep
an in-memory rolling log buffer for each cached resident process.

Buffer rules:

- cap the rolling stderr buffer at 64 KiB per resident process
- when the cap is exceeded, evict the oldest bytes first

Per-run attribution rules:

- when a run starts, record the current resident stderr buffer position
- when the run ends, capture newly drained stderr text from that start position
- persist that per-run excerpt into `stderr_excerpt`, truncated using the same
  excerpt limit policy as one-shot execution
- resident protocol frames read from `stdout` are never copied into
  `stdout_excerpt`

### Request Envelope

For each analysis, the host sends one JSON Lines message:

```json
{
  "message_type": "analyze",
  "request_id": "req_20260407_123456_abcd1234",
  "request": {
    "protocol_version": 1,
    "request_id": "req_20260407_123456_abcd1234",
    "test_run_id": "run_001",
    "toolchain_id": "motor-ai",
    "segments": [...],
    "context": {...},
    "params": {...},
    "window_info": {...}
  },
  "artifacts_dir": "D:/.../artifacts"
}
```

Contract details:

- `request` uses the same logical payload schema already written to
  `request.json`
- `artifacts_dir` remains an absolute filesystem path owned by the host
- no audio bytes, STFT matrices, or other large binary payloads are embedded in
  this pass

### Response Envelope

The child returns exactly one JSON Lines response for each analyze request.

Success shape:

```json
{
  "message_type": "result",
  "request_id": "req_20260407_123456_abcd1234",
  "response": {
    "protocol_version": 1,
    "request_id": "req_20260407_123456_abcd1234",
    "toolchain_name": "...",
    "toolchain_version": "...",
    "ai_result": {...}
  }
}
```

Failure shape:

```json
{
  "message_type": "error",
  "request_id": "req_20260407_123456_abcd1234",
  "error": "human-readable failure summary"
}
```

Rules:

- `message_type = result` must include a full logical AI response payload under
  `response`
- `message_type = error` is treated as a process-level failure for the current
  run
- missing, mismatched, or malformed `request_id` values are protocol failures
- malformed JSON, unexpected message types, or extra unsolicited stdout lines
  are protocol failures

`message_type = error` required behavior:

- current run gets `module_status = "process_error"`
- `error` becomes the current run's `error_summary`
- no `response.json` is written
- the resident process is discarded from cache after the run

If a valid `message_type = "result"` envelope has already completed the current
run and the resident process later emits any extra unsolicited stdout line while
idle, the completed run stays successful, but the cached resident process is
immediately invalidated before the next reuse.

### Shutdown

At application shutdown, or when invalidating a cached resident process, the
host may try to send:

```json
{"message_type": "shutdown"}
```

The host may then terminate the process if it does not exit promptly.

The shutdown handshake is best-effort cleanup, not part of normal analysis
results.

## Request / Response Persistence

Even for `resident_process`, the main app must continue to create an AI run
directory and persist:

- `request.json`
- `artifacts/`
- `response.json` when a valid logical response exists

Required behavior:

- write `request.json` before dispatching the resident request
- write `response.json` only when the resident transport yields a valid logical
  AI response payload
- reuse the existing validation and persistence flow as much as possible after
  the transport layer has produced a logical response payload

This keeps auditability and debug behavior consistent between execution modes.
Resident transport failures and explicit resident `message_type = "error"`
messages do not produce `response.json`.

## Error Handling

### Resident Launch / Transport Errors

Resident-process launch or transport failures should map to existing execution
status categories where possible:

- startup failure / broken pipe / unexpected child exit -> `process_error`
- timeout waiting for a response line -> `timeout`
- stdout line that is not valid JSON -> `protocol_error`
- parsed resident envelope with unexpected `message_type`, missing
  `request_id`, mismatched outer `request_id`, or missing `response` on a
  `message_type = "result"` envelope -> `protocol_error`
- logical response payload inside `response` keeps the existing validation
  split:
  - response-level schema mismatches such as wrong `protocol_version`, wrong
    inner `request_id`, missing `ai_result`, or missing `ai_label` ->
    `protocol_error`
  - payload type/value violations that are already treated as bad tool output
    today, such as invalid numeric field types or invalid artifact-path shapes
    -> `invalid_output`

Resident `message_type = "error"` is not part of the logical response schema;
it always maps to `process_error`.

### Cache Reset On Failure

If a resident process:

- exits unexpectedly
- times out
- returns malformed protocol data

the cached process must be discarded so the next analysis starts from a clean
resident process.

## Testing Strategy

Add or update regression coverage for:

- `AiToolchainTable` default execution mode for new rows
- `AiService.save_toolchain` accepting supported execution modes and rejecting
  unsupported ones
- `AI工具管理` execution-mode selector defaulting to `resident_process`
- `AI工具管理` save/edit flows preserving selected execution mode
- `AiService.run_ai_analysis` routing to `oneshot_process` vs
  `resident_process`
- resident-process reuse across repeated runs of the same toolchain
- resident-process invalidation after toolchain save / rename / delete
- resident save / rename / delete being rejected while that toolchain has an
  in-flight resident request
- resident overlapping analyze calls failing fast instead of queuing
- resident transport timeout and protocol-failure handling
- resident `message_type = error` mapping to `process_error`, no
  `response.json`, and cache discard
- explicit proof that resident failures do not fall back to one-shot execution
- continued audit-file persistence (`request.json`, `response.json`,
  `artifacts_dir`) for resident-mode runs

Verification should include:

- targeted service tests
- targeted AI tool management UI tests
- resident integration tests using repo-local fake resident toolchain scripts
- full `pytest` before implementation completion

## Files Likely Affected

- `app/service/ai_service.py`
- `app/service/db_connection_service.py`
- `model/ai_toolchain_table.py`
- `ui/modules/ai_tool_management.py`
- `tests/test_ai_service.py`
- `tests/test_ai_service_integration.py`
- `tests/test_ai_tool_management_module.py`
- `tests/test_ai_toolchain_table.py`
- `tests/test_db_connection_service.py`

## Non-Goals

This pass does not introduce:

- WebSocket-based AI transport
- shared-memory audio or STFT exchange
- remote AI serving
- multi-request multiplexing on one resident process
- automatic conversion of existing external one-shot toolchains into resident
  toolchains
