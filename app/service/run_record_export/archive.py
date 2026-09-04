"""Pure one-shot ZIP projection of immutable durable run records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import zipfile

from app.service.run_record_export.attachments import (
    serialize_current_csv,
    split_pcm16_wav,
)

from app.service.run_record_export.contracts import (
    AiExportReaderPort,
    RunDataExportOptions,
    RunDataExportResult,
    RunExportReaderPort,
    RunExportRecord,
)


class RunArchiveError(RuntimeError):
    """One run archive request failed without publishing a partial target."""


ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], None]


def write_configurable_package(*args, **kwargs):
    """Load the v2 kernel lazily so its legacy error import stays acyclic."""

    from app.service.run_record_export.package import write_run_data_export

    return write_run_data_export(*args, **kwargs)


@dataclass(frozen=True)
class RunArchiveSkippedRecord:
    run_id: str
    reason: str


@dataclass(frozen=True)
class RunArchiveSelectionInspection:
    exportable_run_ids: tuple[str, ...]
    skipped: tuple[RunArchiveSkippedRecord, ...]


@dataclass(frozen=True)
class _PreparedRecord:
    record: RunExportRecord
    channel_wavs: tuple[bytes, ...]
    current_csv: bytes | None


_INDEX_SCHEMA = """
PRAGMA application_id = 1245926744;
PRAGMA user_version = 1;

CREATE TABLE export_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE export_run (
    run_id TEXT PRIMARY KEY,
    export_order INTEGER NOT NULL UNIQUE CHECK (export_order > 0),
    accepted_at TEXT NOT NULL,
    ingress_kind TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    config_id INTEGER NOT NULL,
    hardware_id INTEGER,
    sn TEXT NOT NULL,
    model TEXT NOT NULL,
    result TEXT NOT NULL,
    state TEXT NOT NULL,
    judgment TEXT,
    judgment_source TEXT NOT NULL,
    fault_type TEXT,
    manual_judgment_revision INTEGER,
    terminal_code TEXT,
    terminal_at TEXT NOT NULL,
    sample_rate_hz INTEGER NOT NULL CHECK (sample_rate_hz > 0),
    channel_count INTEGER NOT NULL CHECK (channel_count > 0),
    frame_count INTEGER NOT NULL CHECK (frame_count > 0),
    manifest_path TEXT NOT NULL UNIQUE,
    current_csv_path TEXT
);

