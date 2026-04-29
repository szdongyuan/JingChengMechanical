# Audio AI Demo Resident Wrapper Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a resident-process demo entrypoint to the sibling `jingcheng-audio-ai-demo-toolchain` repo while preserving the current one-shot CLI and keeping model selection request-driven by `params.model_dir`.

**Architecture:** Implement the resident example in four slices. First refactor the inference layer so one-shot and resident execution can share the same loaded-bundle inference core. Then add a small resident JSONL helper that owns only transport and envelope validation. Next add a model-specific resident adapter plus a thin `resident_cli` entrypoint that manages the per-process bundle cache and optional preload arguments. Finally document resident registration in the sibling README and run the full sibling-project verification suite.

**Tech Stack:** Python 3.12, PyTorch, NumPy, SciPy, subprocess, pytest

---

**Spec:** `D:/Work/jingchengpython/JingChengMechanical/docs/double-sdd/specs/2026-04-08-audio-ai-demo-resident-wrapper-design.md`

**Implementation Repo:** `D:/Work/jingchengpython/jingcheng-audio-ai-demo-toolchain`

**Execution Note:** The spec document lives in the `JingChengMechanical` repo at the absolute path above. All implementation file paths and commands below are relative to the sibling repo root at `D:/Work/jingchengpython/jingcheng-audio-ai-demo-toolchain`, executed from that repo's dedicated worktree.

## File Map

- `src/audio_ai_demo_toolchain/infer.py`
  Refactor the current one-shot orchestration into reusable loaded-bundle inference helpers while keeping `run_inference()` as the thin one-shot wrapper.

- `src/audio_ai_demo_toolchain/resident_protocol.py`
  New internal helper for resident stdio JSON Lines parsing, exact analyze/shutdown envelope validation, result/error envelope writing, and fatal protocol violation handling.

- `src/audio_ai_demo_toolchain/resident_adapter.py`
  New model-specific resident adapter that owns the normalized-absolute-`model_dir` cache, preload behavior, and request handling on top of the refactored inference core.

- `src/audio_ai_demo_toolchain/resident_cli.py`
  New thin CLI entrypoint that parses preload args, wires the adapter into the resident protocol helper, and becomes the command registered for `resident_process`.

- `README.md`
  Expand setup/registration guidance from one-shot only to both one-shot and resident modes, including optional preload examples and request-side `params_json` usage.

- `tests/test_inference_smoke.py`
  Extend inference coverage to lock down the new loaded-bundle inference core and guarantee the existing one-shot wrapper still behaves the same.

- `tests/test_resident_protocol.py`
  New focused tests for resident envelope validation, fatal malformed-input behavior, error/result envelope formatting, and shutdown handling.

- `tests/test_resident_adapter.py`
  New focused tests for cache reuse, multi-model behavior, preload population, and failed-load cache hygiene without needing subprocess orchestration.

- `tests/test_resident_cli.py`
  New subprocess-level tests that exercise the real resident stdio loop, request/result/error behavior, fatal protocol violations, and clean shutdown.

## Task 1: Refactor The Inference Core Around Loaded Bundles

**Files:**
- Modify: `src/audio_ai_demo_toolchain/infer.py`
- Modify: `tests/test_inference_smoke.py`

- [ ] **Step 1: Write the failing loaded-bundle inference tests**

Add focused tests such as:

```python
def test_run_inference_with_loaded_bundle_matches_one_shot_wrapper(tmp_path: Path) -> None:
    audio_path = tmp_path / "input.wav"
    _write_wav(audio_path)
    model_dir = _write_model_bundle(tmp_path / "model_bundle")
    request_payload = _base_request(audio_path=audio_path, model_dir=model_dir)

    bundle = load_model_bundle(str(model_dir))
    direct = run_inference_with_bundle(
        request_payload,
        bundle=bundle,
        artifacts_dir=tmp_path / "direct_artifacts",
    )
    wrapped = run_inference(
        request_payload,
        artifacts_dir=tmp_path / "wrapped_artifacts",
    )

    validate_success_response(direct)
    validate_success_response(wrapped)
    assert direct["ai_result"]["score"] == pytest.approx(wrapped["ai_result"]["score"])
    assert direct["ai_result"]["ai_label"] == wrapped["ai_result"]["ai_label"]
```

