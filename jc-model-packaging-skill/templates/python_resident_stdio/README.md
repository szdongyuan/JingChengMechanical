# Python Resident Stdio Template

Use this template when cold imports dominate startup latency, the model should
stay in memory, and the host will send repeated requests to the same process.

## Runtime Rules

- Read one JSON object per line from `stdin`.
- Emit only protocol JSON lines to `stdout`.
- Keep `stderr` human-readable only.
- Treat malformed inbound JSON or malformed protocol envelopes as fatal:
  log to `stderr` and exit non-zero.
- Treat valid request-scoped analyze failures as non-fatal by emitting one
  `error` envelope and continuing to accept later `analyze` messages.
- Exit cleanly on `{"message_type": "shutdown"}` without writing a `stdout`
  response line.

## Adaptation Order

1. Replace the model preload hook.
2. Replace preprocessing and inference placeholders.
3. Keep any warm-path caches process-local and safe to reuse across requests.
4. Write artifacts only under the host-provided `artifacts_dir`.
5. Emit `result` envelopes only after artifacts and payload are complete.
