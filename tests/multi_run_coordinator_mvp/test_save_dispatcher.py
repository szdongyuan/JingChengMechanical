from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable

import pytest

from app.service.multi_run_coordinator_mvp import (
    AudioMetadataConstraints,
    AudioSaveOutcome,
    AudioSaveSourceKind,
    AudioSaveStatus,
    BoundedAudioSaveDispatcher,
    CapturedAudioSaveRequest,
    CapturedAudioValue,
    CapturedSourceFormat,
    FinalPathRequest,
    ImportedAudioSaveRequest,
    ImportedSourceFormat,
    ImportedSourceReference,
    PersistedAudioFormat,
    PersistedAudioReference,
    SaveCancelStatus,
    SaveDispatcherShutdownStatus,
    SaveSubmitStatus,
    TerminalErrorCategory,
)


def _uuid(value: int) -> str:
    return str(uuid.UUID(int=(4 << 76) | (2 << 62) | value))


def _captured_request(value: int) -> CapturedAudioSaveRequest:
    source_identity = f"captured-{value}"
    return CapturedAudioSaveRequest(
        save_job_id=_uuid(1_000 + value),
        run_id=_uuid(2_000 + value),
        source_kind=AudioSaveSourceKind.CAPTURED_AUDIO,
        source_identity=source_identity,
        audio=CapturedAudioValue(
            source_identity=source_identity,
            sample_rate_hz=48_000,
            channel_count=1,
            frame_count=2,
            format=CapturedSourceFormat.INT16_INTERLEAVED,
            interleaved_bytes=b"\x00\x00\x00\x00",
            channel_indices=(0,),
        ),
        final_path_request=FinalPathRequest(f"C:\\audio\\captured-{value}.wav"),
        metadata=AudioMetadataConstraints(
            expected_sample_rate_hz=48_000,
            expected_channel_count=1,
            expected_frame_count=2,
            format=PersistedAudioFormat.PCM16_WAV,
        ),
    )


def _imported_request(value: int) -> ImportedAudioSaveRequest:
    source_identity = f"imported-{value}"
    return ImportedAudioSaveRequest(
        save_job_id=_uuid(3_000 + value),
        run_id=_uuid(4_000 + value),
        source_kind=AudioSaveSourceKind.IMPORTED_SOURCE,
        source_identity=source_identity,
        source=ImportedSourceReference(
            source_identity=source_identity,
            absolute_path=f"C:\\imports\\source-{value}.wav",
            sample_rate_hz=48_000,
            channel_count=1,
            frame_count=2,
            format=ImportedSourceFormat.PCM16_WAV,
        ),
        final_path_request=FinalPathRequest(f"C:\\audio\\imported-{value}.wav"),
        metadata=AudioMetadataConstraints(
            expected_sample_rate_hz=48_000,
            expected_channel_count=1,
            expected_frame_count=2,
            format=PersistedAudioFormat.PCM16_WAV,
        ),
    )


def _saved(request: CapturedAudioSaveRequest | ImportedAudioSaveRequest) -> AudioSaveOutcome:
    return AudioSaveOutcome(
        save_job_id=request.save_job_id,
        run_id=request.run_id,
        source_kind=request.source_kind,
        source_identity=request.source_identity,
        status=AudioSaveStatus.SAVED,
        persisted_audio=PersistedAudioReference(
            audio_id=_uuid(5_000 + uuid.UUID(request.save_job_id).int % 1_000),
            save_job_id=request.save_job_id,
            run_id=request.run_id,
            source_kind=request.source_kind,
            source_identity=request.source_identity,
            final_path=request.final_path_request.final_path,
            sample_rate_hz=request.metadata.expected_sample_rate_hz,
            channel_count=request.metadata.expected_channel_count,
            frame_count=request.metadata.expected_frame_count,
            byte_size=4,
            format=PersistedAudioFormat.PCM16_WAV,
            content_revision=1,
        ),
        error_category=None,
        reason="",
    )


