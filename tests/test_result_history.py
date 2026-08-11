from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, fields, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock, get_ident

import numpy as np
import pytest

from app.service import result_history
from app.service.result_history import (
    ActiveRunChanged,
    ActiveRunRemoved,
    ConfigSummaryField,
    ErrorDisplayPayload,
    FrozenConfigSummary,
    ResultConclusion,
    ResultHistoryCardSnapshot,
    ResultHistoryError,
    ResultHistorySnapshot,
    ResultHistoryStore,
    ResultHistoryTransition,
    ResultPhase,
    ReviewDisplayPayload,
    ReanalysisCancelled,
    ReanalysisCompleted,
    ReanalysisStarted,
    RunTerminated,
    TerminalResultReplaced,
)


NOW = datetime(2026, 7, 26, 8, 30, tzinfo=timezone.utc)


class MutableHashString(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.hash_salt = 0
        return instance

    def __hash__(self):
        return hash((str(self), self.hash_salt))


class TupleSubclass(tuple):
    pass


class DateTimeSubclass(datetime):
    pass


class MutableOffsetTimezone(tzinfo):
    def __init__(self, offset: timedelta):
        self.offset = offset
        self.raise_on_offset = False
        self.offset_calls = 0

    def utcoffset(self, dt):
        self.offset_calls += 1
        if self.raise_on_offset:
            raise RuntimeError("offset unavailable")
        return self.offset

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "mutable"


class NoneOffsetTimezone(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


class RaisingOffsetTimezone(tzinfo):
    def utcoffset(self, dt):
        raise RuntimeError("broken offset")

    def dst(self, dt):
        return timedelta(0)


class PausingRLock:
    def __init__(self):
        self._lock = RLock()
        self.writer_acquired = Event()
        self.release_writer = Event()
        self.pause_next_acquire = True

    def acquire(self, *args, **kwargs):
        acquired = self._lock.acquire(*args, **kwargs)
        if acquired and self.pause_next_acquire:
            self.pause_next_acquire = False
            self.writer_acquired.set()
            assert self.release_writer.wait(timeout=2)
        return acquired

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


class FinalizerObservation:
    def __init__(self, store: ResultHistoryStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self.armed = False
        self.calls = []
        self.errors = []
        self.requested = Event()
        self.completed = Event()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = self.executor.submit(self._observe_when_requested)

    def _observe_when_requested(self):
        if not self.requested.wait(timeout=2):
            raise AssertionError("finalizer did not request a public observation")
        try:
            self.calls.append(
                (self.store.snapshot(), self.store.display_payload(self.run_id))
            )
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            self.completed.set()

    def observe(self):
        if not self.armed:
            return
        self.requested.set()
        if not self.completed.wait(timeout=2):
            self.errors.append(
                TimeoutError("public observation was blocked by the store lock")
            )

    def close(self):
        self.future.result(timeout=2)
        self.executor.shutdown()


class NestedFinalizerProbe:
    def __init__(self, observation: FinalizerObservation, generation: int = 0):
        self.observation = observation
        self.generation = generation

    def __deepcopy__(self, memo):
        return NestedFinalizerProbe(self.observation, self.generation + 1)

    def __del__(self):
        if self.generation == 1:
            self.observation.observe()


def finalizing_review_payload(
    observation: FinalizerObservation,
) -> ReviewDisplayPayload:
    return ReviewDisplayPayload(
        review={"probe": NestedFinalizerProbe(observation)},
        dashboard_config={"version": 1},
    )


@dataclass(frozen=True)
class MutableConfigSummary(FrozenConfigSummary):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableConfigSummaryField(ConfigSummaryField):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableCardSnapshot(ResultHistoryCardSnapshot):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableReviewDisplayPayload(ReviewDisplayPayload):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableErrorDisplayPayload(ErrorDisplayPayload):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableActiveRunChanged(ActiveRunChanged):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableRunTerminated(RunTerminated):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableTerminalResultReplaced(TerminalResultReplaced):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableReanalysisStarted(ReanalysisStarted):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableReanalysisCompleted(ReanalysisCompleted):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableReanalysisCancelled(ReanalysisCancelled):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableActiveRunRemoved(ActiveRunRemoved):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableHistorySnapshot(ResultHistorySnapshot):
    mutable_state: object = None


@dataclass(frozen=True)
class MutableHistoryTransition(ResultHistoryTransition):
    mutable_state: object = None


def config_summary(config_id: int | None = 7) -> FrozenConfigSummary:
    return FrozenConfigSummary(
        config_id=config_id,
        name="配置 A",
        fault_types=("异响", "漏装"),
        details=(
            ConfigSummaryField("硬件", "麦克风 1"),
            ConfigSummaryField("窗口", "0.0–2.0 s"),
        ),
    )


def card(
    run_id: str,
    *,
    phase: ResultPhase = ResultPhase.RECORDING,
    conclusion: ResultConclusion | None = None,
    terminal: bool = False,
    revision: int = 0,
    reanalysis_allowed: bool = True,
    sn: str = "SN-001",
) -> ResultHistoryCardSnapshot:
    return ResultHistoryCardSnapshot(
        run_id=run_id,
        config=config_summary(),
        sn=sn,
        trigger_source="plc",
        started_at=NOW,
        terminated_at=NOW if terminal else None,
        phase=phase,
        conclusion=conclusion,
        reason=None,
        is_active_run=not terminal,
        manual_judgment_allowed=terminal,
        manual_reanalysis_allowed=terminal and reanalysis_allowed,
        revision=revision,
    )


def active_event(run_id: str) -> ActiveRunChanged:
    return ActiveRunChanged(card=card(run_id))


def finished_card(
    run_id: str,
    *,
    conclusion: ResultConclusion = ResultConclusion.OK,
    revision: int = 0,
) -> ResultHistoryCardSnapshot:
    return card(
        run_id,
        phase=ResultPhase.TERMINAL,
        conclusion=conclusion,
        terminal=True,
        revision=revision,
    )


def mutable_card_subclass(run_id: str = "run-mutable") -> MutableCardSnapshot:
    base = card(run_id)
    values = {
        item.name: getattr(base, item.name)
        for item in fields(ResultHistoryCardSnapshot)
    }
    return MutableCardSnapshot(**values, mutable_state=[])


def unsafe_mutable_card_subclass(
    run_id: str = "run-mutable",
) -> MutableCardSnapshot:
    return unsafe_dataclass_subclass(
        card(run_id),
        MutableCardSnapshot,
        mutable_state=[],
    )


def unsafe_dataclass_subclass(base, subclass, **extra):
    value = object.__new__(subclass)
    for item in fields(base):
        object.__setattr__(value, item.name, getattr(base, item.name))
    for name, field_value in extra.items():
        object.__setattr__(value, name, field_value)
    return value


def history_snapshot(**changes) -> ResultHistorySnapshot:
    values = {
        "revision": 0,
        "follow_latest": True,
        "selected_run_id": None,
        "active_cards": (),
        "terminal_cards": (),
    }
    values.update(changes)
    return ResultHistorySnapshot(**values)


def review_payload(value: float = 1.0) -> ReviewDisplayPayload:
    return ReviewDisplayPayload(
        review={
            "chart_payload": {
                "series": [value, value + 1],
                "samples": np.array([value, value + 2]),
            }
        },
        dashboard_config={"version": 1, "panels": [{"id": "spectrum"}]},
    )


def error_payload(reason: str = "分析失败") -> ErrorDisplayPayload:
    return ErrorDisplayPayload(
        error_category="analysis",
        reason=reason,
        context=(ConfigSummaryField("入口", "自动测试"),),
    )


def terminal_event(
    run_id: str,
    *,
    conclusion: ResultConclusion = ResultConclusion.OK,
    review: ReviewDisplayPayload | None = None,
    error: ErrorDisplayPayload | None = None,
) -> RunTerminated:
    return RunTerminated(
        card=card(
            run_id,
            phase=ResultPhase.TERMINAL,
            conclusion=conclusion,
            terminal=True,
        ),
        review_payload=review,
        error_payload=error,
    )


def test_run_terminated_identity_exists_only_on_card_without_alias():
    event = terminal_event("run-identity", review=review_payload())

    assert event.card.run_id == "run-identity"
    with pytest.raises(AttributeError):
        getattr(event, "run_id")


def test_initial_snapshot_is_empty_locked_and_immutable():
    snapshot = ResultHistoryStore().snapshot()

    assert snapshot.revision == 0
    assert snapshot.follow_latest is True
    assert snapshot.selected_run_id is None
    assert snapshot.active_cards == ()
    assert snapshot.terminal_cards == ()
    with pytest.raises(FrozenInstanceError):
        snapshot.follow_latest = False


@pytest.mark.parametrize(
    ("value_name", "build_value"),
    [
        (
            "ConfigSummaryField",
            lambda: MutableConfigSummaryField("label", "value", mutable_state=[]),
        ),
        (
            "FrozenConfigSummary",
            lambda: MutableConfigSummary(
                config_id=1,
                name="config",
                mutable_state=[],
            ),
        ),
        ("ResultHistoryCardSnapshot", mutable_card_subclass),
        (
            "ReviewDisplayPayload",
            lambda: MutableReviewDisplayPayload(
                review={},
                dashboard_config={},
                mutable_state=[],
            ),
        ),
        (
            "ErrorDisplayPayload",
            lambda: MutableErrorDisplayPayload(
                error_category="error",
                reason="reason",
                mutable_state=[],
            ),
        ),
        (
            "ActiveRunChanged",
            lambda: MutableActiveRunChanged(card("run-1"), mutable_state=[]),
        ),
        (
            "RunTerminated",
            lambda: MutableRunTerminated(
                finished_card("run-1"),
                review_payload=review_payload(),
                mutable_state=[],
            ),
        ),
        (
            "TerminalResultReplaced",
            lambda: MutableTerminalResultReplaced(
                "run-1",
                0,
                finished_card("run-1"),
                review_payload=review_payload(),
                mutable_state=[],
            ),
        ),
        (
            "ReanalysisStarted",
            lambda: MutableReanalysisStarted(
                "run-1",
                "operation-1",
                0,
                mutable_state=[],
            ),
        ),
        (
            "ReanalysisCompleted",
            lambda: MutableReanalysisCompleted(
                "run-1",
                "operation-1",
                1,
                finished_card("run-1", revision=1),
                review_payload=review_payload(),
                mutable_state=[],
            ),
        ),
        (
            "ReanalysisCancelled",
            lambda: MutableReanalysisCancelled(
                "run-1",
                "operation-1",
                1,
                mutable_state=[],
            ),
        ),
        (
            "ActiveRunRemoved",
            lambda: MutableActiveRunRemoved("run-1", mutable_state=[]),
        ),
        (
            "ResultHistorySnapshot",
            lambda: MutableHistorySnapshot(
                revision=0,
                follow_latest=True,
                selected_run_id=None,
                active_cards=(),
                terminal_cards=(),
                mutable_state=[],
            ),
        ),
        (
            "ResultHistoryTransition",
            lambda: MutableHistoryTransition(
                snapshot=ResultHistorySnapshot(0, True, None, (), ()),
                mutable_state=[],
            ),
        ),
    ],
)
def test_public_dataclass_subclasses_are_rejected(value_name, build_value):
    with pytest.raises(ResultHistoryError, match=f"exact {value_name}"):
        build_value()


def test_store_apply_dispatch_rejects_event_subclass_before_mutation():
    store = ResultHistoryStore()
    base_event = active_event("run-1")
    subclass_event = unsafe_dataclass_subclass(
        base_event,
        MutableActiveRunChanged,
        mutable_state=[],
    )

    with pytest.raises(ResultHistoryError, match="exact event"):
        store.apply(subclass_event)

    assert store.snapshot() == ResultHistorySnapshot(0, True, None, (), ())


@pytest.mark.parametrize(
    ("message", "build_event"),
    [
        ("card", lambda: ActiveRunChanged(object())),
        (
            "card",
            lambda: RunTerminated(
                object(),
                review_payload=review_payload(),
            ),
        ),
        (
            "payload",
            lambda: RunTerminated(
                finished_card("run-1"),
                review_payload=object(),
            ),
        ),
        (
            "run_id",
            lambda: TerminalResultReplaced(
                MutableHashString("run-1"),
                0,
                finished_card("run-1"),
                review_payload=review_payload(),
            ),
        ),
        (
            "expected_revision",
            lambda: TerminalResultReplaced(
                "run-1",
                True,
                finished_card("run-1"),
                review_payload=review_payload(),
            ),
        ),
        (
            "operation_id",
            lambda: ReanalysisStarted(
                "run-1",
                MutableHashString("operation-1"),
                0,
            ),
        ),
        (
            "card",
            lambda: ReanalysisCompleted(
                "run-1",
                "operation-1",
                1,
                object(),
                review_payload=review_payload(),
            ),
        ),
        (
            "run_id",
            lambda: ActiveRunRemoved(MutableHashString("run-1")),
        ),
    ],
)
def test_event_constructors_reject_duck_or_inexact_ingress_values(
    message,
    build_event,
):
    with pytest.raises(ResultHistoryError, match=message):
        build_event()


def test_event_constructor_rejects_payload_wrapper_subclass():
    base_payload = review_payload()
    subclass_payload = unsafe_dataclass_subclass(
        base_payload,
        MutableReviewDisplayPayload,
        mutable_state=[],
    )

    with pytest.raises(ResultHistoryError, match="payload"):
        RunTerminated(
            finished_card("run-1"),
            review_payload=subclass_payload,
        )


def test_card_subclass_is_rejected_before_storage_replacement_or_reanalysis():
    active_subclass = unsafe_mutable_card_subclass("run-1")
    terminal_subclass = unsafe_dataclass_subclass(
        finished_card("run-1"),
        MutableCardSnapshot,
        mutable_state=[],
    )
    event_builders = (
        lambda: ActiveRunChanged(active_subclass),
        lambda: RunTerminated(
            terminal_subclass,
            review_payload=review_payload(),
        ),
        lambda: TerminalResultReplaced(
            "run-1",
            0,
            terminal_subclass,
            review_payload=review_payload(),
        ),
        lambda: ReanalysisCompleted(
            "run-1",
            "operation-1",
            1,
            terminal_subclass,
            review_payload=review_payload(),
        ),
    )

    for build_event in event_builders:
        with pytest.raises(ResultHistoryError, match="card"):
            build_event()


@pytest.mark.parametrize(
    ("message", "build_snapshot"),
    [
        ("revision", lambda: history_snapshot(revision=True)),
        ("revision", lambda: history_snapshot(revision=-1)),
        ("follow_latest", lambda: history_snapshot(follow_latest=1)),
        (
            "selected_run_id",
            lambda: history_snapshot(
                follow_latest=False,
                selected_run_id=MutableHashString("run-1"),
            ),
        ),
        ("active_cards", lambda: history_snapshot(active_cards=[])),
        (
            "terminal_cards",
            lambda: history_snapshot(terminal_cards=TupleSubclass()),
        ),
        (
            "active_cards",
            lambda: history_snapshot(active_cards=(object(),)),
        ),
        (
            "terminal_cards",
            lambda: history_snapshot(terminal_cards=(object(),)),
        ),
        (
            "active_cards",
            lambda: history_snapshot(
                active_cards=(unsafe_mutable_card_subclass(),),
            ),
        ),
        (
            "duplicate",
            lambda: history_snapshot(
                follow_latest=False,
                active_cards=(card("run-1"), card("run-1")),
            ),
        ),
        (
            "duplicate",
            lambda: history_snapshot(
                follow_latest=False,
                active_cards=(card("run-1"),),
                terminal_cards=(finished_card("run-1"),),
            ),
        ),
        (
            "six",
            lambda: history_snapshot(
                follow_latest=False,
                terminal_cards=tuple(
                    finished_card(f"run-{index}") for index in range(7)
                ),
            ),
        ),
        (
            "active zone",
            lambda: history_snapshot(
                follow_latest=False,
                active_cards=(finished_card("run-1"),),
            ),
        ),
        (
            "terminal zone",
            lambda: history_snapshot(
                follow_latest=False,
                terminal_cards=(card("run-1"),),
            ),
        ),
        (
            "dangling",
            lambda: history_snapshot(
                follow_latest=False,
                selected_run_id="missing",
            ),
        ),
        (
            "locked",
            lambda: history_snapshot(
                follow_latest=True,
                selected_run_id=None,
                terminal_cards=(finished_card("run-1"),),
            ),
        ),
    ],
)
def test_snapshot_constructor_rejects_invalid_graphs(message, build_snapshot):
    with pytest.raises(ResultHistoryError, match=message):
        build_snapshot()


def test_snapshot_constructor_accepts_valid_locked_and_unlocked_graphs():
    latest = finished_card("run-2")
    older = finished_card("run-1")
    locked = history_snapshot(
        revision=2,
        follow_latest=True,
        selected_run_id="run-2",
        terminal_cards=(latest, older),
    )
    unlocked_active = history_snapshot(
        revision=3,
        follow_latest=False,
        selected_run_id="run-active",
        active_cards=(card("run-active"),),
        terminal_cards=(latest, older),
    )
    locked_active = history_snapshot(
        revision=4,
        follow_latest=True,
        selected_run_id="run-active",
        active_cards=(card("run-active"),),
        terminal_cards=(latest, older),
    )
    locked_older = history_snapshot(
        revision=5,
        follow_latest=True,
        selected_run_id="run-1",
        terminal_cards=(latest, older),
    )

    assert locked.selected_run_id == "run-2"
    assert unlocked_active.selected_run_id == "run-active"
    assert locked_active.selected_run_id == "run-active"
    assert locked_older.selected_run_id == "run-1"


@pytest.mark.parametrize(
    ("message", "build_transition"),
    [
        ("snapshot", lambda: ResultHistoryTransition(snapshot=object())),
        (
            "evicted_run_id",
            lambda: ResultHistoryTransition(
                history_snapshot(),
                evicted_run_id=MutableHashString("run-1"),
                changed=True,
            ),
        ),
        (
            "selection_changed",
            lambda: ResultHistoryTransition(
                history_snapshot(),
                selection_changed=1,
                changed=True,
            ),
        ),
        (
            "notice",
            lambda: ResultHistoryTransition(
                history_snapshot(),
                notice=MutableHashString("notice"),
                changed=True,
            ),
        ),
        (
            "changed",
            lambda: ResultHistoryTransition(history_snapshot(), changed=1),
        ),
        (
            "changed",
            lambda: ResultHistoryTransition(
                history_snapshot(),
                selection_changed=True,
                changed=False,
            ),
        ),
        (
            "changed",
            lambda: ResultHistoryTransition(
                history_snapshot(),
                evicted_run_id="run-1",
                changed=False,
            ),
        ),
        (
            "changed",
            lambda: ResultHistoryTransition(
                history_snapshot(),
                notice="notice",
                changed=False,
            ),
        ),
        (
            "evicted",
            lambda: ResultHistoryTransition(
                history_snapshot(
                    follow_latest=False,
                    active_cards=(card("run-1"),),
                ),
                evicted_run_id="run-1",
                changed=True,
            ),
        ),
    ],
)
def test_transition_constructor_rejects_invalid_graphs(message, build_transition):
    with pytest.raises(ResultHistoryError, match=message):
        build_transition()


def test_config_and_card_snapshots_use_only_immutable_public_fields():
    summary = config_summary()
    snapshot_card = card("run-1")

    assert isinstance(summary.details, tuple)
    assert all(isinstance(item, ConfigSummaryField) for item in summary.details)
    assert summary.fault_types == ("异响", "漏装")
    assert {item.name for item in fields(snapshot_card)} == {
        "run_id",
        "config",
        "sn",
        "trigger_source",
        "started_at",
        "terminated_at",
        "phase",
        "conclusion",
        "reason",
        "is_active_run",
        "manual_judgment_allowed",
        "manual_reanalysis_allowed",
            "revision",
            "operation_id",
            "fault_type",
            "manual_judgment_revision",
        }
    with pytest.raises(FrozenInstanceError):
        summary.name = "changed"
    with pytest.raises(FrozenInstanceError):
        snapshot_card.phase = ResultPhase.ANALYZING
    with pytest.raises(ResultHistoryError):
        FrozenConfigSummary(config_id=1, name="bad", details=[])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "message"),
    [
        ("run_id", "", "run_id"),
        ("run_id", [], "run_id"),
        ("sn", [], "sn"),
        ("trigger_source", {}, "trigger_source"),
        ("started_at", "2026-07-26", "started_at"),
        ("terminated_at", [], "terminated_at"),
        ("reason", [], "reason"),
        ("is_active_run", 1, "is_active_run"),
        ("manual_judgment_allowed", 0, "manual_judgment_allowed"),
        ("manual_reanalysis_allowed", np.bool_(True), "manual_reanalysis_allowed"),
        ("revision", True, "revision"),
        ("revision", 1.5, "revision"),
        ("revision", -1, "revision"),
        ("operation_id", [], "operation_id"),
    ],
)
def test_card_constructor_rejects_mutable_or_wrong_primitive_fields(
    field_name,
    invalid_value,
    message,
):
    with pytest.raises(ResultHistoryError, match=message):
        replace(card("run-1"), **{field_name: invalid_value})


def test_related_frozen_values_reject_bool_config_id_and_non_string_error_fields():
    with pytest.raises(ResultHistoryError, match="config_id"):
        FrozenConfigSummary(config_id=True, name="bad")
    with pytest.raises(ResultHistoryError, match="error_category"):
        ErrorDisplayPayload(error_category=[], reason="bad")  # type: ignore[arg-type]
    with pytest.raises(ResultHistoryError, match="reason"):
        ErrorDisplayPayload(error_category="analysis", reason={})  # type: ignore[arg-type]


def test_rejected_mutable_card_input_cannot_enter_or_mutate_a_later_snapshot():
    store = ResultHistoryStore()
    mutable_sn = ["SN-001"]

    with pytest.raises(ResultHistoryError, match="sn"):
        replace(card("run-invalid"), sn=mutable_sn)
    mutable_sn.append("mutated")

    assert store.snapshot().active_cards == ()
    assert store.snapshot().revision == 0


@pytest.mark.parametrize(
    "build_value",
    [
        lambda value: ConfigSummaryField(value, "value"),
        lambda value: ConfigSummaryField("label", value),
        lambda value: FrozenConfigSummary(config_id=1, name=value),
        lambda value: replace(card("run-1"), run_id=value),
        lambda value: replace(card("run-1"), sn=value),
        lambda value: replace(card("run-1"), trigger_source=value),
        lambda value: replace(card("run-1"), reason=value),
        lambda value: replace(card("run-1"), operation_id=value),
        lambda value: ErrorDisplayPayload(error_category=value, reason="reason"),
        lambda value: ErrorDisplayPayload(error_category="error", reason=value),
    ],
)
def test_string_subclasses_are_rejected_from_all_snapshot_metadata(build_value):
    mutable_string = MutableHashString("mutable")

    with pytest.raises(ResultHistoryError):
        build_value(mutable_string)


def test_datetime_tuple_and_nested_value_subclasses_are_rejected():
    datetime_subclass = DateTimeSubclass(
        2026,
        7,
        26,
        8,
        30,
        tzinfo=timezone.utc,
    )
    with pytest.raises(ResultHistoryError, match="started_at"):
        replace(card("run-1"), started_at=datetime_subclass)
    with pytest.raises(ResultHistoryError, match="terminated_at"):
        replace(
            card(
                "run-1",
                phase=ResultPhase.TERMINAL,
                conclusion=ResultConclusion.OK,
                terminal=True,
            ),
            terminated_at=datetime_subclass,
        )
    with pytest.raises(ResultHistoryError, match="details"):
        FrozenConfigSummary(config_id=1, name="bad", details=TupleSubclass())

    mutable_field = unsafe_dataclass_subclass(
        ConfigSummaryField("label", "value"),
        MutableConfigSummaryField,
        mutable_state=[],
    )
    with pytest.raises(ResultHistoryError, match="details"):
        FrozenConfigSummary(
            config_id=1,
            name="bad",
            details=(mutable_field,),
        )
    mutable_field.mutable_state.append("mutated")


def test_naive_datetime_metadata_is_preserved_exactly():
    naive_started_at = datetime(2026, 7, 26, 8, 30)
    naive_terminated_at = datetime(2026, 7, 26, 8, 31)

    snapshot_card = replace(
        card(
            "run-1",
            phase=ResultPhase.TERMINAL,
            conclusion=ResultConclusion.OK,
            terminal=True,
        ),
        started_at=naive_started_at,
        terminated_at=naive_terminated_at,
    )

    assert snapshot_card.started_at is naive_started_at
    assert snapshot_card.terminated_at is naive_terminated_at


def test_mutable_timezone_cannot_change_cached_active_or_terminal_timestamps():
    active_timezone = MutableOffsetTimezone(timedelta(hours=8))
    terminal_timezone = MutableOffsetTimezone(timedelta(hours=2))
    active_started_at = datetime(
        2026, 7, 26, 8, 30, tzinfo=active_timezone
    )
    terminal_started_at = datetime(
        2026, 7, 26, 10, 0, tzinfo=terminal_timezone
    )
    terminal_terminated_at = datetime(
        2026, 7, 26, 11, 0, tzinfo=terminal_timezone
    )
    active_card = replace(
        card("run-active"),
        started_at=active_started_at,
    )
    finished_card = replace(
        card(
            "run-terminal",
            phase=ResultPhase.TERMINAL,
            conclusion=ResultConclusion.OK,
            terminal=True,
        ),
        started_at=terminal_started_at,
        terminated_at=terminal_terminated_at,
    )
    store = ResultHistoryStore()
    store.apply(ActiveRunChanged(active_card))
    store.apply(
        RunTerminated(
            card=finished_card,
            review_payload=review_payload(),
        )
    )
    before_revision = store.snapshot().revision

    active_timezone.offset = timedelta(hours=-5)
    terminal_timezone.offset = timedelta(hours=10)

    after = store.snapshot()
    assert after.revision == before_revision
    assert after.active_cards[0].started_at == datetime(
        2026, 7, 26, 0, 30, tzinfo=timezone.utc
    )
    assert after.active_cards[0].started_at.tzinfo is timezone.utc
    assert after.terminal_cards[0].started_at == datetime(
        2026, 7, 26, 8, 0, tzinfo=timezone.utc
    )
    assert after.terminal_cards[0].started_at.tzinfo is timezone.utc
    assert after.terminal_cards[0].terminated_at == datetime(
        2026, 7, 26, 9, 0, tzinfo=timezone.utc
    )
    assert after.terminal_cards[0].terminated_at.tzinfo is timezone.utc


def test_apply_never_reinvokes_source_timezone_after_constructor_normalization():
    source_timezone = MutableOffsetTimezone(timedelta(hours=8))
    source_time = datetime(2026, 7, 26, 8, 30, tzinfo=source_timezone)
    normalized_card = replace(
        card("run-1"),
        started_at=source_time,
    )
    constructor_offset_calls = source_timezone.offset_calls
    source_timezone.raise_on_offset = True
    store = ResultHistoryStore()

    transition = store.apply(ActiveRunChanged(normalized_card))

    assert transition.changed is True
    assert source_timezone.offset_calls == constructor_offset_calls
    assert transition.snapshot.active_cards[0].started_at == datetime(
        2026, 7, 26, 0, 30, tzinfo=timezone.utc
    )
    assert transition.snapshot.active_cards[0].started_at.tzinfo is timezone.utc


@pytest.mark.parametrize(
    ("field_name", "invalid_timezone"),
    [
        ("started_at", NoneOffsetTimezone()),
        ("started_at", RaisingOffsetTimezone()),
        ("terminated_at", NoneOffsetTimezone()),
        ("terminated_at", RaisingOffsetTimezone()),
    ],
)
def test_malformed_timezone_is_rejected_before_store_mutation(
    field_name,
    invalid_timezone,
):
    store = ResultHistoryStore()
    store.apply(active_event("existing-run"))
    before = store.snapshot()
    malformed_time = datetime(
        2026,
        7,
        26,
        8,
        30,
        tzinfo=invalid_timezone,
    )
    base_card = card(
        "invalid-run",
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.ERROR,
        terminal=True,
    )

    with pytest.raises(ResultHistoryError, match=field_name):
        replace(base_card, **{field_name: malformed_time})

    assert store.snapshot() == before


def test_mutable_config_subclass_cannot_enter_snapshot_or_corrupt_lookup():
    store = ResultHistoryStore()
    mutable_config = unsafe_dataclass_subclass(
        FrozenConfigSummary(config_id=1, name="mutable"),
        MutableConfigSummary,
        mutable_state=[],
    )

    with pytest.raises(ResultHistoryError, match="config"):
        replace(card("run-1"), config=mutable_config)
    mutable_config.mutable_state.append("mutated")

    assert store.snapshot().active_cards == ()
    assert store.display_payload("run-1") is None


def test_mutable_hash_run_id_is_rejected_before_storage_or_lookup():
    store = ResultHistoryStore()
    mutable_run_id = MutableHashString("run-mutable")

    with pytest.raises(ResultHistoryError, match="run_id"):
        replace(card("run-1"), run_id=mutable_run_id)
    with pytest.raises(ResultHistoryError, match="run_id"):
        store.display_payload(mutable_run_id)
    with pytest.raises(ResultHistoryError, match="run_id"):
        store.apply(ActiveRunRemoved(mutable_run_id))
    mutable_run_id.hash_salt = 99

    assert store.snapshot().revision == 0
    assert store.snapshot().active_cards == ()
    assert store.snapshot().terminal_cards == ()


def test_operation_id_subclasses_are_rejected_atomically_at_start_and_cancel():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    mutable_operation_id = MutableHashString("operation-1")
    before_start = store.snapshot()

    with pytest.raises(ResultHistoryError, match="operation_id"):
        store.apply(
            ReanalysisStarted(
                "run-1",
                mutable_operation_id,
                expected_revision=0,
            )
        )
    assert store.snapshot() == before_start

    started = store.apply(
        ReanalysisStarted("run-1", "operation-1", expected_revision=0)
    )
    with pytest.raises(ResultHistoryError, match="operation_id"):
        store.apply(
            ReanalysisCancelled(
                "run-1",
                mutable_operation_id,
                expected_revision=1,
            )
        )
    assert store.snapshot() == started.snapshot


def test_missing_run_id_and_terminal_without_exactly_one_payload_are_rejected():
    store = ResultHistoryStore()

    with pytest.raises(ResultHistoryError, match="run_id"):
        store.apply(active_event(""))
    with pytest.raises(ResultHistoryError, match="exactly one"):
        store.apply(terminal_event("run-1"))
    with pytest.raises(ResultHistoryError, match="exactly one"):
        store.apply(
            terminal_event(
                "run-1",
                review=review_payload(),
                error=error_payload(),
            )
        )
    invalid_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        terminal=True,
    )
    invalid_card = replace(invalid_card, terminated_at=None)
    with pytest.raises(ResultHistoryError, match="terminal timestamp"):
        store.apply(RunTerminated(card=invalid_card, review_payload=review_payload()))
    invalid_card = replace(
        invalid_card,
        terminated_at=NOW,
        conclusion=None,
    )
    with pytest.raises(ResultHistoryError, match="terminal conclusion"):
        store.apply(RunTerminated(card=invalid_card, review_payload=review_payload()))


