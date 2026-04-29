# Origin Master Sync Into Fh-Master Design

## Context

As of April 15, 2026, `fh-master` must absorb the latest upstream code from `origin/master` while preserving the user’s unrelated dirty primary-checkout changes until the verified merge is ready to land. The current repository state shows:

- active branch: `fh-master`
- upstream sync target: fetched `origin/master` at commit `9badbee8d2090282a8fae1d088f716520967b6c2`
- local primary worktree contains unrelated tracked and untracked changes
- merge overlap is concentrated in:
  - `app/main_window.py`
  - `app/services_container.py`
  - `requirements.txt`

`fh-master` currently carries the AI tool management, loudness, guarded tab switching, stopped-phase analysis display, and shared shutdown flow. `origin/master` carries PLC settings UI, PLC shutdown behavior from `MainWindow.closeEvent()`, and the `python-snap7` dependency.

The upstream PLC slice also arrives through non-conflicting file additions and auto-merges, including:

- `ui/modules/plc_settings.py`
- `app/service/plc_service.py`
- `model/plc_table.py`
- PLC-related fields already covered in `app/service/config_service.py`, `ui/modules/config.py`, and `model/test_config_table.py`

## Goals

- Merge fetched upstream commit `9badbee8d2090282a8fae1d088f716520967b6c2` into `fh-master`.
- Perform the merge in an isolated worktree so the dirty primary checkout is preserved until the verified merge is ready to land.
- Resolve conflicts so both branch feature sets survive:
  - keep `fh-master` AI tool management, loudness, stopped-phase, and guarded tab behavior
  - keep `origin/master` PLC settings UI, PLC cleanup, and PLC dependency wiring
- Verify the merged result with focused pytest coverage around the touched surfaces.

## Non-Goals

- No rebase, force-push, or history rewrite.
- No unrelated refactors or cleanup outside what is required to complete the merge safely.
- No destructive or speculative changes to the user’s unrelated dirty primary-worktree files.

## Design

### Isolated Merge Workspace

Create a dedicated `.worktrees/...` worktree from the current `fh-master` tip and perform the merge there. The isolated branch is the only place where merge state, conflict markers, or resolution edits may occur.

After the merge commit is verified in the isolated worktree, advance the real `fh-master` branch from the primary checkout with a fast-forward merge of the staging branch. That final landing step will update tracked branch files in the primary checkout, but it is allowed only if Git confirms the user’s existing local `.gitignore` edit and untracked files are not being overwritten.

### Conflict Resolution Rules

#### `app/main_window.py`

Preserve the `fh-master` test page and navigation behavior:

- `AiToolManagementModule`
- `LoudnessModule`
- stopped-phase display channel support
- guarded main-tab switching and role enforcement
- shared `services.shutdown()` close path

Also preserve the `origin/master` PLC integration where it does not invalidate the above:

- `PlcSettingsModule` on the hardware page
- PLC service/thread shutdown inside `closeEvent()`
- required Qt imports used by PLC shutdown

Shutdown ownership is explicit:

1. `MainWindow.closeEvent()` remains the owner of PLC service/thread cleanup because those runtime objects live on the `services` object and are created by `PlcSettingsModule`.
2. The concrete runtime interface to preserve is:
   - `services.plc_services`: mapping of PLC ids to service objects
   - `services.plc_threads`: mapping of PLC ids to worker threads
   - for each non-`None` PLC service, call `QMetaObject.invokeMethod(service, "stop_plc_service", Qt.ConnectionType.BlockingQueuedConnection)`
   - for each non-`None` PLC thread, call `quit()` then `wait(2000)`
3. After PLC cleanup is attempted, `MainWindow.closeEvent()` must call `services.shutdown()` once to preserve the existing `fh-master` shared AI/database shutdown boundary.
4. After shared shutdown, `MainWindow.closeEvent()` must still attempt `video.stop_camera()`.
4. Cleanup failures should remain best-effort and must not reintroduce direct `db.close()` ownership into `MainWindow`.

The resolved file should expose both AI tooling and PLC hardware flows in a single coherent window implementation.

#### `app/services_container.py`

Keep the richer event bus and runtime contracts already required by `fh-master`, including:

- `FactoryRuntimeStoppedPayload`
- `ai_toolchains_changed`
- `shutdown()`
- stopped-phase readiness behavior

Also keep the concrete PLC-facing service container surface needed by the merged upstream PLC module wiring:

- `FactoryPlcIngressPayload`
- `EventBus.factory_plc_event`
- `ServiceContainer._start_from_plc()`

`ServiceContainer._start_from_plc()` must remain available because `ui/modules/plc_settings.py` binds it as the PLC start callback. In the merged result it may stay as the lightweight upstream stub/logging hook, but it must not displace the existing `fh-master` event bus contracts or the shared `shutdown()` behavior.

#### `requirements.txt`

Preserve both dependency additions:

- `mosqito==1.2.1`
- `python-snap7==2.0.2`

### Verification

Run focused tests that exercise the touched merge surfaces:

`pytest tests/test_main_window.py tests/test_service_container.py tests/test_config_module.py tests/test_config_service.py tests/test_factory_mode_controller -v`

Acceptance must include direct coverage for the protected PLC-preservation points:

- inside `tests/test_main_window.py`:
  - hardware-page wiring keeps the PLC settings card/module alongside the existing `fh-master` hardware-page content
  - `closeEvent()` attempts PLC service/thread cleanup before `services.shutdown()`, then still attempts camera shutdown
- inside `tests/test_service_container.py`:
  - `ServiceContainer._start_from_plc()` remains present
- direct smoke coverage for:
  - `ui/modules/plc_settings.py`
  - `app/service/plc_service.py`

If a merge resolution reveals a narrower failing boundary, add the minimal extra targeted test needed before concluding the merge is complete.

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: `fh-master` AI tool management UI, loudness UI, stopped-phase behavior, guarded tab navigation, shared shutdown flow, `origin/master` PLC settings UI, PLC shutdown behavior, dependency installation via `requirements.txt`
- Allowed breakage: none
- Migration strategy: none