Also add a focused test proving the bundle-backed helper still writes the standard logical artifacts into the caller-provided `artifacts_dir`.

- [ ] **Step 2: Run the focused inference-core tests to confirm they fail**

Run:

```bash
pytest tests/test_inference_smoke.py -k "loaded_bundle or bundle_backed or run_inference_with_bundle" -v
```

Expected: FAIL because `infer.py` does not yet expose a loaded-bundle inference path.

- [ ] **Step 3: Implement the minimal bundle-backed inference split**

Refactor `src/audio_ai_demo_toolchain/infer.py` so it exposes:

```python
def load_model_bundle(model_dir_value: object) -> LoadedBundle:
    ...


def run_inference_with_bundle(
    request_payload: dict[str, Any],
    *,
    bundle: LoadedBundle,
    artifacts_dir: str | Path,
) -> dict[str, Any]:
    ...


def run_inference(
    request_payload: dict[str, Any],
    *,
    artifacts_dir: str | Path,
) -> dict[str, Any]:
    params = request_payload["params"]
    bundle = load_model_bundle(params.get("model_dir"))
    return run_inference_with_bundle(
        request_payload,
        bundle=bundle,
        artifacts_dir=artifacts_dir,
    )
```

Keep the existing one-shot behavior intact:

- `run_inference()` still chooses the model from `params.model_dir`
- logical success payload shape stays unchanged
- artifact writing stays unchanged
- the refactor only separates bundle loading from bundle-backed execution

- [ ] **Step 4: Re-run the focused inference-core tests**

Run:

```bash
pytest tests/test_inference_smoke.py -k "loaded_bundle or bundle_backed or run_inference_with_bundle" -v
```

Expected: PASS

- [ ] **Step 5: Commit the inference-core slice**

Run:

```bash
git add src/audio_ai_demo_toolchain/infer.py tests/test_inference_smoke.py
git commit -m "refactor: split bundle-backed inference core"
```

## Task 2: Add The Resident JSONL Protocol Helper

**Files:**
- Create: `src/audio_ai_demo_toolchain/resident_protocol.py`
- Create: `tests/test_resident_protocol.py`

- [ ] **Step 1: Write the failing resident-protocol tests**

Create `tests/test_resident_protocol.py` with focused coverage like:

```python
def test_serve_forever_exits_nonzero_on_malformed_json() -> None:
    stdin = io.StringIO("{bad-json\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = serve_forever(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        handle_analyze=lambda _request_id, _request, _artifacts_dir: None,
    )

    assert exit_code != 0
    assert stdout.getvalue() == ""
    assert "json" in stderr.getvalue().lower()


def test_serve_forever_writes_result_envelope_for_valid_analyze() -> None:
    stdin = io.StringIO(json.dumps(_analyze_envelope()) + "\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = serve_forever(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        handle_analyze=lambda request_id, request, artifacts_dir: _success_response(request_id),
    )

    assert exit_code == 0
    envelope = json.loads(stdout.getvalue().strip())
    assert envelope["message_type"] == "result"
    assert envelope["request_id"] == "req-123"


def test_serve_forever_rejects_missing_top_level_message_type() -> None:
    stdin = io.StringIO(json.dumps({"request_id": "req-123"}) + "\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = serve_forever(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        handle_analyze=lambda _request_id, _request, _artifacts_dir: None,
    )

    assert exit_code != 0
    assert stdout.getvalue() == ""
    assert "message_type" in stderr.getvalue()


def test_serve_forever_logs_traceback_and_keeps_loop_after_unexpected_handler_error() -> None:
    stdin = io.StringIO(
        json.dumps(_analyze_envelope()) + "\n" +
        json.dumps({"message_type": "shutdown"}) + "\n"
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    def boom(_request_id: str, _request: dict[str, object], _artifacts_dir: str) -> dict[str, object]:
        raise RuntimeError("boom")

    exit_code = serve_forever(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        handle_analyze=boom,
    )

    assert exit_code == 0
    envelope = json.loads(stdout.getvalue().strip())
    assert envelope["message_type"] == "error"
    assert "Traceback" in stderr.getvalue()
```