def test_review_payload_is_owned_and_returned_as_defensive_copy():
    store = ResultHistoryStore()
    incoming = review_payload()

    store.apply(terminal_event("run-1", review=incoming))
    incoming.review["chart_payload"]["series"][0] = 99
    incoming.review["chart_payload"]["samples"][0] = 98
    incoming.dashboard_config["panels"][0]["id"] = "mutated"

    first = store.display_payload("run-1")
    assert isinstance(first, ReviewDisplayPayload)
    assert first.review["chart_payload"]["series"] == [1.0, 2.0]
    np.testing.assert_array_equal(first.review["chart_payload"]["samples"], [1.0, 3.0])
    assert first.dashboard_config["panels"][0]["id"] == "spectrum"

    first.review["chart_payload"]["series"][1] = 77
    first.review["chart_payload"]["samples"][1] = 76
    second = store.display_payload("run-1")
    assert isinstance(second, ReviewDisplayPayload)
    assert second.review["chart_payload"]["series"] == [1.0, 2.0]
    np.testing.assert_array_equal(second.review["chart_payload"]["samples"], [1.0, 3.0])


def test_error_payload_is_owned_and_returned_as_defensive_copy():
    store = ResultHistoryStore()
    incoming = error_payload()

    store.apply(
        terminal_event(
            "run-error",
            conclusion=ResultConclusion.ERROR,
            error=incoming,
        )
    )
    first = store.display_payload("run-error")
    second = store.display_payload("run-error")

    assert isinstance(first, ErrorDisplayPayload)
    assert first == incoming
    assert first is not incoming
    assert second == first
    assert second is not first


