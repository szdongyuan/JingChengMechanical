# JC Model Packaging Skill

## Purpose

Package an existing inference implementation into a host-usable tool by using
only this skill pack plus the explicit model information you were given. Do not
inspect any source repository or external project docs. Use the embedded
contract in this pack. This package is the contract.

## Supported Outputs

- Python one-shot CLI
- Python resident stdio process
- Optional Windows `.exe` packaging

## Required Decisions Before You Write A Wrapper

Before writing any wrapper, answer:

1. What exact model entrypoint will run inference?
2. Who owns preprocessing: the wrapper or the model module?
3. What sample rate, channels, tensor shape, and normalization are required?
4. How is model output interpreted by the host?
5. How is duration mismatch handled?
6. Is one-shot or resident the correct runtime shape?
7. Are weights kept external or embedded?
8. Is plain Python enough, or is a Windows `.exe` required?
9. How will you verify the wrapper before claiming success?

Never leave duration mismatch implicit. Pick and document one policy:

- fixed-length trim / pad
- fixed-length sliding window with aggregation
- strict reject on mismatch
- true variable-length inference with explicit bounds

Default recommendation when no better domain rule exists:

- normalize to one fixed sample length
- trim if longer
- zero-pad if shorter

If the event may appear anywhere in a long recording, use sliding-window segmentation
with a defined aggregation rule. Do not leave the aggregation
rule implicit. State whether the final decision uses max score across windows,
mean score across windows, majority vote, any-positive-above-threshold, or
another explicit rule.

## Non-Negotiable Rules

- Do not inspect any source repository.
- Use the embedded contract and templates from this pack.
- Resolve sample rate, channels, tensor shape, normalization, and output interpretation before coding.
- Declare the duration policy before packaging.
- Choose one-shot or resident intentionally.
- Decide whether plain Python is enough or Windows `.exe` packaging is
  warranted.
- Verify the wrapper before claiming success.
- Keep imports, asset references, and Markdown links portable.

## Runtime Shape Decision

Choose the one-shot template when:

- file replay is useful
- startup latency is acceptable
- manual debugging is the priority

Choose the resident template when:

- imports are expensive or cold imports dominate startup latency
- the model should stay in memory
- the host will send repeated requests

The resident path uses stdio JSONL envelopes with `message_type` values such as
`"analyze"`, `"result"`, `"error"`, and `"shutdown"`. Expand that contract only
from this package, not from external repository source.

## Packaging Decision Matrix

Use this matrix before you adapt any template:

| Choice | Prefer it when | Avoid it when |
| --- | --- | --- |
| One-shot Python | file replay matters, startup latency is acceptable, manual debugging is the priority | repeated requests are common and cold imports dominate runtime |
| Resident Python | imports are expensive, the process should stay in memory, repeated requests are expected | simple file replay and isolated one-off runs are more important |
| One-shot Windows `.exe` | Python availability is uncertain and file replay is still the best operational shape | package size and unpack startup cost matter more than delivery simplicity |
| Resident Windows `.exe` | Python availability is uncertain and a warm resident process is still the best runtime shape | the target can already run Python and extra packaging cost is not justified |

`.exe` is a delivery format, not a protocol. One-shot and resident can both
remain plain Python or both be packaged as Windows `.exe`.

For Windows packaging, prefer `onedir` as the practical default for ML
payloads. `onefile` is optional and may cost startup time because the bundle
must unpack before your code runs.

## Weights Packaging Decision

Keep weights external when:

- the model files are large enough that copying or rebuilding them separately is
  practical
- operators need to swap or upgrade weights without rebuilding the wrapper
- weights have separate licensing, storage, or approval constraints
- the target environment can reliably deploy sidecar files next to the wrapper

Embed weights when:

- the delivery requirement is a single distributable bundle
- the wrapper and weights must stay version-locked
- target machines cannot be trusted to place sidecar model files correctly
- the model payload is small enough that bundling it will not make startup or
  packaging costs unacceptable

Record the choice in the metadata checklist before you package anything.