Also cover:

- parsed JSON line whose top-level value is not an object -> fatal non-zero exit with no stdout protocol line
- missing top-level `message_type` -> fatal non-zero exit with no stdout protocol line
- top-level `message_type` present but not a string -> fatal non-zero exit with no stdout protocol line
- unsupported inbound `message_type` -> fatal non-zero exit with no stdout protocol line
- missing or invalid `request_id` / `request` -> fatal non-zero exit with no stdout protocol line
- relative or otherwise non-absolute `artifacts_dir` -> fatal non-zero exit with no stdout protocol line
- mismatched inner/outer `request_id` or wrong `request.protocol_version` -> fatal non-zero exit
- valid resident envelope whose inner logical request fails existing
  `validate_request_payload()` rules for `segments`, `params`, or `window_info`
  -> helper logs the request validation failure to `stderr`, emits one matching
  resident `error` envelope, and stays in the loop
- handler returns a logical success payload whose `response.request_id` does not
  match the outer request id -> helper logs to `stderr` and emits a matching
  resident `error` envelope instead of a `result`
- request-scoped handler failure -> one `message_type = "error"` line with matching `request_id`
- request-scoped handler failure followed by `shutdown` -> helper stays in the loop after flushing the `error` line, then exits cleanly on `shutdown`
- `shutdown` -> zero exit and no response line

- [ ] **Step 2: Run the resident-protocol tests to confirm they fail**

Run:

```bash
pytest tests/test_resident_protocol.py -v
```

Expected: FAIL because the resident protocol helper does not exist yet.

- [ ] **Step 3: Implement the small resident protocol helper**

Create `src/audio_ai_demo_toolchain/resident_protocol.py` with a small API similar to:

```python
def serve_forever(
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    handle_analyze: Callable[[str, dict[str, Any], str], dict[str, Any]],
) -> int:
    ...
```

Implementation responsibilities:

- read one UTF-8 JSON object per line
- treat `stdin` / `stdout` / `stderr` as already-configured UTF-8 text streams;
  do not silently rely on platform-default encodings inside the helper
- validate the exact `analyze` / `shutdown` resident envelope contract
- validate the inner logical request payload with the existing
  `audio_ai_demo_toolchain.protocol.validate_request_payload()` rules before
  calling the analyze handler
- for valid analyze requests, call `handle_analyze(request_id, request_payload, artifacts_dir)`
- on successful handler return, validate the logical success payload with the
  existing one-shot response validator, then emit one `message_type = "result"`
  line with that payload under `response`
- require `response.request_id == outer request_id` before writing a result
  envelope; a mismatch is an internal request-scoped failure, not a valid
  success path
- on request-scoped handler errors after a valid `request_id` is known, emit one `message_type = "error"` line
- reject any `artifacts_dir` that is not an absolute filesystem path before
  calling the analyze handler
- when an unexpected handler exception occurs during an active analyze request,
  write the exception summary and traceback to `stderr`, then emit the matching
  `message_type = "error"` envelope
- on malformed JSON or invalid envelopes, write only to `stderr`, emit no stdout protocol frame, and exit non-zero immediately
- flush `stdout` after every emitted protocol line
- after emitting a request-scoped `message_type = "error"` line, remain in the
  request loop and continue reading later inbound lines until EOF, fatal
  protocol violation, or `shutdown`

Keep this helper transport-only:

- no model loading
- no cache
- no preload parsing
- no toolchain-specific path normalization

- [ ] **Step 4: Re-run the resident-protocol tests**

Run:

```bash
pytest tests/test_resident_protocol.py -v
```

Expected: PASS

- [ ] **Step 5: Commit the resident-protocol slice**

Run:

```bash
git add src/audio_ai_demo_toolchain/resident_protocol.py tests/test_resident_protocol.py
git commit -m "feat: add resident jsonl protocol helper"
```

