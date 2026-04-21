# PLC Settings Test Module Removal Design

## Summary

Remove `tests/test_plc_settings_module.py` from the repository entirely.

This is a test-scope cleanup only. It does not change `ui/modules/plc_settings.py`, PLC runtime behavior, hardware management behavior, or any `.dy` import/export code.

The product decision is already confirmed:

- the PLC settings test module is not owned by this workstream
- it is unrelated to the current feature boundary
- it should be deleted rather than skipped, quarantined, or rewritten

## Goals

- Delete `tests/test_plc_settings_module.py` from the repo.
- Stop running PLC settings module tests as part of this branch's verification.
- Keep the change limited to test removal only.

## Non-Goals

- No changes to `ui/modules/plc_settings.py`.
- No changes to PLC services, hardware management, or PLC runtime flow.
- No replacement PLC tests.
- No pytest config changes, markers, skips, or ignore rules.

## Design

### 1. Delete the test module

Remove `tests/test_plc_settings_module.py` from the repository.

No replacement file is added. No partial retention of individual test cases is required.

### 2. Keep production code untouched

This work must not modify:

- `ui/modules/plc_settings.py`
- PLC service/runtime code
- hardware management modules
- `.dy` import/export implementation

### 3. Verification

Verification for this change is limited to:

- confirming `tests/test_plc_settings_module.py` no longer exists
- re-running the current feature-boundary regression commands that do not depend on that file

If `pytest tests -q` still fails after the file is removed, that is outside the scope of this change unless the remaining failure is caused by references to the deleted file.

## Testing

Required verification:

- `Test-Path tests/test_plc_settings_module.py` returns `False`
- `pytest tests/test_config_service.py tests/test_config_module.py tests/test_ai_service.py tests/test_ai_tool_management_module.py tests/test_golden_sample_service.py tests/test_config_package_service.py -q`
- `pytest tests/test_ai_service_integration.py tests/test_ai_resident_runtime.py tests/test_db_connection_service.py tests/test_service_container.py -q`

Optional verification:

- `pytest tests -q`

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces: PLC runtime code, hardware management code, `.dy` import/export code, current non-PLC regression coverage
- Allowed breakage: loss of automated coverage from `tests/test_plc_settings_module.py`
- Migration strategy: none