## One-Shot Contract Summary

The one-shot wrapper must expose these CLI flags:

- `--request`
- `--response`
- `--artifacts-dir`

Rules:

- `--request`, `--response`, and `--artifacts-dir` must all be absolute paths.
- The wrapper may create `--artifacts-dir` if it does not exist.
- Failure to read `--request`, write `--response`, or create or write
  `--artifacts-dir` is fatal.
- Request payloads require `protocol_version = 1`, `request_id`,
  `test_run_id`, `toolchain_id`, `segments`, `context`, `params`, and
  `window_info`.
- If `params.segment_index` is present and valid, select that segment.
  Otherwise fall back to `segments[0]`.
- Success responses must include `toolchain_name`, `toolchain_version`, and
  `ai_result`.
- `ai_result.artifacts[*].relative_path` must stay relative, must not contain
  `..`, and must point to a file already written under the supplied artifacts
  directory.
- On fatal failure, print a human-readable message to `stderr`, exit non-zero,
  do not emit a success payload, and remove any stale `--response` file.

Use the detailed one-shot contract in
`templates/python_oneshot_cli/protocol_contract.md` and adapt the template in
`templates/python_oneshot_cli/toolchain_cli.py`.

## Resident Contract Summary

The resident wrapper must use UTF-8 JSON Lines over `stdin` and `stdout`.

Rules:

- Handle inbound `"message_type": "analyze"` envelopes that contain a matching
  outer `request_id`, inner `request`, and absolute `artifacts_dir`.
- Use `request.params.segment_index` when it is valid. Otherwise fall back to
  `request.segments[0]`.
- Emit success as `"message_type": "result"`.
- Emit request-scoped failures as `"message_type": "error"`.
- Treat malformed protocol input as fatal: log to `stderr`, exit non-zero, and
  do not emit extra stdout text.
- Treat `"message_type": "shutdown"` as a clean stop with no stdout output.
- Report artifacts only through safe `relative_path` values that stay under the
  supplied artifacts directory.

Use the detailed resident contract in
`templates/python_resident_stdio/resident_protocol.md` and adapt the template
in `templates/python_resident_stdio/resident_cli.py`.

## Windows Packaging Notes

Use `templates/pyinstaller_windows/README.md`,
`templates/pyinstaller_windows/build.ps1`,
`templates/pyinstaller_windows/toolchain_oneshot.spec`, and
`templates/pyinstaller_windows/toolchain_resident.spec` when a Windows `.exe`
delivery is actually required.

- Package the one-shot entrypoint for file-oriented replay workflows.
- Package the resident entrypoint for repeated analyze requests.
- Keep weight files external when they are large or swapped independently.
- Bundle weight files only when a single distributable is required.

## Verification Requirements

Before claiming success, verify the chosen runtime shape:

- One-shot packaging: run a smoke replay with `templates/smoke_test/request.json`
  and compare the result against `templates/smoke_test/response.success.json`.
- Resident packaging: replay `templates/smoke_test/resident_input.jsonl` and
  compare success and failure envelopes against the resident smoke fixtures.
- Resident packaging: run a resident warm-path verification by sending
  repeated `analyze` requests through the same process before shutdown.
- Windows `.exe` packaging: build the requested mode and verify the generated
  deliverable still follows the same protocol.
- Validate that every declared artifact exists, is readable, and uses a
  relative traversal-safe `relative_path`.
- Confirm that fatal one-shot failures stay on `stderr` as human-readable
  non-protocol text.

## Final Report Checklist

Your final report must include:

- the chosen mode
- why it was chosen
- the duration policy
- the generated files
- how verification was run

## Packaging Workflow

1. Fill in `templates/metadata/model_contract.template.json`.
2. Complete `templates/metadata/packaging_decision_checklist.md`.
3. Choose one-shot or resident intentionally.
4. Keep imports, links, and paths portable. Do not add repo-relative imports or
   repo-relative Markdown links.
5. Implement the wrapper.
6. Verify request parsing, duration handling, artifact writing, and protocol
   output before claiming success.