## Task 3: Add The Resident Adapter, Cache, And CLI Entrypoint

**Files:**
- Create: `src/audio_ai_demo_toolchain/resident_adapter.py`
- Create: `src/audio_ai_demo_toolchain/resident_cli.py`
- Create: `tests/test_resident_adapter.py`
- Create: `tests/test_resident_cli.py`
- Modify: `src/audio_ai_demo_toolchain/infer.py`

- [ ] **Step 1: Write the failing adapter and resident-cli tests**

Create focused adapter tests like:

```python
def test_adapter_reuses_loaded_bundle_for_same_model_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_dir = tmp_path / "model_bundle"
    request_payload = _request_payload(model_dir=model_dir)
    load_calls: list[str] = []

    def fake_loader(model_dir_value: object) -> LoadedBundle:
        load_calls.append(str(model_dir_value))
        return _fake_bundle(model_dir)

    monkeypatch.setattr("audio_ai_demo_toolchain.resident_adapter.load_model_bundle", fake_loader)

    adapter = ResidentInferenceAdapter()
    adapter.handle_analyze("req-1", request_payload, str(tmp_path / "artifacts1"))
    adapter.handle_analyze("req-2", request_payload, str(tmp_path / "artifacts2"))

    assert load_calls == [str(model_dir)]


def test_adapter_normalizes_equivalent_absolute_model_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_dir = (tmp_path / "model_bundle").resolve()
    model_dir.mkdir(parents=True)
    equivalent_path = model_dir / "." / ".." / model_dir.name
    load_calls: list[str] = []

    def fake_loader(model_dir_value: object) -> LoadedBundle:
        load_calls.append(str(model_dir_value))
        return _fake_bundle(model_dir)

    monkeypatch.setattr("audio_ai_demo_toolchain.resident_adapter.load_model_bundle", fake_loader)

    adapter = ResidentInferenceAdapter()
    adapter.handle_analyze("req-1", _request_payload(model_dir=model_dir), str(tmp_path / "artifacts1"))
    adapter.handle_analyze("req-2", _request_payload(model_dir=equivalent_path), str(tmp_path / "artifacts2"))

    assert load_calls == [str(model_dir)]
    assert len(adapter._cache) == 1
```
```

Add more adapter coverage for:

- different `model_dir` values create distinct cache entries
- equivalent absolute path forms for the same directory hit the same normalized
  cache entry
- failed bundle load does not leave a poisoned cache entry behind
- `preload_model_dir()` populates the same cache used by later analyze requests

Create subprocess CLI tests like:

```python
def test_resident_cli_emits_error_envelope_for_request_failure(tmp_path: Path) -> None:
    process = _start_resident_process()
    _write_line(process.stdin, _analyze_envelope(params={}))
    envelope = _read_json_line(process.stdout)
    assert envelope["message_type"] == "error"
    assert envelope["request_id"] == "req-123"
```

Add an explicit CLI preload-success regression using a recording adapter or
patched `resident_cli.main()` harness so the test proves
`--preload-model-dir ...` is consumed before the first request loop iteration
starts:

```python
def test_resident_cli_preloads_model_dirs_before_entering_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[tuple[str, str]] = []

    class RecordingAdapter:
        def preload_model_dir(self, model_dir: str) -> None:
            events.append(("preload", model_dir))

        def handle_analyze(self, request_id: str, request_payload: dict[str, object], artifacts_dir: str) -> dict[str, object]:
            events.append(("analyze", request_id))
            return _success_response(request_id)

    monkeypatch.setattr("audio_ai_demo_toolchain.resident_cli.ResidentInferenceAdapter", RecordingAdapter)
    exit_code = resident_main([
        "--preload-model-dir",
        str(tmp_path / "bundle_a"),
        "--preload-model-dir",
        str(tmp_path / "bundle_b"),
    ], stdin=io.StringIO(json.dumps({"message_type": "shutdown"}) + "\n"))

    assert exit_code == 0
    assert events == [
        ("preload", str(tmp_path / "bundle_a")),
        ("preload", str(tmp_path / "bundle_b")),
    ]
