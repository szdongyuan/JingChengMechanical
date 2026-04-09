# Python Resident Stdio Template

Use this template when cold imports dominate startup latency, the model should
stay in memory, and the host will send repeated requests to the same process.

## Runtime Rules

- Treat the transport as UTF-8 JSON Lines over `stdin` / `stdout`.
- Read one UTF-8 JSON object per line from `stdin`.
- Emit only UTF-8 protocol JSON lines to `stdout`.
- Keep `stderr` human-readable only.
- Treat malformed inbound JSON or malformed protocol envelopes as fatal:
  log to `stderr` and exit non-zero.
- Treat `preload_model()` and any other process-global runtime initialization
  failure as fatal: log to `stderr`, exit non-zero, and emit no `error`
  envelope.
- Treat valid request-scoped analyze failures after runtime initialization
  succeeds as non-fatal by emitting one `error` envelope and continuing to
  accept later `analyze` messages in the default template. An external host may
  still choose to discard the process.
- Exit cleanly on `{"message_type": "shutdown"}` without writing a `stdout`
  response line.
- Guard cleanup on fatal exit, `shutdown`, and `stdin` EOF so cleanup failures
  are logged to `stderr` and return non-zero instead of escaping as raw
  exceptions.

## Adaptation Order

1. Replace the model preload hook.
2. Replace preprocessing and inference placeholders.
3. Keep any warm-path caches process-local and safe to reuse across requests.
4. Write artifacts only under the host-provided `artifacts_dir`.
5. Emit `result` envelopes only after artifacts and payload are complete and
   every declared artifact already exists as a readable file under
   `artifacts_dir`.
