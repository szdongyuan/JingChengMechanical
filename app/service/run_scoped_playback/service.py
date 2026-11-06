"""Durable resolution and serialized first start for run-scoped playback."""

from __future__ import annotations

from collections.abc import Callable
import math
import sys
import threading
import time

import numpy

from app.service.analysis.scheduler import AudioStorageKind, PersistedAudioReference
from app.service.run_persistence import (
    AudioRecord,
    RunRecord,
    decode_analysis_envelope,
    validate_utc_timestamp,
    validate_uuid4,
)

from .contracts import (
    DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_REPLACEMENT_TIMEOUT_SECONDS,
    BackendStartReport,
    BackendStartStatus,
    BackendJoinReport,
    BackendJoinStatus,
    PlaybackCode,
    PlaybackContractError,
    PlaybackLifecycleCode,
    PlaybackLifecycleReport,
    PlaybackResult,
    RunPlaybackRequest,
    _PLAYBACK_LIFECYCLE_MAPPING,
    _PLAYBACK_RESULT_MAPPING,
    _validate_timeout_seconds,
)
from .ports import (
    AudioRepositoryReader,
    PlaybackAudioLoader,
    PlaybackBackend,
    RunRepositoryReader,
)


_TERMINAL_RUN_STATES = frozenset(
    {"SUCCEEDED", "NO_RESULT", "FAILED", "CANCELED", "INTERRUPTED"}
)
_INGRESS_STORAGE_KINDS = {
    "CAPTURED": AudioStorageKind.CAPTURED_FINAL_WAV,
    "IMPORTED": AudioStorageKind.IMPORTED_FINAL_AUDIO,
}


class _NeverCancelledToken:
    __slots__ = ()

    @property
    def is_cancelled(self) -> bool:
        return False

    @property
    def reason(self) -> None:
        return None

    def wait(self, timeout_seconds: float | None = None) -> bool:
        return False


class _WindowUnavailable(Exception):
    pass


def _result(
    code: PlaybackCode,
    run_id: str,
    replaced_run_id: str | None = None,
) -> PlaybackResult:
    status, summary = _PLAYBACK_RESULT_MAPPING[code]
    return PlaybackResult(status, run_id, code, summary, replaced_run_id)


def _lifecycle_result(
    code: PlaybackLifecycleCode,
) -> PlaybackLifecycleReport:
    status, summary = _PLAYBACK_LIFECYCLE_MAPPING[code]
    return PlaybackLifecycleReport(status, code, summary)


def _require_callable_member(owner: object, name: str, owner_name: str) -> None:
    try:
        member = getattr(owner, name)
    except Exception:
        raise TypeError(f"{owner_name}.{name} must be callable") from None
    if not callable(member):
        raise TypeError(f"{owner_name}.{name} must be callable")


def _require_positive_timeout(value: object, name: str) -> float:
    if type(value) is not float or not math.isfinite(value) or value <= 0.0:
        raise PlaybackContractError(f"{name} must be an exact finite positive float")
    return value