CREATE TABLE export_channel (
    run_id TEXT NOT NULL REFERENCES export_run(run_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    source_channel INTEGER NOT NULL CHECK (source_channel >= 0),
    wav_path TEXT NOT NULL UNIQUE,
    PRIMARY KEY (run_id, position)
) WITHOUT ROWID;

CREATE INDEX export_run_accepted_at_idx ON export_run(accepted_at, run_id);
CREATE INDEX export_run_sn_idx ON export_run(sn);
CREATE INDEX export_run_model_idx ON export_run(model);
CREATE INDEX export_run_result_idx ON export_run(result);
"""


def resolve_export_target(chosen_path: str) -> str:
    """Append ``.zip`` exactly when the chosen filename has no extension."""

    if Path(chosen_path).suffix:
        return chosen_path
    return f"{chosen_path}.zip"


def _reject_target_source_aliases(
    target: Path, records: tuple[RunExportRecord, ...]
) -> None:
    target_normalized = os.path.normcase(str(target.resolve()))
    target_exists = target.exists()
    for record in records:
        source = Path(record.source_wav_path)
        source_normalized = os.path.normcase(str(source.resolve()))
        if target_normalized == source_normalized:
            raise RunArchiveError("target aliases selected source WAV")
        if target_exists and source.exists() and os.path.samefile(target, source):
            raise RunArchiveError("target aliases selected source WAV")


def _current_csv(
    samples: tuple[tuple[float, float], ...] | None,
) -> bytes | None:
    if samples is None:
        return None
    return serialize_current_csv(samples)


def _prepare_record(record: RunExportRecord) -> _PreparedRecord:
    if type(record.sn) is not str or type(record.model) is not str:
        raise ValueError("SN and model must be strings")
    if record.sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    if record.channel_count <= 0:
        raise ValueError("channel count must be positive")
    if record.frame_count <= 0:
        raise ValueError("frame count must be positive")
    if len(record.channel_order) != record.channel_count:
        raise ValueError("channel order length does not match channel count")

    channel_wavs = split_pcm16_wav(
        record.source_wav_path,
        sample_rate_hz=record.sample_rate_hz,
        channel_count=record.channel_count,
        frame_count=record.frame_count,
    )
    return _PreparedRecord(
        record=record,
        channel_wavs=channel_wavs,
        current_csv=_current_csv(record.current_samples),
    )


def _result(record: RunExportRecord) -> str:
    if record.state == "SUCCEEDED":
        if record.judgment == "PASS":
            return "OK"
        if record.judgment == "FAIL":
            return "NG"
    if record.state == "NO_RESULT":
        return "NO_RESULT"
    return "ERROR"


def _manifest(prepared: _PreparedRecord) -> bytes:
    record = prepared.record
    manifest = {
        "schema": "jingcheng-run-export/v1",
        "run": {
            "run_id": record.run_id,
            "accepted_at": record.accepted_at,
            "ingress_kind": record.ingress_kind,
            "trigger_source": record.trigger_source,
            "config_id": record.config_id,
            "hardware_id": record.hardware_id,
            "sn": record.sn,
            "model": record.model,
        },
        "outcome": {
            "result": _result(record),
            "state": record.state,
            "judgment": record.judgment,
            "judgment_source": record.judgment_source,
            "fault_type": record.fault_type,
            "manual_judgment_revision": record.manual_judgment_revision,
            "terminal_code": record.terminal_code,
            "terminal_at": record.terminal_at,
        },
        "audio": {
            "sample_rate_hz": record.sample_rate_hz,
            "frame_count": record.frame_count,
        },
        "channels": [
            {
                "position": position,
                "source_channel": source_channel,
                "file": f"channel-{position:03d}.wav",
            }
            for position, source_channel in enumerate(
                record.channel_order, start=1
            )
        ],
        "current": {
            "present": prepared.current_csv is not None,
            "file": "current.csv" if prepared.current_csv is not None else None,
        },
    }
    return json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _index_run_row(
    prepared: _PreparedRecord,
    export_order: int,
) -> tuple[object, ...]:
    record = prepared.record
    prefix = f"{record.run_id}/"
    return (
        record.run_id,
        export_order,
        record.accepted_at,
        record.ingress_kind,
        record.trigger_source,
        record.config_id,
        record.hardware_id,
        record.sn,
        record.model,
        _result(record),
        record.state,
        record.judgment,
        record.judgment_source,
        record.fault_type,
        record.manual_judgment_revision,
        record.terminal_code,
        record.terminal_at,
        record.sample_rate_hz,
        record.channel_count,
        record.frame_count,
        f"{prefix}manifest.json",
        f"{prefix}current.csv" if prepared.current_csv is not None else None,
    )


def _index_channel_rows(
    prepared: _PreparedRecord,
) -> tuple[tuple[object, ...], ...]:
    record = prepared.record
    return tuple(
        (
            record.run_id,
            position,
            source_channel,
            f"{record.run_id}/channel-{position:03d}.wav",
        )
        for position, source_channel in enumerate(record.channel_order, start=1)
    )


def _validate_index_connection(
    database: sqlite3.Connection,
    prepared_records: tuple[_PreparedRecord, ...],
) -> None:
    integrity = database.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        raise ValueError("export index database failed integrity check")
    if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("export index database has broken foreign keys")

    run_rows = tuple(
        database.execute(
            """
            SELECT run_id, export_order, accepted_at, ingress_kind,
                   trigger_source, config_id, hardware_id, sn, model,
                   result, state, judgment, judgment_source, fault_type,
                   manual_judgment_revision, terminal_code, terminal_at,
                   sample_rate_hz, channel_count, frame_count,
                   manifest_path, current_csv_path
            FROM export_run
            ORDER BY export_order
            """
        )
    )
    expected_run_rows = tuple(
        _index_run_row(prepared, export_order)
        for export_order, prepared in enumerate(prepared_records, start=1)
    )
    if run_rows != expected_run_rows:
        raise ValueError("export index run rows do not match the archive")

    channel_rows = tuple(
        database.execute(
            """
            SELECT run_id, position, source_channel, wav_path
            FROM export_channel
            ORDER BY run_id, position
            """
        )
    )
    expected_channel_rows = tuple(
        sorted(
            (
                row
                for prepared in prepared_records
                for row in _index_channel_rows(prepared)
            ),
            key=lambda row: (row[0], row[1]),
        )
    )
    if channel_rows != expected_channel_rows:
        raise ValueError("export index channel rows do not match the archive")


def _index_database(
    prepared_records: tuple[_PreparedRecord, ...],
) -> bytes:
    database = sqlite3.connect(":memory:")
    try:
        database.execute("PRAGMA foreign_keys = ON")
        database.executescript(_INDEX_SCHEMA)
        with database:
            database.executemany(
                "INSERT INTO export_metadata(key, value) VALUES (?, ?)",
                (
                    ("archive_schema", "jingcheng-run-export/v1"),
                    ("index_schema", "jingcheng-run-export-index/v1"),
                ),
            )
            for export_order, prepared in enumerate(
                prepared_records, start=1
            ):
                database.execute(
                    """
                    INSERT INTO export_run (
                        run_id, export_order, accepted_at, ingress_kind,
                        trigger_source, config_id, hardware_id, sn, model,
                        result, state, judgment, judgment_source, fault_type,
                        manual_judgment_revision, terminal_code, terminal_at,
                        sample_rate_hz, channel_count, frame_count,
                        manifest_path, current_csv_path
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    _index_run_row(prepared, export_order),
                )
                database.executemany(
                    """
                    INSERT INTO export_channel (
                        run_id, position, source_channel, wav_path
                    ) VALUES (?, ?, ?, ?)
                    """,
                    _index_channel_rows(prepared),
                )
        _validate_index_connection(database, prepared_records)
        payload = database.serialize()
    finally:
        database.close()
    if not payload:
        raise ValueError("export index database is empty")
    return payload


def _validate_index_database(
    payload: bytes,
    prepared_records: tuple[_PreparedRecord, ...],
) -> None:
    database = sqlite3.connect(":memory:")
    try:
        database.deserialize(payload)
        database.execute("PRAGMA foreign_keys = ON")
        if database.execute("PRAGMA application_id").fetchone() != (
            1245926744,
        ):
            raise ValueError("export index database application ID is invalid")
        if database.execute("PRAGMA user_version").fetchone() != (1,):
            raise ValueError("export index database version is invalid")
        _validate_index_connection(database, prepared_records)
    finally:
        database.close()


def _emit_progress(
    progress_callback: ProgressCallback | None,
    completed: int,
    total: int,
    message: str,
) -> None:
    if progress_callback is not None:
        progress_callback(completed, total, message)


def _write_record(archive: zipfile.ZipFile, prepared: _PreparedRecord) -> None:
    record = prepared.record
    prefix = f"{record.run_id}/"
    archive.writestr(f"{prefix}manifest.json", _manifest(prepared))
    for position, payload in enumerate(prepared.channel_wavs, start=1):
        archive.writestr(f"{prefix}channel-{position:03d}.wav", payload)
    if prepared.current_csv is not None:
        archive.writestr(f"{prefix}current.csv", prepared.current_csv)


def write_run_archive(
    records: tuple[RunExportRecord, ...],
    target_path: str,
    *,
    copy_database: bytes | None = None,
    progress_callback: ProgressCallback | None = None,
) -> str:
    """Validate, assemble, test, and atomically publish one run archive."""

    if not records:
        raise RunArchiveError("at least one run export record is required")
    if len({record.run_id for record in records}) != len(records):
        raise RunArchiveError("run export record IDs must be unique")
    if copy_database is not None and (
        type(copy_database) is not bytes or not copy_database
    ):
        raise RunArchiveError("copy database must be nonempty bytes")

    resolved_target = resolve_export_target(target_path)
    target = Path(resolved_target)
    total_steps = len(records) * 2 + 2 + (1 if copy_database is not None else 0)
    completed_steps = 0
    _emit_progress(
        progress_callback,
        completed_steps,
        total_steps,
        f"正在准备导出 {len(records)} 条记录...",
    )
    try:
        _reject_target_source_aliases(target, records)
        prepared = []
        for index, record in enumerate(records, start=1):
            prepared.append(_prepare_record(record))
            completed_steps += 1
            _emit_progress(
                progress_callback,
                completed_steps,
                total_steps,
                f"正在准备音频 {index}/{len(records)}...",
            )
        prepared_records = tuple(prepared)
        index_database = _index_database(prepared_records)
        _validate_index_database(index_database, prepared_records)
        completed_steps += 1
        _emit_progress(
            progress_callback,
            completed_steps,
            total_steps,
            "正在校验导出数据...",
        )
    except RunArchiveError:
        raise
    except Exception as exc:
        raise RunArchiveError(f"run archive preflight failed: {exc}") from None

    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".run-archive-",
            suffix=".tmp",
            dir=str(target.parent),
        )
        temporary = Path(temporary_name)
        os.close(descriptor)

        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            if copy_database is not None:
                archive.writestr("copy_data.sqlite3", copy_database)
                completed_steps += 1
                _emit_progress(
                    progress_callback,
                    completed_steps,
                    total_steps,
                    "正在写入数据库副本...",
                )
            for index, prepared in enumerate(prepared_records, start=1):
                _write_record(archive, prepared)
                completed_steps += 1
                _emit_progress(
                    progress_callback,
                    completed_steps,
                    total_steps,
                    f"正在写入压缩包 {index}/{len(prepared_records)}...",
                )

        with zipfile.ZipFile(temporary, "r") as archive:
            bad_member = archive.testzip()
        if bad_member is not None:
            raise RunArchiveError(f"archive member failed CRC: {bad_member}")
        completed_steps += 1
        _emit_progress(
            progress_callback,
            completed_steps,
            total_steps,
            "正在校验导出文件...",
        )

        os.replace(temporary, target)
        _emit_progress(
            progress_callback,
            total_steps,
            total_steps,
            "导出完成",
        )
    except Exception as exc:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        if isinstance(exc, RunArchiveError):
            raise
        raise RunArchiveError(f"run archive export failed: {exc}") from None

    return resolved_target