class _BlockingSaver:
    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.entered = threading.Event()
        self._lock = threading.Lock()
        self.requests: list[CapturedAudioSaveRequest | ImportedAudioSaveRequest] = []
        self.worker_ids: set[int] = set()
        self.worker_daemon_values: set[bool] = set()

    def _save(
        self,
        request: CapturedAudioSaveRequest | ImportedAudioSaveRequest,
    ) -> AudioSaveOutcome:
        with self._lock:
            self.requests.append(request)
            self.worker_ids.add(threading.get_ident())
            self.worker_daemon_values.add(threading.current_thread().daemon)
            self.entered.set()
        assert self.release.wait(2)
        return _saved(request)

    def save_captured(self, request, cancel_token):
        return self._save(request)

    def save_imported(self, request, cancel_token):
        return self._save(request)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    condition = threading.Condition()
    deadline = time.monotonic() + timeout
    with condition:
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail("condition was not satisfied before the deadline")
            condition.wait(min(remaining, 0.01))


@pytest.fixture(autouse=True)
def _retire_dispatcher_workers(monkeypatch):
    dispatchers = []
    original_init = BoundedAudioSaveDispatcher.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        dispatchers.append(self)

    monkeypatch.setattr(BoundedAudioSaveDispatcher, "__init__", tracking_init)
    yield
    for dispatcher in dispatchers:
        dispatcher.shutdown(time.monotonic() + 2)


@pytest.mark.parametrize(
    ("worker_count", "outstanding_capacity"),
    [
        (worker_count, outstanding_capacity)
        for worker_count in range(1, 7)
        for outstanding_capacity in range(worker_count, 7)
    ],
)
def test_constructor_accepts_only_valid_worker_and_capacity_ranges(
    worker_count: int,
    outstanding_capacity: int,
) -> None:
    release = threading.Event()
    release.set()
    saver = _BlockingSaver(release)
    dispatcher = BoundedAudioSaveDispatcher(
        saver,
        saver,
        worker_count=worker_count,
        outstanding_capacity=outstanding_capacity,
    )

    assert len(dispatcher._workers) == worker_count
    assert all(worker.daemon for worker in dispatcher._workers)


@pytest.mark.parametrize(
    ("worker_count", "outstanding_capacity"),
    [
        (True, 1),
        (1, True),
        (False, 1),
        (1, False),
        (0, 1),
        (-1, 1),
        (7, 7),
        (2, 1),
        (1, 0),
        (1, -1),
        (1, 7),
    ],
)
def test_constructor_rejects_invalid_worker_and_capacity_values(
    worker_count: int,
    outstanding_capacity: int,
) -> None:
    release = threading.Event()
    saver = _BlockingSaver(release)

    with pytest.raises(ValueError):
        BoundedAudioSaveDispatcher(
            saver,
            saver,
            worker_count=worker_count,
            outstanding_capacity=outstanding_capacity,
        )


def test_default_workers_and_total_outstanding_hard_bound() -> None:
    release = threading.Event()
    saver = _BlockingSaver(release)
    dispatcher = BoundedAudioSaveDispatcher(saver, saver)
    outcomes: list[AudioSaveOutcome] = []
    requests = [_captured_request(value) for value in range(7)]

    try:
        submissions = [
            dispatcher.try_submit(request, outcomes.append)
            for request in requests[:6]
        ]
        saturated = dispatcher.try_submit(requests[6], outcomes.append)
        _wait_until(lambda: len(saver.requests) == 2)

        assert len(dispatcher._workers) == 2
        assert all(
            submission.status is SaveSubmitStatus.ACCEPTED
            for submission in submissions
        )
        assert saturated.status is SaveSubmitStatus.SATURATED
        assert saturated.run_id == requests[6].run_id
        assert saturated.save_job_id == requests[6].save_job_id
        assert saturated.source_kind is requests[6].source_kind
        assert saturated.source_identity == requests[6].source_identity
        assert saturated.reason
        assert requests[6].save_job_id not in {
            request.save_job_id for request in saver.requests
        }
    finally:
        release.set()

    _wait_until(lambda: len(outcomes) == 6)
    assert len(outcomes) == 6
    assert len(saver.worker_ids) == 2
    assert saver.worker_daemon_values == {True}


