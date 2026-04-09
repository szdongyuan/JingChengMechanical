# Windows PyInstaller Templates

Use these templates only when a Windows `.exe` deliverable is actually
required. `.exe` is a delivery format, not a protocol. The host-facing
protocol stays the same whether you ship plain Python or a packaged `.exe`.

## Runtime Mode Mapping

- `toolchain_oneshot.spec` packages the one-shot CLI entrypoint.
- `toolchain_resident.spec` packages the resident stdio entrypoint.
- Both one-shot and resident modes can remain plain Python.
- Both one-shot and resident modes can also be packaged as Windows `.exe`.

## `onedir` Versus `onefile`

- `onedir` is the practical default for ML payloads because sidecar weight
  files and non-Python assets stay visible, replaceable, and easier to debug.
- `onefile` is optional when a single-file handoff matters more than package
  size or startup latency.
- Expect higher startup time with `onefile` because the bundle must unpack
  before your wrapper starts.

## What To Customize

1. Point each spec file at the correct entry script for the chosen runtime
   shape.
2. Decide whether weight files stay external or are bundled.
3. Add non-Python assets to `datas`.
4. Keep path placeholders portable. Do not hard-code machine-specific project
   paths.

## Build Flow

Use `build.ps1` as a starting point. It shows mode-specific `pyinstaller`
commands plus comments for copying weight files and non-Python assets into the
final deliverable when they stay external.
