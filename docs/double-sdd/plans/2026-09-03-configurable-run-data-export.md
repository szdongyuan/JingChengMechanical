# Configurable Run Data Export Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a configurable run-data export that produces a one-sheet Excel summary and optional audio, unchanged selected-record SQLite copy, and current CSV files with versioned two-level manifests.

**Architecture:** Extend the existing `RunArchiveService` orchestration while preserving its worker-thread, progress, cancellation, and database-copy foundations. Add a tolerant primary/attachment projection, a bulk current-effective AI lookup, pure Excel and manifest builders, and a v2 package kernel; keep each artifact builder independent so missing optional attachments do not invalidate primary rows.

**Tech Stack:** Python 3.12, PyQt6, SQLite/Peewee, `openpyxl==3.1.5`, standard-library `wave`, `zipfile`, `json`, and pytest.

---

## Source Specification

- `docs/double-sdd/specs/2026-09-03-configurable-run-data-export-design.md`

## File Structure

### New files

- `app/service/run_record_export/naming.py` — deterministic result/source display mapping and safe v2 record/member path allocation.
- `app/service/run_record_export/excel.py` — pure one-sheet OOXML workbook projection.
- `app/service/run_record_export/manifests.py` — exact root and per-run v2 manifest builders plus declared-member validation.
- `app/service/run_record_export/attachments.py` — cancel-aware PCM16 channel splitting and current CSV encoding.
- `app/service/run_record_export/package.py` — configurable v2 preparation, ZIP/standalone-XLSX writing, validation, counts, and atomic publication.
- `ui/popups/run_record_export_dialog.py` — modal four-option export-settings dialog and target-path chooser.
- `tests/run_record_export/test_contracts_and_naming.py` — options, immutable values, result mapping, and safe-name tests.
- `tests/run_record_export/test_excel.py` — workbook content, types, formatting, and literal-string tests.
- `tests/run_record_export/test_manifests.py` — exact schema, count, ordering, nullability, and member-closure tests.
- `tests/run_record_export/test_configurable_package.py` — option combinations, attachment recovery, cancellation, and publication tests.
- `tests/test_run_record_export_dialog.py` — dialog defaults, validation, and chooser routing tests.

### Modified files

- `app/service/run_record_export/contracts.py` — add v2 options, tolerant read values, packaged-record values, skip/count/result contracts, and reader/AI protocols; retain the v1 record contract used by playback and legacy tests.
- `app/service/run_record_export/archive.py` — reuse extracted attachment helpers and make `RunArchiveService` orchestrate the v2 package; retain `write_run_archive` as the tested v1 pure function until all existing callers are migrated.
- `app/service/run_record_export/__init__.py` — export the v2 public contracts needed by UI and service wiring.
- `app/service/run_persistence/export_reader.py` — add tolerant selected-run projection while preserving `read_many`, `read_many_report`, and unchanged `read_copy_database` output.
- `app/service/ai_service.py` — add deterministic bulk lookup for current effective successful AI score/label.
- `app/services_container.py` — construct AI service before the archive service and inject the AI export reader.
- `ui/modules/data_management.py` — invoke settings dialog, choose `.xlsx` or `.zip`, pass options and cancellation checkpoints, and show partial-success counts.
- `requirements.txt` — add the pinned Excel writer dependency.
- `tests/run_persistence/test_export_reader.py` — tolerant primary/audio/current projection tests plus database-copy regression.
- `tests/test_ai_service.py` — bulk AI export lookup tests.
- `tests/test_service_container.py` — service injection/order regression.
- `tests/test_data_management_module.py` — configurable UI-to-service integration, path routing, progress, cancellation, and completion-summary tests.

## Implementation Constraints

- Do not modify or delete unrelated dirty-worktree files.
- Do not change the schemas or selected rows produced by `RunExportReader.read_copy_database()`.
- Do not add `ai_execution_table` to `copy_data.sqlite3`.
- Do not require audio or current data for primary Excel/manifest projection.
- Do not silently preserve v1 package layout in the new UI flow; v2 paths and manifests must match the spec exactly.
- Keep the old `write_run_archive()` tests passing until the v2 service/UI path has complete replacement coverage.
- Every task ends with focused tests and a commit before review.

### Task 1: V2 Contracts and Deterministic Naming