def test_terminal_jobs_release_capacity_exactly_once() -> None:
    first_release = threading.Event()
    saver = _BlockingSaver(first_release)
    dispatcher = BoundedAudioSaveDispatcher(saver, saver)
    first_outcomes: list[AudioSaveOutcome] = []
    second_outcomes: list[AudioSaveOutcome] = []

    try:
        for value in range(6):
            assert (
                dispatcher.try_submit(
                    _captured_request(value),
                    first_outcomes.append,
                ).status
                is SaveSubmitStatus.ACCEPTED
            )
        first_release.set()
        _wait_until(lambda: len(first_outcomes) == 6)

        second_release = threading.Event()
        saver.release = second_release
        for value in range(10, 16):
            assert (
                dispatcher.try_submit(
                    _captured_request(value),
                    second_outcomes.append,
                ).status
                is SaveSubmitStatus.ACCEPTED
            )
        assert (
            dispatcher.try_submit(
                _captured_request(16),
                second_outcomes.append,
            ).status
            is SaveSubmitStatus.SATURATED
        )
        second_release.set()
    finally:
        first_release.set()
        if "second_release" in locals():
            second_release.set()

    _wait_until(lambda: len(second_outcomes) == 6)
    assert len(second_outcomes) == 6


class _RoutingSaver:
    def __init__(self) -> None:
        self.captured_requests: list[CapturedAudioSaveRequest] = []
        self.imported_requests: list[ImportedAudioSaveRequest] = []
        self.tokens = []

    def save_captured(self, request, cancel_token):
        self.captured_requests.append(request)
        self.tokens.append(cancel_token)
        return _saved(request)

    def save_imported(self, request, cancel_token):
        self.imported_requests.append(request)
        self.tokens.append(cancel_token)
        return _saved(request)


def test_routes_each_closed_request_only_to_its_exact_saver() -> None:
    captured_saver = _RoutingSaver()
    imported_saver = _RoutingSaver()
    dispatcher = BoundedAudioSaveDispatcher(captured_saver, imported_saver)
    captured = _captured_request(20)
    imported = _imported_request(21)
    outcomes: list[AudioSaveOutcome] = []

    captured_submission = dispatcher.try_submit(captured, outcomes.append)
    imported_submission = dispatcher.try_submit(imported, outcomes.append)
    _wait_until(lambda: len(outcomes) == 2)

    assert captured_saver.captured_requests == [captured]
    assert captured_saver.imported_requests == []
    assert imported_saver.captured_requests == []
    assert imported_saver.imported_requests == [imported]
    assert captured_submission == dispatcher._submission(
        captured,
        SaveSubmitStatus.ACCEPTED,
        "",
    )
    assert imported_submission == dispatcher._submission(
        imported,
        SaveSubmitStatus.ACCEPTED,
        "",
    )
    assert {
        (
            outcome.save_job_id,
            outcome.run_id,
            outcome.source_kind,
            outcome.source_identity,
        )
        for outcome in outcomes
    } == {
        (
            captured.save_job_id,
            captured.run_id,
            captured.source_kind,
            captured.source_identity,
        ),
        (
            imported.save_job_id,
            imported.run_id,
            imported.source_kind,
            imported.source_identity,
        ),
    }


def test_sink_may_finish_before_submitter_observes_receipt() -> None:
    saver = _RoutingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    request = _captured_request(22)
    callback_finished = threading.Event()
    allow_receipt_observation = threading.Event()
    receipt_observed = threading.Event()
    submissions = []

    def sink(outcome: AudioSaveOutcome) -> None:
        assert outcome.save_job_id == request.save_job_id
        callback_finished.set()

    def submit() -> None:
        submission = dispatcher.try_submit(request, sink)
        assert allow_receipt_observation.wait(2)
        submissions.append(submission)
        receipt_observed.set()

    submitter = threading.Thread(target=submit)
    submitter.start()
    assert callback_finished.wait(2)
    assert not receipt_observed.is_set()
    allow_receipt_observation.set()
    submitter.join(2)

    assert not submitter.is_alive()
    assert submissions[0].status is SaveSubmitStatus.ACCEPTED


