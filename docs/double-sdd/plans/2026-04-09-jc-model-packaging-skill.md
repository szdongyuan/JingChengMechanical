# JC Model Packaging Skill Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone root-level `jc-model-packaging-skill/` package that another AI can move elsewhere and use to package an existing inference implementation into a Python one-shot tool, a Python resident stdio tool, and an optional Windows `.exe` delivery.

**Architecture:** Treat the deliverable as a portable skill pack, not a library. Lock the package shape and self-contained portability rules with repository tests first, then fill in the skill, templates, and smoke examples so another AI can operate without reading any JingChengMechanical source code.

**Tech Stack:** Markdown, plain-text template files, Python template scripts, pytest

---

## File Structure

### New Deliverable Files

- Create: `jc-model-packaging-skill/SKILL.md`
- Create: `jc-model-packaging-skill/templates/python_oneshot_cli/README.md`
- Create: `jc-model-packaging-skill/templates/python_oneshot_cli/toolchain_cli.py`
- Create: `jc-model-packaging-skill/templates/python_oneshot_cli/protocol_contract.md`
- Create: `jc-model-packaging-skill/templates/python_resident_stdio/README.md`
- Create: `jc-model-packaging-skill/templates/python_resident_stdio/resident_cli.py`
- Create: `jc-model-packaging-skill/templates/python_resident_stdio/resident_protocol.md`
- Create: `jc-model-packaging-skill/templates/pyinstaller_windows/README.md`
- Create: `jc-model-packaging-skill/templates/pyinstaller_windows/build.ps1`
- Create: `jc-model-packaging-skill/templates/pyinstaller_windows/toolchain_oneshot.spec`
- Create: `jc-model-packaging-skill/templates/pyinstaller_windows/toolchain_resident.spec`
- Create: `jc-model-packaging-skill/templates/smoke_test/README.md`
- Create: `jc-model-packaging-skill/templates/smoke_test/request.json`
- Create: `jc-model-packaging-skill/templates/smoke_test/response.success.json`
- Create: `jc-model-packaging-skill/templates/smoke_test/stderr.error.txt`
- Create: `jc-model-packaging-skill/templates/smoke_test/resident_input.jsonl`
- Create: `jc-model-packaging-skill/templates/smoke_test/resident_shutdown_input.jsonl`
- Create: `jc-model-packaging-skill/templates/smoke_test/resident_output.success.jsonl`
- Create: `jc-model-packaging-skill/templates/smoke_test/resident_output.error.jsonl`
- Create: `jc-model-packaging-skill/templates/metadata/README.md`
- Create: `jc-model-packaging-skill/templates/metadata/model_contract.template.json`
- Create: `jc-model-packaging-skill/templates/metadata/packaging_decision_checklist.md`

### New Verification File

- Create: `tests/test_jc_model_packaging_skill.py`

### Existing Reference Files

- Reference only: `docs/double-sdd/specs/2026-04-09-jc-model-packaging-skill-design.md`

## Task 1: Lock The Deliverable Contract With Repo Tests

**Files:**
- Create: `tests/test_jc_model_packaging_skill.py`

- [ ] **Step 1: Write the failing structure test**

