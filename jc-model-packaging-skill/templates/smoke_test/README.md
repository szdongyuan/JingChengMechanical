# Smoke Test Fixtures

Use these fixtures to verify that a generated wrapper still matches the
embedded protocol contract after adaptation.

## One-Shot Fixtures

- `request.json`: one-shot input request.
- `response.success.json`: expected one-shot success shape.
- `stderr.error.txt`: captured fatal one-shot `stderr` example. This is
  non-protocol human-readable text, not a JSON response file.

## Resident Fixtures

- `resident_input.jsonl`: resident `analyze` input example.
- `resident_output.success.jsonl`: resident `result` output example.
- `resident_output.error.jsonl`: resident request-scoped `error` output example.
- `resident_shutdown_input.jsonl`: resident `shutdown` input example.

## Expected Success Checks

1. Protocol shape matches the documented contract for the chosen runtime mode.
2. Every listed artifact exists before the success payload is emitted.
3. Every listed artifact exists under the supplied artifacts directory.
4. Every artifact `relative_path` stays relative and traversal-safe.
5. No artifact `relative_path` is absolute, drive-anchored, or contains `..`.
6. One-shot fatal failures stay on `stderr` as non-protocol text.
7. Resident warm-path verification reuses the same process for repeated analyze
   requests before shutdown.

## Suggested Replay Flow

1. Run the one-shot wrapper against `request.json`.
2. Compare the produced success payload with `response.success.json`.
3. Trigger one fatal one-shot failure and confirm the emitted `stderr` looks
   like `stderr.error.txt` instead of a JSON success payload.
4. Start one resident process and keep it alive for the full replay.
5. Send repeated analyze requests from `resident_input.jsonl` through the same
   process to verify the resident warm-path behavior.
6. Compare success and request-scoped failure lines with the resident output
   fixtures.
7. Send `resident_shutdown_input.jsonl` and confirm the process exits without
   writing extra stdout lines.