```

Add an explicit subprocess UTF-8 regression that avoids Python text-mode
defaults and writes raw UTF-8 bytes through the real resident pipe:

```python
def test_resident_cli_uses_utf8_stdio_for_json_lines(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "audio_ai_demo_toolchain.resident_cli"],
        cwd=PROJECT_ROOT,
        env=_cli_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )

    request = _analyze_envelope(request_id="req-🙂", params={})
    process.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
    process.stdin.flush()

    response_line = process.stdout.readline()
    envelope = json.loads(response_line.decode("utf-8"))

    assert envelope["request_id"] == "req-🙂"
```

Also cover:

- valid analyze request -> one `result` line containing the standard logical response schema
- valid analyze request -> writes the required artifact files under the provided `artifacts_dir`
- malformed JSON input -> process exits non-zero without a stdout protocol line
- unsupported inbound `message_type` -> process exits non-zero
- startup preload failure -> process exits non-zero before entering the request loop and writes a clear stderr message naming the failing preload path
- request-scoped `error` response does not crash the process before an explicit `shutdown`; prove this by sending a failing analyze request and then a shutdown message on the same process
- `shutdown` -> process exits cleanly

- [ ] **Step 2: Run the resident adapter / CLI tests to confirm they fail**

Run:

```bash
pytest tests/test_resident_adapter.py tests/test_resident_cli.py -v
```

Expected: FAIL because the adapter, cache, and resident CLI entrypoint do not exist yet.

- [ ] **Step 3: Implement the resident adapter and thin CLI**

Create `src/audio_ai_demo_toolchain/resident_adapter.py` with a focused cache-owning API such as:

```python
class ResidentInferenceAdapter:
    def __init__(self) -> None:
        self._cache: dict[str, LoadedBundle] = {}

    def preload_model_dir(self, model_dir: str) -> None:
        ...

    def handle_analyze(
        self,
        request_id: str,
        request_payload: dict[str, Any],
        artifacts_dir: str,
    ) -> dict[str, Any]:
        ...
```

Implementation rules:

- cache key = normalized absolute `model_dir`
- cache value = fully loaded validated `LoadedBundle`
- same `model_dir` reuses the same bundle object
- different `model_dir` values add independent cache entries
- failed load must not mutate the cache
- `handle_analyze()` validates `params.model_dir`, resolves the cache key, loads on miss, and delegates execution to `run_inference_with_bundle()`

Create `src/audio_ai_demo_toolchain/resident_cli.py` as a thin entrypoint:

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(...)
    parser.add_argument("--preload-model-dir", action="append", default=[])
    return parser
```

`resident_cli.py` must:

- parse repeated `--preload-model-dir`
- instantiate `ResidentInferenceAdapter`
- preload every requested `--preload-model-dir` value in order before starting
  the stdio loop
- fail startup with non-zero exit if preload fails
- explicitly configure real process stdio to UTF-8 before entering the loop,
  for example via `sys.stdin.reconfigure(encoding="utf-8")` and matching
  `stdout` / `stderr` reconfiguration or an equivalent wrapper around the
  underlying buffers
- keep `main()` testable, either by accepting optional stream overrides or by
  extracting a small callable that tests can invoke without spawning a child
- hand the adapter's `handle_analyze()` into `serve_forever()`
- keep serving after request-scoped analyze failures that were converted into
  resident `message_type = "error"` envelopes
- keep human-readable logs on `stderr`

- [ ] **Step 4: Re-run the resident adapter / CLI tests**

Run:

```bash
pytest tests/test_resident_adapter.py tests/test_resident_cli.py -v
```

Expected: PASS

- [ ] **Step 5: Commit the resident adapter / CLI slice**

Run:

```bash
git add src/audio_ai_demo_toolchain/infer.py src/audio_ai_demo_toolchain/resident_adapter.py src/audio_ai_demo_toolchain/resident_cli.py tests/test_resident_adapter.py tests/test_resident_cli.py
git commit -m "feat: add resident audio ai demo entrypoint"
```

## Task 4: Update Registration Docs And Run Full Verification

