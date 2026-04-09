# JC Model Packaging Skill Design

## Summary

The user wants a standalone, movable skill package named
`jc-model-packaging-skill/` that teaches another AI how to package an existing
model inference implementation into a deliverable that can be used by
JingChengMechanical without reading this repository's source code.

The skill must be self-contained. It should not assume that the downstream AI
has access to:

- this repository
- this repository's Python modules
- this repository's docs
- any hidden prior project context

Instead, the skill itself must include:

- the integration contract the packaged tool must satisfy
- the packaging decision process
- templates for the supported delivery shapes
- verification guidance

The first version should support packaging into:

- a Python one-shot CLI
- a Python resident `stdin` / `stdout` process
- an optional Windows `.exe` build path

It should also force the AI to resolve the model input contract, especially
audio duration handling, before generating the packaged tool.

## Problem

### Existing Guidance Is Too Repo-Dependent

Right now, an AI working in this repository can inspect code and infer:

- the one-shot toolchain contract
- the resident process contract
- expected request and response payloads
- how model artifacts are returned

But the user explicitly wants a skill that remains usable after being copied
out of this repository. The downstream AI should not need to inspect
JingChengMechanical source code to package a model correctly.

### Packaging Needs More Than Narrative Advice

A plain prose guide is not enough. Another AI may understand the theory but
still fail to produce a working package quickly. The user wants a deliverable
that helps the AI directly create:

- Python scripts
- optional `.exe` packaging
- protocol-compliant wrappers
- smoke-testable outputs

That means the skill must include reusable templates and explicit contracts,
not only generic guidance.

### Duration Mismatch Is A Mandatory Packaging Decision

The user raised a critical issue: model input duration may not match runtime
audio duration. That decision cannot be left implicit.

The skill must require the downstream AI to determine, document, and implement
one explicit duration policy before packaging:

- fixed-length normalize by trim / pad
- fixed-length sliding windows with aggregation
- strict reject on mismatch
- true variable-length inference with defined bounds

Without that, the packaged tool may be runnable but behaviorally undefined.

## Scope

In scope:

- create a standalone root-level directory `jc-model-packaging-skill/`
- include a top-level `SKILL.md`
- include reusable templates inside the skill directory
- make the skill self-contained so it can be moved to another repository
- embed the current host integration contract directly inside the skill
- support guidance for packaging existing inference logic into:
  - Python one-shot CLI tools
  - Python resident `stdin` / `stdout` tools
  - optional Windows `.exe` deliverables
- include required decision points for:
  - model input contract
  - audio duration normalization
  - preprocessing ownership
  - artifact writing
  - error handling
  - verification
- provide concrete examples of host-facing request and response schemas
- provide a packaging decision matrix so another AI can choose between one-shot,
  resident, and optional `.exe`
- include template files that another AI can copy and adapt

Out of scope:

- modifying the current application code
- adding automatic packaging commands to this repository
- building or shipping an actual packaged model in this pass
- creating a reusable Python library to automate packaging
- covering non-Windows binary packagers beyond a short mention
- supporting every ML framework equally deeply
- teaching model training
- documenting model accuracy or dataset practices

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - the user-requested directory name `jc-model-packaging-skill/`
  - the existence of a top-level `SKILL.md`
  - self-contained guidance that does not require reading repository source
  - support for optional `.exe` packaging guidance
- Allowed breakage:
  - no compatibility with earlier unpublished drafts is required
  - internal template layout may be designed fresh
  - no migration support is needed
- Migration strategy: none

## Approved Decisions

1. The deliverable lives at the repository root in
   `jc-model-packaging-skill/`.
2. The user will move the directory elsewhere later, so the package must be
   self-contained.
3. The skill is written for other AIs, not as end-user documentation.
4. The skill may include templates, and templates are preferred over a
   prose-only skill.
5. The skill must not require reading JingChengMechanical source code in order
   to package a model correctly.
6. The skill should explain how to package an existing inference script,
   preprocessing code, or equivalent model runtime into:
   - Python script form
   - resident process form
   - optional `.exe` form
