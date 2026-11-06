"""One-generation bounded sounddevice playback backend."""

from __future__ import annotations

import math
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy
import sounddevice as sd

from .contracts import (
    BackendJoinReport,
    BackendJoinStatus,
    BackendStartReport,
    BackendStartStatus,
    PlaybackContractError,
    _validate_timeout_seconds,
)


DEFAULT_OUTPUT_LATENCY_SECONDS = 0.05


def _absolute_deadline(timeout_seconds: float) -> float:
    now = time.monotonic()
    if timeout_seconds > sys.float_info.max - now:
        return sys.float_info.max
    return now + timeout_seconds


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _wait_for_event(event: threading.Event, timeout_seconds: float) -> bool:
    deadline = _absolute_deadline(timeout_seconds)
    while not event.is_set():
        remaining = _remaining_seconds(deadline)
        if remaining <= 0.0:
            return event.wait(0.0)
        if event.wait(min(remaining, threading.TIMEOUT_MAX)):
            return True
    return True


def _join_thread(thread: threading.Thread, timeout_seconds: float) -> bool:
    deadline = _absolute_deadline(timeout_seconds)
    first_wait = True
    while thread.is_alive():
        remaining = _remaining_seconds(deadline)
        if remaining <= 0.0:
            if first_wait:
                thread.join(0.0)
            return not thread.is_alive()
        thread.join(min(remaining, threading.TIMEOUT_MAX))
        first_wait = False
    return True


class _EndpointResolutionError(Exception):
    pass


@dataclass(slots=True)
class _Generation:
    samples: numpy.ndarray
    sample_rate_hz: int
    device_index: int
    stop_event: threading.Event = field(default_factory=threading.Event)
    startup_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    thread: threading.Thread | None = None
    position: int = 0
    startup_succeeded: bool = False
    stop_requested: bool = False
    callback_failed: bool = False
    terminal_status: BackendJoinStatus | None = None