**Files:**
- Modify: `app/service/run_record_export/contracts.py`
- Create: `app/service/run_record_export/naming.py`
- Modify: `app/service/run_record_export/__init__.py`
- Create: `tests/run_record_export/test_contracts_and_naming.py`

- [ ] **Step 1: Write failing option-contract tests**

Cover exact booleans, all-false rejection, documented UI defaults, immutable values, and a helper/property that returns `xlsx` only for Excel-only output and `zip` for every other valid combination.

```python
def test_default_export_options_match_approved_dialog_defaults():
    options = RunDataExportOptions.defaults()
    assert options == RunDataExportOptions(
        include_excel=True,
        include_audio=True,
        include_database=True,
        include_current_csv=False,
    )
    assert options.output_kind == "zip"


def test_excel_only_routes_to_xlsx():
    options = RunDataExportOptions(True, False, False, False)
    assert options.output_kind == "xlsx"
```

- [ ] **Step 2: Run the option tests and verify failure**

Run: `python -m pytest tests/run_record_export/test_contracts_and_naming.py -k options -v`

Expected: FAIL because `RunDataExportOptions` does not exist.

- [ ] **Step 3: Add immutable v2 values and protocols**

Add focused dataclasses with exact validation:

```python
@dataclass(frozen=True, slots=True)
class RunDataExportOptions:
    include_excel: bool
    include_audio: bool
    include_database: bool
    include_current_csv: bool

    def __post_init__(self) -> None:
        values = (...)
        if any(type(value) is not bool for value in values):
            raise TypeError("export options must be exact booleans")
        if not any(values):
            raise ValueError("at least one export option is required")
```

Also define the minimal immutable interfaces used by later tasks:

- `AudioExportFacts` with optional durable source metadata and a read-stage issue.
- `CurrentExportFacts` with optional sample tuple and a read-stage issue.
- `AiExportValue(score: float | None, label: str | None)`.
- `RunDataExportRecord` containing mandatory primary facts, a zero-based `selection_index`, and the two optional attachment facts.
- `RunDataExportSkippedRecord(run_id, component, reason, selection_index)` using the same zero-based original-selection ordinal.
- `RunDataExportReadReport(selected_run_ids, records, skipped)` preserving the exact validated input tuple as well as projected values.
- `PackagedAudio`, `PackagedCurrent`, and `PackagedRunRecord` for exact emitted paths/statuses.
- `RunDataExportCounts` and `RunDataExportResult` for completion reporting.
- Reader protocols for tolerant run projection, unchanged database-copy reads, and bulk AI reads.

Validate canonical UUIDs, exact strings, finite AI scores, nonnegative selection indexes, three-state attachment statuses, and positive package sequence values at construction boundaries. Require report record/skip UUIDs and indexes to agree with `selected_run_ids`, so later sorting cannot lose original selection order.

- [ ] **Step 4: Write failing naming and display-mapping tests**

Test exact result precedence, judgment-source translation, six-digit sequence prefixes, empty/repeated/Unicode SN values, all invalid characters, control characters, underscore collapse, trailing dot/space trimming, 48-code-point truncation, and case-insensitive reserved basenames with extensions.

```python
@pytest.mark.parametrize(
    ("state", "judgment", "expected"),
    [
        ("FAILED", "PASS", "OK"),
        ("CANCELED", "FAIL", "NG"),
        ("NO_RESULT", None, "无结果"),
        ("INTERRUPTED", None, "错误"),
    ],
)
def test_excel_result_uses_approved_precedence(state, judgment, expected):
    assert excel_result(state=state, judgment=judgment) == expected
```

- [ ] **Step 5: Implement naming and mapping helpers**