7. Audio duration mismatch handling is a mandatory explicit decision in the
   workflow.
8. The one-shot packaging path should remain file-oriented because that is
   useful for debugging and simpler than forcing one-shot `stdin` / `stdout`.
9. The resident packaging path should use `stdin` / `stdout`.

## Approach Options

### Option 1: Prose-Only Skill

- one `SKILL.md`
- explains the process
- no templates

Pros:

- simplest to write
- easiest to read

Cons:

- weaker for downstream execution
- another AI still has to invent wrapper structure
- higher risk of inconsistent packaging output

### Option 2: Self-Contained Skill With Templates

- `SKILL.md` explains the workflow
- `templates/` contains wrapper and packaging skeletons
- protocol contract is copied into the skill itself

Pros:

- best balance of clarity and execution speed
- another AI can adapt templates directly
- no dependency on this repository's source tree

Cons:

- more files to maintain
- some duplication of protocol details

### Option 3: Mini SDK Package

- package the templates plus shared helper modules
- downstream AI imports the helper package directly

Pros:

- less repetition in generated tools
- strongest consistency

Cons:

- turns the deliverable into a maintained software product
- larger scope than requested
- more coupling than a movable skill pack

### Recommendation

Choose Option 2.

It satisfies the user's portability requirement, gives another AI enough
concrete structure to work quickly, and avoids expanding the scope into a full
SDK.

## Design Summary

Create `jc-model-packaging-skill/` as a portable skill pack with these
responsibilities:

- teach the downstream AI how to classify the packaging target
- force explicit resolution of the model input contract
- provide host protocol details inline
- provide wrapper templates for one-shot and resident tools
- provide optional Windows `.exe` packaging templates
- provide smoke-test templates and verification checklists

The skill should assume the downstream AI has complete information about the
model and preprocessing logic, but no information about this repository.

## Deliverable Layout

The intended first-version layout is:

```text
jc-model-packaging-skill/
  SKILL.md
  templates/
    python_oneshot_cli/
      README.md
      toolchain_cli.py
      protocol_contract.md
    python_resident_stdio/
      README.md
      resident_cli.py
      resident_protocol.md
    pyinstaller_windows/
      README.md
      build.ps1
      toolchain_oneshot.spec
      toolchain_resident.spec
    smoke_test/
      README.md
      request.json
      response.success.json
      stderr.error.txt
      resident_input.jsonl
      resident_shutdown_input.jsonl
      resident_output.success.jsonl
      resident_output.error.jsonl
    metadata/
      README.md
      model_contract.template.json
      packaging_decision_checklist.md
```

These filenames are fixed for the first version. The implementation may add
extra helper files, but it must provide at least the exact files listed above.

## Detailed Design

### 1. SKILL.md Responsibilities

`SKILL.md` should tell another AI how to proceed from available model
information to a working package.

It must cover:

- what complete input information is required before packaging starts
- how to choose output form:
  - one-shot Python
  - resident Python
  - optional `.exe`
- when to keep model weights external versus embedding them
- how to assign preprocessing ownership
- how to define and implement the input contract
- how to verify the result

The skill should explicitly tell the downstream AI not to inspect
JingChengMechanical source code and instead rely on the contract included in
the skill package.

### 2. Embedded Host Contract

The skill must include enough protocol detail for another AI to target the
host correctly.

That contract must document:

#### One-Shot Mode

- registered command form:
  - executable or Python command
- required CLI arguments:
  - `--request`
  - `--response`
  - `--artifacts-dir`
- logical request payload fields:
  - `protocol_version`
  - `request_id`
  - `test_run_id`
  - `toolchain_id`
  - `segments`
  - `context`
  - `params`
  - `window_info`
- logical response payload fields:
  - `protocol_version`
  - `request_id`
  - `toolchain_name`
  - `toolchain_version`
  - `ai_result`
- artifact relative-path rules

The one-shot docs and templates must define this exact request shape:

