from __future__ import annotations

import inspect
import math
import threading

import pytest

from app.service.run_scoped_playback.contracts import (
    BackendJoinReport,
    BackendJoinStatus,
    BackendStartReport,
    BackendStartStatus,
    PlaybackCode,
    PlaybackContractError,
    PlaybackLifecycleCode,
    PlaybackLifecycleStatus,
    RunPlaybackRequest,
)
from app.service.run_scoped_playback.service import RunScopedPlaybackService

from .conftest import (
    OTHER_RUN_ID,
    RUN_ID,
    FakeAudioLoader,
    FakeAudioRepository,
    FakeLoadedAudio,
    FakeRunRepository,
    SequenceClock,
    make_audio,
    make_run,
)


OTHER_AUDIO_ID = "123e4567-e89b-42d3-a456-426614174011"


def request(run_id: str = RUN_ID) -> RunPlaybackRequest:
    return RunPlaybackRequest(run_id, 7, 0.0, 2.0)


class ConstantClock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.value


class QueueLoader:
    def __init__(self, *loaded: FakeLoadedAudio) -> None:
        self.loaded = list(loaded)
        self.calls = 0

    def open(self, reference: object, cancel_token: object) -> FakeLoadedAudio:
        result = self.loaded[self.calls]
        self.calls += 1
        return result


class LifecycleBackend:
    def __init__(
        self,
        *,
        joins: list[object] | None = None,
        start_reports: list[object] | None = None,
        stop_error: Exception | None = None,
        join_entered: threading.Event | None = None,
        join_release: threading.Event | None = None,
    ) -> None:
        self.joins = list(joins or [])
        self.start_reports = list(start_reports or [])
        self.stop_error = stop_error
        self.join_entered = join_entered
        self.join_release = join_release
        self.start_arguments: list[tuple[object, ...]] = []
        self.stop_calls = 0
        self.join_calls: list[float] = []

    def start(
        self,
        segment: object,
        sample_rate_hz: int,
        output_device_name: str,
        output_host_api: str,
        gain_db: float,
        startup_timeout_seconds: float,
    ) -> object:
        self.start_arguments.append(
            (
                segment,
                sample_rate_hz,
                output_device_name,
                output_host_api,
                gain_db,
                startup_timeout_seconds,
            )
        )
        if self.start_reports:
            report = self.start_reports.pop(0)
            if isinstance(report, Exception):
                raise report
            return report
        return BackendStartReport(BackendStartStatus.STARTED, True, False)

    def request_stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error

    def join(self, timeout_seconds: float) -> object:
        self.join_calls.append(timeout_seconds)
        if self.join_entered is not None:
            self.join_entered.set()
        if self.join_release is not None:
            assert self.join_release.wait(2.0)
        report = (
            self.joins.pop(0)
            if self.joins
            else BackendJoinReport(BackendJoinStatus.CLEAN, False)
        )
        if isinstance(report, Exception):
            raise report
        return report


def make_service(
    *,
    backend: object | None = None,
    clock: object | None = None,
    run_repository: object | None = None,
    audio_repository: object | None = None,
    loader: object | None = None,
) -> RunScopedPlaybackService:
    return RunScopedPlaybackService(
        run_repository=run_repository or FakeRunRepository(),
        audio_repository=audio_repository or FakeAudioRepository(),
        audio_loader=loader or FakeAudioLoader(),
        backend=backend or LifecycleBackend(),
        monotonic=clock or ConstantClock(),
    )


def start(service: RunScopedPlaybackService) -> None:
    assert service.play(request()).code is PlaybackCode.STARTED


def select_other(
    run_repository: FakeRunRepository,
    audio_repository: FakeAudioRepository,
) -> None:
    run_repository.record = make_run(run_id=OTHER_RUN_ID)
    audio_repository.record = make_audio(
        audio_id=OTHER_AUDIO_ID, run_id=OTHER_RUN_ID
    )


def forged_join_report() -> BackendJoinReport:
    report = object.__new__(BackendJoinReport)
    object.__setattr__(report, "status", BackendJoinStatus.CLEAN)
    object.__setattr__(report, "generation_owned", True)
    return report


def assert_owned(service: RunScopedPlaybackService, run_id: str = RUN_ID) -> None:
    assert service._generation_owned is True
    assert service._owned_run_id == run_id