def test_display_payload_deepcopy_failure_leaves_snapshot_selection_revision_and_lookup_unchanged(
    monkeypatch,
):
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    before = store.snapshot()
    before_payload = store.display_payload("run-1")

    def fail_copy(value):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(result_history, "deepcopy", fail_copy)
    with pytest.raises(ResultHistoryError, match="copy"):
        store.apply(terminal_event("run-2", review=review_payload(2.0)))
    monkeypatch.undo()

    assert store.snapshot() == before
    after_payload = store.display_payload("run-1")
    assert isinstance(before_payload, ReviewDisplayPayload)
    assert isinstance(after_payload, ReviewDisplayPayload)
    assert after_payload.review["chart_payload"]["series"] == (
        before_payload.review["chart_payload"]["series"]
    )
    np.testing.assert_array_equal(
        after_payload.review["chart_payload"]["samples"],
        before_payload.review["chart_payload"]["samples"],
    )
    assert after_payload.dashboard_config == before_payload.dashboard_config
    assert store.display_payload("run-2") is None


def test_ingress_deepcopy_executes_outside_store_lock(monkeypatch):
    store = ResultHistoryStore()
    copy_entered = Event()
    release_copy = Event()
    original_deepcopy = result_history.deepcopy

    def blocking_copy(value):
        copy_entered.set()
        assert release_copy.wait(timeout=2)
        return original_deepcopy(value)

    monkeypatch.setattr(result_history, "deepcopy", blocking_copy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            store.apply,
            terminal_event("run-1", review=review_payload()),
        )
        assert copy_entered.wait(timeout=2)
        observer = executor.submit(store.snapshot)
        try:
            observed_while_copying = observer.result(timeout=2)
        finally:
            release_copy.set()
        assert observed_while_copying == history_snapshot()
        transition = writer.result(timeout=2)
    monkeypatch.setattr(result_history, "deepcopy", original_deepcopy)

    assert transition.snapshot.revision == 1
    assert [item.run_id for item in transition.snapshot.terminal_cards] == [
        "run-1"
    ]
    assert store.display_payload("run-1") is not None