class RunArchiveService:
    """Read one durable selection and publish it through the pure kernel."""

    def __init__(
        self,
        reader: RunExportReaderPort,
        ai_reader: AiExportReaderPort | None = None,
        *,
        enrichment_wait_timeout_seconds: float = 2.0,
        enrichment_poll_seconds: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        for value, name in (
            (enrichment_wait_timeout_seconds, "enrichment_wait_timeout_seconds"),
            (enrichment_poll_seconds, "enrichment_poll_seconds"),
        ):
            if (
                type(value) is not float
                or not math.isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"{name} must be a finite positive float")
        if not callable(monotonic) or not callable(wait):
            raise TypeError("monotonic and wait must be callable")
        self._reader = reader
        self._ai_reader = ai_reader
        self._enrichment_wait_timeout_seconds = enrichment_wait_timeout_seconds
        self._enrichment_poll_seconds = enrichment_poll_seconds
        self._monotonic = monotonic
        self._wait = wait

    def _await_enrichment(self, run_ids: tuple[str, ...]) -> None:
        pending_reader = getattr(
            self._reader,
            "pending_enrichment_run_ids",
            None,
        )
        if not callable(pending_reader):
            return
        try:
            deadline = (
                float(self._monotonic())
                + self._enrichment_wait_timeout_seconds
            )
            while True:
                pending = tuple(pending_reader(run_ids))
                if not pending:
                    return
                remaining = deadline - float(self._monotonic())
                if remaining <= 0.0:
                    raise RunArchiveError(
                        "analysis result artifact unavailable before export timeout"
                    )
                self._wait(min(self._enrichment_poll_seconds, remaining))
        except RunArchiveError:
            raise
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise RunArchiveError(
                f"analysis result artifact unavailable: {detail}"
            ) from None

    def inspect_selection(
        self,
        run_ids: tuple[str, ...],
    ) -> RunArchiveSelectionInspection:
        read_many_report = getattr(self._reader, "read_many_report", None)
        if callable(read_many_report):
            report = read_many_report(run_ids)
            return RunArchiveSelectionInspection(
                exportable_run_ids=tuple(
                    record.run_id for record in report.records
                ),
                skipped=tuple(
                    RunArchiveSkippedRecord(
                        run_id=skipped.run_id,
                        reason=skipped.reason,
                    )
                    for skipped in report.skipped
                ),
            )

        records = self._reader.read_many(run_ids)
        return RunArchiveSelectionInspection(
            exportable_run_ids=tuple(record.run_id for record in records),
            skipped=(),
        )

    def export(
        self,
        run_ids: tuple[str, ...],
        target_path: str,
        *,
        options: RunDataExportOptions,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> RunDataExportResult:
        if type(options) is not RunDataExportOptions:
            raise TypeError("options must be RunDataExportOptions")
        if cancel_check is not None and not callable(cancel_check):
            raise TypeError("cancel_check must be callable")
        if self._ai_reader is None:
            raise RuntimeError("AI export reader is unavailable")

        def check_cancel() -> None:
            if cancel_check is not None:
                cancel_check()

        check_cancel()
        _emit_progress(
            progress_callback,
            0,
            0,
            f"正在读取 {len(run_ids)} 条导出数据...",
        )
        check_cancel()
        report = self._reader.read_configurable_report(
            run_ids,
            cancel_check=cancel_check,
        )
        check_cancel()

        ai_values = {}
        if options.include_excel or options.output_kind == "zip":
            _emit_progress(
                progress_callback,
                0,
                0,
                "正在读取 AI 导出数据...",
            )
            check_cancel()
            ai_values = self._ai_reader.get_current_effective_export_values(
                report.selected_run_ids
            )
            check_cancel()

        database_bytes = None
        if options.include_database:
            _emit_progress(
                progress_callback,
                0,
                0,
                "正在生成数据库副本...",
            )
            check_cancel()
            database_bytes = self._reader.read_copy_database(
                report.selected_run_ids,
                cancel_check=cancel_check,
            )
            check_cancel()

        return write_configurable_package(
            report,
            target_path,
            ai_values=ai_values,
            options=options,
            database_bytes=database_bytes,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