def assert_cleared(service: RunScopedPlaybackService) -> None:
    assert service._generation_owned is False
    assert service._owned_run_id is None
    assert service._current_run_id is None
    assert service._stop_requested is False


@pytest.mark.parametrize(
    ("new_run", "expected"),
    [
        (None, PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE),
        (
            make_run(run_id=OTHER_RUN_ID, state="UNKNOWN"),
            PlaybackCode.PERSISTED_FACT_INVALID,
        ),
    ],
)
def test_invalid_or_unavailable_replacement_leaves_current_untouched(
    new_run, expected
):
    backend = LifecycleBackend()
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    service = make_service(
        backend=backend, run_repository=runs, audio_repository=audio
    )
    start(service)
    runs.record = new_run

    result = service.play(request(OTHER_RUN_ID))

    assert result.code is expected
    assert backend.stop_calls == 0
    assert backend.join_calls == []
    assert len(backend.start_arguments) == 1
    assert_owned(service)
    assert service._current_run_id == RUN_ID


@pytest.mark.parametrize("naturally_complete", [False, True])
def test_live_or_naturally_clean_replacement_stops_joins_then_starts_once(
    naturally_complete,
):
    backend = LifecycleBackend(
        joins=[BackendJoinReport(BackendJoinStatus.CLEAN, False)]
    )
    backend.naturally_complete = naturally_complete
    clock = SequenceClock(10.0, 10.0, 20.0, 20.25, 30.0, 30.5)
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    old_loaded = FakeLoadedAudio()
    new_loaded = FakeLoadedAudio()
    service = make_service(
        backend=backend,
        clock=clock,
        run_repository=runs,
        audio_repository=audio,
        loader=QueueLoader(old_loaded, new_loaded),
    )
    start(service)
    select_other(runs, audio)

    result = service.play(request(OTHER_RUN_ID))

    assert result.code is PlaybackCode.STARTED
    assert result.replaced_run_id == RUN_ID
    assert backend.stop_calls == 1
    assert backend.join_calls == [0.75]
    assert len(backend.start_arguments) == 2
    assert backend.start_arguments[1][-1] == 0.5
    assert old_loaded.close_calls == new_loaded.close_calls == 1
    assert_owned(service, OTHER_RUN_ID)
    assert service._current_run_id == OTHER_RUN_ID
    assert clock.calls == 6


def test_natural_non_clean_replacement_consumes_terminal_and_does_not_start():
    backend = LifecycleBackend(
        joins=[BackendJoinReport(BackendJoinStatus.NON_CLEAN, False)]
    )
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    service = make_service(
        backend=backend, run_repository=runs, audio_repository=audio
    )
    start(service)
    select_other(runs, audio)

    result = service.play(request(OTHER_RUN_ID))

    assert result.code is PlaybackCode.REPLACEMENT_NON_CLEAN
    assert result.replaced_run_id is None
    assert backend.stop_calls == 1
    assert len(backend.join_calls) == 1
    assert len(backend.start_arguments) == 1
    assert_cleared(service)


def test_public_join_consumes_identity_so_later_start_is_not_replacement():
    backend = LifecycleBackend(
        joins=[BackendJoinReport(BackendJoinStatus.CLEAN, False)]
    )
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    service = make_service(
        backend=backend, run_repository=runs, audio_repository=audio
    )
    start(service)
    assert service.join(1.0).code is PlaybackLifecycleCode.CLEAN
    select_other(runs, audio)

    result = service.play(request(OTHER_RUN_ID))

    assert result.code is PlaybackCode.STARTED
    assert result.replaced_run_id is None
    assert backend.stop_calls == 0
    assert len(backend.join_calls) == 1
    assert len(backend.start_arguments) == 2