def test_output_deepcopy_captures_old_payload_without_blocking_replacement(
    monkeypatch,
):
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload(1.0)))
    reader_entered_copy = Event()
    release_reader_copy = Event()
    replacement_done = Event()
    reader_thread_id = None
    original_deepcopy = result_history.deepcopy

    def blocking_reader_copy(value):
        if get_ident() == reader_thread_id:
            reader_entered_copy.set()
            assert release_reader_copy.wait(timeout=2)
        return original_deepcopy(value)

    def read_payload():
        nonlocal reader_thread_id
        reader_thread_id = get_ident()
        return store.display_payload("run-1")

    replacement_card = finished_card(
        "run-1",
        conclusion=ResultConclusion.NG,
    )

    def replace_payload():
        transition = store.apply(
            TerminalResultReplaced(
                "run-1",
                0,
                replacement_card,
                review_payload=review_payload(9.0),
            )
        )
        replacement_done.set()
        return transition

    monkeypatch.setattr(result_history, "deepcopy", blocking_reader_copy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(read_payload)
        assert reader_entered_copy.wait(timeout=2)
        replacement = executor.submit(replace_payload)
        try:
            assert replacement_done.wait(timeout=2)
        finally:
            release_reader_copy.set()
        replacement_transition = replacement.result(timeout=2)
        old_payload = reader.result(timeout=2)
    monkeypatch.setattr(result_history, "deepcopy", original_deepcopy)

    assert replacement_transition.snapshot.revision == 2
    assert isinstance(old_payload, ReviewDisplayPayload)
    assert old_payload.review["chart_payload"]["series"] == [1.0, 2.0]
    new_payload = store.display_payload("run-1")
    assert isinstance(new_payload, ReviewDisplayPayload)
    assert new_payload.review["chart_payload"]["series"] == [9.0, 10.0]


@pytest.mark.parametrize("payload_kind", ["review", "error"])
def test_manual_replacement_releases_nested_finalizer_after_commit(payload_kind):
    store = ResultHistoryStore()
    observation = FinalizerObservation(store, "run-1")
    store.apply(
        terminal_event(
            "run-1",
            review=finalizing_review_payload(observation),
        )
    )
    observation.armed = True
    replacement_card = finished_card(
        "run-1",
        conclusion=(
            ResultConclusion.NG
            if payload_kind == "review"
            else ResultConclusion.ERROR
        ),
    )
    payload_args = (
        {"review_payload": review_payload(9.0)}
        if payload_kind == "review"
        else {"error_payload": error_payload("manual replacement failed")}
    )

    transition = store.apply(
        TerminalResultReplaced(
            run_id="run-1",
            expected_revision=0,
            card=replacement_card,
            **payload_args,
        )
    )
    observation.close()

    assert observation.errors == []
    assert len(observation.calls) == 1
    observed_snapshot, observed_payload = observation.calls[0]
    assert observed_snapshot == transition.snapshot
    assert observed_snapshot.revision == 2
    assert observed_snapshot.terminal_cards[0].revision == 1
    if payload_kind == "review":
        assert isinstance(observed_payload, ReviewDisplayPayload)
        assert observed_payload.review["chart_payload"]["series"] == [9.0, 10.0]
    else:
        assert isinstance(observed_payload, ErrorDisplayPayload)
        assert observed_payload.reason == "manual replacement failed"


def test_seventh_result_releases_evicted_nested_finalizer_after_commit():
    store = ResultHistoryStore()
    observation = FinalizerObservation(store, "run-1")
    store.apply(
        terminal_event(
            "run-1",
            review=finalizing_review_payload(observation),
        )
    )
    for index in range(2, 7):
        store.apply(
            terminal_event(
                f"run-{index}",
                review=review_payload(float(index)),
            )
        )
    observation.armed = True

    transition = store.apply(
        terminal_event(
            "run-7",
            conclusion=ResultConclusion.ERROR,
            error=error_payload("seventh result failed"),
        )
    )
    observation.close()

    assert observation.errors == []
    assert len(observation.calls) == 1
    observed_snapshot, observed_payload = observation.calls[0]
    assert observed_snapshot == transition.snapshot
    assert observed_snapshot.revision == 7
    assert transition.evicted_run_id == "run-1"
    assert observed_payload is None
    newest_payload = store.display_payload("run-7")
    assert isinstance(newest_payload, ErrorDisplayPayload)
    assert newest_payload.reason == "seventh result failed"


@pytest.mark.parametrize("payload_kind", ["review", "error"])
def test_reanalysis_completion_releases_nested_finalizer_after_commit(
    payload_kind,
):
    store = ResultHistoryStore()
    observation = FinalizerObservation(store, "run-1")
    store.apply(
        terminal_event(
            "run-1",
            review=finalizing_review_payload(observation),
        )
    )
    store.apply(ReanalysisStarted("run-1", "operation-1", expected_revision=0))
    observation.armed = True
    completed_card = finished_card(
        "run-1",
        conclusion=(
            ResultConclusion.OK
            if payload_kind == "review"
            else ResultConclusion.ERROR
        ),
        revision=1,
    )
    payload_args = (
        {"review_payload": review_payload(20.0)}
        if payload_kind == "review"
        else {"error_payload": error_payload("reanalysis failed")}
    )

    transition = store.apply(
        ReanalysisCompleted(
            run_id="run-1",
            operation_id="operation-1",
            expected_revision=1,
            card=completed_card,
            **payload_args,
        )
    )
    observation.close()

    assert observation.errors == []
    assert len(observation.calls) == 1
    observed_snapshot, observed_payload = observation.calls[0]
    assert observed_snapshot == transition.snapshot
    assert observed_snapshot.revision == 3
    assert observed_snapshot.terminal_cards[0].revision == 2
    if payload_kind == "review":
        assert isinstance(observed_payload, ReviewDisplayPayload)
        assert observed_payload.review["chart_payload"]["series"] == [20.0, 21.0]
    else:
        assert isinstance(observed_payload, ErrorDisplayPayload)
        assert observed_payload.reason == "reanalysis failed"


def test_reanalysis_cancel_releases_nested_backup_finalizer_after_commit(
    monkeypatch,
):
    store = ResultHistoryStore()
    observation = FinalizerObservation(store, "run-1")

    class BackupWithNestedFinalizer:
        def __init__(self, card, payload):
            self.card = card
            self.payload = payload
            self.probe = NestedFinalizerProbe(observation, generation=1)

    monkeypatch.setattr(
        result_history,
        "_ReanalysisBackup",
        BackupWithNestedFinalizer,
    )
    store.apply(terminal_event("run-1", review=review_payload(3.0)))
    store.apply(ReanalysisStarted("run-1", "operation-1", expected_revision=0))
    observation.armed = True

    transition = store.apply(
        ReanalysisCancelled(
            run_id="run-1",
            operation_id="operation-1",
            expected_revision=1,
        )
    )
    observation.close()

    assert observation.errors == []
    assert len(observation.calls) == 1
    observed_snapshot, observed_payload = observation.calls[0]
    assert observed_snapshot == transition.snapshot
    assert observed_snapshot.revision == 3
    assert observed_snapshot.terminal_cards[0].revision == 2
    assert isinstance(observed_payload, ReviewDisplayPayload)
    assert observed_payload.review["chart_payload"]["series"] == [3.0, 4.0]


def test_service_module_has_no_pyqt_or_screenshot_types():
    source = Path(result_history.__file__).read_text(encoding="utf-8")

    for forbidden in ("PyQt", "QPixmap", "QImage", "QPicture", "grab("):
        assert forbidden not in source


def test_active_cards_keep_admission_order_across_phase_updates():
    store = ResultHistoryStore()
    store.apply(active_event("run-1"))
    store.apply(active_event("run-2"))
    updated = replace(
        card("run-1"),
        phase=ResultPhase.WAITING_ANALYSIS,
        revision=1,
    )

    transition = store.apply(ActiveRunChanged(updated))

    assert [item.run_id for item in transition.snapshot.active_cards] == [
        "run-2",
        "run-1",
    ]
    assert transition.snapshot.active_cards[1].phase is ResultPhase.WAITING_ANALYSIS
    assert len(transition.snapshot.active_cards) == 2

    identical_but_newer = store.apply(ActiveRunChanged(card("run-2")))
    assert [item.run_id for item in identical_but_newer.snapshot.active_cards] == [
        "run-2",
        "run-1",
    ]
    assert len(identical_but_newer.snapshot.active_cards) == 2


def test_terminal_transition_removes_active_and_selects_latest_while_locked():
    store = ResultHistoryStore()
    store.apply(active_event("run-1"))

    transition = store.apply(
        terminal_event("run-1", review=review_payload())
    )

    assert transition.snapshot.active_cards == ()
    assert [item.run_id for item in transition.snapshot.terminal_cards] == ["run-1"]
    assert transition.snapshot.selected_run_id == "run-1"
    assert transition.selection_changed is False


def test_store_revision_card_revision_and_transition_flags_are_exact():
    store = ResultHistoryStore()

    active = store.apply(active_event("run-1"))
    assert active.changed is True
    assert active.selection_changed is True
    assert active.snapshot.revision == 1
    assert active.snapshot.active_cards[0].revision == 0

    active_noop = store.apply(active_event("run-1"))
    assert active_noop.changed is False
    assert active_noop.selection_changed is False
    assert active_noop.snapshot.revision == 1

    terminated = store.apply(
        terminal_event("run-1", review=review_payload())
    )
    assert terminated.changed is True
    assert terminated.selection_changed is False
    assert terminated.snapshot.revision == 2
    assert terminated.snapshot.terminal_cards[0].revision == 0

    unlocked = store.set_follow_latest(False)
    assert unlocked.changed is True
    assert unlocked.selection_changed is False
    assert unlocked.snapshot.revision == 3
    unlock_noop = store.set_follow_latest(False)
    assert unlock_noop.changed is False
    assert unlock_noop.selection_changed is False
    assert unlock_noop.snapshot.revision == 3

    selected_noop = store.select_run("run-1")
    assert selected_noop.changed is False
    assert selected_noop.selection_changed is False
    assert selected_noop.snapshot.revision == 3

    replaced = store.apply(
        TerminalResultReplaced(
            "run-1",
            0,
            finished_card("run-1", conclusion=ResultConclusion.NG),
            review_payload=review_payload(2.0),
        )
    )
    assert replaced.changed is True
    assert replaced.selection_changed is False
    assert replaced.snapshot.revision == 4
    assert replaced.snapshot.terminal_cards[0].revision == 1


def test_cached_duplicate_run_terminated_is_rejected_without_reordering_or_replacement():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload(1.0)))
    store.apply(terminal_event("run-2", review=review_payload(2.0)))
    before = store.snapshot()

    with pytest.raises(ResultHistoryError, match="already terminal"):
        store.apply(terminal_event("run-1", review=review_payload(99.0)))

    assert store.snapshot() == before
    payload = store.display_payload("run-1")
    assert isinstance(payload, ReviewDisplayPayload)
    assert payload.review["chart_payload"]["series"] == [1.0, 2.0]


