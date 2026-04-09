# Python One-Shot CLI Template

Use this template when file replay and manual debugging matter more than warm
process reuse.

## Required CLI

The wrapper must accept:

- `--request`
- `--response`
- `--artifacts-dir`

All three paths must be absolute. The wrapper may create `--artifacts-dir`.
Failure to read `--request`, write `--response`, or create or write
`--artifacts-dir` is fatal.

## Runtime Rules

- Load a JSON request from `--request`.
- If `params.segment_index` is valid, select that segment. Otherwise use
  `segments[0]`.
- Write artifacts only under `--artifacts-dir`.
- Report artifacts with safe `relative_path` values only.
- On fatal failure, print to `stderr`, exit non-zero, and remove any stale
  `--response` file instead of leaving stale success data behind.

## Adaptation Order

1. Replace preprocessing placeholders.
2. Connect the real inference entrypoint.
3. Write artifacts only after they exist on disk.
4. Emit the success response only after artifacts and payload are complete.
