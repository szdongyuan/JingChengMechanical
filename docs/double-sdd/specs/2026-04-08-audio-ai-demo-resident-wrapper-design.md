# Audio AI Demo Resident Wrapper Design

## Summary

The sibling project `jingcheng-audio-ai-demo-toolchain` currently exposes only a
one-shot CLI entrypoint: it reads `request.json`, loads the model bundle,
performs one inference, writes `response.json`, and exits.

That is useful for protocol validation, but it is not a good example of how to
package a model as a resident toolchain for the main app's new
`resident_process` execution mode.

The user-approved direction for this pass is:

- keep the existing one-shot demo entrypoint
- add a separate resident demo entrypoint in the sibling repo
- prioritize clean structure over compatibility workarounds
- design the resident demo for future model replacement, not fixed single-model
  startup binding
- keep software-side configuration simple by continuing to pass `model_dir`
  through `params_json`
- avoid forcing every future external model project to rewrite the same
  resident-protocol boilerplate

The recommended design is:

- add a small internal resident helper for stdio JSON Lines protocol handling
- refactor the demo inference layer so resident execution can reuse loaded model
  bundles instead of reloading on every request
- cache loaded bundles inside one resident process by `params.model_dir`
- keep the helper internal to the demo project, not a promised public framework

## Problem

### The Current Demo Only Shows One-Shot Packaging

The existing sibling project documents registration of:

- `python.exe -m audio_ai_demo_toolchain.cli`

That entrypoint requires `--request`, `--response`, and `--artifacts-dir`, so
it only demonstrates file-based one-shot execution. It does not show:

- a long-lived stdio request loop
- resident request/response framing
- process-local model reuse
- shutdown handling
- resident-safe error mapping

### Model Selection Is Already Request-Scoped

The current demo does not represent a fixed single-model executable. It already
loads the model bundle from `params.model_dir` inside each request.

That matters because the main app can run multiple configs in one program
session, and each config has its own AI binding `params_json`. Therefore a
single resident toolchain process may need to serve requests for different
`model_dir` values over time.

A resident demo that binds one process to one fixed model would be a poor match
for the actual request model already used by the app.

### Repeating Resident Boilerplate Would Be Wasteful

If the resident demo is implemented as a one-off script with protocol loop,
error mapping, and cache logic mixed directly into model-specific code, future
external toolchains will still need to rewrite substantial boilerplate.

This pass should instead show a clean, minimal pattern that future model
projects can follow with much less repeated resident-wrapper code.

## Scope

In scope:

- preserve the current one-shot CLI entrypoint
- add a separate resident demo entrypoint in
  `jingcheng-audio-ai-demo-toolchain`
- refactor inference flow so loaded bundles can be reused across resident
  requests
- introduce a small internal helper for resident stdio JSON Lines handling
- cache loaded bundles inside the resident process by normalized absolute
  `model_dir`
- allow one resident process to serve requests from multiple configs and
  multiple model directories
- keep request-side model selection in `params.model_dir`
- optionally support startup preloading of one or more model directories
- add regression coverage for resident protocol behavior, bundle reuse, and
  multi-model cache behavior
- update the sibling repo README with resident registration guidance

Out of scope:

- changing the main app's resident protocol
- adding WebSocket, sockets, shared memory, or direct binary transfer
- introducing a stable public SDK or separately versioned resident-wrapper
  package
- adding cache eviction policy tuning or production memory governance
- optimizing model accuracy, training behavior, or bundle schema
- removing the current one-shot CLI
- changing the main app's config schema or UI shape for AI binding parameters
- solving remote serving or multi-client concurrency

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces:
  - the existing one-shot CLI entrypoint
    `python -m audio_ai_demo_toolchain.cli`
  - the current bundle directory contract:
    `model.pt`, `model_meta.json`, `training_report.json`
  - request-side use of `params.model_dir` and `params.decision_threshold`
  - the current logical request/response schema version (`protocol_version = 1`)