def test_active_update_cannot_revive_a_cached_terminal_run():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    before = store.snapshot()

    with pytest.raises(ResultHistoryError, match="already terminal"):
        store.apply(active_event("run-1"))

    assert store.snapshot() == before


def test_unknown_run_terminated_is_accepted_without_a_prior_active_card():
    store = ResultHistoryStore()

    transition = store.apply(
        terminal_event("run-rejected-at-start", review=review_payload(4.0))
    )

    assert transition.changed is True
    assert transition.snapshot.active_cards == ()
    assert [item.run_id for item in transition.snapshot.terminal_cards] == [
        "run-rejected-at-start"
    ]
    assert transition.snapshot.selected_run_id == "run-rejected-at-start"
    payload = store.display_payload("run-rejected-at-start")
    assert isinstance(payload, ReviewDisplayPayload)
    assert payload.review["chart_payload"]["series"] == [4.0, 5.0]


def test_all_terminal_conclusions_count_toward_six():
    store = ResultHistoryStore()
    conclusions = list(ResultConclusion)

    for index, conclusion in enumerate(conclusions, 1):
        payload_kwargs = (
            {"error": error_payload()}
            if conclusion is ResultConclusion.ERROR
            else {"review": review_payload(float(index))}
        )
        store.apply(
            terminal_event(
                f"run-{index}",
                conclusion=conclusion,
                **payload_kwargs,
            )
        )

    assert len(store.snapshot().terminal_cards) == 6
    assert [item.conclusion for item in store.snapshot().terminal_cards] == list(
        reversed(conclusions)
    )