```python
from pathlib import Path


def test_skill_package_contains_all_required_files():
    root = Path("jc-model-packaging-skill")
    required = [
        root / "SKILL.md",
        root / "templates" / "python_oneshot_cli" / "README.md",
        root / "templates" / "python_oneshot_cli" / "toolchain_cli.py",
        root / "templates" / "python_oneshot_cli" / "protocol_contract.md",
        root / "templates" / "python_resident_stdio" / "README.md",
        root / "templates" / "python_resident_stdio" / "resident_cli.py",
        root / "templates" / "python_resident_stdio" / "resident_protocol.md",
        root / "templates" / "pyinstaller_windows" / "README.md",
        root / "templates" / "pyinstaller_windows" / "build.ps1",
        root / "templates" / "pyinstaller_windows" / "toolchain_oneshot.spec",
        root / "templates" / "pyinstaller_windows" / "toolchain_resident.spec",
        root / "templates" / "smoke_test" / "README.md",
        root / "templates" / "smoke_test" / "request.json",
        root / "templates" / "smoke_test" / "response.success.json",
        root / "templates" / "smoke_test" / "stderr.error.txt",
        root / "templates" / "smoke_test" / "resident_input.jsonl",
        root / "templates" / "smoke_test" / "resident_shutdown_input.jsonl",
        root / "templates" / "smoke_test" / "resident_output.success.jsonl",
        root / "templates" / "smoke_test" / "resident_output.error.jsonl",
        root / "templates" / "metadata" / "README.md",
        root / "templates" / "metadata" / "model_contract.template.json",
        root / "templates" / "metadata" / "packaging_decision_checklist.md",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    assert missing == []
```

- [ ] **Step 2: Add a failing portability/content test**

```python
from pathlib import Path


def test_skill_is_self_contained_and_mentions_required_modes():
    text_files = [
        path for path in Path("jc-model-packaging-skill").rglob("*")
        if path.is_file() and path.suffix in {".md", ".py", ".ps1", ".spec", ".jsonl", ".txt", ".json"}
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in text_files)
    assert "one-shot" in combined.lower()
    assert "resident" in combined.lower()
    assert "duration" in combined.lower()
    forbidden = ["docs/double-sdd", "app/", "ui/", "JingChengMechanical"]
    assert all(token not in combined for token in forbidden)
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `pytest tests/test_jc_model_packaging_skill.py -q`

Expected: FAIL because `jc-model-packaging-skill/` and its required files do not exist yet.

- [ ] **Step 4: Expand the test to lock critical protocol phrases**

```python
def test_skill_documents_protocol_contract_and_artifact_rules():
    skill_text = Path("jc-model-packaging-skill/SKILL.md").read_text(encoding="utf-8")
    assert "--request" in skill_text
    assert "--response" in skill_text
    assert "--artifacts-dir" in skill_text
    assert '"message_type": "analyze"' in skill_text
    assert '"message_type": "result"' in skill_text
    assert '"message_type": "error"' in skill_text
    assert "relative_path" in skill_text
```

- [ ] **Step 5: Commit the failing tests**

```bash
git add tests/test_jc_model_packaging_skill.py
git commit -m "test: lock jc model packaging skill contract"
```

## Task 2: Create The Root Skill And Metadata Guidance

**Files:**
- Create: `jc-model-packaging-skill/SKILL.md`
- Create: `jc-model-packaging-skill/templates/metadata/README.md`
- Create: `jc-model-packaging-skill/templates/metadata/model_contract.template.json`
- Create: `jc-model-packaging-skill/templates/metadata/packaging_decision_checklist.md`
- Test: `tests/test_jc_model_packaging_skill.py`

- [ ] **Step 1: Write the minimum skill skeleton to satisfy the package existence tests**

Add a top-level `SKILL.md` with these sections:

```markdown
# JC Model Packaging Skill

## Purpose

Package an existing inference implementation into a host-usable tool without reading repository source code.

## Supported Outputs