Implement `excel_result`, `manifest_result`, `excel_judgment_source`, `sanitize_sn_segment`, `record_directory`, `audio_member_path`, and `current_member_path`. Return POSIX member paths and validate that no emitted path contains empty, `.` or `..` segments.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/run_record_export/test_contracts_and_naming.py -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add app/service/run_record_export/contracts.py app/service/run_record_export/naming.py app/service/run_record_export/__init__.py tests/run_record_export/test_contracts_and_naming.py
git commit -m "feat: define configurable run export contracts"
```

### Task 2: Tolerant Run and Attachment Projection

**Files:**
- Modify: `app/service/run_persistence/export_reader.py`
- Modify: `tests/run_persistence/test_export_reader.py`

- [ ] **Step 1: Write failing primary-without-audio tests**

Add tests proving `read_configurable_report()` returns a primary record when the audio row is absent, when the source file is absent, and when audio metadata is malformed. Assert `audio.issue` is populated while primary fields remain available. Assert every record carries its original zero-based selection index and the report preserves the exact input UUID tuple.

- [ ] **Step 2: Write failing tolerant identity and current tests**

Cover:

- malformed or absent frozen workorder values become empty SN/model strings;
- invalid current JSON becomes `CurrentExportFacts(samples=None, issue=...)`;
- one missing run becomes a `primary` skip using the selected UUID and original selection index;
- terminal `ACTIVE` or inconsistent judgment facts become primary skips;
- output records sort by `accepted_at DESC, run_id DESC` regardless of selection order.

- [ ] **Step 3: Run the new reader tests and verify failure**

Run: `python -m pytest tests/run_persistence/test_export_reader.py -k configurable -v`

Expected: FAIL because `read_configurable_report()` does not exist.

- [ ] **Step 4: Implement separate primary and audio reads**

Add `RunExportReader.read_configurable_report(run_ids)` without changing existing methods. Use one transaction with separate `run` and `audio` queries so duplicate/missing audio cannot duplicate or invalidate primary facts. Validate selection first, retain `selected_run_ids` unchanged in the report, enumerate it into an index map before any data sorting, group by UUID, and use the spec's mandatory-primary rules.

Use tolerant workorder extraction:

```python
def _display_identity(config_snapshot_json: object) -> tuple[str, str]:
    # Return ("", "") on malformed JSON/envelope or non-text values.
```

Decode channel order only for audio attachment facts. Convert any audio metadata/channel-order failure to `AudioExportFacts(..., issue=reason)` rather than a primary exception. Decode the entire current trace; any failure becomes a current attachment issue.

- [ ] **Step 5: Preserve and re-run legacy reader tests**

Run: `python -m pytest tests/run_persistence/test_export_reader.py -v`

Expected: PASS, including unchanged `read_many`, playback-facing strict validation, and the exact three-table database copy.

- [ ] **Step 6: Commit Task 2**

```bash
git add app/service/run_persistence/export_reader.py tests/run_persistence/test_export_reader.py
git commit -m "feat: add tolerant run export projection"
```

### Task 3: Bulk Current-Effective AI Export Lookup

**Files:**
- Modify: `app/service/ai_service.py`
- Modify: `tests/test_ai_service.py`

- [ ] **Step 1: Write failing bulk lookup tests**

Create multiple executions for several run UUIDs and prove the new method:

- accepts one exact nonempty tuple of unique canonical UUIDs;
- returns one mapping entry per requested UUID;
- selects only `is_current_effective = true` plus `module_status = success`;
- resolves inconsistent duplicates by `started_time DESC, request_id DESC`;
- preserves a nonblank raw label exactly;
- returns null fields for missing, failed, empty-label, boolean-score, and non-finite-score cases;
- performs one bulk select rather than one query per run.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_ai_service.py -k export_values -v`

Expected: FAIL because the bulk export method is absent.

- [ ] **Step 3: Implement deterministic bulk lookup**

Add a method such as:

```python
def get_current_effective_export_values(
    self, run_ids: tuple[str, ...]
) -> dict[str, AiExportValue]:
    ...
```

Issue one ordered Peewee query using `test_run_id.in_(run_ids)`, exact effective/success predicates, and descending time/request ordering. Keep the first row per run, validate finite numeric scores without imposing a range, preserve accepted labels exactly, and fill missing run IDs with `AiExportValue(None, None)`.

- [ ] **Step 4: Run focused and AI regression tests**

Run: `python -m pytest tests/test_ai_service.py tests/test_ai_execution_table.py -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add app/service/ai_service.py tests/test_ai_service.py
git commit -m "feat: expose effective AI export values"
```

### Task 4: One-Sheet Excel Builder

