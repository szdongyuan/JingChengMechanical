# JC Model Packaging Skill

## Purpose

Package an existing inference implementation into a host-usable tool by using
only this skill pack plus the model/runtime inputs you were given. Do not
inspect any source repository or external project docs. This package is the
contract.

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

## Runtime Shape Decision

Choose the one-shot template when:

- file replay is useful
- startup latency is acceptable
- manual debugging is the priority

Choose the resident template when:

- imports are expensive
- the model should stay in memory
- the host will send repeated requests

The resident path uses stdio JSONL envelopes with `message_type` values such as
`"analyze"`, `"result"`, `"error"`, and `"shutdown"`. Expand that contract only
from this package, not from external repository source.

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

## Packaging Workflow

1. Fill in `templates/metadata/model_contract.template.json`.
2. Complete `templates/metadata/packaging_decision_checklist.md`.
3. Choose one-shot or resident intentionally.
4. Keep imports, links, and paths portable. Do not add repo-relative imports or
   repo-relative Markdown links.
5. Implement the wrapper.
6. Verify request parsing, duration handling, artifact writing, and protocol
   output before claiming success.