- Allowed breakage:
  - internal module layout inside `jingcheng-audio-ai-demo-toolchain`
  - current inference helper function boundaries and signatures
  - introduction of new resident-only modules and helper abstractions
  - README registration instructions expanding from one entrypoint to two
- Migration strategy: none; preserve the one-shot path, but allow a clean
  internal refactor with no compatibility layer for old internal call graphs

## Approved Decisions

1. The sibling demo project keeps the current one-shot CLI and adds a separate
   resident entrypoint.
2. The resident entrypoint is a first-class example of how to package a
   long-lived model toolchain for the main app's `resident_process` mode.
3. Resident requests continue to choose the model via `params.model_dir` rather
   than binding one process to one fixed model.
4. One resident process may serve requests for multiple `model_dir` values.
5. Loaded bundles are cached in process memory by normalized absolute
   `model_dir`.
6. The first version uses a simple cache dictionary with no eviction policy.
   This keeps the example clean and easy to understand.
7. Optional startup preloading of one or more model directories is supported as
   a convenience, not as the only loading mode.
8. A small internal helper is preferred over a monolithic resident demo script.
9. That helper is internal to the demo project and not a promised public API.
10. The main app's software-side configuration remains request-driven through
    per-config `params_json`, with `model_dir` as the primary model selector.

## Design Summary

Refactor the sibling toolchain into four responsibilities:

- one-shot CLI wrapper
- resident stdio helper
- bundle loading / caching
- pure inference against an already loaded bundle

The resident path should demonstrate this model:

- the main app starts one long-lived command such as
  `python.exe -u -m audio_ai_demo_toolchain.resident_cli`
- the resident process reads JSON Lines messages from `stdin`
- each request resolves `params.model_dir`
- the resident process reuses a cached loaded bundle when available
- otherwise it loads the bundle once, stores it in memory, and serves the
  request
- the process writes one JSON Lines result or error envelope to `stdout`
- human-readable logs and tracebacks go to `stderr`

This produces a clean reference implementation without forcing future model
projects to copy a large amount of protocol-loop code into every model script.

## Detailed Design

### External Entrypoints

The sibling repo exposes two explicit user-facing modes:

- one-shot:
  - `python.exe -m audio_ai_demo_toolchain.cli`
- resident:
  - `python.exe -u -m audio_ai_demo_toolchain.resident_cli`

The one-shot CLI remains file-based and unchanged in purpose.

The resident CLI becomes the registration target for the main app's
`resident_process` mode.

### Inference Refactor

The current `infer.py` mixes:

- request-driven bundle loading
- waveform preparation
- model execution
- artifact writing
- response building

This pass should separate those concerns so resident mode can reuse the already
loaded bundle.

Required target shape:

- one function that loads and validates a bundle from `model_dir`
- one function that runs inference using an already loaded bundle plus the
  request payload
- one thin one-shot wrapper that preserves current CLI behavior by:
  - loading the bundle from the request
  - calling the bundle-based inference core

The exact file split is implementation-defined, but the structure must make the
resident path reuse loaded bundles directly rather than routing back through a
reload-per-request wrapper.

### Resident Helper

Add a small internal helper module responsible only for resident transport and
protocol-loop concerns:

- read UTF-8 JSON Lines from `stdin`
- validate the exact top-level resident request envelope shape
- dispatch only supported `message_type` values
- write exactly one JSON Lines response per analyze request
- emit best-effort shutdown behavior on `message_type = "shutdown"`
- route internal errors to `stderr`
- convert request-scoped analyze failures into protocol `message_type = "error"`

This helper must stay intentionally small. It is not a framework layer and
should not introduce plugin systems, abstract registries, or generalized
multi-protocol support.

The helper supports exactly two inbound message types:

- `analyze`
- `shutdown`

For `analyze`, the accepted resident request envelope is:

```json
{
  "message_type": "analyze",
  "request_id": "req_20260408_120000_abcd1234",
  "request": {
    "protocol_version": 1,
    "request_id": "req_20260408_120000_abcd1234",
    "test_run_id": "run_001",
    "toolchain_id": "audio-demo",
    "segments": [],
    "context": {},
    "params": {
      "model_dir": "D:/models/demo"
    },
    "window_info": {}
  },
  "artifacts_dir": "D:/host-run/artifacts"
}
```