**Files:**
- Create: `app/service/run_record_export/excel.py`
- Create: `tests/run_record_export/test_excel.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Pin the workbook dependency and write failing workbook tests**

Add `openpyxl==3.1.5` to `requirements.txt`. Write tests around an in-memory `build_excel_workbook(records, cancel_check=None) -> bytes` contract.

Verify:

- only `检测记录` exists;
- exact ten-column order;
- records are accepted in already allocated sequence order;
- UTC timestamps display in Asia/Shanghai using a real Excel datetime cell and `yyyy-mm-dd hh:mm:ss` format;
- SN `00123` and strings beginning with `=`, `+`, `-`, or `@` remain literal text;
- result/source mapping, numeric AI score, raw AI label, and blank values;
- emitted audio paths alone are joined with `\n`;
- freeze pane `A2`, autofilter range, widths, wrap, and approved fills;
- `cancel_check` runs once per data row.

- [ ] **Step 2: Run workbook tests and verify failure**

Run: `python -m pytest tests/run_record_export/test_excel.py -v`

Expected: FAIL because the Excel builder does not exist.

- [ ] **Step 3: Implement a bounded-memory workbook writer**

Use `openpyxl.Workbook(write_only=True)`, configure the worksheet before rows are appended, use `WriteOnlyCell` styles, and explicitly set user-originated text cells to string type so formula-looking values never become formulas. Apply immutable named styles or reused style objects rather than creating a style per cell.

```python
def build_excel_workbook(
    records: tuple[PackagedRunRecord, ...],
    *,
    cancel_check: Callable[[], None] | None = None,
) -> bytes:
    ...
```

Save to `BytesIO`, reopen with `openpyxl.load_workbook(..., read_only=True, data_only=False)` for structural validation, and return nonempty bytes.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/run_record_export/test_excel.py -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add requirements.txt app/service/run_record_export/excel.py tests/run_record_export/test_excel.py
git commit -m "feat: build run export Excel summary"
```

### Task 5: Exact V2 Manifest Builders

**Files:**
- Create: `app/service/run_record_export/manifests.py`
- Create: `tests/run_record_export/test_manifests.py`

- [ ] **Step 1: Write failing exact-schema tests**

Build exported, skipped, and not-selected packaged records. Assert the complete Python dictionaries and serialized JSON for:

- `jingcheng-run-export-package/v2` root schema;
- `jingcheng-run-export-record/v2` per-run schema;
- exact required keys and enums;
- option booleans, nullable root artifacts, and deterministic UTC timestamp;
- count units and zero counts for unselected components;
- ordered record index and skips ordered by original `selection_index`, then component order `primary`, `audio`, `current`, even though exported records use accepted-time order;
- finite/null AI values;
- package-root-relative POSIX paths.

- [ ] **Step 2: Write failing declared-member closure tests**

Require a validation function to reject undeclared per-run attachments, paths naming absent members, duplicate member paths, traversal segments, status/path contradictions, count mismatches, and a per-run manifest missing from the root index.

- [ ] **Step 3: Run tests and verify failure**

Run: `python -m pytest tests/run_record_export/test_manifests.py -v`

Expected: FAIL because v2 manifest builders do not exist.

- [ ] **Step 4: Implement pure builders and validation**

Expose focused functions:

```python
def build_record_manifest(record: PackagedRunRecord) -> bytes: ...

def build_export_manifest(
    *, options, exported_at, selected_run_ids, records, skips, excel_path, database_path
) -> bytes: ...

def validate_declared_members(
    *, root_manifest: bytes, record_manifests: dict[str, bytes], members: set[str]
) -> None: ...
```