```json
{
  "protocol_version": 1,
  "request_id": "req_20260409_120000_abcd1234",
  "test_run_id": "run_001",
  "toolchain_id": "example-toolchain",
  "segments": [
    {
      "segment_id": "segment_001",
      "role": "device_1",
      "audio_path": "D:/absolute/path/input.wav",
      "channels": [0],
      "duration_ms": 4000,
      "sample_rate": 44100,
      "metadata": {}
    }
  ],
  "context": {},
  "params": {},
  "window_info": {}
}
```

The one-shot docs and templates must define this exact success response shape:

```json
{
  "protocol_version": 1,
  "request_id": "req_20260409_120000_abcd1234",
  "toolchain_name": "Example Toolchain",
  "toolchain_version": "1.0.0",
  "ai_result": {
    "ai_label": "PASS",
    "score": 0.91,
    "summary": "Inference succeeded.",
    "confidence": 0.88,
    "artifacts": [
      {
        "type": "report_json",
        "relative_path": "report.json"
      }
    ],
    "extra": {}
  }
}
```

The one-shot docs and templates must define fatal failure behavior as:

- print a human-readable error to `stderr`
- exit non-zero
- do not write a protocol success response
- if a stale response file already exists at `--response`, the packaged tool
  must overwrite it only on success or remove it on fatal failure

The one-shot contract must explicitly define these type and presence rules:

- top-level request value must be a JSON object
- `protocol_version` is required and must equal `1`
- `request_id` is required and must be a string
- `test_run_id` is required and must be a string
- `toolchain_id` is required and must be a string
- `segments` is required and must be a list
- `segments` must not be empty
- each segment must be a JSON object
- each segment must include:
  - string `segment_id`
  - string `role`
  - string absolute `audio_path`
  - list `channels`
  - integer `duration_ms`
  - integer `sample_rate`
  - object `metadata`
- `context` is required and must be an object
- `params` is required and must be an object
- `window_info` is required and must be an object
- `params.segment_index`, if present, must be a non-negative integer
- if `params.segment_index` is absent, invalid, or out of range, the default
  template behavior is to select `segments[0]`
- top-level success response must be a JSON object
- success response must include `ai_result` as an object
- `toolchain_name` is required and must be a string
- `toolchain_version` is required and must be a string
- `ai_result.ai_label` is required and must be a string
- `score` and `confidence`, if present, must be numeric
- `summary`, if present, must be a string
- `extra`, if present, must be an object
- `artifacts`, if present, must be a list of objects
- `--request`, `--response`, and `--artifacts-dir` must all be absolute paths
- the packaged tool may create `--artifacts-dir` if it does not exist
- the packaged tool must fail fatally if `--request` is unreadable, if
  `--response` is unwritable, or if `--artifacts-dir` cannot be created or
  written

#### Resident Mode

- startup command form
- transport:
  - UTF-8 JSON Lines over `stdin` / `stdout`
- supported inbound message types:
  - `analyze`
  - `shutdown`
- success envelope:
  - `message_type = "result"`
- request-scoped failure envelope:
  - `message_type = "error"`
- stderr usage for human-readable logs
- no unsolicited stdout output outside the protocol

The resident docs and templates must define this exact `analyze` input
envelope:

```json
{
  "message_type": "analyze",
  "request_id": "req_20260409_120000_abcd1234",
  "request": {
    "protocol_version": 1,
    "request_id": "req_20260409_120000_abcd1234",
    "test_run_id": "run_001",
    "toolchain_id": "example-toolchain",
    "segments": [
      {
        "segment_id": "segment_001",
        "role": "device_1",
        "audio_path": "D:/absolute/path/input.wav",
        "channels": [0],
        "duration_ms": 4000,
        "sample_rate": 44100,
        "metadata": {}
      }
    ],
    "context": {},
    "params": {},
    "window_info": {}
  },
  "artifacts_dir": "D:/absolute/path/artifacts"
}
```

The resident docs and templates must define this exact `shutdown` input
envelope:

```json
{
  "message_type": "shutdown"
}
```

The resident docs and templates must define this exact success output
envelope:

