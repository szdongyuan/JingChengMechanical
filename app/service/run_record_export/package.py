"""Atomic configurable XLSX/ZIP publication kernel for run exports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

import openpyxl

from app.service.run_record_export.archive import RunArchiveError
from app.service.run_record_export.attachments import (
    serialize_current_csv,
    write_wav_channels,
)
from app.service.run_record_export.contracts import (
    AiExportValue,
    PackagedAudio,
    PackagedAudioChannel,
    PackagedCurrent,
    PackagedRunRecord,
    RunDataExportCounts,
    RunDataExportOptions,
    RunDataExportReadReport,
    RunDataExportResult,
    RunDataExportSkippedRecord,
)
from app.service.run_record_export.excel import build_excel_workbook
from app.service.run_record_export.manifests import (
    build_export_manifest,
    build_record_manifest,
    validate_declared_members,
)
from app.service.run_record_export.naming import (
    audio_member_path,
    current_member_path,
    record_directory,
)


ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], None]
Clock = Callable[[], datetime | str]
_STREAM_CHUNK = 1024 * 1024
_COMPONENT_ORDER = {"primary": 0, "audio": 1, "current": 2}
_EXCEL_HEADERS = (
    "序号", "检测时间", "SN", "产品型号", "最终结果", "判定来源",
    "故障类型", "AI评分", "AI标签", "音频相对路径",
)


class _Cancellation(Exception):
    def __init__(self, original: BaseException) -> None:
        self.original = original


@dataclass(frozen=True)
class _Member:
    name: str
    payload: bytes | Path


class _StageStore:
    def __init__(self, parent: Path) -> None:
        self._path: Path | None = Path(
            tempfile.mkdtemp(prefix=".run-export-stage-", dir=str(parent))
        )
        self._next_index = 0

    def reserve_audio_paths(self, count: int) -> tuple[Path, ...]:
        if self._path is None:
            raise RuntimeError("attachment staging is already closed")
        paths = []
        for _ in range(count):
            self._next_index += 1
            paths.append(
                self._path / f"audio-{self._next_index:08d}.wav"
            )
        return tuple(paths)

    @staticmethod
    def discard(paths: tuple[Path, ...]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def cleanup(self, *, suppress_errors: bool = False) -> None:
        path = self._path
        if path is None:
            return
        try:
            shutil.rmtree(path)
        except OSError:
            if not suppress_errors:
                raise
        else:
            self._path = None


def _check(cancel_check: CancelCheck | None) -> None:
    if cancel_check is None:
        return
    try:
        cancel_check()
    except BaseException as exc:
        raise _Cancellation(exc) from exc


def _progress(
    callback: ProgressCallback | None,
    completed: int,
    total: int,
    message: str,
) -> None:
    if callback is not None:
        callback(completed, total, message)


def _reason(value: object, fallback: str) -> str:
    text = str(value).strip() or fallback
    return text[:500]


def _resolved_target(target_path: str, output_kind: str) -> Path:
    if type(target_path) is not str or not target_path:
        raise ValueError("target path must be a nonempty string")
    target = Path(target_path)
    if not target.suffix:
        target = target.with_suffix(f".{output_kind}")
    return target


def _ordered_records(report: RunDataExportReadReport):
    return tuple(
        sorted(
            report.records,
            key=lambda record: (record.accepted_at, record.run_id),
            reverse=True,
        )
    )


def _reject_target_audio_aliases(target: Path, report: RunDataExportReadReport) -> None:
    target_name = os.path.normcase(str(target.resolve()))
    target_exists = target.exists()
    for record in report.records:
        source_text = record.audio.source_wav_path
        if source_text is None:
            continue
        source = Path(source_text)
        if target_name == os.path.normcase(str(source.resolve())):
            raise RunArchiveError("target aliases a selected source WAV")
        if target_exists and source.exists() and os.path.samefile(target, source):
            raise RunArchiveError("target aliases a selected source WAV")


def _not_selected_audio() -> PackagedAudio:
    return PackagedAudio(False, "not_selected", None, None, None, ())


def _not_selected_current() -> PackagedCurrent:
    return PackagedCurrent(False, "not_selected", None, None)


def _skipped_audio(reason: str) -> PackagedAudio:
    return PackagedAudio(True, "skipped", reason, None, None, ())


def _skipped_current(reason: str) -> PackagedCurrent:
    return PackagedCurrent(True, "skipped", reason, None)


def _prepare_audio(record, directory: str, cancel_check, staging: _StageStore):
    facts = record.audio
    if facts.issue is not None:
        return _skipped_audio(_reason(facts.issue, "audio unavailable")), (), facts.issue
    required = (
        facts.source_wav_path,
        facts.sample_rate_hz,
        facts.channel_count,
        facts.frame_count,
        facts.channel_order,
    )
    if any(value is None for value in required):
        reason = "audio attachment facts are incomplete"
        return _skipped_audio(reason), (), reason
    source_channels = tuple(facts.channel_order)
    paths = tuple(
        audio_member_path(directory, source_channel=channel)
        for channel in source_channels
    )
    if len(set(paths)) != len(paths):
        reason = "audio source-channel names collide"
        return _skipped_audio(reason), (), reason
    staged_paths = staging.reserve_audio_paths(len(paths))
    try:
        _check(cancel_check)
        write_wav_channels(
            facts.source_wav_path,
            output_paths=staged_paths,
            sample_rate_hz=facts.sample_rate_hz,
            channel_count=facts.channel_count,
            frame_count=facts.frame_count,
            cancel_check=(lambda: _check(cancel_check)),
        )
    except _Cancellation:
        staging.discard(staged_paths)
        raise
    except (OSError, ValueError) as exc:
        staging.discard(staged_paths)
        reason = _reason(exc, "audio attachment is invalid")
        return _skipped_audio(reason), (), reason
    channels = tuple(
        PackagedAudioChannel(position, source_channel, path)
        for position, (source_channel, path) in enumerate(
            zip(source_channels, paths, strict=True), start=1
        )
    )
    attachment = PackagedAudio(
        True,
        "exported",
        None,
        facts.sample_rate_hz,
        facts.frame_count,
        channels,
    )
    staged = tuple(
        _Member(path, staged_path)
        for path, staged_path in zip(paths, staged_paths, strict=True)
    )
    return attachment, staged, None


def _prepare_current(record, directory: str):
    facts = record.current
    if facts.issue is not None:
        return _skipped_current(_reason(facts.issue, "current unavailable")), None, facts.issue
    if facts.samples is None:
        reason = "current attachment is missing"
        return _skipped_current(reason), None, reason
    try:
        payload = serialize_current_csv(facts.samples)
    except (TypeError, ValueError) as exc:
        reason = _reason(exc, "current attachment is invalid")
        return _skipped_current(reason), None, reason
    path = current_member_path(directory)
    return PackagedCurrent(True, "exported", None, path), _Member(path, payload), None


def _counts(
    report: RunDataExportReadReport,
    records: tuple[PackagedRunRecord, ...],
) -> RunDataExportCounts:
    primary_skips = sum(skip.component == "primary" for skip in report.skipped)
    return RunDataExportCounts(
        selected_runs=len(report.selected_run_ids),
        exported_runs=len(records),
        skipped_runs=primary_skips,
        audio_runs_emitted=sum(r.audio.status == "exported" for r in records),
        audio_runs_skipped=sum(r.audio.status == "skipped" for r in records),
        audio_files_emitted=sum(len(r.audio.channels) for r in records),
        current_runs_emitted=sum(r.current.status == "exported" for r in records),
        current_runs_skipped=sum(r.current.status == "skipped" for r in records),
    )


def _write_bytes(path: Path, payload: bytes, cancel_check: CancelCheck | None) -> None:
    with path.open("wb") as output:
        for offset in range(0, len(payload), _STREAM_CHUNK):
            output.write(payload[offset : offset + _STREAM_CHUNK])
            _check(cancel_check)


def _write_zip_member(
    archive: zipfile.ZipFile,
    member: _Member,
    cancel_check: CancelCheck | None,
) -> None:
    path, payload = member.name, member.payload
    if isinstance(payload, Path):
        with payload.open("rb") as source, archive.open(path, "w") as output:
            while True:
                _check(cancel_check)
                chunk = source.read(_STREAM_CHUNK)
                _check(cancel_check)
                if not chunk:
                    break
                output.write(chunk)
                _check(cancel_check)
    elif len(payload) > _STREAM_CHUNK:
        with archive.open(path, "w") as output:
            for offset in range(0, len(payload), _STREAM_CHUNK):
                output.write(payload[offset : offset + _STREAM_CHUNK])
                _check(cancel_check)
    else:
        archive.writestr(path, payload)
    _check(cancel_check)


def _validate_xlsx(path: Path) -> None:
    with path.open("rb") as stream:
        workbook = openpyxl.load_workbook(
            stream, read_only=True, data_only=False
        )
        try:
            if workbook.sheetnames != ["检测记录"]:
                raise ValueError(
                    "Excel workbook has an unexpected worksheet layout"
                )
            rows = workbook["检测记录"].iter_rows(values_only=True)
            if tuple(next(rows, ())) != _EXCEL_HEADERS:
                raise ValueError("Excel workbook has unexpected columns")
        finally:
            workbook.close()


def _validate_zip(
    path: Path,
    *,
    expected_members: set[str],
    root_manifest: bytes,
    record_manifests: dict[str, bytes],
    cancel_check: CancelCheck | None,
) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != expected_members:
            raise ValueError("archive member set does not match expected members")
        manifest_payloads: dict[str, bytes] = {}
        manifest_names = {"export_manifest.json", *record_manifests}
        for name in names:
            chunks = [] if name in manifest_names else None
            try:
                with archive.open(name, "r") as source:
                    while True:
                        chunk = source.read(_STREAM_CHUNK)
                        if not chunk:
                            break
                        if chunks is not None:
                            chunks.append(chunk)
                        _check(cancel_check)
            except zipfile.BadZipFile:
                raise ValueError(f"archive member failed CRC: {name}") from None
            _check(cancel_check)
            if chunks is not None:
                manifest_payloads[name] = b"".join(chunks)
        if manifest_payloads["export_manifest.json"] != root_manifest:
            raise ValueError("archive root manifest changed during publication")
        for member, payload in record_manifests.items():
            if manifest_payloads[member] != payload:
                raise ValueError("archive record manifest changed during publication")
    validate_declared_members(
        root_manifest=root_manifest,
        record_manifests=record_manifests,
        members=expected_members,
    )


def _write_run_data_export(
    report: RunDataExportReadReport,
    target_path: str,
    *,
    ai_values: Mapping[str, AiExportValue],
    options: RunDataExportOptions,
    database_bytes: bytes | None = None,
    copy_database: bytes | None = None,
    clock: Clock = lambda: datetime.now(timezone.utc),
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    staging: _StageStore,
) -> RunDataExportResult:
    """Prepare selected artifacts and atomically publish one export target."""
    if type(report) is not RunDataExportReadReport:
        raise TypeError("report must be a RunDataExportReadReport")
    if type(options) is not RunDataExportOptions:
        raise TypeError("options must be RunDataExportOptions")
    if not isinstance(ai_values, Mapping):
        raise TypeError("AI values must be a mapping")
    if copy_database is not None:
        if database_bytes is not None:
            raise ValueError("database bytes were provided twice")
        database_bytes = copy_database
    if options.include_database:
        if type(database_bytes) is not bytes or not database_bytes:
            raise RunArchiveError("selected database copy is unavailable")
    elif database_bytes is not None:
        raise ValueError("database bytes require the database option")
    if not report.records:
        raise RunArchiveError("no valid primary runs remain")

    target = _resolved_target(target_path, options.output_kind)
    _reject_target_audio_aliases(target, report)
    total = len(report.records) + 4
    completed = 0
    _progress(progress_callback, completed, total, "正在准备导出记录...")
    try:
        _check(cancel_check)
    except _Cancellation as exc:
        raise exc.original
    selected_skip_components = {"primary"}
    if options.include_audio:
        selected_skip_components.add("audio")
    if options.include_current_csv:
        selected_skip_components.add("current")
    skips = [
        RunDataExportSkippedRecord(
            skip.run_id,
            skip.component,
            _reason(skip.reason, f"{skip.component} unavailable"),
            skip.selection_index,
        )
        for skip in report.skipped
        if skip.component in selected_skip_components
    ]
    existing_skip_reasons = {
        (skip.selection_index, skip.component): skip.reason for skip in skips
    }
    packaged_records = []
    attachment_members: list[_Member] = []
    try:
        for sequence, record in enumerate(_ordered_records(report), start=1):
            _check(cancel_check)
            directory = record_directory(sequence, record.sn)
            if options.include_audio:
                audio_key = (record.selection_index, "audio")
                if audio_key in existing_skip_reasons:
                    audio = _skipped_audio(existing_skip_reasons[audio_key])
                    members, audio_reason = (), None
                else:
                    audio, members, audio_reason = _prepare_audio(
                        record, directory, cancel_check, staging
                    )
                attachment_members.extend(members)
                if audio_reason is not None:
                    reason = _reason(audio_reason, "audio unavailable")
                    skips.append(RunDataExportSkippedRecord(
                        record.run_id, "audio", reason, record.selection_index,
                    ))
                    existing_skip_reasons[audio_key] = reason
            else:
                audio = _not_selected_audio()
            if options.include_current_csv:
                current_key = (record.selection_index, "current")
                if current_key in existing_skip_reasons:
                    current = _skipped_current(existing_skip_reasons[current_key])
                    member, current_reason = None, None
                else:
                    current, member, current_reason = _prepare_current(record, directory)
                if member is not None:
                    attachment_members.append(member)
                if current_reason is not None:
                    reason = _reason(current_reason, "current unavailable")
                    skips.append(RunDataExportSkippedRecord(
                        record.run_id, "current", reason, record.selection_index,
                    ))
                    existing_skip_reasons[current_key] = reason
            else:
                current = _not_selected_current()
            ai = ai_values.get(record.run_id, AiExportValue(None, None))
            if type(ai) is not AiExportValue:
                raise TypeError("AI mapping values must be AiExportValue")
            packaged_records.append(
                PackagedRunRecord(sequence, record, ai, directory, audio, current)
            )
            completed += 1
            _progress(
                progress_callback,
                completed,
                total,
                f"正在准备记录 {sequence}/{len(report.records)}...",
            )
            _check(cancel_check)
        records = tuple(packaged_records)
        counts = _counts(report, records)
        result = RunDataExportResult(str(target), options.output_kind, counts)
        if (
            not options.include_excel
            and not options.include_database
            and counts.audio_files_emitted + counts.current_runs_emitted == 0
        ):
            raise RunArchiveError("no useful selected artifacts could be exported")

        _check(cancel_check)
        excel_payload = (
            build_excel_workbook(
                records,
                cancel_check=(lambda: _check(cancel_check)),
            )
            if options.include_excel
            else None
        )
        _check(cancel_check)
        completed += 1
        _progress(progress_callback, completed, total, "正在构建导出文件...")
        _check(cancel_check)
    except _Cancellation as exc:
        raise exc.original
    except RunArchiveError:
        raise
    except Exception as exc:
        raise RunArchiveError(f"run export preparation failed: {exc}") from None

    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".run-export-", suffix=".tmp", dir=str(target.parent)
        )
        temporary = Path(temporary_name)
        os.close(descriptor)

        if options.output_kind == "xlsx":
            assert excel_payload is not None
            _write_bytes(temporary, excel_payload, cancel_check)
            completed += 1
            _progress(progress_callback, completed, total, "正在写入 Excel...")
            _check(cancel_check)
            _validate_xlsx(temporary)
        else:
            exported_at = clock()
            ordered_skips = tuple(
                sorted(
                    skips,
                    key=lambda skip: (
                        skip.selection_index,
                        _COMPONENT_ORDER[skip.component],
                    ),
                )
            )
            record_manifests = {}
            for record in records:
                _check(cancel_check)
                manifest_path = f"{record.directory}/manifest.json"
                record_manifests[manifest_path] = build_record_manifest(record)
                _check(cancel_check)
            _check(cancel_check)
            root_manifest = build_export_manifest(
                options=options,
                exported_at=exported_at,
                selected_run_ids=report.selected_run_ids,
                records=records,
                skips=ordered_skips,
                excel_path="检测记录.xlsx" if options.include_excel else None,
                database_path="copy_data.sqlite3"
                if options.include_database
                else None,
            )
            _check(cancel_check)
            members: list[_Member] = [_Member("export_manifest.json", root_manifest)]
            if excel_payload is not None:
                members.append(_Member("检测记录.xlsx", excel_payload))
            if database_bytes is not None:
                members.append(_Member("copy_data.sqlite3", database_bytes))
            for record in records:
                manifest_path = f"{record.directory}/manifest.json"
                members.append(_Member(manifest_path, record_manifests[manifest_path]))
                record_prefix = f"{record.directory}/"
                members.extend(
                    member
                    for member in attachment_members
                    if member.name.startswith(record_prefix)
                )
            expected_members = {member.name for member in members}
            if len(expected_members) != len(members):
                raise ValueError("duplicate package member path")
            _check(cancel_check)
            validate_declared_members(
                root_manifest=root_manifest,
                record_manifests=record_manifests,
                members=expected_members,
            )
            _check(cancel_check)
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for member in members:
                    _write_zip_member(archive, member, cancel_check)
            _check(cancel_check)
            completed += 1
            _progress(progress_callback, completed, total, "正在写入压缩包...")
            _check(cancel_check)
            _validate_zip(
                temporary,
                expected_members=expected_members,
                root_manifest=root_manifest,
                record_manifests=record_manifests,
                cancel_check=cancel_check,
            )
            _check(cancel_check)

        completed += 1
        _progress(progress_callback, completed, total, "正在校验导出文件...")
        _check(cancel_check)
        staging.cleanup()
        _progress(progress_callback, total, total, "正在提交导出文件...")
        _check(cancel_check)
        os.replace(temporary, target)
        temporary = None
    except _Cancellation as exc:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise exc.original
    except BaseException as exc:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        if isinstance(exc, RunArchiveError):
            raise
        if isinstance(exc, Exception):
            raise RunArchiveError(f"run export publication failed: {exc}") from None
        raise

    return result


def write_run_data_export(
    report: RunDataExportReadReport,
    target_path: str,
    *,
    ai_values: Mapping[str, AiExportValue],
    options: RunDataExportOptions,
    database_bytes: bytes | None = None,
    copy_database: bytes | None = None,
    clock: Clock = lambda: datetime.now(timezone.utc),
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> RunDataExportResult:
    """Prepare selected artifacts and atomically publish one export target."""
    if type(report) is not RunDataExportReadReport:
        raise TypeError("report must be a RunDataExportReadReport")
    if type(options) is not RunDataExportOptions:
        raise TypeError("options must be RunDataExportOptions")
    target = _resolved_target(target_path, options.output_kind)
    try:
        staging = _StageStore(target.parent)
    except Exception as exc:
        raise RunArchiveError(f"run export publication failed: {exc}") from None
    try:
        return _write_run_data_export(
            report,
            target_path,
            ai_values=ai_values,
            options=options,
            database_bytes=database_bytes,
            copy_database=copy_database,
            clock=clock,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            staging=staging,
        )
    finally:
        staging.cleanup(suppress_errors=True)


# Descriptive alias used by orchestration callers.
write_configurable_package = write_run_data_export