- Python one-shot CLI
- Python resident stdio process
- Optional Windows .exe packaging
```

- [ ] **Step 2: Fill in the required decision workflow**

`SKILL.md` must explicitly require the downstream AI to resolve:

- model entrypoint and preprocessing ownership
- sample rate and channel contract
- tensor shape contract
- normalization assumptions
- output interpretation
- duration mismatch policy
- one-shot vs resident choice
- optional `.exe` choice
- verification before claiming success
- whether to keep weights external or embed them

Include a compact mandatory checklist such as:

```markdown
Before writing any wrapper, answer:
1. What exact input shape does the model expect?
2. What sample rate, channels, tensor shape, and normalization are required?
3. How is duration mismatch handled?
4. Who owns preprocessing: the wrapper or the model module?
5. How is model output interpreted by the host?
6. Is one-shot or resident the correct runtime shape?
7. Is a Windows `.exe` required?
```

- [ ] **Step 3: Add metadata templates**

Create `model_contract.template.json` with placeholders like:

```json
{
  "model_name": "",
  "model_version": "",
  "input_modality": "audio",
  "sample_rate_hz": 44100,
  "channels": 1,
  "target_num_samples": 176400,
  "duration_policy": "trim_pad",
  "trim_policy": "tail",
  "pad_policy": "zero",
  "windowing": {
    "enabled": false,
    "window_size_ms": null,
    "hop_size_ms": null,
    "aggregation": null
  },
  "preprocessing_steps": [],
  "output_contract": {
    "kind": "binary_classification",
    "score_name": "score",
    "label_rule": "score >= threshold => PASS"
  }
}
```

- [ ] **Step 4: Add the packaging decision checklist**

Create a Markdown checklist that covers:

- choose one-shot or resident
- choose external bundle or embedded weights
- choose Python script or `.exe`
- explain when `.exe` is useful and when plain Python is enough
- declare duration policy
- cover all supported duration strategies:
  - trim / pad
  - sliding window + aggregation
  - strict reject
  - true variable-length with bounds
- document the default recommendation:
  - fixed sample length
  - trim if longer
  - zero-pad if shorter
- declare artifact outputs
- declare verification commands

- [ ] **Step 5: Run tests and confirm partial progress**

Run: `pytest tests/test_jc_model_packaging_skill.py -q`

Expected: still FAIL, but only because the remaining template files are missing or incomplete.

- [ ] **Step 6: Commit**

```bash
git add jc-model-packaging-skill/SKILL.md jc-model-packaging-skill/templates/metadata tests/test_jc_model_packaging_skill.py
git commit -m "feat: add jc model packaging skill core guidance"
```

## Task 3: Add One-Shot Packaging Template And Contract

**Files:**
- Create: `jc-model-packaging-skill/templates/python_oneshot_cli/README.md`
- Create: `jc-model-packaging-skill/templates/python_oneshot_cli/toolchain_cli.py`
- Create: `jc-model-packaging-skill/templates/python_oneshot_cli/protocol_contract.md`
- Modify: `jc-model-packaging-skill/SKILL.md`
- Test: `tests/test_jc_model_packaging_skill.py`

- [ ] **Step 1: Add the one-shot contract documentation**

The contract doc must include:

- required CLI args:
  - `--request`
  - `--response`
  - `--artifacts-dir`
- required rule that all three CLI paths are absolute
- required rule that `--artifacts-dir` may be created by the tool
- required rule that failure to read `--request`, write `--response`, or create
  / write `--artifacts-dir` is fatal
- exact request JSON example
- exact success response JSON example
- fatal failure behavior:
  - stderr message
  - non-zero exit
  - no success response
  - stale `--response` cleanup when fatal failure happens after a stale file
    already exists
- explicit request field rules:
  - required `protocol_version = 1`
  - required string `request_id`
  - required string `test_run_id`
  - required string `toolchain_id`
  - required non-empty list `segments`
  - required object `context`
  - required object `params`
  - required object `window_info`
- explicit segment field rules:
  - string `segment_id`
  - string `role`
  - absolute string `audio_path`
  - list `channels`
  - integer `duration_ms`
  - integer `sample_rate`
  - object `metadata`
- explicit default segment selection rule:
  - use `params.segment_index` when valid
  - otherwise use `segments[0]`
- explicit response field rules:
  - required string `toolchain_name`
  - required string `toolchain_version`
  - required object `ai_result`
  - required string `ai_result.ai_label`
  - optional numeric `score`
  - optional numeric `confidence`
  - optional string `summary`
  - optional object `extra`
  - optional list `artifacts`
- artifact safety rules

- [ ] **Step 2: Create the one-shot Python wrapper template**

`toolchain_cli.py` should include placeholders and comments for:

```python
def load_request(path: str) -> dict: ...
def preprocess_from_request(request_payload: dict): ...
def run_model(preprocessed): ...
def write_artifacts(artifacts_dir: str, inference_output) -> list[dict]: ...
def build_success_response(request_payload: dict, inference_output, artifacts: list[dict]) -> dict: ...
```

The template must show:

- argument parsing
- request loading
- preprocessing call site
- inference call site
- artifact writing
- response writing
- fatal exception -> stderr + exit 1

- [ ] **Step 3: Update `SKILL.md` to point to the one-shot template**

Add a direct decision rule like:

```markdown
Choose the one-shot template when:
- file replay is useful
- startup latency is acceptable
- manual debugging is the priority
```

- [ ] **Step 4: Tighten the repository test to assert one-shot specifics**

Add assertions such as:

```python
def test_oneshot_template_mentions_required_cli_flags():
    text = Path("jc-model-packaging-skill/templates/python_oneshot_cli/toolchain_cli.py").read_text(encoding="utf-8")
    assert "--request" in text
    assert "--response" in text
    assert "--artifacts-dir" in text


