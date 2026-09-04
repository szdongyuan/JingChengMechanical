"""Immutable contracts for legacy and configurable run-record exports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
from typing import Literal, Protocol
from uuid import UUID


ExportOutputKind = Literal["xlsx", "zip"]
ExportComponent = Literal["primary", "audio", "current"]
AttachmentStatus = Literal["not_selected", "exported", "skipped"]


def _validate_uuid4(value: object, field_name: str = "run_id") -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be lowercase canonical UUIDv4 text")
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        raise ValueError(
            f"{field_name} must be lowercase canonical UUIDv4 text"
        ) from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(
            f"{field_name} must be lowercase canonical UUIDv4 text"
        )
    return value


def _exact_string(
    value: object,
    field_name: str,
    *,
    nonempty: bool = False,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if nonempty and not value.strip():
        raise ValueError(f"{field_name} must be nonempty")
    return value


def _nonnegative_integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be nonnegative")
    return value


def _positive_integer(value: object, field_name: str) -> int:
    result = _nonnegative_integer(value, field_name)
    if result == 0:
        raise ValueError(f"{field_name} must be positive")
    return result


def _validate_member_path(value: object, field_name: str = "path") -> str:
    path = _exact_string(value, field_name, nonempty=True)
    if "\\" in path or path.startswith("/"):
        raise ValueError(f"{field_name} must be a package-relative POSIX path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must be a package-relative POSIX path")
    return path


@dataclass(frozen=True, slots=True)
class RunDataExportOptions:
    include_excel: bool
    include_audio: bool
    include_database: bool
    include_current_csv: bool

    def __post_init__(self) -> None:
        values = (
            self.include_excel,
            self.include_audio,
            self.include_database,
            self.include_current_csv,
        )
        if any(type(value) is not bool for value in values):
            raise TypeError("export options must be exact booleans")
        if not any(values):
            raise ValueError("at least one export option is required")

    @classmethod
    def defaults(cls) -> RunDataExportOptions:
        return cls(
            include_excel=True,
            include_audio=True,
            include_database=True,
            include_current_csv=False,
        )

    @property
    def output_kind(self) -> ExportOutputKind:
        if (
            self.include_excel
            and not self.include_audio
            and not self.include_database
            and not self.include_current_csv
        ):
            return "xlsx"
        return "zip"


@dataclass(frozen=True, slots=True)
class AudioExportFacts:
    source_wav_path: str | None
    sample_rate_hz: int | None
    channel_count: int | None
    frame_count: int | None
    channel_order: tuple[int, ...] | None
    issue: str | None

    def __post_init__(self) -> None:
        if self.source_wav_path is not None:
            _exact_string(
                self.source_wav_path,
                "audio source WAV path",
                nonempty=True,
            )
        for value, field_name in (
            (self.sample_rate_hz, "audio sample rate"),
            (self.channel_count, "audio channel count"),
            (self.frame_count, "audio frame count"),
        ):
            if value is not None:
                _positive_integer(value, field_name)
        if self.channel_order is not None:
            if type(self.channel_order) is not tuple or not self.channel_order:
                raise ValueError("audio channel order must be a nonempty tuple")
            for source_channel in self.channel_order:
                _nonnegative_integer(source_channel, "audio source channel")
            if len(set(self.channel_order)) != len(self.channel_order):
                raise ValueError("audio source channels must be unique")
        if self.issue is not None:
            _exact_string(self.issue, "audio issue", nonempty=True)


@dataclass(frozen=True, slots=True)
class CurrentExportFacts:
    samples: tuple[tuple[float, float], ...] | None
    issue: str | None

    def __post_init__(self) -> None:
        if self.issue is not None:
            _exact_string(self.issue, "current issue", nonempty=True)
        if self.samples is None:
            return
        if type(self.samples) is not tuple or not self.samples:
            raise ValueError("current samples must be a nonempty tuple")
        previous_time: float | None = None
        for sample in self.samples:
            if type(sample) is not tuple or len(sample) != 2:
                raise ValueError("each current sample must contain two values")
            time_sec, current_a = sample
            if (
                isinstance(time_sec, bool)
                or isinstance(current_a, bool)
                or not isinstance(time_sec, (int, float))
                or not isinstance(current_a, (int, float))
                or not math.isfinite(float(time_sec))
                or not math.isfinite(float(current_a))
                or time_sec < 0
                or (previous_time is not None and time_sec <= previous_time)
            ):
                raise ValueError(
                    "current samples must be finite and strictly increasing"
                )
            previous_time = float(time_sec)


@dataclass(frozen=True, slots=True)
class AiExportValue:
    score: float | None
    label: str | None

    def __post_init__(self) -> None:
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(
                self.score, (int, float)
            ):
                raise TypeError("AI score must be a finite number or None")
            score = float(self.score)
            if not math.isfinite(score):
                raise ValueError("AI score must be finite")
            object.__setattr__(self, "score", score)
        if self.label is not None:
            _exact_string(self.label, "AI label")
            if not self.label.strip():
                raise ValueError("AI label must be nonblank")


@dataclass(frozen=True, slots=True)
class RunDataExportRecord:
    run_id: str
    selection_index: int
    accepted_at: str
    terminal_at: str
    sn: str
    model: str
    state: str
    terminal_code: str
    judgment: str | None
    judgment_source: str
    fault_type: str | None
    audio: AudioExportFacts
    current: CurrentExportFacts

    def __post_init__(self) -> None:
        _validate_uuid4(self.run_id)
        _nonnegative_integer(self.selection_index, "selection index")
        _exact_string(self.accepted_at, "accepted timestamp", nonempty=True)
        _exact_string(self.terminal_at, "terminal timestamp", nonempty=True)
        _exact_string(self.sn, "SN")
        _exact_string(self.model, "model")
        _exact_string(self.state, "state")
        if self.state not in {
            "SUCCEEDED",
            "NO_RESULT",
            "FAILED",
            "CANCELED",
            "INTERRUPTED",
        }:
            raise ValueError("state is not exportable")
        _exact_string(self.terminal_code, "terminal code", nonempty=True)
        if self.judgment is not None:
            _exact_string(self.judgment, "judgment")
        if self.judgment not in {None, "PASS", "FAIL"}:
            raise ValueError("judgment is invalid")
        _exact_string(self.judgment_source, "judgment source")
        if self.judgment_source not in {"AUTOMATIC", "MANUAL", "NONE"}:
            raise ValueError("judgment source is invalid")
        if self.fault_type is not None:
            _exact_string(self.fault_type, "fault type", nonempty=True)
        if type(self.audio) is not AudioExportFacts:
            raise TypeError("audio must be AudioExportFacts")
        if type(self.current) is not CurrentExportFacts:
            raise TypeError("current must be CurrentExportFacts")


@dataclass(frozen=True, slots=True)
class RunDataExportSkippedRecord:
    run_id: str
    component: ExportComponent
    reason: str
    selection_index: int

    def __post_init__(self) -> None:
        _validate_uuid4(self.run_id)
        if self.component not in {"primary", "audio", "current"}:
            raise ValueError("skip component is invalid")
        _exact_string(self.reason, "skip reason", nonempty=True)
        _nonnegative_integer(self.selection_index, "selection index")


@dataclass(frozen=True, slots=True)
class RunDataExportReadReport:
    selected_run_ids: tuple[str, ...]
    records: tuple[RunDataExportRecord, ...]
    skipped: tuple[RunDataExportSkippedRecord, ...]

    def __post_init__(self) -> None:
        if type(self.selected_run_ids) is not tuple or not self.selected_run_ids:
            raise ValueError("selected run IDs must be a nonempty tuple")
        for run_id in self.selected_run_ids:
            _validate_uuid4(run_id, "selected run ID")
        if len(set(self.selected_run_ids)) != len(self.selected_run_ids):
            raise ValueError("selected run IDs must be unique")
        if type(self.records) is not tuple or any(
            type(record) is not RunDataExportRecord for record in self.records
        ):
            raise TypeError("records must be a tuple of RunDataExportRecord")
        if type(self.skipped) is not tuple or any(
            type(skip) is not RunDataExportSkippedRecord for skip in self.skipped
        ):
            raise TypeError(
                "skipped must be a tuple of RunDataExportSkippedRecord"
            )

        record_indexes: set[int] = set()
        primary_skip_indexes: set[int] = set()
        attachment_skip_indexes: set[int] = set()
        skip_keys: set[tuple[int, ExportComponent]] = set()
        for record in self.records:
            self._validate_selection_identity(
                record.run_id,
                record.selection_index,
            )
            if record.selection_index in record_indexes:
                raise ValueError("record selection indexes must be unique")
            record_indexes.add(record.selection_index)
        for skip in self.skipped:
            self._validate_selection_identity(skip.run_id, skip.selection_index)
            key = (skip.selection_index, skip.component)
            if key in skip_keys:
                raise ValueError(
                    "skip selection index/component pairs must be unique"
                )
            skip_keys.add(key)
            if skip.component == "primary":
                primary_skip_indexes.add(skip.selection_index)
            else:
                attachment_skip_indexes.add(skip.selection_index)

        expected_indexes = set(range(len(self.selected_run_ids)))
        if record_indexes & primary_skip_indexes:
            raise ValueError("selection cannot be both a record and primary skip")
        if primary_skip_indexes & attachment_skip_indexes:
            raise ValueError("a primary skip cannot also have attachment skips")
        if record_indexes | primary_skip_indexes != expected_indexes:
            raise ValueError(
                "every selection must have a record or primary skip"
            )

    def _validate_selection_identity(self, run_id: str, index: int) -> None:
        if index >= len(self.selected_run_ids):
            raise ValueError("selection index is outside selected run IDs")
        if self.selected_run_ids[index] != run_id:
            raise ValueError("run identity does not agree with selection index")


@dataclass(frozen=True, slots=True)
class PackagedAudioChannel:
    position: int
    source_channel: int
    path: str

    def __post_init__(self) -> None:
        _positive_integer(self.position, "audio channel position")
        _nonnegative_integer(self.source_channel, "audio source channel")
        _validate_member_path(self.path)


@dataclass(frozen=True, slots=True)
class PackagedAudio:
    selected: bool
    status: AttachmentStatus
    reason: str | None
    sample_rate_hz: int | None
    frame_count: int | None
    channels: tuple[PackagedAudioChannel, ...]

    def __post_init__(self) -> None:
        if type(self.selected) is not bool:
            raise TypeError("audio selected must be an exact boolean")
        if self.status not in {"not_selected", "exported", "skipped"}:
            raise ValueError("audio status is invalid")
        if type(self.channels) is not tuple or any(
            type(channel) is not PackagedAudioChannel
            for channel in self.channels
        ):
            raise TypeError("audio channels must be an exact tuple")
        if self.reason is not None:
            _exact_string(self.reason, "audio reason", nonempty=True)

        if self.status == "not_selected":
            if (
                self.selected
                or self.reason is not None
                or self.sample_rate_hz is not None
                or self.frame_count is not None
                or self.channels
            ):
                raise ValueError("audio not_selected state is inconsistent")
            return
        if self.status == "skipped":
            if (
                not self.selected
                or self.reason is None
                or self.sample_rate_hz is not None
                or self.frame_count is not None
                or self.channels
            ):
                raise ValueError("audio skipped state is inconsistent")
            return
        if (
            not self.selected
            or self.reason is not None
            or self.sample_rate_hz is None
            or self.frame_count is None
            or not self.channels
        ):
            raise ValueError("audio exported state is inconsistent")
        _positive_integer(self.sample_rate_hz, "audio sample rate")
        _positive_integer(self.frame_count, "audio frame count")
        expected_positions = tuple(range(1, len(self.channels) + 1))
        if tuple(channel.position for channel in self.channels) != expected_positions:
            raise ValueError("audio channels must be ordered by position")
        source_channels = tuple(
            channel.source_channel for channel in self.channels
        )
        if len(set(source_channels)) != len(source_channels):
            raise ValueError("audio source channel identities must be unique")
        paths = tuple(channel.path for channel in self.channels)
        if len(set(paths)) != len(paths):
            raise ValueError("audio channel paths must be unique")


@dataclass(frozen=True, slots=True)
class PackagedCurrent:
    selected: bool
    status: AttachmentStatus
    reason: str | None
    path: str | None

    def __post_init__(self) -> None:
        if type(self.selected) is not bool:
            raise TypeError("current selected must be an exact boolean")
        if self.status not in {"not_selected", "exported", "skipped"}:
            raise ValueError("current status is invalid")
        if self.reason is not None:
            _exact_string(self.reason, "current reason", nonempty=True)
        if self.path is not None:
            _validate_member_path(self.path)

        if self.status == "not_selected":
            if self.selected or self.reason is not None or self.path is not None:
                raise ValueError("current not_selected state is inconsistent")
        elif self.status == "skipped":
            if not self.selected or self.reason is None or self.path is not None:
                raise ValueError("current skipped state is inconsistent")
        elif not self.selected or self.reason is not None or self.path is None:
            raise ValueError("current exported state is inconsistent")


@dataclass(frozen=True, slots=True)
class PackagedRunRecord:
    sequence: int
    record: RunDataExportRecord
    ai: AiExportValue
    directory: str
    audio: PackagedAudio
    current: PackagedCurrent

    def __post_init__(self) -> None:
        _positive_integer(self.sequence, "package sequence")
        if type(self.record) is not RunDataExportRecord:
            raise TypeError("record must be RunDataExportRecord")
        if type(self.ai) is not AiExportValue:
            raise TypeError("ai must be AiExportValue")
        directory = _validate_member_path(self.directory, "record directory")
        from app.service.run_record_export.naming import (
            audio_member_path,
            current_member_path,
            record_directory,
        )

        if directory != record_directory(self.sequence, self.record.sn):
            raise ValueError("record directory must match sequence and SN")
        if type(self.audio) is not PackagedAudio:
            raise TypeError("audio must be PackagedAudio")
        if type(self.current) is not PackagedCurrent:
            raise TypeError("current must be PackagedCurrent")
        if self.audio.status == "exported":
            for channel in self.audio.channels:
                expected_path = audio_member_path(
                    directory,
                    source_channel=channel.source_channel,
                )
                if channel.path != expected_path:
                    raise ValueError(
                        "audio channel path must match record and source channel"
                    )
        if self.current.status == "exported":
            if self.current.path != current_member_path(directory):
                raise ValueError("current path must match record directory")

    @property
    def run(self) -> RunDataExportRecord:
        return self.record


@dataclass(frozen=True, slots=True)
class RunDataExportCounts:
    selected_runs: int
    exported_runs: int
    skipped_runs: int
    audio_runs_emitted: int
    audio_runs_skipped: int
    audio_files_emitted: int
    current_runs_emitted: int
    current_runs_skipped: int

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            _nonnegative_integer(getattr(self, field_name), field_name)
        if self.selected_runs == 0:
            raise ValueError("selected_runs must be positive")
        if self.exported_runs + self.skipped_runs != self.selected_runs:
            raise ValueError(
                "exported and skipped run counts must sum to selected runs"
            )
        if self.audio_runs_emitted + self.audio_runs_skipped > self.exported_runs:
            raise ValueError("audio run counts exceed exported runs")
        if (
            self.audio_runs_emitted == 0
            and self.audio_files_emitted != 0
        ) or (
            self.audio_runs_emitted > 0
            and self.audio_files_emitted < self.audio_runs_emitted
        ):
            raise ValueError("audio emitted runs/files are inconsistent")
        if self.current_runs_emitted + self.current_runs_skipped > self.exported_runs:
            raise ValueError("current run counts exceed exported runs")


@dataclass(frozen=True, slots=True)
class RunDataExportResult:
    target_path: str
    output_kind: ExportOutputKind
    counts: RunDataExportCounts

    def __post_init__(self) -> None:
        _exact_string(self.target_path, "target path", nonempty=True)
        if self.output_kind not in {"xlsx", "zip"}:
            raise ValueError("output kind is invalid")
        if type(self.counts) is not RunDataExportCounts:
            raise TypeError("counts must be RunDataExportCounts")

    @property
    def path(self) -> str:
        return self.target_path


@dataclass(frozen=True)
class RunExportRecord:
    run_id: str
    accepted_at: str
    ingress_kind: str
    trigger_source: str
    config_id: int
    hardware_id: int | None
    sn: str
    model: str
    state: str
    judgment: str | None
    judgment_source: str
    fault_type: str | None
    manual_judgment_revision: int | None
    terminal_code: str | None
    terminal_at: str
    source_wav_path: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    channel_order: tuple[int, ...]
    current_samples: tuple[tuple[float, float], ...] | None


class RunExportReaderPort(Protocol):
    def read_many(
        self,
        run_ids: tuple[str, ...],
    ) -> tuple[RunExportRecord, ...]: ...


class RunDataExportReaderPort(Protocol):
    def read_configurable_report(
        self,
        run_ids: tuple[str, ...],
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> RunDataExportReadReport: ...


class RunDatabaseCopyReaderPort(Protocol):
    def read_copy_database(
        self,
        run_ids: tuple[str, ...],
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> bytes: ...


class AiExportReaderPort(Protocol):
    def get_current_effective_export_values(
        self,
        run_ids: tuple[str, ...],
    ) -> dict[str, AiExportValue]: ...