```json
{
  "message_type": "result",
  "request_id": "req_20260409_120000_abcd1234",
  "response": {
    "protocol_version": 1,
    "request_id": "req_20260409_120000_abcd1234",
    "toolchain_name": "Example Toolchain",
    "toolchain_version": "1.0.0",
    "ai_result": {
      "ai_label": "PASS",
      "score": 0.91,
      "summary": "Inference succeeded.",
      "confidence": 0.88,
      "artifacts": [
        {
          "type": "report_json",
          "relative_path": "report.json"
        }
      ],
      "extra": {}
    }
  }
}
```

The resident docs and templates must define this exact request-scoped failure
envelope:

```json
{
  "message_type": "error",
  "request_id": "req_20260409_120000_abcd1234",
  "error": "human-readable summary"
}
```

The resident contract must explicitly define these rules:

- inbound top-level value must be a JSON object
- `message_type` is required and must be a string
- `analyze` requires string `request_id`
- `analyze` requires object `request`
- `analyze` requires string `artifacts_dir`
- `artifacts_dir` must be an absolute path
- `request.request_id` must equal outer `request_id`
- `request.protocol_version` must equal `1`
- `request.test_run_id` is required and must be a string
- `request.toolchain_id` is required and must be a string
- `request.segments` is required, must be a non-empty list, and each segment
  must satisfy the same field rules as one-shot mode
- `request.context` is required and must be an object
- `request.params` is required and must be an object
- `request.window_info` is required and must be an object
- `request.params.segment_index`, if present, must be a non-negative integer
- if `request.params.segment_index` is absent, invalid, or out of range, the
  default template behavior is to select `request.segments[0]`
- `shutdown` expects no output line
- malformed inbound protocol messages are logged to `stderr` and terminate the
  process non-zero
- valid request-scoped analyze failures emit one `error` envelope tied to a
  stable `request_id`
- the default resident template behavior is to remain alive after a valid
  request-scoped `error` envelope and continue accepting later `analyze`
  messages, while noting that an external host may still choose to discard the
  process

#### Artifact Interface

The embedded host contract must define artifact behavior explicitly.

Artifacts live inside:

- one-shot: the host-provided `--artifacts-dir`
- resident: the host-provided `artifacts_dir`

Artifacts are referenced inside:

- one-shot success response:
  - `ai_result.artifacts`
- resident success response:
  - `response.ai_result.artifacts`

Each artifact object must have this exact shape:

```json
{
  "type": "report_json",
  "relative_path": "report.json"
}
```

Artifact safety rules must be documented explicitly:

- `type` must be a non-empty string
- `type` is descriptive metadata for host and debugging use, not a host-parsed
  enum
- example artifact types may include:
  - `report_json`
  - `spectrogram_npy`
  - `image_png`
  - `audio_wav`
- `relative_path` must be a non-empty string
- `relative_path` must be relative, not absolute
- `relative_path` must not contain `..`
- every artifact listed in a success response must already exist under the
  supplied artifacts directory when the response is emitted
- listed artifacts must be readable by the host at response time
- the packaged tool must never reference files outside the supplied artifacts
  directory

This contract should be copied into the skill's docs and template comments so
the downstream AI does not need repo context.

### 3. Packaging Decision Matrix

The skill should provide a decision matrix with recommendations such as:

- choose one-shot when:
  - debugging is primary
  - startup latency is acceptable
  - file-based replay is valuable
- choose resident when:
  - cold imports dominate runtime
  - model reuse matters
  - repeated analysis is expected
- choose `.exe` when:
  - Python runtime availability on target machines is uncertain
  - deployment simplicity matters more than package size

The skill should also explain that:

- one-shot and resident can both remain Python scripts
- one-shot and resident can both be packaged as `.exe`
- `.exe` is a delivery format, not a protocol

### 4. Model Input Contract Resolution

This is a mandatory section of the skill.

Before generating any wrapper, the downstream AI must resolve and document:

- expected sample rate
- expected number of channels
- expected tensor shape
- preprocessing steps and their order
- normalization assumptions
- output interpretation
- duration handling strategy

The skill should forbid leaving duration behavior implicit.

Supported duration strategies should be documented as:

- fixed-length trim / pad
- fixed-length sliding windows with aggregation
- strict reject on mismatch
- true variable-length inference with explicit bounds

The skill should recommend this default when no better domain-specific rule is
provided:

- normalize to one fixed sample length
- trim if longer
- zero-pad if shorter

If the task explicitly says the event may appear anywhere in a long recording,
the skill should recommend sliding-window segmentation with an explicit
aggregation rule.

### 5. Template Responsibilities

Each template must be simple enough for another AI to adapt without needing the
source repository that created the skill.

#### `python_oneshot_cli`

Must show:

- argument parsing
- request loading
- preprocessing call site
- inference call site
- response writing
- artifact writing
- fatal error handling to non-zero exit

#### `python_resident_stdio`

Must show:

- UTF-8 JSONL loop
- `analyze` and `shutdown`
- request-scoped error envelope behavior
- prohibition on extra stdout lines
- optional bundle preloading hook
- process-local model cache hook

#### `pyinstaller_windows`

Must show:

- one reasonable Windows packaging path
- how to include non-Python assets
- how to package or externalize model files
- a PowerShell build command example
- notes about `onedir` versus `onefile`

The first version should prefer documenting `onedir` as the practical default
for ML payloads, while allowing `onefile` as an explicitly optional variant.
The two fixed spec files should be mode-specific:

- `toolchain_oneshot.spec` packages the one-shot entrypoint
- `toolchain_resident.spec` packages the resident entrypoint

#### `smoke_test`

Must show:

- one-shot replay example
- one-shot success response example
- one-shot fatal failure example
- resident JSONL analyze example
- resident success output example
- resident request-scoped error output example
- shutdown example
- expected success checks

The `stderr.error.txt` file is a captured one-shot fatal `stderr` example, not
a protocol response file.

#### `metadata`

Must show:

- a template for recording the model contract
- a packaging decision checklist
- a short checklist for duration policy, preprocessing ownership, and delivery
  mode

### 6. Verification Guidance

The skill must require downstream verification before claiming success.

It should require a generated package to be verified by:

- one-shot smoke run if one-shot packaging was produced
- resident warm-path run if resident packaging was produced
- packaging build verification if `.exe` packaging was requested
- explicit validation that request and response payloads are protocol-compliant
- explicit validation that artifact paths are relative and safe
- explicit validation that every listed artifact exists and is readable when a
  success response is produced

The skill should tell the downstream AI to report:

- what packaging mode was chosen
- why it was chosen
- what duration policy was implemented
- what files were generated
- how verification was run

The skill and templates must explicitly forbid:

- repo-relative imports
- repo-relative markdown links
- instructions to inspect this repository's source code or docs
- hard-coded references to `JingChengMechanical`, `docs/double-sdd`, `app/`,
  `ui/`, or other repository-local paths

### 7. Writing Style

Because this skill is for agents, not end users, it should be:

- direct
- procedural
- explicit about required decisions
- template-oriented
- light on motivational prose

It may still include short rationale when that prevents incorrect packaging
choices.

## Testing Strategy

Implementation verification for this pass should focus on the skill package
itself:

- the directory `jc-model-packaging-skill/` exists at repository root
- `SKILL.md` exists
- all fixed template filenames listed in `Deliverable Layout` exist
- the skill is self-contained and contains no repo-relative imports, links, or
  instructions to inspect repository source/docs
- the skill explicitly documents both one-shot and resident integration modes
- the skill explicitly documents duration mismatch handling as a required
  packaging decision
- the skill explicitly defines default multi-segment selection behavior
- the skill explicitly defines one-shot path rules and failure cleanup behavior
- the skill explicitly defines artifact `type` and `relative_path` semantics
- the templates include placeholder points for preprocessing, inference, and
  verification

No executable model packaging needs to be produced in this pass.

## Non-Goals / Future Work

Future versions could add:

- non-Windows binary packaging templates
- framework-specific variants for Torch, ONNX Runtime, TensorFlow, or classical
  DSP models
- optional shared helper utilities
- multilingual variants of the skill

Those are intentionally excluded from the first version.
