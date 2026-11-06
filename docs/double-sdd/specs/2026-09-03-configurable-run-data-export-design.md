# Configurable Run Data Export Design

## Summary

Replace the current fixed run-record ZIP action with a small configurable export flow. The operator selects one or more existing data-management records, chooses Excel summary, per-channel audio, the existing selected-record SQLite copy, and/or current-sample CSV files, and receives either a standalone `.xlsx` file or one atomic ZIP package. The first version deliberately avoids a general field designer, export profiles, chart rendering, and full analysis-array projection.

## Goals

- Let operators choose which high-value artifacts to export without building a general-purpose export platform.
- Add a readable one-sheet Excel summary containing operator-facing run facts, the current effective AI score and raw AI label, and package-relative audio locations.
- Keep the existing selected-record database-copy contents and schema unchanged.
- Preserve the existing per-run manifest concept while introducing a root export manifest for package-level integrity and diagnostics.
- Keep useful primary records exportable when optional audio or current-sample attachments are missing or invalid.
- Preserve the current data-management selection, progress, cancellation, and atomic-publication behavior.

## Non-Goals

- No arbitrary Excel field chooser.
- No saved export profiles or remembered last-used options.
- No extra Excel worksheets, formulas, charts, pivots, or statistics.
- No export of full FFT, STFT, octave, loudness, vibration, or other chart arrays as workbook sheets.
- No chart-image, log, video, AI artifact, configuration-package, or general attachment export.
- No whole-application database backup.
- No import or migration of legacy export packages.
- No requirement that the new ZIP layout or manifest schemas remain compatible with the existing `jingcheng-run-export/v1` archive.

## Chosen Approach

Extend the existing `RunArchiveService` workflow instead of creating four independent exporters or replacing it with a plugin-style export platform.

- Reuse the current selection handoff, worker thread, progress dialog, cancellation mechanism, WAV validation and channel splitting, selected-record database-copy kernel, temporary-file publication, and ZIP CRC validation.
- Introduce explicit export options and a projection that separates primary run facts from optional attachment availability.
- Add focused Excel and manifest builders.
- Read AI score and label from the existing application database without adding AI tables to the selected-record database copy.

This approach minimizes duplicate I/O, validation, progress, and failure-handling code while keeping the first release bounded.

## User Flow

1. The operator selects one or more records in the existing Data Management module and clicks `导出`.
2. A modal `导出设置` dialog shows `已选择 N 条记录` and four independent checkboxes:
   - `Excel 汇总`, checked by default.
   - `音频文件`, checked by default.
   - `数据库副本`, checked by default.
   - `电流数据 CSV`, unchecked by default.
3. At least one checkbox must be selected before `确定导出` is enabled.
4. If Excel is the only selection, the save dialog produces a standalone `.xlsx` target.
5. Every other valid option combination produces one `.zip` target.
6. The existing modal progress and cancel experience remains in use during export.
7. Completion reports the number of primary records written plus counts of missing or skipped audio and current-sample attachments.
8. The dialog does not persist the previous selection; every opening uses the documented defaults.

## Export Options Contract

Represent the settings as one immutable, validated value with exact booleans:

- `include_excel`
- `include_audio`
- `include_database`
- `include_current_csv`

Reject a value with all four fields false. Options control only independently emitted artifacts:

- `include_current_csv` controls emitted `current.csv` files, not the `current_trace_json` field already present in the unchanged database copy.
- `include_audio` controls WAV validation, channel splitting, audio files, and Excel audio paths.
- Per-run and root manifests are structural ZIP metadata rather than selectable user artifacts and are always present for ZIP output.

## Export Data Projection

Create an immutable export-item projection for each selected run. It must keep these concerns separate:

- Primary run facts: accepted time, SN, model, durable state and judgment, judgment source, fault type, and internal run UUID.
- AI display facts: current effective AI execution `score` and raw `ai_label` only.
- Audio attachment facts: source location, sampling facts, source-channel order, validation status, emitted relative paths, and any skip reason.
- Current attachment facts: samples, validation status, emitted relative path, and any skip reason.

The primary projection must not require a committed or readable source WAV. This intentionally removes the current coupling in which a missing audio fact makes the complete record ineligible for export.