@pytest.mark.parametrize(
    ("join_outcome", "expected"),
    [
        (
            BackendJoinReport(BackendJoinStatus.TIMED_OUT, True),
            PlaybackCode.REPLACEMENT_TIMED_OUT,
        ),
        (
            BackendJoinReport(BackendJoinStatus.NON_CLEAN, False),
            PlaybackCode.REPLACEMENT_NON_CLEAN,
        ),
        (RuntimeError("join detail"), PlaybackCode.REPLACEMENT_NON_CLEAN),
        (object(), PlaybackCode.REPLACEMENT_NON_CLEAN),
        (forged_join_report(), PlaybackCode.REPLACEMENT_NON_CLEAN),
    ],
)
def test_replacement_join_failures_close_new_audio_once_and_never_start(
    join_outcome, expected
):
    backend = LifecycleBackend(joins=[join_outcome])
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    old_loaded = FakeLoadedAudio()
    new_loaded = FakeLoadedAudio()
    service = make_service(
        backend=backend,
        run_repository=runs,
        audio_repository=audio,
        loader=QueueLoader(old_loaded, new_loaded),
    )
    start(service)
    select_other(runs, audio)

    result = service.play(request(OTHER_RUN_ID))

    assert result.code is expected
    assert new_loaded.close_calls == 1
    assert len(backend.start_arguments) == 1
    if expected is PlaybackCode.REPLACEMENT_TIMED_OUT:
        assert_owned(service)
    elif (
        type(join_outcome) is BackendJoinReport
        and join_outcome.status is BackendJoinStatus.NON_CLEAN
    ):
        assert_cleared(service)
    else:
        assert_owned(service)


def test_replacement_stop_failure_retains_unsignaled_identity_without_join():
    backend = LifecycleBackend(stop_error=RuntimeError("stop detail"))
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    new_loaded = FakeLoadedAudio()
    service = make_service(
        backend=backend,
        run_repository=runs,
        audio_repository=audio,
        loader=QueueLoader(FakeLoadedAudio(), new_loaded),
    )
    start(service)
    select_other(runs, audio)

    result = service.play(request(OTHER_RUN_ID))

    assert result.code is PlaybackCode.REPLACEMENT_NON_CLEAN
    assert backend.stop_calls == 1
    assert backend.join_calls == []
    assert new_loaded.close_calls == 1
    assert_owned(service)
    assert service._current_run_id == RUN_ID
    assert service._stop_requested is False


@pytest.mark.parametrize(
    "clock_values",
    [
        (10.0, 10.0, RuntimeError("clock")),
        (10.0, 10.0, 1),
        (10.0, 10.0, math.nan),
        (10.0, 10.0, 20.0, RuntimeError("clock")),
        (10.0, 10.0, 20.0, math.inf),
    ],
)
def test_replacement_clock_failure_retains_identity_without_retry(clock_values):
    backend = LifecycleBackend()
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    old_loaded = FakeLoadedAudio()
    new_loaded = FakeLoadedAudio()
    service = make_service(
        backend=backend,
        clock=SequenceClock(*clock_values),
        run_repository=runs,
        audio_repository=audio,
        loader=QueueLoader(old_loaded, new_loaded),
    )
    start(service)
    select_other(runs, audio)

    result = service.play(request(OTHER_RUN_ID))

    assert result.code is PlaybackCode.DEADLINE_CLOCK_FAILED
    assert_owned(service)
    assert service._current_run_id == RUN_ID
    assert len(backend.start_arguments) == 1
    assert old_loaded.close_calls == new_loaded.close_calls == 1


def test_replacement_decreasing_clock_never_extends_deadlines():
    backend = LifecycleBackend()
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    clock = SequenceClock(10.0, 10.0, 20.0, 19.0, 30.0, 29.0)
    service = make_service(
        backend=backend,
        clock=clock,
        run_repository=runs,
        audio_repository=audio,
    )
    start(service)
    select_other(runs, audio)

    assert service.play(request(OTHER_RUN_ID)).code is PlaybackCode.STARTED

    assert backend.join_calls == [1.0]
    assert backend.start_arguments[1][-1] == 1.0
    assert clock.calls == 6


@pytest.mark.parametrize("value", [None, True, 1, -0.1, math.nan, math.inf])
@pytest.mark.parametrize("method", ["stop", "join", "shutdown"])
def test_invalid_lifecycle_timeout_raises_before_gate_clock_or_backend(
    method, value
):
    backend = LifecycleBackend()
    clock = ConstantClock()
    service = make_service(backend=backend, clock=clock)
    assert service._operation_gate.acquire(blocking=False)
    try:
        with pytest.raises(PlaybackContractError):
            getattr(service, method)(value)
    finally:
        service._operation_gate.release()
    assert clock.calls == 0
    assert backend.stop_calls == 0
    assert backend.join_calls == []