def test_mismatched_saver_outcome_becomes_one_identity_preserving_failure() -> None:
    request = _captured_request(23)
    forged_request = _captured_request(24)

    class MismatchingSaver(_RoutingSaver):
        def save_captured(self, request, cancel_token):
            return AudioSaveOutcome(
                save_job_id=forged_request.save_job_id,
                run_id=forged_request.run_id,
                source_kind=forged_request.source_kind,
                source_identity=forged_request.source_identity,
                status=AudioSaveStatus.FAILED,
                persisted_audio=None,
                error_category=TerminalErrorCategory.AUDIO_SAVE_FAILED,
                reason="forged",
            )

    saver = MismatchingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    outcomes: list[AudioSaveOutcome] = []

    assert (
        dispatcher.try_submit(request, outcomes.append).status
        is SaveSubmitStatus.ACCEPTED
    )
    _wait_until(lambda: len(outcomes) == 1)

    assert outcomes == [
        AudioSaveOutcome(
            save_job_id=request.save_job_id,
            run_id=request.run_id,
            source_kind=request.source_kind,
            source_identity=request.source_identity,
            status=AudioSaveStatus.FAILED,
            persisted_audio=None,
            error_category=TerminalErrorCategory.AUDIO_SAVE_FAILED,
            reason="saver outcome identity mismatch",
        )
    ]


def test_mismatched_saved_metadata_becomes_identity_preserving_failure() -> None:
    request = _captured_request(25)

    class MismatchingSaver(_RoutingSaver):
        def save_captured(self, request, cancel_token):
            outcome = _saved(request)
            persisted = outcome.persisted_audio
            assert persisted is not None
            return AudioSaveOutcome(
                save_job_id=outcome.save_job_id,
                run_id=outcome.run_id,
                source_kind=outcome.source_kind,
                source_identity=outcome.source_identity,
                status=AudioSaveStatus.SAVED,
                persisted_audio=PersistedAudioReference(
                    audio_id=persisted.audio_id,
                    save_job_id=persisted.save_job_id,
                    run_id=persisted.run_id,
                    source_kind=persisted.source_kind,
                    source_identity=persisted.source_identity,
                    final_path=persisted.final_path,
                    sample_rate_hz=44_100,
                    channel_count=persisted.channel_count,
                    frame_count=persisted.frame_count,
                    byte_size=persisted.byte_size,
                    format=persisted.format,
                    content_revision=persisted.content_revision,
                ),
                error_category=None,
                reason="",
            )

    saver = MismatchingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    outcomes: list[AudioSaveOutcome] = []

    dispatcher.try_submit(request, outcomes.append)
    _wait_until(lambda: len(outcomes) == 1)

    assert outcomes[0].status is AudioSaveStatus.FAILED
    assert outcomes[0].save_job_id == request.save_job_id
    assert outcomes[0].run_id == request.run_id
    assert outcomes[0].source_kind is request.source_kind
    assert outcomes[0].source_identity == request.source_identity


def test_saver_ordinary_exception_becomes_one_bounded_failure() -> None:
    request = _captured_request(26)

    class RaisingSaver(_RoutingSaver):
        def save_captured(self, request, cancel_token):
            raise RuntimeError("保存失败" * 300)

    saver = RaisingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    outcomes: list[AudioSaveOutcome] = []

    dispatcher.try_submit(request, outcomes.append)
    _wait_until(lambda: len(outcomes) == 1)

    outcome = outcomes[0]
    assert outcome.status is AudioSaveStatus.FAILED
    assert outcome.save_job_id == request.save_job_id
    assert outcome.run_id == request.run_id
    assert outcome.source_kind is request.source_kind
    assert outcome.source_identity == request.source_identity
    assert outcome.error_category is TerminalErrorCategory.AUDIO_SAVE_FAILED
    assert 0 < len(outcome.reason.encode("utf-8")) <= 512


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("string conversion failed")


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(
            lambda: RuntimeError("before\x00after"),
            id="nul",
        ),
        pytest.param(
            lambda: RuntimeError("before\ud800after"),
            id="lone-surrogate",
        ),
        pytest.param(
            lambda: RuntimeError("保存失败" * 300),
            id="overlong-multibyte",
        ),
        pytest.param(
            _UnprintableError,
            id="str-raises",
        ),
    ],
)
def test_hostile_saver_exception_is_sanitized_and_worker_continues(
    error_factory,
) -> None:
    first = _captured_request(260)
    second = _captured_request(261)

    class RaiseOnceSaver(_RoutingSaver):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def save_captured(self, request, cancel_token):
            self.call_count += 1
            if self.call_count == 1:
                raise error_factory()
            return _saved(request)

    saver = RaiseOnceSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    first_outcomes: list[AudioSaveOutcome] = []
    second_outcomes: list[AudioSaveOutcome] = []

    dispatcher.try_submit(first, first_outcomes.append)
    _wait_until(lambda: len(first_outcomes) == 1)
    assert (
        dispatcher.try_submit(second, second_outcomes.append).status
        is SaveSubmitStatus.ACCEPTED
    )
    _wait_until(lambda: len(second_outcomes) == 1)
    report = dispatcher.shutdown(time.monotonic() + 2)

    failure = first_outcomes[0]
    assert failure.status is AudioSaveStatus.FAILED
    assert failure.save_job_id == first.save_job_id
    assert failure.run_id == first.run_id
    assert failure.source_kind is first.source_kind
    assert failure.source_identity == first.source_identity
    assert failure.reason.startswith("save_saver_exception:")
    assert "\x00" not in failure.reason
    assert len(failure.reason.encode("utf-8")) <= 512
    assert second_outcomes == [_saved(second)]
    assert report.status is SaveDispatcherShutdownStatus.CLOSED
    assert report.errors
    assert report.errors[0].startswith("save_saver_exception:")
    assert "\x00" not in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512