class SoundDevicePlaybackBackend:
    """Own at most one finite mono output-stream generation."""

    def __init__(
        self,
        *,
        device_resolver: Callable[[str, str], int] | None = None,
        output_latency_seconds: float = DEFAULT_OUTPUT_LATENCY_SECONDS,
    ) -> None:
        if device_resolver is not None and not callable(device_resolver):
            raise TypeError("device_resolver must be callable or None")
        if (
            type(output_latency_seconds) is not float
            or not math.isfinite(output_latency_seconds)
            or output_latency_seconds <= 0.0
        ):
            raise PlaybackContractError(
                "output_latency_seconds must be an exact finite positive float"
            )
        self._device_resolver = (
            self._resolve_default_endpoint
            if device_resolver is None
            else device_resolver
        )
        self._uses_default_resolver = device_resolver is None
        self._output_latency_seconds = output_latency_seconds
        self._lock = threading.Lock()
        self._generation: _Generation | None = None

    def start(
        self,
        segment: numpy.ndarray,
        sample_rate_hz: int,
        output_device_name: str,
        output_host_api: str,
        gain_db: float,
        startup_timeout_seconds: float,
    ) -> BackendStartReport:
        self._validate_start_inputs(
            segment,
            sample_rate_hz,
            output_device_name,
            output_host_api,
            gain_db,
            startup_timeout_seconds,
        )

        with self._lock:
            if self._generation is not None:
                self._signal_stop(self._generation)
                return BackendStartReport(
                    BackendStartStatus.FAILED,
                    generation_owned=True,
                    stop_requested=True,
                )

            try:
                device_index = self._device_resolver(
                    output_device_name,
                    output_host_api,
                )
            except _EndpointResolutionError:
                return BackendStartReport(
                    BackendStartStatus.FAILED,
                    generation_owned=False,
                    stop_requested=False,
                )
            except Exception:
                return BackendStartReport(
                    BackendStartStatus.FAILED,
                    generation_owned=False,
                    stop_requested=False,
                )
            if type(device_index) is not int or device_index < 0:
                raise PlaybackContractError(
                    "device resolver must return an exact nonnegative integer"
                )

            gained = numpy.empty(segment.shape, dtype=numpy.float32, order="C")
            numpy.multiply(
                segment,
                10.0 ** (gain_db / 20.0),
                out=gained,
                casting="unsafe",
            )
            numpy.clip(gained, -1.0, 1.0, out=gained)
            generation = _Generation(
                samples=gained,
                sample_rate_hz=sample_rate_hz,
                device_index=device_index,
            )
            generation.thread = threading.Thread(
                target=self._run_generation,
                args=(generation,),
                daemon=True,
                name="RunScopedPlayback",
            )
            self._generation = generation
            try:
                generation.thread.start()
            except Exception:
                generation.callback_failed = True
                generation.terminal_status = BackendJoinStatus.NON_CLEAN
                generation.startup_event.set()
                self._signal_stop(generation)
                return BackendStartReport(
                    BackendStartStatus.FAILED,
                    generation_owned=True,
                    stop_requested=True,
                )

        if not _wait_for_event(
            generation.startup_event,
            startup_timeout_seconds,
        ):
            with self._lock:
                self._signal_stop(generation)
            return BackendStartReport(
                BackendStartStatus.TIMED_OUT,
                generation_owned=True,
                stop_requested=True,
            )

        with generation.state_lock:
            startup_succeeded = generation.startup_succeeded
        if startup_succeeded:
            return BackendStartReport(
                BackendStartStatus.STARTED,
                generation_owned=True,
                stop_requested=False,
            )

        with self._lock:
            self._signal_stop(generation)
        return BackendStartReport(
            BackendStartStatus.FAILED,
            generation_owned=True,
            stop_requested=True,
        )

    def request_stop(self) -> None:
        with self._lock:
            generation = self._generation
            if generation is not None:
                self._signal_stop(generation)

    def join(self, timeout_seconds: float) -> BackendJoinReport:
        timeout = _validate_timeout_seconds(timeout_seconds)
        with self._lock:
            generation = self._generation
        if generation is None:
            return BackendJoinReport(
                BackendJoinStatus.CLEAN,
                generation_owned=False,
            )

        thread = generation.thread
        thread_finished = True
        if thread is not None:
            try:
                thread_finished = _join_thread(thread, timeout)
            except RuntimeError:
                thread_finished = not thread.is_alive()
        if not thread_finished:
            return BackendJoinReport(
                BackendJoinStatus.TIMED_OUT,
                generation_owned=True,
            )

        with self._lock:
            if self._generation is not generation:
                return BackendJoinReport(
                    BackendJoinStatus.CLEAN,
                    generation_owned=False,
                )
            with generation.state_lock:
                terminal_status = generation.terminal_status
            self._generation = None
        if terminal_status is BackendJoinStatus.CLEAN:
            return BackendJoinReport(
                BackendJoinStatus.CLEAN,
                generation_owned=False,
            )
        return BackendJoinReport(
            BackendJoinStatus.NON_CLEAN,
            generation_owned=False,
        )

    @staticmethod
    def _validate_start_inputs(
        segment: object,
        sample_rate_hz: object,
        output_device_name: object,
        output_host_api: object,
        gain_db: object,
        startup_timeout_seconds: object,
    ) -> None:
        if (
            type(segment) is not numpy.ndarray
            or segment.dtype != numpy.dtype("float32")
            or segment.ndim != 1
            or segment.size == 0
            or not segment.flags.c_contiguous
            or not segment.flags.owndata
            or not numpy.isfinite(segment).all()
        ):
            raise PlaybackContractError(
                "segment must be an owned nonempty finite C-contiguous "
                "one-dimensional float32 array"
            )
        if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
            raise PlaybackContractError(
                "sample_rate_hz must be an exact positive integer"
            )
        for value, name in (
            (output_device_name, "output_device_name"),
            (output_host_api, "output_host_api"),
        ):
            if type(value) is not str or not value.strip():
                raise PlaybackContractError(f"{name} must be an exact nonblank string")
        if (
            type(gain_db) is not float
            or not math.isfinite(gain_db)
            or not -60.0 <= gain_db <= 35.0
        ):
            raise PlaybackContractError(
                "gain_db must be an exact finite float from -60.0 to 35.0"
            )
        _validate_timeout_seconds(startup_timeout_seconds)

    @staticmethod
    def _signal_stop(generation: _Generation) -> None:
        with generation.state_lock:
            if generation.stop_requested:
                return
            generation.stop_requested = True
            generation.stop_event.set()

    @staticmethod
    def _resolve_default_endpoint(
        output_device_name: str,
        output_host_api: str,
    ) -> int:
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            matches: list[int] = []
            for index, device in enumerate(devices):
                if device["name"] != output_device_name:
                    continue
                hostapi_index = device["hostapi"]
                if type(hostapi_index) is not int:
                    continue
                if hostapi_index < 0 or hostapi_index >= len(hostapis):
                    continue
                if hostapis[hostapi_index]["name"] != output_host_api:
                    continue
                output_channels = device["max_output_channels"]
                if type(output_channels) is not int or output_channels <= 0:
                    continue
                matches.append(index)
        except Exception as exc:
            raise _EndpointResolutionError from exc
        if len(matches) != 1:
            raise _EndpointResolutionError
        return matches[0]

    def _run_generation(self, generation: _Generation) -> None:
        callback = self._make_callback(generation)
        non_clean = False
        stream = None
        stream_started = False
        try:
            stream = sd.OutputStream(
                device=generation.device_index,
                samplerate=generation.sample_rate_hz,
                channels=1,
                dtype="float32",
                latency=self._output_latency_seconds,
                callback=callback,
            )
            stream.start()
            stream_started = True
            with generation.state_lock:
                if (
                    not generation.stop_requested
                    and not generation.callback_failed
                ):
                    generation.startup_succeeded = True
            generation.startup_event.set()
            while (
                not generation.stop_event.is_set()
                and not generation.done_event.wait(0.01)
            ):
                pass
            with generation.state_lock:
                non_clean = generation.callback_failed
        except Exception as exc:
            if not isinstance(exc, sd.CallbackStop):
                non_clean = True
        finally:
            if stream is not None:
                if stream_started:
                    try:
                        stream.stop(ignore_errors=False)
                    except Exception:
                        non_clean = True
                try:
                    stream.close(ignore_errors=False)
                except Exception:
                    non_clean = True
            generation.startup_event.set()
            generation.done_event.set()
            with generation.state_lock:
                if generation.callback_failed:
                    non_clean = True
                generation.terminal_status = (
                    BackendJoinStatus.NON_CLEAN
                    if non_clean
                    else BackendJoinStatus.CLEAN
                )

    @staticmethod
    def _make_callback(generation: _Generation):
        def callback(outdata, frames, time_info, status) -> None:
            del time_info
            try:
                if status:
                    raise RuntimeError("output callback status")
                if (
                    type(frames) is not int
                    or frames < 0
                    or type(outdata) is not numpy.ndarray
                    or outdata.ndim != 2
                    or outdata.shape[0] < frames
                    or outdata.shape[1] != 1
                ):
                    raise RuntimeError("invalid output callback buffer")
                outdata[:frames, :] = 0.0
                with generation.state_lock:
                    if generation.stop_requested:
                        generation.done_event.set()
                        raise sd.CallbackStop
                    remaining = generation.samples.size - generation.position
                    write_count = min(frames, remaining)
                    if write_count:
                        start = generation.position
                        end = start + write_count
                        outdata[:write_count, 0] = generation.samples[start:end]
                        generation.position = end
                    exhausted = generation.position >= generation.samples.size
                if exhausted:
                    generation.done_event.set()
                    raise sd.CallbackStop
            except (sd.CallbackStop, sd.CallbackAbort):
                raise
            except Exception:
                with generation.state_lock:
                    generation.callback_failed = True
                generation.done_event.set()
                raise sd.CallbackAbort

        return callback