def test_all_public_calls_are_busy_during_gate_owned_play_with_zero_extra_work():
    entered = threading.Event()
    release = threading.Event()
    loaded = FakeLoadedAudio()
    loader = FakeAudioLoader(loaded, entered=entered, release=release)
    backend = LifecycleBackend()
    clock = ConstantClock()
    service = make_service(backend=backend, clock=clock, loader=loader)
    owner_results: list[object] = []
    owner = threading.Thread(
        target=lambda: owner_results.append(service.play(request()))
    )
    owner.start()
    assert entered.wait(2.0)

    assert service.play(request()).code is PlaybackCode.BUSY
    for method in ("stop", "join", "shutdown"):
        assert (
            getattr(service, method)(1.0).code
            is PlaybackLifecycleCode.BUSY
        )
    assert clock.calls == 0
    assert backend.stop_calls == 0
    assert backend.join_calls == []
    assert backend.start_arguments == []
    assert service._shutdown_report is None
    assert service._closed is False

    release.set()
    owner.join(2.0)
    assert not owner.is_alive()
    assert owner_results[0].code is PlaybackCode.STARTED
    assert loaded.close_calls == 1


@pytest.mark.parametrize("owner_method", ["stop", "join", "shutdown"])
def test_all_public_calls_are_busy_during_owned_lifecycle_operation(owner_method):
    entered = threading.Event()
    release = threading.Event()
    backend = LifecycleBackend(join_entered=entered, join_release=release)
    clock = ConstantClock()
    service = make_service(backend=backend, clock=clock)
    start(service)
    owner_results: list[object] = []
    owner = threading.Thread(
        target=lambda: owner_results.append(
            getattr(service, owner_method)(1.0)
        )
    )
    owner.start()
    assert entered.wait(2.0)
    clock_calls = clock.calls
    stop_calls = backend.stop_calls
    join_calls = list(backend.join_calls)
    starts = len(backend.start_arguments)

    assert service.play(request()).code is PlaybackCode.BUSY
    for method in ("stop", "join", "shutdown"):
        assert (
            getattr(service, method)(1.0).code
            is PlaybackLifecycleCode.BUSY
        )
    assert clock.calls == clock_calls
    assert backend.stop_calls == stop_calls
    assert backend.join_calls == join_calls
    assert len(backend.start_arguments) == starts

    release.set()
    owner.join(2.0)
    assert not owner.is_alive()
    assert owner_results[0].code is PlaybackLifecycleCode.CLEAN


@pytest.mark.parametrize("method", ["stop", "join"])
def test_lifecycle_without_generation_is_clean_without_clock_or_backend(method):
    backend = LifecycleBackend()
    clock = ConstantClock()
    service = make_service(backend=backend, clock=clock)

    report = getattr(service, method)(0.0)

    assert report.status is PlaybackLifecycleStatus.CLEAN
    assert report.code is PlaybackLifecycleCode.CLEAN
    assert clock.calls == 0
    assert backend.stop_calls == 0
    assert backend.join_calls == []


def test_stop_signals_at_most_once_and_joins_once_per_call_with_one_deadline():
    backend = LifecycleBackend(
        joins=[
            BackendJoinReport(BackendJoinStatus.TIMED_OUT, True),
            BackendJoinReport(BackendJoinStatus.CLEAN, False),
        ]
    )
    clock = SequenceClock(1.0, 1.0, 10.0, 10.25, 20.0, 20.5)
    service = make_service(backend=backend, clock=clock)
    start(service)

    first = service.stop(1.0)
    second = service.stop(1.0)

    assert first.code is PlaybackLifecycleCode.BACKEND_JOIN_TIMED_OUT
    assert second.code is PlaybackLifecycleCode.CLEAN
    assert backend.stop_calls == 1
    assert backend.join_calls == [0.75, 0.5]
    assert_cleared(service)
    assert clock.calls == 6