def test_sink_ordinary_exception_isolated_and_worker_accepts_later_job() -> None:
    saver = _RoutingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    first = _captured_request(27)
    second = _captured_request(28)
    second_outcomes: list[AudioSaveOutcome] = []
    first_called = threading.Event()

    def raising_sink(outcome: AudioSaveOutcome) -> None:
        first_called.set()
        raise RuntimeError("sink failed")

    dispatcher.try_submit(first, raising_sink)
    assert first_called.wait(2)
    assert (
        dispatcher.try_submit(second, second_outcomes.append).status
        is SaveSubmitStatus.ACCEPTED
    )
    _wait_until(lambda: len(second_outcomes) == 1)

    assert second_outcomes[0].save_job_id == second.save_job_id


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(
            lambda: RuntimeError("before\x00after"),
            id="nul",
        ),
        pytest.param(
            lambda: RuntimeError("before\ud800after"),
            id="lone-surrogate",
        ),
        pytest.param(
            lambda: RuntimeError("回调失败" * 300),
            id="overlong-multibyte",
        ),
        pytest.param(
            _UnprintableError,
            id="str-raises",
        ),
    ],
)
def test_hostile_sink_exception_is_sanitized_and_worker_continues(
    error_factory,
) -> None:
    saver = _RoutingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    first = _captured_request(270)
    second = _captured_request(271)
    first_called = threading.Event()
    second_outcomes: list[AudioSaveOutcome] = []

    def hostile_sink(outcome: AudioSaveOutcome) -> None:
        first_called.set()
        raise error_factory()

    dispatcher.try_submit(first, hostile_sink)
    assert first_called.wait(2)
    assert (
        dispatcher.try_submit(second, second_outcomes.append).status
        is SaveSubmitStatus.ACCEPTED
    )
    _wait_until(lambda: len(second_outcomes) == 1)
    report = dispatcher.shutdown(time.monotonic() + 2)

    assert second_outcomes == [_saved(second)]
    assert report.status is SaveDispatcherShutdownStatus.CLOSED
    assert report.errors
    assert report.errors[0].startswith("save_sink_exception:")
    assert "\x00" not in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512


class _TokenObservingSaver:
    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self._lock = threading.Lock()
        self.entered = threading.Event()
        self.tokens_by_job_id = {}
        self.calls: list[str] = []

    def _save(self, request, cancel_token):
        with self._lock:
            self.calls.append(request.save_job_id)
            self.tokens_by_job_id[request.save_job_id] = cancel_token
            self.entered.set()
        assert self.release.wait(2)
        return _saved(request)

    def save_captured(self, request, cancel_token):
        return self._save(request, cancel_token)

    def save_imported(self, request, cancel_token):
        return self._save(request, cancel_token)