def test_oneshot_contract_locks_absolute_paths_and_segment_fallback():
    text = Path("jc-model-packaging-skill/templates/python_oneshot_cli/protocol_contract.md").read_text(encoding="utf-8")
    assert "absolute" in text
    assert "segments[0]" in text
    assert "segment_index" in text
    assert "stale" in text or "remove" in text
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_jc_model_packaging_skill.py -q`

Expected: still FAIL only because resident / pyinstaller / smoke files are not complete yet.

- [ ] **Step 6: Commit**

```bash
git add jc-model-packaging-skill/templates/python_oneshot_cli jc-model-packaging-skill/SKILL.md tests/test_jc_model_packaging_skill.py
git commit -m "feat: add one-shot model packaging template"
```

## Task 4: Add Resident Stdio Template And Protocol

**Files:**
- Create: `jc-model-packaging-skill/templates/python_resident_stdio/README.md`
- Create: `jc-model-packaging-skill/templates/python_resident_stdio/resident_cli.py`
- Create: `jc-model-packaging-skill/templates/python_resident_stdio/resident_protocol.md`
- Modify: `jc-model-packaging-skill/SKILL.md`
- Test: `tests/test_jc_model_packaging_skill.py`

- [ ] **Step 1: Add the resident protocol documentation**

Document exact examples for:

- `analyze` input envelope
- `shutdown` input envelope
- `result` output envelope
- `error` output envelope
- explicit required field rules for:
  - outer `request_id`
  - inner `request.request_id`
  - `request.test_run_id`
  - `request.toolchain_id`
  - `request.segments`
  - `request.context`
  - `request.params`
  - `request.window_info`
  - absolute `artifacts_dir`
- explicit rule that inner and outer `request_id` must match
- explicit default segment selection rule:
  - valid `request.params.segment_index` selects that segment
  - otherwise default to `request.segments[0]`
- rule: no extra stdout
- rule: stderr is human-readable only
- rule: malformed inbound JSON or protocol envelope -> `stderr` + non-zero exit
- rule: request-scoped analyze failure -> emit one `error` envelope and keep
  accepting later `analyze` messages in the default template

- [ ] **Step 2: Create the resident Python wrapper template**

`resident_cli.py` should show:

```python
def handle_analyze(request_id: str, request_payload: dict, artifacts_dir: str) -> dict: ...

def main():
    for line in sys.stdin:
        envelope = json.loads(line)
        ...
```

It must include placeholders for:

- model preload hook
- in-process cache hook
- request-scoped error handling
- clean shutdown handling

- [ ] **Step 3: Update `SKILL.md` to recommend resident packaging when cold imports dominate**

Add decision guidance such as:

```markdown
Choose the resident template when:
- imports are expensive
- the model should stay in memory
- the host will send repeated requests
```

- [ ] **Step 4: Tighten the repository test to assert resident protocol phrases**

Add assertions such as:

```python
def test_resident_template_mentions_protocol_envelopes():
    text = Path("jc-model-packaging-skill/templates/python_resident_stdio/resident_protocol.md").read_text(encoding="utf-8")
    assert '"message_type": "analyze"' in text
    assert '"message_type": "result"' in text
    assert '"message_type": "error"' in text
    assert '"message_type": "shutdown"' in text