def test_join_never_signals_and_maps_clean_while_clearing_identity():
    backend = LifecycleBackend(
        joins=[BackendJoinReport(BackendJoinStatus.CLEAN, False)]
    )
    service = make_service(backend=backend)
    start(service)

    report = service.join(1.0)

    assert report.code is PlaybackLifecycleCode.CLEAN
    assert backend.stop_calls == 0
    assert len(backend.join_calls) == 1
    assert_cleared(service)


@pytest.mark.parametrize("method", ["stop", "join", "shutdown"])
@pytest.mark.parametrize(
    "tail",
    [
        (RuntimeError("clock detail"),),
        (1,),
        (math.nan,),
        (10.0, RuntimeError("clock detail")),
        (10.0, True),
        (10.0, math.inf),
    ],
)
def test_lifecycle_clock_failures_map_exactly_without_retry(method, tail):
    backend = LifecycleBackend()
    clock = SequenceClock(1.0, 1.0, *tail)
    service = make_service(backend=backend, clock=clock)
    start(service)

    report = getattr(service, method)(1.0)

    assert report.code is PlaybackLifecycleCode.DEADLINE_CLOCK_FAILED
    assert_owned(service)
    if method == "shutdown":
        assert service._closed is True
        assert service._shutdown_report is report
        assert service.stop(1.0) is report
        assert service.join(1.0) is report
        assert service.shutdown(1.0) is report


@pytest.mark.parametrize("method", ["stop", "join", "shutdown"])
def test_lifecycle_decreasing_clock_clamps_join_timeout(method):
    backend = LifecycleBackend(
        joins=[BackendJoinReport(BackendJoinStatus.TIMED_OUT, True)]
    )
    clock = SequenceClock(1.0, 1.0, 10.0, 9.0)
    service = make_service(backend=backend, clock=clock)
    start(service)

    report = getattr(service, method)(1.0)

    assert report.code is PlaybackLifecycleCode.BACKEND_JOIN_TIMED_OUT
    assert backend.join_calls == [1.0]
    assert clock.calls == 4
    assert_owned(service)


@pytest.mark.parametrize("method", ["stop", "join", "shutdown"])
@pytest.mark.parametrize(
    ("outcome", "expected", "cleared"),
    [
        (
            BackendJoinReport(BackendJoinStatus.CLEAN, False),
            PlaybackLifecycleCode.CLEAN,
            True,
        ),
        (
            BackendJoinReport(BackendJoinStatus.TIMED_OUT, True),
            PlaybackLifecycleCode.BACKEND_JOIN_TIMED_OUT,
            False,
        ),
        (
            BackendJoinReport(BackendJoinStatus.NON_CLEAN, False),
            PlaybackLifecycleCode.BACKEND_NON_CLEAN,
            True,
        ),
        (RuntimeError("join detail"), PlaybackLifecycleCode.BACKEND_NON_CLEAN, False),
        (object(), PlaybackLifecycleCode.BACKEND_NON_CLEAN, False),
        (forged_join_report(), PlaybackLifecycleCode.BACKEND_NON_CLEAN, False),
    ],
)
def test_lifecycle_join_outcomes_have_exact_mapping_and_ownership(
    method, outcome, expected, cleared
):
    backend = LifecycleBackend(joins=[outcome])
    service = make_service(backend=backend)
    start(service)

    report = getattr(service, method)(1.0)

    assert report.code is expected
    assert len(backend.join_calls) == 1
    assert backend.stop_calls == (0 if method == "join" else 1)
    if cleared:
        assert_cleared(service)
    else:
        assert_owned(service)
    if method == "shutdown":
        assert service._shutdown_report is report
        assert service.stop(1.0) is report
        assert service.join(1.0) is report
        assert service.shutdown(1.0) is report


@pytest.mark.parametrize("method", ["stop", "shutdown"])
def test_stop_signal_failure_retains_unsignaled_identity_and_never_joins(method):
    backend = LifecycleBackend(stop_error=RuntimeError("stop detail"))
    service = make_service(backend=backend)
    start(service)

    report = getattr(service, method)(1.0)

    assert report.code is PlaybackLifecycleCode.BACKEND_STOP_FAILED
    assert backend.stop_calls == 1
    assert backend.join_calls == []
    assert_owned(service)
    assert service._stop_requested is False
    if method == "shutdown":
        assert service._shutdown_report is report
        assert service.stop(1.0) is report
        assert service.join(1.0) is report
        assert service.shutdown(1.0) is report


