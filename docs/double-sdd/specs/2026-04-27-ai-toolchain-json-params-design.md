# AI Toolchain JSON Params Design

## Goal
Move AI JSON parameters from per-config AI binding to global AI tool management so each AI toolchain owns its request parameter JSON.

## Requirements
- `AI工具管理` edits and persists a toolchain-level `params_json` field.
- The config page AI binding card no longer shows or saves JSON params.
- AI preflight and request building use `AiToolchainTable.params_json`.
- No backward compatibility or fallback to binding-level params is required because the software is not released.
- Existing binding-level `params_json` may remain as an unused database/model field, but runtime and UI must not read it.

## Data Flow
- Toolchain CRUD validates `params_json` as a JSON object string and stores normalized text.
- `resolve_binding()` returns the selected binding plus the selected toolchain params for runtime consumers.
- `save_binding()` ignores any incoming binding params and stores `{}` in the old binding column.
- Config package toolchain metadata includes `params_json`; AI binding package metadata no longer depends on params.

## Compatibility / Migration
- Backward compatibility: not required.
- Protected surfaces: current toolchain CRUD, config binding selection, AI runtime request creation, config package import/export after this change.
- Allowed breakage: old binding-level JSON params are ignored.
- Migration strategy: add a toolchain-level column with default `{}` and do not migrate per-binding values.

## Verification
- Service tests cover toolchain params persistence, validation, preflight, and request payloads.
- UI tests cover config-page removal and AI tool management editing.
- Package tests cover toolchain metadata round-trip.