class RunScopedPlaybackService:
    def __init__(
        self,
        *,
        run_repository: RunRepositoryReader,
        audio_repository: AudioRepositoryReader,
        audio_loader: PlaybackAudioLoader,
        backend: PlaybackBackend,
        monotonic: Callable[[], float] = time.monotonic,
        replacement_timeout_seconds: float = DEFAULT_REPLACEMENT_TIMEOUT_SECONDS,
        backend_startup_timeout_seconds: float = (
            DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS
        ),
    ) -> None:
        _require_callable_member(run_repository, "get", "run_repository")
        _require_callable_member(
            audio_repository, "get_for_run", "audio_repository"
        )
        _require_callable_member(audio_loader, "open", "audio_loader")
        for name in ("start", "request_stop", "join"):
            _require_callable_member(backend, name, "backend")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")

        self._run_repository = run_repository
        self._audio_repository = audio_repository
        self._audio_loader = audio_loader
        self._backend = backend
        self._monotonic = monotonic
        self._replacement_timeout_seconds = _require_positive_timeout(
            replacement_timeout_seconds, "replacement_timeout_seconds"
        )
        self._backend_startup_timeout_seconds = _require_positive_timeout(
            backend_startup_timeout_seconds, "backend_startup_timeout_seconds"
        )

        self._operation_gate = threading.Lock()
        self._closed = False
        self._shutdown_report = None
        self._current_run_id: str | None = None
        self._owned_run_id: str | None = None
        self._generation_owned = False
        self._playback_owner: object | None = None
        self._stop_requested = False

    def play(
        self, request: RunPlaybackRequest, *, owner: object | None = None
    ) -> PlaybackResult:
        """Start playback, optionally associating its generation with a caller token."""
        if type(request) is not RunPlaybackRequest:
            raise TypeError("request must be an exact RunPlaybackRequest")
        if not self._operation_gate.acquire(blocking=False):
            return _result(PlaybackCode.BUSY, request.run_id)
        try:
            if self._closed:
                return _result(PlaybackCode.SERVICE_CLOSED, request.run_id)
            return self._play_owned(request, owner)
        finally:
            self._operation_gate.release()

    def stop(self, timeout_seconds: float) -> PlaybackLifecycleReport:
        timeout = _validate_timeout_seconds(timeout_seconds)
        if not self._operation_gate.acquire(blocking=False):
            return _lifecycle_result(PlaybackLifecycleCode.BUSY)
        try:
            if self._shutdown_report is not None:
                return self._shutdown_report
            return self._finish_generation(timeout, request_stop=True)
        finally:
            self._operation_gate.release()

    def stop_if_owner(
        self, owner: object, timeout_seconds: float
    ) -> PlaybackLifecycleReport:
        """Stop only the generation belonging to this exact non-None caller token."""
        timeout = _validate_timeout_seconds(timeout_seconds)
        if not self._operation_gate.acquire(blocking=False):
            return _lifecycle_result(PlaybackLifecycleCode.BUSY)
        try:
            if owner is None or self._playback_owner is not owner:
                return _lifecycle_result(PlaybackLifecycleCode.CLEAN)
            if self._shutdown_report is not None:
                return self._shutdown_report
            return self._finish_generation(timeout, request_stop=True)
        finally:
            self._operation_gate.release()

    def join(self, timeout_seconds: float) -> PlaybackLifecycleReport:
        timeout = _validate_timeout_seconds(timeout_seconds)
        if not self._operation_gate.acquire(blocking=False):
            return _lifecycle_result(PlaybackLifecycleCode.BUSY)
        try:
            if self._shutdown_report is not None:
                return self._shutdown_report
            return self._finish_generation(timeout, request_stop=False)
        finally:
            self._operation_gate.release()

    def shutdown(self, timeout_seconds: float) -> PlaybackLifecycleReport:
        timeout = _validate_timeout_seconds(timeout_seconds)
        if not self._operation_gate.acquire(blocking=False):
            return _lifecycle_result(PlaybackLifecycleCode.BUSY)
        try:
            if self._shutdown_report is not None:
                return self._shutdown_report
            self._closed = True
            report = self._finish_generation(timeout, request_stop=True)
            self._shutdown_report = report
            return report
        finally:
            self._operation_gate.release()

    def _play_owned(
        self, request: RunPlaybackRequest, owner: object | None
    ) -> PlaybackResult:
        run_resolution = self._resolve_run(request)
        if type(run_resolution) is PlaybackResult:
            return run_resolution
        run, storage_kind = run_resolution

        audio_resolution = self._resolve_audio(request, storage_kind)
        if type(audio_resolution) is PlaybackResult:
            return audio_resolution
        audio, reference = audio_resolution

        config_resolution = self._resolve_config(request, run, audio)
        if type(config_resolution) is PlaybackResult:
            return config_resolution
        stored_columns, output_device_name, output_host_api, gain_db = (
            config_resolution
        )

        segment_resolution = self._load_segment(
            request, audio, reference, stored_columns
        )
        if type(segment_resolution) is PlaybackResult:
            return segment_resolution
        segment = segment_resolution

        replaced_run_id = None
        if self._generation_owned:
            replaced_run_id = self._current_run_id
            replacement = self._replace_generation(request.run_id)
            if replacement is not None:
                return replacement

        return self._start_generation(
            request.run_id,
            segment,
            audio.sample_rate_hz,
            output_device_name,
            output_host_api,
            gain_db,
            replaced_run_id,
            owner,
        )

    def _start_generation(
        self,
        run_id: str,
        segment: numpy.ndarray,
        sample_rate_hz: int,
        output_device_name: str,
        output_host_api: str,
        gain_db: float,
        replaced_run_id: str | None,
        owner: object | None,
    ) -> PlaybackResult:
        remaining = self._startup_remaining()
        if remaining is None:
            return _result(PlaybackCode.DEADLINE_CLOCK_FAILED, run_id)

        self._playback_owner = owner
        try:
            report = self._backend.start(
                segment,
                sample_rate_hz,
                output_device_name,
                output_host_api,
                gain_db,
                remaining,
            )
            if type(report) is not BackendStartReport:
                raise PlaybackContractError(
                    "backend start report must be an exact BackendStartReport"
                )
            report.__post_init__()
        except Exception:
            self._assume_uncertain_start(run_id)
            return _result(PlaybackCode.BACKEND_START_FAILED, run_id)

        if report.status is BackendStartStatus.STARTED:
            self._generation_owned = True
            self._owned_run_id = run_id
            self._current_run_id = run_id
            self._stop_requested = False
            return _result(PlaybackCode.STARTED, run_id, replaced_run_id)
        if report.status is BackendStartStatus.FAILED:
            if report.generation_owned:
                self._generation_owned = True
                self._owned_run_id = run_id
                self._stop_requested = True
            else:
                self._clear_generation()
            self._current_run_id = None
            return _result(PlaybackCode.BACKEND_START_FAILED, run_id)

        self._generation_owned = True
        self._owned_run_id = run_id
        self._current_run_id = None
        self._stop_requested = True
        return _result(PlaybackCode.BACKEND_START_TIMED_OUT, run_id)

    def _replace_generation(self, run_id: str) -> PlaybackResult | None:
        deadline = self._establish_deadline(self._replacement_timeout_seconds)
        if deadline is None:
            return _result(PlaybackCode.DEADLINE_CLOCK_FAILED, run_id)

        if not self._stop_requested:
            try:
                self._backend.request_stop()
            except Exception:
                return _result(PlaybackCode.REPLACEMENT_NON_CLEAN, run_id)
            self._stop_requested = True

        remaining = self._deadline_remaining(
            deadline, self._replacement_timeout_seconds
        )
        if remaining is None:
            return _result(PlaybackCode.DEADLINE_CLOCK_FAILED, run_id)
        report = self._validated_join(remaining)
        if report is None:
            return _result(PlaybackCode.REPLACEMENT_NON_CLEAN, run_id)
        if report.status is BackendJoinStatus.TIMED_OUT:
            return _result(PlaybackCode.REPLACEMENT_TIMED_OUT, run_id)

        self._clear_generation()
        if report.status is BackendJoinStatus.NON_CLEAN:
            return _result(PlaybackCode.REPLACEMENT_NON_CLEAN, run_id)
        return None

    def _resolve_run(
        self, request: RunPlaybackRequest
    ) -> tuple[RunRecord, AudioStorageKind] | PlaybackResult:
        try:
            run = self._run_repository.get(request.run_id)
        except Exception:
            return _result(PlaybackCode.PERSISTED_FACT_INVALID, request.run_id)
        if run is None:
            return _result(
                PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE, request.run_id
            )
        if type(run) is not RunRecord:
            return _result(PlaybackCode.PERSISTED_FACT_INVALID, request.run_id)
        try:
            validate_uuid4(run.run_id)
            if run.run_id != request.run_id:
                raise ValueError
            if type(run.config_id) is not int:
                raise ValueError
            if type(run.state) is not str:
                raise ValueError
            if run.state == "ACTIVE":
                return _result(
                    PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE, request.run_id
                )
            if run.state not in _TERMINAL_RUN_STATES:
                raise ValueError
            if type(run.ingress_kind) is not str:
                raise ValueError
            storage_kind = _INGRESS_STORAGE_KINDS[run.ingress_kind]
            validate_utc_timestamp(run.accepted_at)
            validate_utc_timestamp(run.updated_at)
            if run.terminal_at is None:
                raise ValueError
            validate_utc_timestamp(run.terminal_at)
        except Exception:
            return _result(PlaybackCode.PERSISTED_FACT_INVALID, request.run_id)
        return run, storage_kind

    def _resolve_audio(
        self, request: RunPlaybackRequest, storage_kind: AudioStorageKind
    ) -> tuple[AudioRecord, PersistedAudioReference] | PlaybackResult:
        try:
            audio = self._audio_repository.get_for_run(request.run_id)
        except Exception:
            return _result(PlaybackCode.PERSISTED_FACT_INVALID, request.run_id)
        if audio is None:
            return _result(
                PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE, request.run_id
            )
        if type(audio) is not AudioRecord:
            return _result(PlaybackCode.PERSISTED_FACT_INVALID, request.run_id)
        try:
            validate_uuid4(audio.audio_id)
            validate_uuid4(audio.run_id)
            if audio.run_id != request.run_id:
                raise ValueError
            if (
                type(audio.container_format) is not str
                or audio.container_format not in {"WAV_PCM", "WAV_IEEE_FLOAT"}
            ):
                raise ValueError
            for value in (
                audio.sample_rate_hz,
                audio.channel_count,
                audio.frame_count,
                audio.duration_ms,
                audio.byte_size,
            ):
                if type(value) is not int or value <= 0:
                    raise ValueError
            expected_duration_ms = math.ceil(
                audio.frame_count * 1000 / audio.sample_rate_hz
            )
            if audio.duration_ms != expected_duration_ms:
                raise ValueError
            validate_utc_timestamp(audio.closed_verified_at)
            validate_utc_timestamp(audio.created_at)
            reference = PersistedAudioReference(
                reference_id=audio.audio_id,
                audio_data_id=audio.audio_id,
                storage_kind=storage_kind,
                absolute_path=audio.final_path,
                sample_rate_hz=audio.sample_rate_hz,
                channel_count=audio.channel_count,
                frame_count=audio.frame_count,
                dtype="<f8" if storage_kind is AudioStorageKind.IMPORTED_FINAL_AUDIO else "<i2",
                source_container_format=audio.container_format if storage_kind is AudioStorageKind.IMPORTED_FINAL_AUDIO else None,
                content_revision=f"{audio.audio_id}:{audio.byte_size}",
            )
        except Exception:
            return _result(PlaybackCode.PERSISTED_FACT_INVALID, request.run_id)
        return audio, reference

    def _resolve_config(
        self, request: RunPlaybackRequest, run: RunRecord, audio: AudioRecord
    ) -> tuple[tuple[int, ...], str, str, float] | PlaybackResult:
        try:
            envelope = decode_analysis_envelope(run.config_snapshot_json)
            config = envelope.config
            selected_channels = config["selected_channels"]
            if envelope.is_import:
                if run.ingress_kind != "IMPORTED":
                    raise ValueError("explicit import map requires imported audio")
                selected_channels = envelope.replay_channel_order
            if type(selected_channels) is not tuple or not selected_channels:
                raise ValueError
            if len(selected_channels) != audio.channel_count:
                raise ValueError
            if any(
                type(channel) is not int or channel < 0
                for channel in selected_channels
            ):
                raise ValueError
            if len(set(selected_channels)) != len(selected_channels):
                raise ValueError
            output_device_name = config["output_device_name"]
            output_host_api = config["output_device_hostapi"]
            gain_db = config["monitor_gain_db"]
            if type(output_device_name) is not str or not output_device_name:
                raise ValueError
            if type(output_host_api) is not str or not output_host_api:
                raise ValueError
            if (
                type(gain_db) is not float
                or not math.isfinite(gain_db)
                or not -60.0 <= gain_db <= 35.0
            ):
                raise ValueError
        except Exception:
            return _result(PlaybackCode.CONFIG_SNAPSHOT_INVALID, request.run_id)

        requested_channels = request.display_channel_indices or (
            request.display_channel_index,
        )
        try:
            stored_columns = tuple(
                selected_channels.index(channel)
                for channel in requested_channels
            )
        except ValueError:
            return _result(PlaybackCode.CHANNEL_UNAVAILABLE, request.run_id)
        if request.gain_db_override is not None:
            gain_db = request.gain_db_override
        return stored_columns, output_device_name, output_host_api, gain_db

    def _load_segment(
        self,
        request: RunPlaybackRequest,
        audio: AudioRecord,
        reference: PersistedAudioReference,
        stored_columns: tuple[int, ...],
    ) -> numpy.ndarray | PlaybackResult:
        try:
            loaded = self._audio_loader.open(reference, _NeverCancelledToken())
        except Exception:
            return _result(PlaybackCode.AUDIO_LOAD_FAILED, request.run_id)

        try:
            close = loaded.close
        except Exception:
            return _result(PlaybackCode.AUDIO_LOAD_FAILED, request.run_id)
        if not callable(close):
            return _result(PlaybackCode.AUDIO_LOAD_FAILED, request.run_id)

        result: numpy.ndarray | PlaybackResult
        close_failed = False
        try:
            try:
                result = self._copy_segment(
                    request, audio, loaded, stored_columns
                )
            except _WindowUnavailable:
                result = _result(
                    PlaybackCode.WINDOW_UNAVAILABLE, request.run_id
                )
            except Exception:
                result = _result(
                    PlaybackCode.AUDIO_LOAD_FAILED, request.run_id
                )
        finally:
            propagating = sys.exc_info()[0] is not None
            try:
                close()
            except Exception:
                if not propagating:
                    close_failed = True
        if close_failed:
            return _result(PlaybackCode.AUDIO_LOAD_FAILED, request.run_id)
        return result

    @staticmethod
    def _copy_segment(
        request: RunPlaybackRequest,
        audio: AudioRecord,
        loaded: object,
        stored_columns: tuple[int, ...],
    ) -> numpy.ndarray:
        if (
            type(loaded.sample_rate_hz) is not int
            or loaded.sample_rate_hz <= 0
            or loaded.sample_rate_hz != audio.sample_rate_hz
            or type(loaded.channel_count) is not int
            or loaded.channel_count <= 0
            or loaded.channel_count != audio.channel_count
            or type(loaded.frame_count) is not int
            or loaded.frame_count <= 0
            or loaded.frame_count != audio.frame_count
        ):
            raise ValueError
        samples = loaded.samples
        if (
            type(samples) is not numpy.ndarray
            or samples.dtype not in (numpy.dtype("<i2"), numpy.dtype("<f8"))
            or (audio.source_kind != "IMPORTED_SOURCE" and samples.dtype != numpy.dtype("<i2"))
            or not samples.flags.owndata
            or not samples.flags.c_contiguous
            or samples.shape != (audio.frame_count, audio.channel_count)
        ):
            raise ValueError

        duration_seconds = audio.frame_count / audio.sample_rate_hz
        if not (
            0.0
            <= request.window_start_seconds
            < request.window_end_seconds
            <= duration_seconds
        ):
            raise _WindowUnavailable
        start_frame = math.floor(
            request.window_start_seconds * audio.sample_rate_hz
        )
        end_frame = math.floor(request.window_end_seconds * audio.sample_rate_hz)
        if not 0 <= start_frame < end_frame <= audio.frame_count:
            raise _WindowUnavailable

        selected = numpy.asarray(
            samples[start_frame:end_frame, stored_columns],
            dtype=numpy.float32,
            order="C",
        )
        if selected.ndim == 1:
            selected = selected.reshape(-1, 1)
        segment = numpy.array(
            numpy.mean(selected, axis=1),
            dtype=numpy.float32,
            order="C",
            copy=True,
        )
        if samples.dtype == numpy.dtype("<i2"):
            segment /= numpy.float32(32768.0)
        if (
            type(segment) is not numpy.ndarray
            or not segment.flags.owndata
            or not segment.flags.c_contiguous
            or not numpy.isfinite(segment).all()
        ):
            raise ValueError
        return segment

    def _finish_generation(
        self,
        timeout_seconds: float,
        *,
        request_stop: bool,
    ) -> PlaybackLifecycleReport:
        if not self._generation_owned:
            return _lifecycle_result(PlaybackLifecycleCode.CLEAN)

        deadline = self._establish_deadline(timeout_seconds)
        if deadline is None:
            return _lifecycle_result(
                PlaybackLifecycleCode.DEADLINE_CLOCK_FAILED
            )

        if request_stop and not self._stop_requested:
            try:
                self._backend.request_stop()
            except Exception:
                return _lifecycle_result(
                    PlaybackLifecycleCode.BACKEND_STOP_FAILED
                )
            self._stop_requested = True

        remaining = self._deadline_remaining(deadline, timeout_seconds)
        if remaining is None:
            return _lifecycle_result(
                PlaybackLifecycleCode.DEADLINE_CLOCK_FAILED
            )
        report = self._validated_join(remaining)
        if report is None:
            return _lifecycle_result(
                PlaybackLifecycleCode.BACKEND_NON_CLEAN
            )
        if report.status is BackendJoinStatus.TIMED_OUT:
            return _lifecycle_result(
                PlaybackLifecycleCode.BACKEND_JOIN_TIMED_OUT
            )

        self._clear_generation()
        if report.status is BackendJoinStatus.NON_CLEAN:
            return _lifecycle_result(
                PlaybackLifecycleCode.BACKEND_NON_CLEAN
            )
        return _lifecycle_result(PlaybackLifecycleCode.CLEAN)

    def _validated_join(self, timeout_seconds: float) -> BackendJoinReport | None:
        try:
            report = self._backend.join(timeout_seconds)
            if type(report) is not BackendJoinReport:
                raise PlaybackContractError(
                    "backend join report must be an exact BackendJoinReport"
                )
            report.__post_init__()
            return report
        except Exception:
            return None

    def _establish_deadline(self, timeout_seconds: float) -> float | None:
        try:
            started_at = self._monotonic()
            if type(started_at) is not float or not math.isfinite(started_at):
                raise ValueError
            deadline = started_at + timeout_seconds
            if not math.isfinite(deadline):
                raise ValueError
            return deadline
        except Exception:
            return None

    def _deadline_remaining(
        self,
        deadline: float,
        last_remaining: float,
    ) -> float | None:
        try:
            now = self._monotonic()
            if type(now) is not float or not math.isfinite(now):
                raise ValueError
            delta = deadline - now
            if not math.isfinite(delta):
                raise ValueError
            return min(last_remaining, max(0.0, delta))
        except Exception:
            return None

    def _startup_remaining(self) -> float | None:
        timeout = self._backend_startup_timeout_seconds
        deadline = self._establish_deadline(timeout)
        if deadline is None:
            return None
        return self._deadline_remaining(deadline, timeout)

    def _assume_uncertain_start(self, run_id: str) -> None:
        self._generation_owned = True
        self._owned_run_id = run_id
        self._current_run_id = None
        self._stop_requested = False
        try:
            self._backend.request_stop()
        except Exception:
            return
        self._stop_requested = True

    def _clear_generation(self) -> None:
        self._generation_owned = False
        self._playback_owner = None
        self._owned_run_id = None
        self._current_run_id = None
        self._stop_requested = False