### Selection and primary validity

- Validate the request before database access as one nonempty tuple of unique canonical lowercase UUIDv4 strings. An invalid selection fails the request, so every later skip entry always has a valid run UUID from the selection.
- A primary run is valid when exactly one row exists for the selected UUID; `accepted_at` and `terminal_at` are valid durable UTC timestamps; `state` is one of `SUCCEEDED`, `NO_RESULT`, `FAILED`, `CANCELED`, or `INTERRUPTED`; `terminal_code` is a nonempty string; and judgment facts satisfy the current persistence invariants.
- Judgment invariants are: `NONE` requires a null judgment; `AUTOMATIC` requires `SUCCEEDED` plus `PASS` or `FAIL`; `MANUAL` requires `PASS` or `FAIL` plus a positive manual revision; and a non-null fault type is valid only for a manual `FAIL`.
- `SN` and model are tolerant display projections from the frozen configuration snapshot. A missing, malformed, or non-text source value becomes an empty string and does not invalidate an otherwise valid primary run.
- Missing or malformed audio and current facts are attachment failures, not primary failures.
- The unchanged database copy is built from the original validated selection and therefore contains every matching source row even when a primary row is omitted from Excel/manifests as malformed. It is not narrowed to the successfully projected primary set.

### AI selection rule

- Query `ai_execution_table` for the same run UUID with `is_current_effective = true` and `module_status = 'success'`. If inconsistent data contains more than one match, choose deterministically by `started_time DESC, request_id DESC`.
- Export its original numeric `score` and unmodified string `ai_label`.
- Do not export summary, confidence, module status, toolchain identity, or translated labels to Excel.
- Accept a score only when it is an `int` or `float` other than `bool` and converts to a finite float. No artificial score range is imposed. Invalid or non-finite scores are treated as absent.
- Accept a label only when it is a string containing at least one non-whitespace character. Preserve the accepted original string exactly; do not translate, trim, or normalize it.
- If there is no current effective successful result, or either requested field is absent or invalid, the corresponding Excel cell is blank and the JSON value is `null`.
- Manual changes to the final run judgment never rewrite or reinterpret the AI score or label.

## Excel Workbook

### Workbook shape

- One worksheet named `检测记录`.
- One row per successfully projected primary run.
- Rows sorted by accepted time descending, with a deterministic run-identity tie-breaker.
- No additional worksheet.

### Columns

The columns appear in this exact order:

1. `序号`
2. `检测时间`
3. `SN`
4. `产品型号`
5. `最终结果`
6. `判定来源`
7. `故障类型`
8. `AI评分`
9. `AI标签`
10. `音频相对路径`

The internal run UUID is intentionally omitted from the operator-facing workbook.

### Value rules

- `序号` starts at 1 and matches the six-digit record directory prefix in ZIP output.
- `检测时间` is converted from the durable UTC value to Asia/Shanghai time and displayed as `yyyy-MM-dd HH:mm:ss`.
- `SN`, product model, fault type, and AI label are written as literal text. Numeric-looking SN values retain leading zeroes and no string is interpreted as an Excel formula.
- `最终结果` uses this exact precedence: any durable `PASS` judgment displays `OK`; any durable `FAIL` displays `NG`; otherwise `NO_RESULT` displays `无结果`; and `FAILED`, `CANCELED`, or `INTERRUPTED` displays `错误`. `ACTIVE` is not exportable.
- `判定来源` maps `AUTOMATIC` to `自动`, `MANUAL` to `人工`, and `NONE` to a blank cell. This remains independent of the final-result display.
- `AI评分` is a numeric cell when a current effective score exists and is blank otherwise.
- `AI标签` preserves the raw label and is blank when unavailable.
- `音频相对路径` contains only WAV members that were actually emitted. Multiple channel paths are separated with in-cell line breaks. It is blank when audio was not selected or no audio member for the run was emitted.

### Presentation

- Freeze the header row and enable an autofilter over the complete data range.
- Use readable bounded column widths and row heights; wrap audio-path cells.
- Apply light green fill to `OK`, light red fill to `NG`, and light yellow fill to `无结果` and `错误` cells.
- Generate new `.xlsx` files with a dedicated OOXML writer configured to treat user-originated strings as literal text.

