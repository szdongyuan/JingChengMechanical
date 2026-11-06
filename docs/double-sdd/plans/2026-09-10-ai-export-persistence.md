# AI Export Persistence Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore persisted production AI scores and raw labels in Excel and ZIP exports, including recoverable existing records.

**Architecture:** Save a compact AI snapshot under the existing result review object; read it via an attempt-backed export adapter using the existing artifact store. Older records recover score from the saved result and raw label from the same attempt response file.

**Tech Stack:** Python, SQLite/Peewee, pytest, openpyxl (existing exporter).

---

### Task 1: Repair the production-to-export AI data path

Ownership: `app/service/analysis/production_executor.py`, new `app/service/run_persistence/ai_export_reader.py`, `app/services_container.py`, directly related tests only. Preserve all unrelated work. Worktree: `.worktrees/ai-export-persistence-fix`; base f32661392eb833bd27e84246aec80e8a8c47ffeb. Interpreter: main workspace `.venv/Scripts/python.exe` (absolute path), cwd worktree. No dependencies need installing.

- [ ] Read `analysis/staged_result_payload.py`, production executor Decision/Enrichment, `run_persistence/attempt_repository.py`, `analysis_result_artifact.py`, historical reader, export contracts and container wiring. Record baseline targeted tests.
- [ ] Add a regression test with successful persisted analysis, absent legacy AI rows, exported Excel H/I populated (including score 0). Run and confirm failure before implementation. Add direct production test proving Decision and Enrichment retain an unmodified raw custom label and PASS label.
- [ ] Save `review.ai_result = {"status": ai_review.get("status"), "score": ai_review.get("score"), "ai_label": <raw label>}`; preserve raw string before normalization used for pass/fail. Keep the fixed top-level result contract unchanged. Ensure Enrichment copies the snapshot.
- [ ] Implement `RunAiExportReader` backed by `AttemptRepository(runtime, result_artifact_store=store).latest_successful_for_run(run_id)`. Read new successful snapshots with finite numeric scores (not booleans), nonblank raw strings. For records lacking the snapshot read `result.ai_score`, and same-attempt response under the configured result store root's sibling `ai_runs`; leave unavailable values empty. Catch specific artifact/JSON/file problems but do not hide DB failures. Avoid fallback to stale attempts or fabricated labels.
- [ ] Wire `RunArchiveService` to this adapter instead of `self.ai_service`; preserve the existing public reader protocol and legacy AiService functions used elsewhere. Update wiring tests.
- [ ] Add tests for no result, disabled/failed AI, missing values, 0, raw whitespace, old response file missing/invalid, compressed artifacts, recent successful reanalysis and later failed attempt selection. Test ZIP manifest and Excel agree; tests may build fixture DBs in temp directories only.
- [ ] Verify missing/corrupt result attachments yield empty unavailable AI fields without blocking export, while database query errors propagate. A new snapshot's missing values must not be filled from conflicting legacy result/response values.
- [ ] Run focused commands with absolute interpreter: `python -m pytest tests/run_record_export tests/run_persistence tests/analysis_scheduler/test_staged_production_executor.py tests/test_service_container.py -q`. Add relevant production executor test modules found during inspection. Compare pre-existing failures rather than altering unrelated code.
- [ ] Verify `git diff --check`; commit explicit owned paths with `tmp/sdd: repair persisted AI export`. Report tests and remaining practical limitations.

### Completion

- [ ] Paired specification/quality review for the full single-task diff; resolve verified, actionable findings.
- [ ] Independently verify focused regression tests and a real-data read-only export; compare saved AI score/raw label with Excel H/I.
- [ ] Apply reviewed changes back to main without including existing unrelated modifications; verify integrated tests and export behavior. No push or application restart required.