def test_stop_then_later_play_succeeds_without_replacement_identity():
    backend = LifecycleBackend()
    runs = FakeRunRepository()
    audio = FakeAudioRepository()
    service = make_service(
        backend=backend, run_repository=runs, audio_repository=audio
    )
    start(service)
    assert service.stop(1.0).code is PlaybackLifecycleCode.CLEAN
    select_other(runs, audio)

    result = service.play(request(OTHER_RUN_ID))

    assert result.code is PlaybackCode.STARTED
    assert result.replaced_run_id is None
    assert len(backend.start_arguments) == 2


def test_shutdown_without_generation_caches_same_report_for_all_lifecycle_calls():
    backend = LifecycleBackend()
    clock = ConstantClock()
    service = make_service(backend=backend, clock=clock)

    report = service.shutdown(0.0)

    assert report.code is PlaybackLifecycleCode.CLEAN
    assert service.stop(1.0) is report
    assert service.join(1.0) is report
    assert service.shutdown(1.0) is report
    assert clock.calls == 0
    assert backend.stop_calls == 0
    assert backend.join_calls == []


def test_shutdown_cache_is_same_object_and_play_is_closed_before_repository():
    backend = LifecycleBackend(
        joins=[BackendJoinReport(BackendJoinStatus.TIMED_OUT, True)]
    )
    runs = FakeRunRepository()
    service = make_service(backend=backend, run_repository=runs)
    start(service)
    calls_before = list(runs.calls)

    first = service.shutdown(1.0)
    join_calls = list(backend.join_calls)
    stop_calls = backend.stop_calls
    for method in ("stop", "join", "shutdown"):
        assert getattr(service, method)(1.0) is first
    closed = service.play(request())

    assert first.code is PlaybackLifecycleCode.BACKEND_JOIN_TIMED_OUT
    assert closed.code is PlaybackCode.SERVICE_CLOSED
    assert runs.calls == calls_before
    assert backend.join_calls == join_calls
    assert backend.stop_calls == stop_calls


def test_busy_shutdown_does_not_latch_or_cache_and_later_shutdown_owns_once():
    entered = threading.Event()
    release = threading.Event()
    loader = FakeAudioLoader(
        FakeLoadedAudio(), entered=entered, release=release
    )
    backend = LifecycleBackend()
    service = make_service(backend=backend, loader=loader)
    owner = threading.Thread(target=lambda: service.play(request()))
    owner.start()
    assert entered.wait(2.0)

    busy = service.shutdown(1.0)

    assert busy.code is PlaybackLifecycleCode.BUSY
    assert service._closed is False
    assert service._shutdown_report is None
    release.set()
    owner.join(2.0)
    assert not owner.is_alive()

    report = service.shutdown(1.0)
    assert report.code is PlaybackLifecycleCode.CLEAN
    assert backend.stop_calls == 1
    assert len(backend.join_calls) == 1


def test_shutdown_latches_before_cleanup_and_overlap_never_becomes_owner():
    entered = threading.Event()
    release = threading.Event()
    backend = LifecycleBackend(join_entered=entered, join_release=release)
    service = make_service(backend=backend)
    start(service)
    reports: list[object] = []
    owner = threading.Thread(target=lambda: reports.append(service.shutdown(1.0)))
    owner.start()
    assert entered.wait(2.0)

    assert service.play(request()).code is PlaybackCode.BUSY
    assert service.shutdown(1.0).code is PlaybackLifecycleCode.BUSY

    release.set()
    owner.join(2.0)
    assert not owner.is_alive()
    assert reports[0].code is PlaybackLifecycleCode.CLEAN
    assert service.play(request()).code is PlaybackCode.SERVICE_CLOSED
    assert service.shutdown(1.0) is reports[0]
    assert backend.stop_calls == 1
    assert len(backend.join_calls) == 1


def test_service_has_one_nonblocking_gate_and_no_completion_probe_or_handoff():
    source = inspect.getsource(RunScopedPlaybackService)

    assert source.count("threading.Lock()") == 1
    assert "acquire(blocking=False)" in source
    assert "poll_terminal" not in source
    assert "handoff" not in source
    assert "retry" not in source