def test_cancel_sets_only_the_exact_job_token() -> None:
    release = threading.Event()
    saver = _TokenObservingSaver(release)
    dispatcher = BoundedAudioSaveDispatcher(
        saver,
        saver,
        worker_count=1,
        outstanding_capacity=2,
    )
    first = _captured_request(30)
    second = _captured_request(31)
    outcomes: list[AudioSaveOutcome] = []
    dispatcher.try_submit(first, outcomes.append)
    dispatcher.try_submit(second, outcomes.append)
    assert saver.entered.wait(2)

    wrong_run = dispatcher.cancel(second.run_id, first.save_job_id)
    wrong_job = dispatcher.cancel(second.run_id, _captured_request(32).save_job_id)
    canceled = dispatcher.cancel(second.run_id, second.save_job_id)

    assert wrong_run.status is SaveCancelStatus.NOT_FOUND
    assert wrong_job.status is SaveCancelStatus.NOT_FOUND
    assert canceled.status is SaveCancelStatus.CANCEL_REQUESTED
    assert (
        saver.tokens_by_job_id[first.save_job_id].is_cancel_requested()
        is False
    )
    release.set()
    _wait_until(lambda: second.save_job_id in saver.tokens_by_job_id)
    assert saver.tokens_by_job_id[second.save_job_id].is_cancel_requested()
    _wait_until(lambda: len(outcomes) == 2)


def test_cancel_of_terminal_job_returns_already_terminal() -> None:
    saver = _RoutingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    request = _captured_request(33)
    outcomes: list[AudioSaveOutcome] = []
    dispatcher.try_submit(request, outcomes.append)
    _wait_until(lambda: len(outcomes) == 1)

    result = dispatcher.cancel(request.run_id, request.save_job_id)

    assert result.status is SaveCancelStatus.ALREADY_TERMINAL
    assert result.run_id == request.run_id
    assert result.save_job_id == request.save_job_id
    assert result.reason == ""


def test_shutdown_closes_admission_before_canceling_current_tokens() -> None:
    cancel_observed = threading.Event()
    release = threading.Event()
    closed_submissions = []
    request = _captured_request(34)
    later = _captured_request(35)

    class AdmissionCheckingSaver(_RoutingSaver):
        def save_captured(self, request, cancel_token):
            assert cancel_token._event.wait(2)
            closed_submissions.append(
                dispatcher.try_submit(later, lambda outcome: None)
            )
            cancel_observed.set()
            assert release.wait(2)
            return _saved(request)

    saver = AdmissionCheckingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    dispatcher.try_submit(request, lambda outcome: None)
    reports = []

    shutdown_thread = threading.Thread(
        target=lambda: reports.append(
            dispatcher.shutdown(time.monotonic() + 2)
        )
    )
    shutdown_thread.start()
    assert cancel_observed.wait(2)
    release.set()
    shutdown_thread.join(2)

    assert not shutdown_thread.is_alive()
    assert closed_submissions[0].status is SaveSubmitStatus.CLOSED
    assert reports[0].status is SaveDispatcherShutdownStatus.CLOSED


def test_shutdown_cancels_all_jobs_and_joins_workers_before_deadline() -> None:
    release = threading.Event()
    saver = _TokenObservingSaver(release)
    dispatcher = BoundedAudioSaveDispatcher(
        saver,
        saver,
        worker_count=2,
        outstanding_capacity=2,
    )
    requests = [_captured_request(36), _imported_request(37)]
    outcomes: list[AudioSaveOutcome] = []
    for request in requests:
        dispatcher.try_submit(request, outcomes.append)
    _wait_until(lambda: len(saver.tokens_by_job_id) == 2)
    reports = []

    shutdown_thread = threading.Thread(
        target=lambda: reports.append(
            dispatcher.shutdown(time.monotonic() + 2)
        )
    )
    shutdown_thread.start()
    _wait_until(
        lambda: all(
            token.is_cancel_requested()
            for token in saver.tokens_by_job_id.values()
        )
    )
    release.set()
    shutdown_thread.join(2)

    assert not shutdown_thread.is_alive()
    report = reports[0]
    assert report.status is SaveDispatcherShutdownStatus.CLOSED
    assert report.accepted_count == 2
    assert report.canceled_count == 2
    assert report.completed_count == 2
    assert report.remaining_save_job_ids == ()
    assert report.live_worker_ids == ()