def test_resident_contract_locks_request_matching_and_error_lifecycle():
    text = Path("jc-model-packaging-skill/templates/python_resident_stdio/resident_protocol.md").read_text(encoding="utf-8")
    assert "artifacts_dir" in text and "absolute" in text
    assert "must equal outer request_id" in text or "inner and outer" in text
    assert "non-zero" in text
    assert "continue accepting later analyze" in text
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_jc_model_packaging_skill.py -q`

Expected: still FAIL only because pyinstaller and smoke examples are incomplete.

- [ ] **Step 6: Commit**

```bash
git add jc-model-packaging-skill/templates/python_resident_stdio jc-model-packaging-skill/SKILL.md tests/test_jc_model_packaging_skill.py
git commit -m "feat: add resident model packaging template"
```

## Task 5: Add Windows Packaging Templates And Smoke Examples

**Files:**
- Create: `jc-model-packaging-skill/templates/pyinstaller_windows/README.md`
- Create: `jc-model-packaging-skill/templates/pyinstaller_windows/build.ps1`
- Create: `jc-model-packaging-skill/templates/pyinstaller_windows/toolchain_oneshot.spec`
- Create: `jc-model-packaging-skill/templates/pyinstaller_windows/toolchain_resident.spec`
- Create: `jc-model-packaging-skill/templates/smoke_test/README.md`
- Create: `jc-model-packaging-skill/templates/smoke_test/request.json`
- Create: `jc-model-packaging-skill/templates/smoke_test/response.success.json`
- Create: `jc-model-packaging-skill/templates/smoke_test/stderr.error.txt`
- Create: `jc-model-packaging-skill/templates/smoke_test/resident_input.jsonl`
- Create: `jc-model-packaging-skill/templates/smoke_test/resident_shutdown_input.jsonl`
- Create: `jc-model-packaging-skill/templates/smoke_test/resident_output.success.jsonl`
- Create: `jc-model-packaging-skill/templates/smoke_test/resident_output.error.jsonl`
- Test: `tests/test_jc_model_packaging_skill.py`

- [ ] **Step 1: Add the Windows packaging docs**

The README must explicitly explain:

- `onedir` is the practical default for ML payloads
- `onefile` is optional and may cost startup time
- `.exe` is a delivery format, not a protocol
- both one-shot and resident modes can be packaged

- [ ] **Step 2: Add the PowerShell build template**

`build.ps1` should show placeholder commands like:

```powershell
pyinstaller .\toolchain_oneshot.spec
pyinstaller .\toolchain_resident.spec
```

with comments about:

- copying model files
- including non-Python assets
- choosing onedir vs onefile

- [ ] **Step 3: Add the spec templates**

Each `.spec` file should be obviously mode-specific:

- one-shot entrypoint template
- resident entrypoint template

No real project-specific paths should be hard-coded.

- [ ] **Step 4: Add smoke-test examples**

The smoke-test directory must provide:

- one-shot request example
- one-shot success response example
- one-shot stderr failure example
- resident analyze input example
- resident success output example
- resident error output example
- resident shutdown input example
- README instructions describing expected success checks:
  - protocol shape matches contract
  - every listed artifact exists
  - artifact paths are relative and traversal-safe
  - stderr failure is non-protocol text

- [ ] **Step 5: Expand the repository test for smoke examples and portability**

Add assertions such as:

```python
def test_smoke_examples_cover_success_and_failure_paths():
    root = Path("jc-model-packaging-skill/templates/smoke_test")
    assert (root / "response.success.json").is_file()
    assert (root / "stderr.error.txt").is_file()
    assert (root / "resident_output.success.jsonl").is_file()
    assert (root / "resident_output.error.jsonl").is_file()
    assert (root / "resident_shutdown_input.jsonl").is_file()