**Files:**
- Modify: `README.md`
- Modify: `tests/test_inference_smoke.py`
- Modify: `tests/test_resident_protocol.py`
- Modify: `tests/test_resident_adapter.py`
- Modify: `tests/test_resident_cli.py`
- Modify: `src/audio_ai_demo_toolchain/infer.py`
- Modify: `src/audio_ai_demo_toolchain/resident_protocol.py`
- Modify: `src/audio_ai_demo_toolchain/resident_adapter.py`
- Modify: `src/audio_ai_demo_toolchain/resident_cli.py`

- [ ] **Step 1: Update the README and only do targeted cleanup**

Update `README.md` so it documents both registration modes:

```text
One-shot:
D:\Work\jingchengpython\jingcheng-audio-ai-demo-toolchain\.venv\Scripts\python.exe -m audio_ai_demo_toolchain.cli

Resident:
D:\Work\jingchengpython\jingcheng-audio-ai-demo-toolchain\.venv\Scripts\python.exe -u -m audio_ai_demo_toolchain.resident_cli

Resident with preload:
D:\Work\jingchengpython\jingcheng-audio-ai-demo-toolchain\.venv\Scripts\python.exe -u -m audio_ai_demo_toolchain.resident_cli --preload-model-dir D:\Work\models\jingcheng_demo_bundle
```

Also explain:

- register the resident command once
- keep choosing the model through per-binding `params_json.model_dir`
- one resident process may serve multiple model dirs over time
- request-scoped resident failures return a resident `error` envelope, which the host maps to `process_error`
- successful `--preload-model-dir` arguments warm the resident cache before the
  first request, including repeated preload flags
- preload failures abort resident startup before the loop begins

This task is not a catch-all for missing behavior. By the time Task 4 starts,
the resident regressions from Tasks 2 and 3 must already exist with explicit
named tests for:

- absolute `artifacts_dir` enforcement
- top-level non-object JSON lines, missing `message_type`, and non-string
  top-level `message_type`
- request-scoped `error` followed by continued loop / clean `shutdown`
- normalized absolute-path cache reuse
- successful preload arguments warming the cache before the first request
- preload startup failure with non-zero exit and clear `stderr`
- binary-pipe UTF-8 JSON Lines behavior with a non-ASCII `request_id`

- [ ] **Step 2: Run the focused resident + inference regression suite**

Run:

```bash
pytest tests/test_protocol_cli.py tests/test_inference_smoke.py tests/test_resident_protocol.py tests/test_resident_adapter.py tests/test_resident_cli.py -v
```

Expected: PASS

- [ ] **Step 3: Run the full sibling-project test suite**

Run:

```bash
pytest -v
```

Expected: PASS

- [ ] **Step 4: Inspect the final diff for scope control**

Run:

```bash
git status --short --untracked-files=all
git log --oneline -n 4
git diff HEAD~3 -- README.md src/audio_ai_demo_toolchain/infer.py src/audio_ai_demo_toolchain/resident_protocol.py src/audio_ai_demo_toolchain/resident_adapter.py src/audio_ai_demo_toolchain/resident_cli.py tests/test_inference_smoke.py tests/test_resident_protocol.py tests/test_resident_adapter.py tests/test_resident_cli.py
```

Expected:

- `git log --oneline -n 4` shows the three earlier slice commits plus the
  preceding baseline commit under them
- `git status --short --untracked-files=all` shows the current uncommitted Task
  4 state
- `git diff HEAD~3 -- ...` shows the cumulative implementation scope from
  before Task 1 through the current worktree
- only the planned resident-wrapper, inference-core, README, and regression
  files appear in that cumulative diff

- [ ] **Step 5: Commit the verification/docs slice**

Run:

```bash
git add README.md src/audio_ai_demo_toolchain/infer.py src/audio_ai_demo_toolchain/resident_protocol.py src/audio_ai_demo_toolchain/resident_adapter.py src/audio_ai_demo_toolchain/resident_cli.py tests/test_inference_smoke.py tests/test_resident_protocol.py tests/test_resident_adapter.py tests/test_resident_cli.py
git commit -m "docs: add resident registration guidance"
```
