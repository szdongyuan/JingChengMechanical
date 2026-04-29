# Origin Master Sync Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge `origin/master` into `fh-master` in an isolated worktree and resolve the overlapping files so both the AI/loudness branch work and the PLC branch work remain functional.

**Architecture:** Perform the merge from a fresh worktree created off `fh-master`, let Git auto-merge the non-overlapping files, then resolve the true conflict set with explicit branch-preservation rules. Verify the merged result with focused pytest coverage on the touched window, service-container, config, and factory-mode surfaces.

**Tech Stack:** Git worktrees, Python, PyQt6, pytest

---

### Task 1: Perform The Isolated Merge And Resolve Conflicts

**Files:**
- Modify: `app/main_window.py`
- Modify: `app/services_container.py`
- Modify or carry through auto-merged upstream files: `app/service/config_service.py`
- Modify: `requirements.txt`
- Create or carry through auto-merged upstream files: `ui/modules/plc_settings.py`
- Modify or carry through auto-merged upstream files: `ui/modules/config.py`
- Create or carry through auto-merged upstream files: `app/service/plc_service.py`
- Create or carry through auto-merged upstream files: `model/plc_table.py`
- Modify or carry through auto-merged upstream files: `model/test_config_table.py`
- Test: `tests/test_plc_service.py`
- Test: `tests/test_plc_settings_module.py`
- Test: `tests/test_main_window.py`
- Test: `tests/test_service_container.py`
- Test: `tests/test_config_module.py`
- Test: `tests/test_config_service.py`
- Test: `tests/test_factory_mode_controller`

- [ ] **Step 1: Create the isolated merge worktree**

Run:

```bash
git worktree add .worktrees/origin-master-sync-20260415 -b origin-master-sync-20260415 fh-master
```

Expected: a new worktree rooted at `.worktrees/origin-master-sync-20260415` on branch `origin-master-sync-20260415`.

- [ ] **Step 2: Merge the fetched upstream branch**

Run inside `.worktrees/origin-master-sync-20260415`:

```bash
git merge 9badbee8d2090282a8fae1d088f716520967b6c2
```

Expected: Git auto-merges most files and stops on the true overlap set, which is expected to center on `app/main_window.py`, `app/services_container.py`, and `requirements.txt`.

If Git reports additional conflicted files, inspect them against the same preservation rules:

- keep the existing `fh-master` AI/loudness/stopped-phase behavior
- keep the upstream PLC/config/dependency additions
- if a reported extra conflict is unrelated to those boundaries or cannot be resolved locally without redefining scope, stop and escalate instead of guessing

- [ ] **Step 3: Verify the upstream PLC-only file additions landed in the staging branch**

After the merge stops, confirm the staging branch now contains the upstream PLC slice:

- `ui/modules/plc_settings.py`
- `app/service/plc_service.py`
- `model/plc_table.py`
- `app/service/config_service.py`
- `ui/modules/config.py`
- `model/test_config_table.py`

Expected: these files are present from `origin/master` without manual reimplementation.

- [ ] **Step 4: Resolve `app/main_window.py`**

Resolve the conflict by combining both branch feature sets:

- keep `fh-master` AI tool management page, loudness module, stopped-phase analysis visibility, guarded tab switching, and `services.shutdown()` close flow
- keep `origin/master` PLC settings module on the hardware page
- keep PLC cleanup inside `closeEvent()` without regressing the shared shutdown path
- keep the imports and helper methods required by the combined implementation

Resolve shutdown ownership explicitly:

- PLC service/thread cleanup stays in `MainWindow.closeEvent()`
- `services.shutdown()` runs once after PLC cleanup
- `video.stop_camera()` is still attempted after shared shutdown
- do not reintroduce direct `db.close()` ownership into `MainWindow`

- [ ] **Step 5: Resolve `app/services_container.py`**

Resolve the conflict by preserving the existing richer `fh-master` service-container API while keeping the concrete PLC-facing wiring from `origin/master`:

- keep `FactoryRuntimeStoppedPayload`
- keep `ai_toolchains_changed`
- keep stopped-phase readiness behavior
- keep `shutdown()`
- keep `FactoryPlcIngressPayload`
- keep `EventBus.factory_plc_event`
- restore `ServiceContainer._start_from_plc()` because `PlcSettingsModule` calls it

- [ ] **Step 6: Resolve `requirements.txt`**

Preserve both dependency lines:

```text
mosqito==1.2.1
python-snap7==2.0.2
```

- [ ] **Step 7: Add direct PLC-preservation coverage to `tests/test_main_window.py`, `tests/test_service_container.py`, and PLC smoke tests**

Update `tests/test_main_window.py` so the merged result proves:

- the hardware page still includes the PLC settings module/card alongside the existing `fh-master` hardware content
- `closeEvent()` attempts PLC service/thread cleanup before `services.shutdown()`
- `video.stop_camera()` is attempted after shared shutdown
- camera shutdown still runs even when shared shutdown or PLC cleanup are best-effort paths
- `MainWindow` does not reclaim direct `db.close()` ownership

Update `tests/test_service_container.py` so the merged result proves:

- `ServiceContainer._start_from_plc()` remains present after the merge

Add direct smoke tests for the new upstream PLC units:

- `tests/test_plc_service.py`
- `tests/test_plc_settings_module.py`

- [ ] **Step 8: Verify the merge is staged and conflict-free**

Run inside `.worktrees/origin-master-sync-20260415`:

```bash
git status --short
```

Expected: no unmerged paths remain.

- [ ] **Step 9: Run focused verification**

Run inside `.worktrees/origin-master-sync-20260415`:

```bash
pytest tests/test_main_window.py tests/test_service_container.py tests/test_config_module.py tests/test_config_service.py tests/test_factory_mode_controller tests/test_plc_service.py tests/test_plc_settings_module.py -v
```

Expected: PASS. If a failure appears in a directly adjacent upstream surface, fix only that merge regression and rerun the same command.

- [ ] **Step 10: Add the minimal adjacent test only if verification exposes a narrower failing boundary**

If Step 9 surfaces a regression that is not fully covered by the existing focused command, add the smallest targeted test that reproduces that boundary inside one of the already-included focused test files (`tests/test_main_window.py`, `tests/test_service_container.py`, `tests/test_config_module.py`, `tests/test_config_service.py`, `tests/test_factory_mode_controller/...`, `tests/test_plc_service.py`, or `tests/test_plc_settings_module.py`), fix only the merge regression, and rerun:

```bash
pytest tests/test_main_window.py tests/test_service_container.py tests/test_config_module.py tests/test_config_service.py tests/test_factory_mode_controller tests/test_plc_service.py tests/test_plc_settings_module.py -v
```

Expected: PASS again with the new targeted coverage included in the branch.

- [ ] **Step 11: Commit the merge result**

Run inside `.worktrees/origin-master-sync-20260415`:

```bash
git add app ui model requirements.txt tests
git commit -m "Merge origin/master into fh-master"
```

Expected: a merge commit on `origin-master-sync-20260415` whose first parent is the pre-merge `fh-master` tip.

- [ ] **Step 12: Fast-forward the real `fh-master` branch to the verified merge commit**

Run in the primary checkout at the repo root:

```bash
git merge --ff-only origin-master-sync-20260415
```

Expected: `fh-master` advances to the verified merge commit without overwriting the user’s unrelated local `.gitignore` change or untracked files. If Git reports that the fast-forward would overwrite local changes, stop and reassess instead of forcing the branch update.