def test_seventh_terminal_atomically_evicts_oldest_card_and_payload():
    store = ResultHistoryStore()
    for index in range(1, 7):
        store.apply(
            terminal_event(f"run-{index}", review=review_payload(float(index)))
        )

    transition = store.apply(
        terminal_event("run-7", review=review_payload(7.0))
    )

    assert [item.run_id for item in transition.snapshot.terminal_cards] == [
        "run-7",
        "run-6",
        "run-5",
        "run-4",
        "run-3",
        "run-2",
    ]
    assert transition.evicted_run_id == "run-1"
    assert store.display_payload("run-1") is None


def test_new_active_becomes_locked_selection_ahead_of_terminal_result():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))

    transition = store.apply(active_event("run-active"))

    assert transition.snapshot.follow_latest is True
    assert transition.snapshot.selected_run_id == "run-active"
    assert transition.selection_changed is True


def test_follow_latest_selects_new_terminal_even_when_another_run_is_active():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-old", review=review_payload()))
    store.apply(active_event("run-1"))
    store.apply(active_event("run-2"))
    store.select_run("run-old")

    transition = store.apply(
        terminal_event("run-2", review=review_payload())
    )

    assert [item.run_id for item in transition.snapshot.active_cards] == [
        "run-1"
    ]
    assert transition.snapshot.selected_run_id == "run-2"
    assert transition.selection_changed is True


def test_unlock_keeps_selection_and_new_terminal_does_not_steal_it():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))

    unlocked = store.set_follow_latest(False)
    transition = store.apply(
        terminal_event("run-2", review=review_payload(2.0))
    )

    assert unlocked.snapshot.selected_run_id == "run-1"
    assert transition.snapshot.follow_latest is False
    assert transition.snapshot.selected_run_id == "run-1"
    assert transition.selection_changed is False


def test_relock_selects_latest_and_is_idempotent_without_terminals():
    empty = ResultHistoryStore()
    assert empty.set_follow_latest(True).changed is False
    assert empty.set_follow_latest(False).changed is True
    relocked_empty = empty.set_follow_latest(True)
    assert relocked_empty.changed is True
    assert relocked_empty.snapshot.selected_run_id is None
    assert empty.set_follow_latest(True).changed is False

    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    store.set_follow_latest(False)
    store.apply(terminal_event("run-2", review=review_payload(2.0)))

    relocked = store.set_follow_latest(True)

    assert relocked.snapshot.selected_run_id == "run-2"
    assert relocked.selection_changed is True


def test_follow_latest_allows_old_selection_and_next_result_returns_to_top():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    store.apply(terminal_event("run-2", review=review_payload(2.0)))

    selected = store.select_run("run-1")

    assert selected.snapshot.follow_latest is True
    assert selected.snapshot.selected_run_id == "run-1"
    assert selected.selection_changed is True
    assert selected.changed is True
    assert store.select_run("run-1").changed is False

    next_result = store.apply(
        terminal_event("run-3", review=review_payload(3.0))
    )

    assert next_result.snapshot.follow_latest is True
    assert next_result.snapshot.selected_run_id == "run-3"
    assert next_result.selection_changed is True


def test_follow_latest_old_selection_survives_active_phase_updates_until_result():
    store = ResultHistoryStore()
    store.apply(active_event("run-a"))
    store.apply(active_event("run-b"))
    store.select_run("run-a")
    by_id = {card.run_id: card for card in store.snapshot().active_cards}

    first_update = store.apply(
        ActiveRunChanged(
            replace(
                by_id["run-a"],
                phase=ResultPhase.WAITING_ANALYSIS,
                revision=by_id["run-a"].revision + 1,
            )
        )
    )

    assert first_update.snapshot.follow_latest is True
    assert first_update.snapshot.selected_run_id == "run-a"
    assert first_update.selection_changed is False

    by_id = {card.run_id: card for card in store.snapshot().active_cards}
    second_update = store.apply(
        ActiveRunChanged(
            replace(
                by_id["run-b"],
                phase=ResultPhase.WAITING_ANALYSIS,
                revision=by_id["run-b"].revision + 1,
            )
        )
    )

    assert second_update.snapshot.selected_run_id == "run-a"
    assert second_update.selection_changed is False

    completed = store.apply(
        terminal_event("run-b", review=review_payload(2.0))
    )

    assert completed.snapshot.selected_run_id == "run-b"
    assert completed.selection_changed is True