Validation rules for the helper:

- top-level JSON value must be an object
- `message_type` must be a string
- `analyze` requires string `request_id`
- `analyze` requires object `request`
- `analyze` requires string `artifacts_dir`
- `artifacts_dir` must be an absolute host-owned filesystem path
- `request.request_id` must equal the outer `request_id`
- `request.protocol_version` must be `1`
- `shutdown` requires no response payload and no additional contract beyond
  best-effort exit

For a successful analyze request, the helper writes exactly one resident result
envelope to `stdout`:

```json
{
  "message_type": "result",
  "request_id": "req_20260408_120000_abcd1234",
  "response": {
    "protocol_version": 1,
    "request_id": "req_20260408_120000_abcd1234",
    "toolchain_name": "Audio AI Demo",
    "toolchain_version": "1.0",
    "ai_result": {
      "ai_label": "PASS",
      "score": 0.91,
      "summary": "OK",
      "confidence": 0.88,
      "artifacts": [
        {
          "type": "report_json",
          "relative_path": "report.json"
        }
      ]
    }
  }
}
```

The inner `response` payload must use the same logical response schema as the
existing one-shot CLI output. The resident helper does not define a separate
logical AI payload shape.

### Resident Adapter

The demo resident entrypoint owns the model-specific resident behavior:

- parse optional startup arguments such as repeated `--preload-model-dir`
- create a process-local cache keyed by normalized absolute `model_dir`
- on each analyze request:
  - parse `params.model_dir`
  - normalize it to the cache key
  - load and cache the bundle on miss
  - run inference with the cached bundle
  - persist artifacts to the host-provided `artifacts_dir`
  - return the standard logical success payload inside the resident result
    envelope

The resident adapter, not the generic helper, owns model caching because the
cache key and loaded object type are model-domain concerns.

### Model Cache Rules

The first version uses:

- cache key: normalized absolute `model_dir`
- cache value: fully loaded validated bundle including model and metadata
- storage: in-process dictionary for the lifetime of the resident process
- eviction: none

Cache behavior:

- first request for a `model_dir` loads the bundle and stores it
- later requests for the same `model_dir` reuse it
- requests for a different `model_dir` load an additional entry
- a failed load must not leave a partial or poisoned cache entry behind
- startup preloading populates the same cache used by normal requests

No cache-size policy is added in this pass because the demo is intended to show
clean resident packaging, not production-grade memory management policy.

### Preload Behavior

The resident entrypoint supports optional repeated preload arguments:

- `--preload-model-dir D:/models/a`
- `--preload-model-dir D:/models/b`

Rules:

- preload is optional
- preload uses the same validation and load path as on-demand requests
- preload failure stops startup with a non-zero exit code and clear `stderr`
- model directories not preloaded may still be loaded later on request

This resolves the earlier product tension:

- startup preloading is available when warm startup matters
- on-demand loading is still available for additional models

### Multi-Config Behavior

The resident demo must be designed for the main app's actual usage pattern:

- the host caches resident processes by registered toolchain
- config-specific binding values live in `params_json`
- multiple configs may reuse one toolchain entry while varying `params_json`

Therefore the resident demo must not key its internal cache by `config_id` or
assume one config per process. The effective model selection boundary is the
request's `model_dir`.

### Error Handling

The resident demo must distinguish inbound protocol violations from
request-scoped analyze failures.

Inbound protocol violations mean the host message itself is not a valid
resident request envelope. Those failures are treated as fatal for the helper,
not as recoverable analyze results.

Resident analyze failures mean the envelope is valid, the request has a stable
`request_id`, and the demo failed while validating model inputs, loading a
bundle, running inference, or writing artifacts for that specific request.

Resident analyze failures such as:

- missing or invalid `params.model_dir`
- unreadable bundle directory
- missing `model.pt`
- bad metadata
- unreadable audio file

must return this resident error envelope:

```json
{
  "message_type": "error",
  "request_id": "...",
  "error": "human-readable summary"
}
```

Rules:

- a valid `message_type = "error"` response is only allowed after the helper
  has already validated the inbound analyze envelope and captured a valid
  `request_id`
- request failures during bundle load must not cache partial bundle state
- unexpected internal exceptions during an active analyze request must be
  logged to `stderr` and converted into the same `message_type = "error"`
  envelope when a valid `request_id` is already known
- the main app's current resident runtime contract treats
  `message_type = "error"` as a current-run `process_error` and discards the
  cached resident process after that response
- this wrapper spec does not change that host behavior and does not promise
  continued host-side reuse after an emitted `message_type = "error"`
- successful requests still return the same logical response payload schema used
  by the one-shot CLI

The helper's behavior matrix is:

| Condition | Helper stdout | Helper stderr | Helper process behavior | Expected host effect |
| --- | --- | --- | --- | --- |
| Malformed JSON line on `stdin` | no response line | log parse failure | exit non-zero immediately | current run fails; host discards resident cache |
| Parsed JSON value is not an object | no response line | log protocol violation | exit non-zero immediately | current run fails; host discards resident cache |
| Missing / invalid top-level `message_type` | no response line | log protocol violation | exit non-zero immediately | current run fails; host discards resident cache |
| Unsupported inbound `message_type` other than `analyze` / `shutdown` | no response line | log protocol violation | exit non-zero immediately | current run fails; host discards resident cache |
| `analyze` missing / invalid `request_id` | no response line | log protocol violation | exit non-zero immediately | current run fails; host discards resident cache |
| `analyze` missing / invalid `request` object | no response line | log protocol violation | exit non-zero immediately | current run fails; host discards resident cache |
| `analyze` missing / invalid `artifacts_dir` | no response line | log protocol violation | exit non-zero immediately | current run fails; host discards resident cache |
| `analyze` has mismatched inner / outer `request_id` or wrong `request.protocol_version` | no response line | log protocol violation | exit non-zero immediately | current run fails; host discards resident cache |
| Valid `analyze` envelope but request-scoped validation / load / inference / artifact-write failure | one `message_type = "error"` line with matching `request_id` | log human-readable error and traceback when applicable | remain in loop after flush, but no reuse guarantee is assumed | host maps current run to `process_error` and discards resident cache |
| Valid `analyze` envelope and successful inference | one `message_type = "result"` line with matching `request_id` | optional diagnostic logs only | continue loop | host may reuse resident process |
| `shutdown` | no response line | optional shutdown log | best-effort clean exit with zero status | host treats shutdown as cleanup |

### README Guidance

The sibling repo README must document both registration modes:

- one-shot registration
- resident registration

It must also explain the intended configuration model:

- register the resident entrypoint once
- select `resident_process` in the main app
- configure per-binding `params_json` with `model_dir`
- optionally use preload flags in the registered command when a deployment
  wants startup warmup for known bundles

## Testing Strategy

Required regression coverage in the sibling repo:

- existing one-shot CLI tests remain green
- resident happy path for one request
- two resident requests for the same `model_dir` reuse one loaded bundle
- two resident requests for different `model_dir` values load two separate
  bundles
- preload arguments populate cache before first request
- failed bundle load does not poison cache
- resident error envelope is emitted for request-level failures
- malformed JSON input exits non-zero without writing a stdout protocol line
- invalid analyze envelopes (`request_id`, `request`, `artifacts_dir`,
  `protocol_version`, unsupported `message_type`) fail as fatal protocol
  violations instead of pretending to be valid analyze results
- resident shutdown message exits cleanly
- resident path still writes the same logical artifacts and success payload
  shape as the one-shot path

Verification should prioritize wrapper behavior and clean packaging boundaries,
not model quality or training accuracy.

## Non-Goals / Future Work

Future passes may add:

- cache eviction or size controls
- model unload commands
- richer health / stats introspection
- extracting the resident helper into a separately reusable shared package

Those are intentionally excluded here to keep the example compact and clean.