def test_smoke_examples_lock_protocol_and_artifact_rules():
    success = (Path("jc-model-packaging-skill/templates/smoke_test") / "response.success.json").read_text(encoding="utf-8")
    resident_success = (Path("jc-model-packaging-skill/templates/smoke_test") / "resident_output.success.jsonl").read_text(encoding="utf-8")
    smoke_readme = (Path("jc-model-packaging-skill/templates/smoke_test") / "README.md").read_text(encoding="utf-8")
    assert '"relative_path"' in success
    assert '"message_type": "result"' in resident_success
    assert "artifact exists" in smoke_readme.lower()
    assert "relative" in smoke_readme.lower()
```

- [ ] **Step 6: Run the targeted verification test**

Run: `pytest tests/test_jc_model_packaging_skill.py -q`

Expected: PASS for the targeted repository test, while `.exe` build remains a
documented template path rather than a required built artifact in this pass.

- [ ] **Step 7: Commit**

```bash
git add jc-model-packaging-skill/templates/pyinstaller_windows jc-model-packaging-skill/templates/smoke_test tests/test_jc_model_packaging_skill.py
git commit -m "feat: add packaging and smoke templates for jc model skill"
```

## Task 6: Final Skill Pass And Full Verification

**Files:**
- Modify: `jc-model-packaging-skill/SKILL.md`
- Modify: `tests/test_jc_model_packaging_skill.py`
- Test: `tests/test_jc_model_packaging_skill.py`

- [ ] **Step 1: Re-read `SKILL.md` and ensure it is agent-facing, not end-user-facing**

Verify the final skill explicitly tells the downstream AI:

- do not inspect repository source
- use the embedded contract
- choose one-shot vs resident intentionally
- declare duration policy before packaging
- verify before claiming success
- decide whether Python or `.exe` is warranted
- resolve tensor shape, normalization, and output interpretation first

- [ ] **Step 2: Add one final repository assertion for forbidden references**

Add a stronger test like:

```python
def test_skill_contains_no_repo_dependent_instructions():
    text_files = [
        path for path in Path("jc-model-packaging-skill").rglob("*")
        if path.is_file() and path.suffix in {".md", ".py", ".ps1", ".spec", ".json", ".jsonl", ".txt"}
    ]
    forbidden = ["docs/double-sdd", "app/", "ui/", "model/", "consts/", "JingChengMechanical"]
    for path in text_files:
        text = path.read_text(encoding="utf-8")
        assert all(token not in text for token in forbidden), str(path)


def test_skill_pack_contains_no_repo_relative_markdown_links():
    text_files = Path("jc-model-packaging-skill").rglob("*.md")
    forbidden = ["(/", "](docs/", "](app/", "](ui/"]
    for path in text_files:
        text = path.read_text(encoding="utf-8")
        assert all(token not in text for token in forbidden), str(path)
```

- [ ] **Step 3: Run the full verification**

Run: `pytest tests/test_jc_model_packaging_skill.py -q`

Expected: PASS with all tests green.

- [ ] **Step 4: Manually inspect smoke fixture semantics and deliverable tree**

Run:

```powershell
Get-Content jc-model-packaging-skill\templates\smoke_test\README.md
Get-Content jc-model-packaging-skill\templates\smoke_test\request.json
Get-Content jc-model-packaging-skill\templates\smoke_test\response.success.json
Get-Content jc-model-packaging-skill\templates\smoke_test\resident_output.success.jsonl
Get-ChildItem jc-model-packaging-skill -Recurse
```

Expected:

- the fixed layout from the spec is present
- smoke fixtures actually match the documented protocol
- no repo-relative instructions slipped into the movable package

- [ ] **Step 5: Commit**

```bash
git add jc-model-packaging-skill tests/test_jc_model_packaging_skill.py
git commit -m "feat: add portable jc model packaging skill pack"
```