Serialize with `ensure_ascii=False`, `allow_nan=False`, compact separators, UTF-8 without BOM, and deterministic insertion/array order.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/run_record_export/test_manifests.py -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add app/service/run_record_export/manifests.py tests/run_record_export/test_manifests.py
git commit -m "feat: add versioned run export manifests"
```

### Task 6: Cancel-Aware Attachments and Configurable Package Kernel

**Files:**
- Create: `app/service/run_record_export/attachments.py`
- Create: `app/service/run_record_export/package.py`
- Modify: `app/service/run_record_export/archive.py`
- Modify: `app/service/run_record_export/__init__.py`
- Create: `tests/run_record_export/test_configurable_package.py`
- Modify: `tests/run_record_export/test_archive.py`

- [ ] **Step 1: Extract attachment behavior under legacy regression tests**

Move reusable PCM16 reading/splitting and current CSV serialization into `attachments.py`. Preserve byte-for-byte v1 outputs by adapting `archive.py` to call the extracted helpers. Add cancellation tests proving checks occur at least every 4,096 audio frames and before/after file reads; test whole-current-attachment rejection, UTF-8 without BOM, LF endings, round-trip float text, and trailing LF.

- [ ] **Step 2: Run extraction regressions with an unambiguous passing expectation**

Run: `python -m pytest tests/run_record_export/test_archive.py -k "current or split or cancel" -v`

Expected: PASS for all selected legacy/extraction regressions.

- [ ] **Step 3: Write failing option-combination and layout tests**

Parameterize all 15 valid option combinations. Assert:

- Excel-only writes a plain `.xlsx` file;
- every other combination writes ZIP;
- ZIP contains both manifest levels and only selected optional roots;
- record directories use six-digit sequence plus safe SN;
- WAV files use `CH{source_channel + 1}.wav` and Excel paths name actual emitted members;
- current files use `records/<dir>/current.csv`;
- unchanged database bytes are stored as `copy_data.sqlite3`;
- audio/current read-stage and file-stage issues are recoverable;
- neither Excel nor database selected plus zero emitted optional files fails generically;
- partial success counts and skips use run units and file units exactly.
- primary, audio, and current skips remain ordered by original selection index then component order after records are reordered for Excel/package sequence.
- duplicate source-channel identities that would produce the same `CH<n>.wav` name skip all audio for that run, publish no partial WAV member, and produce matching per-run/root skip and count values.

- [ ] **Step 4: Implement preparation and pure output assembly**

Implement a package function that receives the complete `RunDataExportReadReport` (including original `selected_run_ids` and selection indexes), AI values, options, optional database bytes, a deterministic clock, progress callback, and cancellation callback. Sort exported records by accepted time/run UUID for package sequence without changing their selection indexes; allocate paths, prepare each requested attachment all-or-nothing per run, merge AI values, build Excel after real audio paths are known, then sort all root skip entries by selection index and component order before building manifests. Validate member closure and return `RunDataExportResult`.

For ZIP members larger than 1 MiB, stream in 1 MiB chunks through `ZipFile.open(..., "w")` and call `cancel_check` between chunks. Check after every member.

- [ ] **Step 5: Implement atomic standalone and ZIP publication**

Use a sibling `mkstemp` with a short fixed prefix. Close and structurally validate the temporary target before `os.replace`:

- standalone XLSX: reopen through `openpyxl`;
- ZIP: `testzip()`, exact expected-member set, and manifest closure.

On cancellation or failure, remove only the resolved temporary file and preserve any pre-existing valid target.

- [ ] **Step 6: Add the complete request-failure and cleanup matrix**

Add explicit tests for:

- all selected primaries invalid;
- requested Excel builder failure;
- root and per-run manifest builder/consistency failure;
- ZIP CRC failure and XLSX structural-validation failure;
- `mkstemp`, streamed member write, and `os.replace` failure;
- a pre-existing target surviving every failure and cancellation path byte-for-byte;
- sibling temporary-file cleanup after each failure;
- cancellation during preparation, during a multi-chunk member write, and immediately before `os.replace`.

Run: `python -m pytest tests/run_record_export/test_configurable_package.py -k "failure or cancel or cleanup or existing_target or zero" -v`

Expected: PASS.

- [ ] **Step 7: Add a representative large-selection/streaming test**

Create at least 2,000 lightweight primary records plus one audio member larger than 2 MiB. Prove:

- the Excel builder is instantiated with `write_only=True`;
- workbook cancellation is checked for every row;
- package progress values are monotonic whenever the total is determinate;
- large ZIP members use chunks no larger than 1 MiB and do not use one giant `writestr` call;
- cancellation injected at a deterministic row/chunk prevents publication;
- the same fixture without cancellation publishes an XLSX/ZIP that reopens and passes manifest/CRC validation.

Run: `python -m pytest tests/run_record_export/test_configurable_package.py -k large_selection -v`

Expected: PASS.

- [ ] **Step 8: Preserve v1 kernel coverage**

Run: `python -m pytest tests/run_record_export/test_archive.py tests/run_record_export/test_configurable_package.py -v`

Expected: PASS. Existing `write_run_archive()` stays functional; new v2 tests exercise the new package kernel.

- [ ] **Step 9: Commit Task 6**

```bash
git add app/service/run_record_export/attachments.py app/service/run_record_export/package.py app/service/run_record_export/archive.py app/service/run_record_export/__init__.py tests/run_record_export/test_archive.py tests/run_record_export/test_configurable_package.py
git commit -m "feat: build configurable run export packages"
```

### Task 7: Service Orchestration and Dependency Wiring

**Files:**
- Modify: `app/service/run_record_export/archive.py`
- Modify: `app/service/run_persistence/export_reader.py`
- Modify: `app/services_container.py`
- Modify: `tests/run_record_export/test_pinned_handoff.py`
- Modify: `tests/run_persistence/test_export_reader.py`
- Modify: `tests/test_service_container.py`
- Modify: `tests/run_record_export/test_configurable_package.py`

- [ ] **Step 1: Write failing orchestration tests**

Specify the new service call:

```python
result = service.export(
    run_ids,
    target_path,
    options=options,
    progress_callback=progress,
    cancel_check=cancel_check,
)
```

Prove it:

- calls `read_configurable_report` once with the validated selection and passes the returned original-order report intact to the package kernel;
- bulk-reads AI only when Excel or ZIP manifests need it (ZIP always needs it);
- calls unchanged `read_copy_database` only when database is selected and with the original selection;
- does not await chart enrichment because Excel exports only primary/AI facts and no full chart arrays;
- passes selection-indexed skips, bytes, callbacks, and both original selection order and deterministic package ordering to the v2 kernel;
- returns completion counts rather than only a path string.
- emits one or more indeterminate `(completed=0, total=0)` progress events while reading run/AI/database facts, then determinate events with a positive total whose completed values never decrease.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/run_record_export/test_pinned_handoff.py tests/run_record_export/test_configurable_package.py -k service -v`