## ZIP Layout

The ZIP layout is intentionally not backward compatible with the current UUID-rooted archive:

```text
运行记录导出.zip
├─ export_manifest.json
├─ 检测记录.xlsx                 # when selected
├─ copy_data.sqlite3             # when selected
└─ records/
   ├─ 000001_SN123/
   │  ├─ manifest.json
   │  ├─ audio/                  # when selected and available
   │  │  ├─ CH1.wav
   │  │  └─ CH2.wav
   │  └─ current.csv             # when selected and available
   └─ 000002_SN456/
      ├─ manifest.json
      └─ audio/
         └─ CH1.wav
```

### Record directory naming

- Prefix every directory with the six-digit Excel/export sequence number.
- Append `_` and a filesystem-safe SN.
- Use `无SN` when the normalized SN is empty.
- Normalize the SN segment in this exact order: preserve Unicode text; trim surrounding whitespace; replace every control character and each of `< > : " / \\ | ? *` with `_`; collapse consecutive `_` characters; trim trailing spaces and periods; use `无SN` if empty; case-insensitively detect Windows reserved basenames `CON`, `PRN`, `AUX`, `NUL`, `COM1` through `COM9`, and `LPT1` through `LPT9` even when followed by an extension and prefix such values with `_`; truncate to the first 48 Unicode code points; trim trailing spaces and periods again; and fall back to `无SN` if truncation leaves an empty value.
- The numeric prefix guarantees uniqueness even when SN values repeat or normalize to the same text.

### Audio naming

- Preserve the existing behavior of emitting validated mono PCM16 WAV files split from the persisted multichannel source.
- Name each emitted file after its user-visible one-based source channel, such as `CH1.wav`.
- The per-run manifest remains authoritative for exported position, source-channel identity, and relative path.
- If source-channel naming could collide after normalization, fail that run's audio projection before publishing any of its WAV members and report the audio skip.

## Manifest Schemas

Introduce the exact versioned schemas below. Legacy schema compatibility is not required. All paths use `/`, are relative to the package root, contain no `.` or `..` segment, and name an actual member when non-null. All JSON text is UTF-8 without a BOM. Unless a field is explicitly nullable below, it is required and non-null.

### Root `export_manifest.json`

Exact shape:

```json
{
  "schema": "jingcheng-run-export-package/v2",
  "exported_at": "2026-09-03T01:02:03.000000Z",
  "options": {
    "excel": true,
    "audio": true,
    "database": true,
    "current_csv": false
  },
  "counts": {
    "selected_runs": 2,
    "exported_runs": 2,
    "skipped_runs": 0,
    "audio_runs_emitted": 2,
    "audio_runs_skipped": 0,
    "audio_files_emitted": 3,
    "current_runs_emitted": 0,
    "current_runs_skipped": 0
  },
  "artifacts": {
    "excel": "检测记录.xlsx",
    "database": "copy_data.sqlite3"
  },
  "records": [
    {
      "sequence": 1,
      "run_id": "00000000-0000-4000-8000-000000000001",
      "directory": "records/000001_SN123",
      "manifest": "records/000001_SN123/manifest.json"
    }
  ],
  "skips": []
}
```

- `schema` is the exact string shown.
- `exported_at` is a durable UTC timestamp in `YYYY-MM-DDTHH:MM:SS.ffffffZ` form.
- The four `options` members are required booleans.
- Every `counts` member is a required nonnegative integer. `selected_runs` counts validated requested UUIDs; `exported_runs` and `skipped_runs` count primary runs and sum to `selected_runs`; audio run counts count exported primary runs, not channels; `audio_files_emitted` counts WAV members; current counts count exported primary runs. When a component is unselected, both of its emitted and skipped counts are zero. A primary-skipped run is not also counted as an audio/current skip.
- `artifacts.excel` and `artifacts.database` are package-root-relative strings when emitted and JSON `null` when unselected.
- `records` is required and ordered by `sequence`; each entry has a positive integer sequence, canonical UUIDv4 string, record-directory path, and manifest path. It includes exported primary runs only.
- `skips` is required and ordered first by input selection order and then component order `primary`, `audio`, `current`. Each entry has exact fields `run_id` (canonical UUIDv4 string), `component` (one of those three strings), and `reason` (nonempty bounded diagnostic string).