def test_blocked_saver_returns_immediate_exact_incomplete_report() -> None:
    release = threading.Event()
    saver = _TokenObservingSaver(release)
    dispatcher = BoundedAudioSaveDispatcher(
        saver,
        saver,
        worker_count=1,
        outstanding_capacity=2,
    )
    requests = [_captured_request(38), _captured_request(39)]
    for request in requests:
        dispatcher.try_submit(request, lambda outcome: None)
    assert saver.entered.wait(2)

    report = dispatcher.shutdown(time.monotonic())

    assert report.status is SaveDispatcherShutdownStatus.INCOMPLETE
    assert report.accepted_count == 2
    assert report.canceled_count == 2
    assert report.completed_count == 0
    assert report.remaining_save_job_ids == tuple(
        sorted(request.save_job_id for request in requests)
    )
    assert report.live_worker_ids == ("audio-save-worker-1",)
    assert (
        dispatcher.try_submit(
            _captured_request(40),
            lambda outcome: None,
        ).status
        is SaveSubmitStatus.CLOSED
    )
    release.set()
    final_report = dispatcher.shutdown(time.monotonic() + 2)
    assert final_report.status is SaveDispatcherShutdownStatus.CLOSED


def test_repeated_shutdown_reports_current_state_without_new_workers_or_retries() -> None:
    release = threading.Event()
    saver = _TokenObservingSaver(release)
    dispatcher = BoundedAudioSaveDispatcher(
        saver,
        saver,
        worker_count=1,
        outstanding_capacity=1,
    )
    request = _captured_request(41)
    dispatcher.try_submit(request, lambda outcome: None)
    assert saver.entered.wait(2)
    worker_objects = dispatcher._workers

    first = dispatcher.shutdown(time.monotonic())
    second = dispatcher.shutdown(time.monotonic())

    assert first == second
    assert dispatcher._workers is worker_objects
    assert saver.calls == [request.save_job_id]

    release.set()
    third = dispatcher.shutdown(time.monotonic() + 2)
    fourth = dispatcher.shutdown(time.monotonic() + 2)
    assert third.status is SaveDispatcherShutdownStatus.CLOSED
    assert fourth == third
    assert saver.calls == [request.save_job_id]


@pytest.mark.parametrize(
    "invalid_deadline",
    [True, False, math.nan, math.inf, -math.inf],
)
def test_invalid_shutdown_deadline_does_not_change_admission(
    invalid_deadline: float,
) -> None:
    saver = _RoutingSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)

    with pytest.raises(ValueError):
        dispatcher.shutdown(invalid_deadline)

    request = _captured_request(42)
    outcomes: list[AudioSaveOutcome] = []
    assert (
        dispatcher.try_submit(request, outcomes.append).status
        is SaveSubmitStatus.ACCEPTED
    )
    _wait_until(lambda: len(outcomes) == 1)
    assert (
        dispatcher.shutdown(time.monotonic() + 2).status
        is SaveDispatcherShutdownStatus.CLOSED
    )


def test_finite_past_shutdown_deadline_is_valid_and_nonblocking() -> None:
    release = threading.Event()
    saver = _TokenObservingSaver(release)
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    request = _captured_request(43)
    dispatcher.try_submit(request, lambda outcome: None)
    assert saver.entered.wait(2)

    started = time.monotonic()
    report = dispatcher.shutdown(started - 1)
    elapsed = time.monotonic() - started

    assert report.status is SaveDispatcherShutdownStatus.INCOMPLETE
    assert elapsed < 0.25
    release.set()
    assert (
        dispatcher.shutdown(time.monotonic() + 2).status
        is SaveDispatcherShutdownStatus.CLOSED
    )


def test_huge_finite_shutdown_deadline_uses_platform_safe_join_slices() -> None:
    entered = threading.Event()
    cancel_observed = threading.Event()
    release = threading.Event()

    class CancelAwareSaver(_RoutingSaver):
        def save_captured(self, request, cancel_token):
            entered.set()
            assert cancel_token._event.wait(2)
            cancel_observed.set()
            assert release.wait(2)
            return _saved(request)

    saver = CancelAwareSaver()
    dispatcher = BoundedAudioSaveDispatcher(saver, saver, worker_count=1)
    request = _captured_request(280)
    dispatcher.try_submit(request, lambda outcome: None)
    assert entered.wait(2)
    reports = []
    errors = []

    def shut_down() -> None:
        try:
            reports.append(dispatcher.shutdown(1e308))
        except Exception as exc:
            errors.append(exc)

    shutdown_thread = threading.Thread(target=shut_down)
    shutdown_thread.start()
    assert cancel_observed.wait(2)
    release.set()
    shutdown_thread.join(2)

    assert not shutdown_thread.is_alive()
    assert errors == []
    assert len(reports) == 1
    assert reports[0].status is SaveDispatcherShutdownStatus.CLOSED