Expected: FAIL against the old `RunArchiveService.export` signature and behavior.

- [ ] **Step 3: Switch `RunArchiveService` to v2 orchestration**

Inject the tolerant reader plus an `AiExportReaderPort`. Keep the v1 pure function available but make the service's UI-facing `export()` use the v2 package kernel. Add cancellation checkpoints around bulk reads and database serialization; update `read_copy_database` internals only as needed to check between tables/every 500 copied rows without changing output.

- [ ] **Step 4: Add service/database failure, cancellation, and progress tests**

Add explicit tests proving:

- an exception from requested `read_copy_database` fails the request before publication, preserves a pre-existing target byte-for-byte, and leaves no sibling temporary file;
- cancellation before/after the tolerant run read prevents later AI/package work;
- cancellation before/after the bulk AI read prevents database/package work;
- `read_copy_database(..., cancel_check=...)` checks before/after each table query, every 500 copied rows, and before/after final SQLite serialization while preserving byte-identical logical output;
- cancellation at any database-copy checkpoint prevents package publication and preserves an existing target;
- progress begins with indeterminate read events (`total == 0`) and transitions once to determinate package events (`total > 0`) whose completed values are monotonic through completion.

Run: `python -m pytest tests/run_record_export/test_pinned_handoff.py tests/run_persistence/test_export_reader.py -k "cancel or progress or database_failure or copy_database" -v`

Expected: PASS.

- [ ] **Step 5: Wire the AI dependency in the service container**

Construct `AiService` after the application database is connected but before `RunArchiveService`, then pass it into the archive service. Preserve its single shared instance for historical report and config-package services.

- [ ] **Step 6: Run orchestration and container tests**

Run: `python -m pytest tests/run_record_export/test_pinned_handoff.py tests/run_record_export/test_configurable_package.py tests/test_service_container.py -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 7**

```bash
git add app/service/run_record_export/archive.py app/service/run_persistence/export_reader.py app/services_container.py tests/run_record_export/test_pinned_handoff.py tests/run_persistence/test_export_reader.py tests/run_record_export/test_configurable_package.py tests/test_service_container.py
git commit -m "feat: orchestrate configurable run exports"
```

### Task 8: Export Settings Dialog and Data Management Integration

**Files:**
- Create: `ui/popups/run_record_export_dialog.py`
- Create: `tests/test_run_record_export_dialog.py`
- Modify: `ui/modules/data_management.py`
- Modify: `tests/test_data_management_module.py`

- [ ] **Step 1: Write failing dialog tests**

Verify modal title, selected-count label, exact checkbox labels/defaults, all-false disabling of `确定导出`, cancellation returning `None`, accepted immutable options, and fresh defaults on every dialog construction.

- [ ] **Step 2: Implement the small modal dialog**

Follow `ui/popups/config_package_dialogs.py` patterns. Keep the chooser functions injectable/testable:

```python
def choose_run_export_options(parent, *, selected_count: int) -> RunDataExportOptions | None: ...