def test_selected_terminal_eviction_keeps_unlocked_selects_latest_and_notices():
    store = ResultHistoryStore()
    for index in range(1, 7):
        store.apply(
            terminal_event(f"run-{index}", review=review_payload(float(index)))
        )
    store.set_follow_latest(False)
    store.select_run("run-1")

    transition = store.apply(
        terminal_event("run-7", review=review_payload(7.0))
    )

    assert transition.snapshot.follow_latest is False
    assert transition.snapshot.selected_run_id == "run-7"
    assert transition.selection_changed is True
    assert transition.notice == "所查看结果已移出缓存"


def test_selected_active_terminal_keeps_same_run_selected():
    store = ResultHistoryStore()
    store.set_follow_latest(False)
    store.apply(active_event("run-active"))
    store.select_run("run-active")

    transition = store.apply(
        terminal_event("run-active", review=review_payload())
    )

    assert transition.snapshot.follow_latest is False
    assert transition.snapshot.selected_run_id == "run-active"
    assert transition.selection_changed is False


def test_selected_active_removal_selects_latest_terminal_or_none():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-terminal", review=review_payload()))
    store.set_follow_latest(False)
    store.apply(active_event("run-active"))
    store.select_run("run-active")

    repaired = store.apply(ActiveRunRemoved("run-active"))

    assert repaired.snapshot.follow_latest is False
    assert repaired.snapshot.selected_run_id == "run-terminal"
    assert repaired.selection_changed is True

    empty = ResultHistoryStore()
    empty.set_follow_latest(False)
    empty.apply(active_event("run-active"))
    empty.select_run("run-active")
    repaired_empty = empty.apply(ActiveRunRemoved("run-active"))
    assert repaired_empty.snapshot.selected_run_id is None
    assert repaired_empty.selection_changed is True


def test_missing_or_evicted_run_cannot_be_selected_looked_up_or_replaced():
    store = ResultHistoryStore()
    store.set_follow_latest(False)

    with pytest.raises(ResultHistoryError, match="not cached"):
        store.select_run("missing")
    assert store.display_payload("missing") is None

    for index in range(1, 8):
        store.apply(
            terminal_event(f"run-{index}", review=review_payload(float(index)))
        )
    with pytest.raises(ResultHistoryError, match="not cached"):
        store.select_run("run-1")
    assert store.display_payload("run-1") is None
    with pytest.raises(ResultHistoryError, match="not cached"):
        store.apply(
            TerminalResultReplaced(
                run_id="run-1",
                expected_revision=0,
                card=card(
                    "run-1",
                    phase=ResultPhase.TERMINAL,
                    conclusion=ResultConclusion.OK,
                    terminal=True,
                ),
                review_payload=review_payload(),
            )
        )


def test_evicted_identity_reuses_unknown_terminal_path_without_permanent_dedup_state():
    store = ResultHistoryStore()
    for index in range(1, 8):
        transition = store.apply(
            terminal_event(f"run-{index}", review=review_payload(float(index)))
        )

    assert transition.evicted_run_id == "run-1"
    assert store.display_payload("run-1") is None
    reused_event = terminal_event("run-1", review=review_payload(100.0))
    reused_event = replace(
        reused_event,
        card=replace(reused_event.card, sn="SN-SYNTHETIC-REUSE"),
    )

    # Future MultiRunCoordinator process-wide uniqueness and at-most-once
    # terminal publication prevents this synthetic identity reuse in production.
    reused = store.apply(reused_event)

    assert reused.changed is True
    assert reused.evicted_run_id == "run-2"
    assert [item.run_id for item in reused.snapshot.terminal_cards] == [
        "run-1",
        "run-7",
        "run-6",
        "run-5",
        "run-4",
        "run-3",
    ]
    assert len(reused.snapshot.terminal_cards) == 6
    assert reused.snapshot.terminal_cards[0].sn == "SN-SYNTHETIC-REUSE"
    assert reused.snapshot.selected_run_id == "run-1"
    assert store.display_payload("run-2") is None
    payload = store.display_payload("run-1")
    assert isinstance(payload, ReviewDisplayPayload)
    assert payload.review["chart_payload"]["series"] == [100.0, 101.0]


def test_manual_replacement_is_revision_cas_and_does_not_reorder():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    store.apply(terminal_event("run-2", review=review_payload(2.0)))
    replacement_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.NG,
        terminal=True,
        revision=99,
        sn="SN-MANUAL",
    )

    transition = store.apply(
        TerminalResultReplaced(
            run_id="run-1",
            expected_revision=0,
            card=replacement_card,
            review_payload=review_payload(10.0),
        )
    )

    assert [item.run_id for item in transition.snapshot.terminal_cards] == [
        "run-2",
        "run-1",
    ]
    replaced_card = transition.snapshot.terminal_cards[1]
    assert replaced_card.conclusion is ResultConclusion.NG
    assert replaced_card.sn == "SN-MANUAL"
    assert replaced_card.revision == 1
    before_stale = store.snapshot()
    with pytest.raises(ResultHistoryError, match="revision"):
        store.apply(
            TerminalResultReplaced(
                run_id="run-1",
                expected_revision=0,
                card=replacement_card,
                review_payload=review_payload(11.0),
            )
        )
    assert store.snapshot() == before_stale


@pytest.mark.parametrize(
    "cas_path",
    ["terminal_replacement", "reanalysis_start", "reanalysis_complete", "reanalysis_cancel"],
)
def test_bool_expected_revision_is_rejected_atomically_across_all_cas_paths(
    cas_path,
):
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    completion_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        terminal=True,
        revision=1,
    )
    if cas_path == "terminal_replacement":
        event_factory = lambda: TerminalResultReplaced(
            "run-1",
            False,
            completion_card,
            review_payload=review_payload(2.0),
        )
    elif cas_path == "reanalysis_start":
        event_factory = lambda: ReanalysisStarted(
            "run-1",
            "operation-1",
            False,
        )
    else:
        store.apply(ReanalysisStarted("run-1", "operation-1", expected_revision=0))
        if cas_path == "reanalysis_complete":
            event_factory = lambda: ReanalysisCompleted(
                "run-1",
                "operation-1",
                True,
                completion_card,
                review_payload=review_payload(2.0),
            )
        else:
            event_factory = lambda: ReanalysisCancelled(
                "run-1",
                "operation-1",
                True,
            )
    before = store.snapshot()
    before_payload = store.display_payload("run-1")

    with pytest.raises(ResultHistoryError, match="expected revision"):
        store.apply(event_factory())

    assert store.snapshot() == before
    assert_review_payload_equal(store.display_payload("run-1"), before_payload)


def test_reanalysis_start_preserves_backup_marks_overlay_and_does_not_reorder():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    store.apply(terminal_event("run-2", review=review_payload(2.0)))
    before_payload = store.display_payload("run-1")

    transition = store.apply(
        ReanalysisStarted(
            run_id="run-1",
            operation_id="operation-1",
            expected_revision=0,
        )
    )

    assert [item.run_id for item in transition.snapshot.terminal_cards] == [
        "run-2",
        "run-1",
    ]
    overlay = transition.snapshot.terminal_cards[1]
    assert overlay.phase is ResultPhase.REANALYZING
    assert overlay.is_active_run is False
    assert overlay.operation_id == "operation-1"
    assert overlay.revision == 1
    assert_review_payload_equal(store.display_payload("run-1"), before_payload)


def test_reanalysis_success_replaces_review_and_discards_backup():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    started = store.apply(
        ReanalysisStarted("run-1", "operation-1", expected_revision=0)
    )
    assert started.snapshot.terminal_cards[0].revision == 1
    completed_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        terminal=True,
        revision=1,
        sn="SN-REANALYZED",
    )

    transition = store.apply(
        ReanalysisCompleted(
            run_id="run-1",
            operation_id="operation-1",
            expected_revision=1,
            card=completed_card,
            review_payload=review_payload(20.0),
        )
    )

    final_card = transition.snapshot.terminal_cards[0]
    assert final_card.phase is ResultPhase.TERMINAL
    assert final_card.operation_id is None
    assert final_card.revision == 2
    assert final_card.sn == "SN-REANALYZED"
    payload = store.display_payload("run-1")
    assert isinstance(payload, ReviewDisplayPayload)
    assert payload.review["chart_payload"]["series"] == [20.0, 21.0]
    with pytest.raises(ResultHistoryError, match="operation"):
        store.apply(ReanalysisCancelled("run-1", "operation-1", expected_revision=2))


def test_reanalysis_failure_replaces_with_error_page():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    store.apply(ReanalysisStarted("run-1", "operation-1", expected_revision=0))
    failed_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.ERROR,
        terminal=True,
        revision=1,
    )

    transition = store.apply(
        ReanalysisCompleted(
            run_id="run-1",
            operation_id="operation-1",
            expected_revision=1,
            card=failed_card,
            error_payload=error_payload("重新分析失败"),
        )
    )

    assert transition.snapshot.terminal_cards[0].conclusion is ResultConclusion.ERROR
    payload = store.display_payload("run-1")
    assert isinstance(payload, ErrorDisplayPayload)
    assert payload.reason == "重新分析失败"