### Per-run `manifest.json`

Exact shape:

```json
{
  "schema": "jingcheng-run-export-record/v2",
  "sequence": 1,
  "run": {
    "run_id": "00000000-0000-4000-8000-000000000001",
    "accepted_at": "2026-09-03T01:02:03.000000Z",
    "sn": "SN123",
    "model": "M1"
  },
  "outcome": {
    "result": "OK",
    "judgment": "PASS",
    "judgment_source": "AUTOMATIC",
    "fault_type": null
  },
  "ai": {
    "score": 0.95,
    "label": "PASS"
  },
  "audio": {
    "selected": true,
    "status": "exported",
    "reason": null,
    "sample_rate_hz": 48000,
    "frame_count": 96000,
    "channels": [
      {
        "position": 1,
        "source_channel": 0,
        "path": "records/000001_SN123/audio/CH1.wav"
      }
    ]
  },
  "current": {
    "selected": false,
    "status": "not_selected",
    "reason": null,
    "path": null
  }
}
```

- `schema` is the exact string shown and `sequence` is a positive integer matching Excel and the root record entry.
- `run.run_id` is canonical UUIDv4 text; `accepted_at` is a durable UTC timestamp; `sn` and `model` are strings and may be empty.
- `outcome.result` is one of `OK`, `NG`, `NO_RESULT`, or `ERROR` using the same precedence as Excel. `judgment` is `PASS`, `FAIL`, or null; `judgment_source` is `AUTOMATIC`, `MANUAL`, or `NONE`; `fault_type` is a string or null.
- `ai.score` is a finite JSON number or null. `ai.label` is the accepted unmodified nonblank string or null.
- `audio.selected` is a boolean. `status` is `not_selected`, `exported`, or `skipped`. `not_selected` requires `selected = false`, null reason, null sampling facts, and an empty channel array. `exported` requires `selected = true`, null reason, positive integer sampling facts, and a nonempty ordered channel array. `skipped` requires `selected = true`, a nonempty reason, null sampling facts, and an empty channel array.
- Each channel has positive integer `position`, nonnegative integer `source_channel`, and a unique package-root-relative path. Channels are ordered by position. The filename is `CH{source_channel + 1}.wav`.
- `current` uses the same `selected` and three status values. `not_selected` requires false/null/null; `exported` requires true/null/non-null path; `skipped` requires true/nonempty reason/null path.

Every manifest path must describe an actual ZIP member, and every emitted per-run attachment must be declared by its per-run manifest.

## Database Copy

Keep the current selected-record database-copy behavior unchanged:

- The file remains `copy_data.sqlite3`.
- It contains the selected rows from `run`, `audio`, and `analysis_attempt` using the existing source schemas.
- It remains directly openable as SQLite.
- It retains the current absolute source-audio path values; portable audio discovery is provided by Excel and manifests instead.
- It does not add `ai_execution_table`. AI score and label are independently read from the application database for Excel and manifests.
- Database-copy generation remains an all-or-nothing selected component: if requested and it fails, the complete export request fails.

## Current CSV

- Emit one `current.csv` under the corresponding record directory when `include_current_csv` is true and valid samples exist.
- Preserve the existing header and rows: `time_sec,current_a` followed by strictly increasing finite time/value pairs.
- Validate the complete sample tuple before writing. Any malformed pair, boolean, non-finite value, negative time, or non-increasing time rejects the complete per-run current attachment; individual pairs are never filtered.
- Serialize as UTF-8 without BOM, comma delimiter, LF newlines, exact header `time_sec,current_a`, Python round-trip finite-float text for each value, and one trailing LF.
- Missing or invalid current samples skip only that attachment and are reflected in both manifest levels and the completion report.
- No current-data path column is added to the first-version Excel workbook.

## Data Flow and Components

```text
selected run IDs
  -> validated export options
  -> primary + AI + optional-attachment projection
  -> deterministic ordering and safe path allocation
  -> selected artifact builders
  -> manifest consistency validation
  -> XLSX/ZIP structural validation
  -> atomic target replacement
```

Recommended responsibility boundaries:

- Export settings dialog: UI-only option collection and validation.
- Export projection reader: cross-database read orchestration and attachment availability without source-file mutation.
- Excel builder: pure workbook projection from ordered export items.
- Existing database-copy reader: unchanged selected-row SQLite generation.
- Per-run attachment preparation: audio/current validation and byte generation.
- Manifest builders: pure, versioned JSON projections.
- Package writer/service: progress, cancellation boundaries, temporary output, structural validation, and atomic publication.

## Progress and Cancellation

- Continue using the existing worker thread and modal progress dialog.
- Report indeterminate progress while reading cross-database facts, then determinate progress for per-record attachment preparation and package writing when a total is known.
- Check cancellation between records, before expensive audio splitting, at least every 4,096 frames while splitting audio, after every Excel row, between each database table and every 500 copied database rows, between artifact builders, after every ZIP member, every 1 MiB while streaming a large member, and immediately before target replacement. SQLite query/serialize calls that cannot be interrupted are bracketed by checks before and after the call.
- Cancellation closes and deletes temporary files and never removes or replaces a previously valid target.
- UI controls return to their pre-export state after success, failure, or cancellation.

## Validation and Error Handling

### Recoverable per-record/component conditions

- Missing, unreadable, corrupt, or format-incompatible audio skips that run's audio only.
- Missing or invalid current samples skip that run's current CSV only.
- Missing AI fields produce blank Excel cells and JSON `null` values.
- A malformed primary run fact skips the primary run and all of its attachments, with a concise reported reason.

### Request-failing conditions

- Invalid or all-false export options.
- No valid primary runs remain.
- After preparation, neither Excel nor database is selected and the combined number of emitted audio and current files is zero. This generic rule covers audio-only, current-only, and audio-plus-current selections.
- Requested Excel generation fails or the workbook fails structural validation.
- Requested database-copy generation fails.
- Root or per-run manifest generation or consistency validation fails.
- The target parent is unavailable, temporary output cannot be created, ZIP CRC validation fails, or atomic replacement fails.

When a request fails or is canceled, do not publish a partial target. When optional components are skipped but at least one requested user artifact remains useful, publish the valid result and report the skip counts.

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces: [source application and run databases, existing selected-record SQLite copy schema and contents, Data Management filtering and cross-page selection semantics, existing progress and cancellation UX, original source WAV and result artifact files]
- Allowed breakage: [ZIP directory layout, member names, per-run manifest schema and paths, introduction of a root manifest, audio member naming, default export contents now include Excel]
- Migration strategy: none; existing export ZIP files remain untouched and no importer or compatibility layer is added

## Verification

- Dialog tests cover labels, defaults, selected count, all-false rejection, acceptance, and no preference persistence.
- Output-selection tests cover all 15 non-empty option combinations and prove standalone `.xlsx` versus ZIP routing.
- Excel tests validate exact worksheet and column order, descending deterministic ordering, timezone conversion, text-preserved SN values, literal string handling, result/source translations, AI current-effective selection, AI blanks, raw labels, line-broken actual audio paths, formatting, filter, and frozen header.
- Path tests cover duplicate, empty, long, control-character, Windows-invalid, and reserved-name SN values plus deterministic uniqueness.
- Audio tests preserve PCM16 validation, channel splitting, rate/frame content, source-channel naming, missing/corrupt-file recovery, and manifest/path consistency.
- Current CSV tests cover opt-in behavior, exact CSV content, missing and invalid samples, and database-copy independence.
- Manifest tests cover exact schemas, counts, options, sequence-to-UUID mapping, `null` AI values, skip details, declared-member closure, and absence of paths to missing members.
- Database-copy regression tests prove unchanged table set, schemas, selected rows, absolute audio paths, and plain-SQLite readability.
- Failure tests cover malformed primary facts, zero-useful-artifact requests, Excel/database/manifest failures, target write failures, cancellation at multiple phases, pre-existing targets, and cleanup of temporary output.
- UI integration tests prove existing selection order/scope, operation gating, progress, cancellation, and completion summaries remain correct.
- A representative large-selection test verifies bounded-memory or streaming workbook generation, responsive cancellation points, monotonic progress, and successful atomic publication.