def choose_run_export_path(parent, *, output_kind: Literal["xlsx", "zip"], initial_dir: str) -> str | None: ...
```

Normalize only the expected `.xlsx` or `.zip` suffix and provide matching save filters.

- [ ] **Step 3: Run dialog tests**

Run: `python -m pytest tests/test_run_record_export_dialog.py -v`

Expected: PASS.

- [ ] **Step 4: Write failing Data Management integration tests**

Update the archive service stub to accept options and callbacks. Cover:

- no selected rows warns before opening settings;
- settings cancel stops before path selection;
- all four options reach the service exactly;
- Excel-only uses `.xlsx`; all other choices use `.zip`;
- the worker calls both progress and cancel checkpoints;
- the progress dialog starts indeterminate during reads, changes to a determinate range when the service publishes a positive total, and never displays a decreasing determinate value;
- success dialog reports exported primary count and nonzero missing audio/current counts;
- failure and cancellation restore every operation control;
- the old `audio_data_service.export_zip` path remains unreachable.

- [ ] **Step 5: Integrate UI flow and explicit cancellation callback**

Update `_ExportWorker` so its runner receives both `progress_callback` and a non-signaling `cancel_check`. Replace the direct ZIP save dialog with option and target chooser calls, pass options to the service, and update `_finish_export` to consume `RunDataExportResult`.

Do not preflight-reject runs merely because audio is absent; tolerant selection/skips belong to the service. Preserve existing cross-page selected UUID collection and operation gating.

- [ ] **Step 6: Run UI integration tests**

Run: `python -m pytest tests/test_run_record_export_dialog.py tests/test_data_management_module.py -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 8**

```bash
git add ui/popups/run_record_export_dialog.py ui/modules/data_management.py tests/test_run_record_export_dialog.py tests/test_data_management_module.py
git commit -m "feat: add configurable export workflow"
```

### Task 9: Full Regression and Packaging Verification

**Files:**
- Modify only files proven necessary by failures; do not broaden scope.

- [ ] **Step 1: Run the complete export-focused suite**

Run:

```bash
python -m pytest tests/run_record_export tests/run_persistence/test_export_reader.py tests/test_ai_service.py tests/test_ai_execution_table.py tests/test_run_record_export_dialog.py tests/test_data_management_module.py tests/test_service_container.py -v
```

Expected: PASS.

- [ ] **Step 2: Run the complete repository test suite**

Run: `python -m pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Re-run the representative large-selection acceptance test**

Run: `python -m pytest tests/run_record_export/test_configurable_package.py -k large_selection -v`

Expected: PASS, proving write-only workbook creation, per-row cancellation, monotonic progress, 1 MiB package streaming, cancellation without publication, and successful atomic output.

- [ ] **Step 4: Verify dependency and importability**

Run:

```bash
python -c "import openpyxl; from app.service.run_record_export.excel import build_excel_workbook; from ui.popups.run_record_export_dialog import choose_run_export_options; print(openpyxl.__version__)"
```

Expected: prints `3.1.5` and exits zero.

- [ ] **Step 5: Run static repository checks**

Run:

```bash
git diff --check
python -m compileall -q app ui tests
```

Expected: both commands exit zero.

- [ ] **Step 6: Inspect the final diff for scope and generated files**

Run: `git status --short && git diff --stat HEAD~8..HEAD`

Expected: only planned source/tests/dependency files are changed; no `.xlsx`, `.zip`, database, audio, cache, or temporary artifacts are tracked.

- [ ] **Step 7: Commit any verification-only fixes**

If and only if verification required fixes:

```bash
git add <exact-fixed-files>
git commit -m "fix: close configurable export regressions"
```

Re-run every command that previously failed and the full suite before declaring completion.