@pytest.mark.parametrize(
    ("conclusion", "payload_kind"),
    [
        (ResultConclusion.ERROR, "review"),
        (ResultConclusion.ERROR, "none"),
        (ResultConclusion.OK, "error"),
        (ResultConclusion.OK, "both"),
        (ResultConclusion.NG, "error"),
        (ResultConclusion.NO_RESULT, "error"),
        (ResultConclusion.CAPACITY_REJECTED, "review"),
        (ResultConclusion.CAPACITY_REJECTED, "error"),
        (ResultConclusion.MANUAL_STOPPED, "review"),
        (ResultConclusion.MANUAL_STOPPED, "error"),
    ],
)
def test_reanalysis_completion_rejects_invalid_conclusion_payload_pair_atomically(
    conclusion,
    payload_kind,
):
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    started = store.apply(
        ReanalysisStarted("run-1", "operation-1", expected_revision=0)
    )
    before = started.snapshot
    before_payload = store.display_payload("run-1")
    completed_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=conclusion,
        terminal=True,
        revision=1,
    )
    payload_args_by_kind = {
        "review": {"review_payload": review_payload(20.0)},
        "error": {"error_payload": error_payload("重新分析失败")},
        "both": {
            "review_payload": review_payload(20.0),
            "error_payload": error_payload("重新分析失败"),
        },
        "none": {},
    }
    payload_args = payload_args_by_kind[payload_kind]

    with pytest.raises(ResultHistoryError, match="conclusion|payload"):
        store.apply(
            ReanalysisCompleted(
                run_id="run-1",
                operation_id="operation-1",
                expected_revision=1,
                card=completed_card,
                **payload_args,
            )
        )

    assert store.snapshot() == before
    assert_review_payload_equal(store.display_payload("run-1"), before_payload)


@pytest.mark.parametrize(
    "conclusion",
    [ResultConclusion.NG, ResultConclusion.NO_RESULT],
)
def test_reanalysis_completion_accepts_other_successful_review_conclusions(
    conclusion,
):
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    store.apply(ReanalysisStarted("run-1", "operation-1", expected_revision=0))
    completed_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=conclusion,
        terminal=True,
        revision=1,
    )

    transition = store.apply(
        ReanalysisCompleted(
            run_id="run-1",
            operation_id="operation-1",
            expected_revision=1,
            card=completed_card,
            review_payload=review_payload(20.0),
        )
    )

    assert transition.snapshot.terminal_cards[0].conclusion is conclusion
    assert isinstance(store.display_payload("run-1"), ReviewDisplayPayload)


def test_reanalysis_cancel_restores_exact_previous_card_and_payload():
    store = ResultHistoryStore()
    store.apply(
        terminal_event(
            "run-1",
            conclusion=ResultConclusion.NG,
            review=review_payload(3.0),
        )
    )
    store.apply(terminal_event("run-2", review=review_payload(4.0)))
    previous_order = [
        item.run_id for item in store.snapshot().terminal_cards
    ]
    previous_card = store.snapshot().terminal_cards[1]
    previous_payload = store.display_payload("run-1")
    store.apply(ReanalysisStarted("run-1", "operation-1", expected_revision=0))

    transition = store.apply(
        ReanalysisCancelled(
            run_id="run-1",
            operation_id="operation-1",
            expected_revision=1,
        )
    )

    assert [item.run_id for item in transition.snapshot.terminal_cards] == (
        previous_order
    )
    restored = transition.snapshot.terminal_cards[1]
    assert replace(restored, revision=previous_card.revision) == previous_card
    assert restored.revision == 2
    assert transition.changed is True
    assert transition.selection_changed is False
    assert transition.snapshot.revision == 4
    assert_review_payload_equal(store.display_payload("run-1"), previous_payload)


def test_reanalysis_eviction_removes_card_payload_and_backup_and_rejects_all_late_operations():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    store.apply(ReanalysisStarted("run-1", "operation-1", expected_revision=0))
    for index in range(2, 8):
        transition = store.apply(
            terminal_event(f"run-{index}", review=review_payload(float(index)))
        )

    assert transition.evicted_run_id == "run-1"
    assert store.display_payload("run-1") is None
    after_eviction = store.snapshot()
    remaining_payloads = {
        item.run_id: store.display_payload(item.run_id)
        for item in after_eviction.terminal_cards
    }
    replacement_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        terminal=True,
        revision=1,
    )
    late_events = (
        TerminalResultReplaced(
            run_id="run-1",
            expected_revision=1,
            card=replacement_card,
            review_payload=review_payload(20.0),
        ),
        ReanalysisStarted(
            "run-1",
            "operation-2",
            expected_revision=1,
        ),
        ReanalysisCompleted(
            "run-1",
            "operation-1",
            expected_revision=1,
            card=replacement_card,
            review_payload=review_payload(20.0),
        ),
        ReanalysisCancelled(
            "run-1",
            "operation-1",
            expected_revision=1,
        ),
    )

    for late_event in late_events:
        with pytest.raises(ResultHistoryError, match="not cached"):
            store.apply(late_event)
        current = store.snapshot()
        assert current == after_eviction
        assert current.revision == after_eviction.revision
        assert current.selected_run_id == after_eviction.selected_run_id
        assert [item.run_id for item in current.active_cards] == [
            item.run_id for item in after_eviction.active_cards
        ]
        assert [item.run_id for item in current.terminal_cards] == [
            item.run_id for item in after_eviction.terminal_cards
        ]
        assert store.display_payload("run-1") is None
        for run_id, expected_payload in remaining_payloads.items():
            assert_review_payload_equal(
                store.display_payload(run_id),
                expected_payload,
            )


def test_late_start_complete_or_cancel_is_rejected_by_revision_or_operation():
    store = ResultHistoryStore()
    store.apply(terminal_event("run-1", review=review_payload()))
    store.apply(ReanalysisStarted("run-1", "operation-1", expected_revision=0))
    completion_card = card(
        "run-1",
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        terminal=True,
        revision=1,
    )

    with pytest.raises(ResultHistoryError, match="revision"):
        store.apply(ReanalysisStarted("run-1", "operation-2", expected_revision=0))
    with pytest.raises(ResultHistoryError, match="operation"):
        store.apply(
            ReanalysisCompleted(
                "run-1",
                "wrong-operation",
                expected_revision=1,
                card=completion_card,
                review_payload=review_payload(2.0),
            )
        )
    store.apply(ReanalysisCancelled("run-1", "operation-1", expected_revision=1))
    before_late = store.snapshot()
    with pytest.raises(ResultHistoryError, match="revision|operation"):
        store.apply(
            ReanalysisCompleted(
                "run-1",
                "operation-1",
                expected_revision=1,
                card=completion_card,
                review_payload=review_payload(3.0),
            )
        )
    with pytest.raises(ResultHistoryError, match="revision|operation"):
        store.apply(
            ReanalysisCancelled("run-1", "operation-1", expected_revision=1)
        )
    assert store.snapshot() == before_late


def test_snapshot_observer_never_sees_card_without_payload_or_payload_without_card():
    store = ResultHistoryStore()
    pausing_lock = PausingRLock()
    store._lock = pausing_lock
    observer_attempting_snapshot = Event()

    def observe_snapshot():
        observer_attempting_snapshot.set()
        return store.snapshot()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            store.apply,
            terminal_event("run-1", review=review_payload()),
        )
        assert pausing_lock.writer_acquired.wait(timeout=2)
        observer = executor.submit(observe_snapshot)
        assert observer_attempting_snapshot.wait(timeout=2)
        try:
            assert observer.done() is False
        finally:
            pausing_lock.release_writer.set()
        writer_transition = writer.result(timeout=2)
        observed = observer.result(timeout=2)

    assert writer_transition.snapshot.revision == 1
    assert observed == writer_transition.snapshot
    assert [item.run_id for item in observed.terminal_cards] == ["run-1"]
    assert store.display_payload("run-1") is not None
    for terminal_card in observed.terminal_cards:
        assert store.display_payload(terminal_card.run_id) is not None


def assert_review_payload_equal(left, right) -> None:
    assert isinstance(left, ReviewDisplayPayload)
    assert isinstance(right, ReviewDisplayPayload)
    assert left.review["chart_payload"]["series"] == right.review["chart_payload"][
        "series"
    ]
    np.testing.assert_array_equal(
        left.review["chart_payload"]["samples"],
        right.review["chart_payload"]["samples"],
    )
    assert left.dashboard_config == right.dashboard_config
