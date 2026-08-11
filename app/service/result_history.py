from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import TypeAlias


class ResultHistoryError(ValueError):
    """Raised when an event violates the result-history domain contract."""


class ResultPhase(str, Enum):
    RECORDING = "recording"
    WAITING_SAVE = "waiting_save"
    WAITING_ANALYSIS = "waiting_analysis"
    ANALYZING = "analyzing"
    REANALYZING = "reanalyzing"
    TERMINAL = "terminal"


class ResultConclusion(str, Enum):
    OK = "ok"
    NG = "ng"
    NO_RESULT = "no_result"
    ERROR = "error"
    CAPACITY_REJECTED = "capacity_rejected"
    MANUAL_STOPPED = "manual_stopped"


_MAX_TERMINAL_RESULTS = 6
_EVICTION_NOTICE = "所查看结果已移出缓存"


def _require_exact_instance(value: object, expected_type: type) -> None:
    if type(value) is not expected_type:
        raise ResultHistoryError(
            f"value must be exact {expected_type.__name__}"
        )


def _require_nonempty_string(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ResultHistoryError(f"{field_name} must be a non-empty string")


def _require_optional_string(value: object, field_name: str) -> None:
    if value is not None and type(value) is not str:
        raise ResultHistoryError(f"{field_name} must be a string or None")


def _require_revision(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ResultHistoryError(
            f"{field_name} must be a non-negative integer"
        )


def _normalize_card_datetime(value: datetime, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise ResultHistoryError(f"card {field_name} must be a datetime")
    if value.tzinfo is None:
        return value
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise ResultHistoryError(
            f"card {field_name} timezone offset is invalid"
        ) from exc
    if offset is None:
        raise ResultHistoryError(
            f"card {field_name} timezone offset is invalid"
        )
    try:
        naive_utc = value.replace(tzinfo=None) - offset
        return naive_utc.replace(tzinfo=timezone.utc, fold=0)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ResultHistoryError(
            f"card {field_name} timezone offset is invalid"
        ) from exc


@dataclass(frozen=True)
class ConfigSummaryField:
    label: str
    value: str

    def __post_init__(self) -> None:
        _require_exact_instance(self, ConfigSummaryField)
        if type(self.label) is not str or type(self.value) is not str:
            raise ResultHistoryError(
                "config summary labels and values must be strings"
            )


@dataclass(frozen=True)
class FrozenConfigSummary:
    config_id: int | None
    name: str
    details: tuple[ConfigSummaryField, ...] = ()
    fault_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_exact_instance(self, FrozenConfigSummary)
        if self.config_id is not None and type(self.config_id) is not int:
            raise ResultHistoryError("config_id must be an integer or None")
        if type(self.name) is not str:
            raise ResultHistoryError("config summary name must be a string")
        if type(self.details) is not tuple or not all(
            type(item) is ConfigSummaryField for item in self.details
        ):
            raise ResultHistoryError(
                "config summary details must be exact ConfigSummaryField "
                "values in an exact tuple"
            )
        if (
            type(self.fault_types) is not tuple
            or not all(
                type(item) is str and bool(item.strip())
                for item in self.fault_types
            )
            or len(set(self.fault_types)) != len(self.fault_types)
        ):
            raise ResultHistoryError(
                "fault_types must be unique non-empty exact strings "
                "in an exact tuple"
            )


@dataclass(frozen=True)
class ResultHistoryCardSnapshot:
    run_id: str
    config: FrozenConfigSummary
    sn: str
    trigger_source: str
    started_at: datetime
    terminated_at: datetime | None
    phase: ResultPhase
    conclusion: ResultConclusion | None
    reason: str | None
    is_active_run: bool
    manual_judgment_allowed: bool
    manual_reanalysis_allowed: bool
    revision: int
    operation_id: str | None = None
    fault_type: str | None = None
    manual_judgment_revision: int | None = None

    def __post_init__(self) -> None:
        _require_exact_instance(self, ResultHistoryCardSnapshot)
        _require_nonempty_string(self.run_id, "run_id")
        if type(self.config) is not FrozenConfigSummary:
            raise ResultHistoryError(
                "card config must be an exact FrozenConfigSummary"
            )
        if type(self.sn) is not str:
            raise ResultHistoryError("card sn must be a string")
        if type(self.trigger_source) is not str:
            raise ResultHistoryError("card trigger_source must be a string")
        normalized_started_at = _normalize_card_datetime(
            self.started_at,
            "started_at",
        )
        normalized_terminated_at = (
            None
            if self.terminated_at is None
            else _normalize_card_datetime(self.terminated_at, "terminated_at")
        )
        object.__setattr__(self, "started_at", normalized_started_at)
        object.__setattr__(self, "terminated_at", normalized_terminated_at)
        if type(self.phase) is not ResultPhase:
            raise ResultHistoryError("card phase must be a ResultPhase")
        if self.conclusion is not None and type(self.conclusion) is not ResultConclusion:
            raise ResultHistoryError(
                "card conclusion must be a ResultConclusion or None"
            )
        _require_optional_string(self.reason, "card reason")
        for field_name in (
            "is_active_run",
            "manual_judgment_allowed",
            "manual_reanalysis_allowed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ResultHistoryError(f"card {field_name} must be a bool")
        _require_revision(self.revision, "card revision")
        if self.operation_id is not None:
            _require_nonempty_string(self.operation_id, "operation_id")
        _require_optional_string(self.fault_type, "fault_type")
        if self.fault_type is not None:
            _require_nonempty_string(self.fault_type, "fault_type")
            if self.conclusion is not ResultConclusion.NG:
                raise ResultHistoryError("fault_type requires an NG card")
            if self.fault_type not in self.config.fault_types:
                raise ResultHistoryError(
                    "fault_type must belong to the frozen config tuple"
                )
        if self.manual_judgment_revision is not None:
            _require_revision(
                self.manual_judgment_revision,
                "manual_judgment_revision",
            )
        if self.is_active_run and (
            self.fault_type is not None
            or self.manual_judgment_revision is not None
        ):
            raise ResultHistoryError(
                "active cards cannot contain manual judgment fields"
            )


@dataclass(frozen=True)
class ReviewDisplayPayload:
    review: object
    dashboard_config: object

    def __post_init__(self) -> None:
        _require_exact_instance(self, ReviewDisplayPayload)


@dataclass(frozen=True)
class ErrorDisplayPayload:
    error_category: str
    reason: str
    context: tuple[ConfigSummaryField, ...] = ()

    def __post_init__(self) -> None:
        _require_exact_instance(self, ErrorDisplayPayload)
        if type(self.error_category) is not str:
            raise ResultHistoryError("error_category must be a string")
        if type(self.reason) is not str:
            raise ResultHistoryError("error payload reason must be a string")
        if type(self.context) is not tuple or not all(
            type(item) is ConfigSummaryField for item in self.context
        ):
            raise ResultHistoryError(
                "error context must be exact ConfigSummaryField values "
                "in an exact tuple"
            )


ResultDisplayPayload: TypeAlias = ReviewDisplayPayload | ErrorDisplayPayload


def _validate_active_card_value(card: object) -> ResultHistoryCardSnapshot:
    if type(card) is not ResultHistoryCardSnapshot:
        raise ResultHistoryError(
            "card must be an exact ResultHistoryCardSnapshot"
        )
    if not card.is_active_run:
        raise ResultHistoryError("active zone requires an active card")
    if card.phase not in {
        ResultPhase.RECORDING,
        ResultPhase.WAITING_SAVE,
        ResultPhase.WAITING_ANALYSIS,
        ResultPhase.ANALYZING,
    }:
        raise ResultHistoryError("active zone card has an invalid phase")
    if card.terminated_at is not None or card.conclusion is not None:
        raise ResultHistoryError("active zone card cannot have terminal fields")
    if card.operation_id is not None:
        raise ResultHistoryError(
            "active zone card cannot have an operation identity"
        )
    return card


def _validate_terminal_zone_card(
    card: object,
) -> ResultHistoryCardSnapshot:
    if type(card) is not ResultHistoryCardSnapshot:
        raise ResultHistoryError(
            "card must be an exact ResultHistoryCardSnapshot"
        )
    if card.is_active_run:
        raise ResultHistoryError("terminal zone requires a terminal card")
    if card.terminated_at is None:
        raise ResultHistoryError(
            "terminal timestamp is required in the terminal zone"
        )
    if card.conclusion is None:
        raise ResultHistoryError(
            "terminal conclusion is required in the terminal zone"
        )
    if card.phase is ResultPhase.TERMINAL:
        if card.operation_id is not None:
            raise ResultHistoryError(
                "terminal zone card cannot retain an operation identity"
            )
        return card
    if card.phase is ResultPhase.REANALYZING:
        if (
            card.operation_id is None
            or card.manual_judgment_allowed
            or card.manual_reanalysis_allowed
        ):
            raise ResultHistoryError(
                "terminal zone reanalyzing card is inconsistent"
            )
        return card
    raise ResultHistoryError("terminal zone card has an invalid phase")


def _validate_new_terminal_card(
    card: object,
) -> ResultHistoryCardSnapshot:
    validated = _validate_terminal_zone_card(card)
    if validated.phase is not ResultPhase.TERMINAL:
        raise ResultHistoryError("new terminal card requires terminal phase")
    return validated


def _validate_payload_variants(
    review_payload: object,
    error_payload: object,
) -> ResultDisplayPayload:
    supplied = int(review_payload is not None) + int(error_payload is not None)
    if supplied != 1:
        raise ResultHistoryError(
            "terminal result requires exactly one display payload"
        )
    payload = review_payload if review_payload is not None else error_payload
    if type(payload) not in (ReviewDisplayPayload, ErrorDisplayPayload):
        raise ResultHistoryError(
            "payload must be an exact display payload wrapper"
        )
    return payload


def _validate_reanalysis_outcome_values(
    card: ResultHistoryCardSnapshot,
    review_payload: object,
    error_payload: object,
) -> ResultDisplayPayload:
    payload = _validate_payload_variants(review_payload, error_payload)
    if card.conclusion in {
        ResultConclusion.OK,
        ResultConclusion.NG,
        ResultConclusion.NO_RESULT,
    }:
        if type(payload) is not ReviewDisplayPayload:
            raise ResultHistoryError(
                "successful reanalysis conclusion requires a review payload"
            )
        return payload
    if card.conclusion is ResultConclusion.ERROR:
        if type(payload) is not ErrorDisplayPayload:
            raise ResultHistoryError(
                "error reanalysis conclusion requires an error payload"
            )
        return payload
    conclusion = (
        card.conclusion.value
        if type(card.conclusion) is ResultConclusion
        else card.conclusion
    )
    raise ResultHistoryError(
        f"reanalysis conclusion {conclusion!r} is not supported"
    )


@dataclass(frozen=True)
class ActiveRunChanged:
    card: ResultHistoryCardSnapshot

    def __post_init__(self) -> None:
        _require_exact_instance(self, ActiveRunChanged)
        _validate_active_card_value(self.card)

    @property
    def run_id(self) -> str:
        return self.card.run_id


@dataclass(frozen=True)
class RunTerminated:
    card: ResultHistoryCardSnapshot
    review_payload: ReviewDisplayPayload | None = None
    error_payload: ErrorDisplayPayload | None = None

    def __post_init__(self) -> None:
        _require_exact_instance(self, RunTerminated)
        _validate_new_terminal_card(self.card)
        _validate_payload_variants(self.review_payload, self.error_payload)

@dataclass(frozen=True)
class TerminalResultReplaced:
    run_id: str
    expected_revision: int
    card: ResultHistoryCardSnapshot
    review_payload: ReviewDisplayPayload | None = None
    error_payload: ErrorDisplayPayload | None = None

    def __post_init__(self) -> None:
        _require_exact_instance(self, TerminalResultReplaced)
        _require_nonempty_string(self.run_id, "run_id")
        _require_revision(
            self.expected_revision,
            "expected revision (expected_revision)",
        )
        card = _validate_new_terminal_card(self.card)
        if card.run_id != self.run_id:
            raise ResultHistoryError(
                "event run_id does not match replacement card"
            )
        _validate_payload_variants(self.review_payload, self.error_payload)


@dataclass(frozen=True)
class ReanalysisStarted:
    run_id: str
    operation_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        _require_exact_instance(self, ReanalysisStarted)
        _require_nonempty_string(self.run_id, "run_id")
        _require_nonempty_string(self.operation_id, "operation_id")
        _require_revision(
            self.expected_revision,
            "expected revision (expected_revision)",
        )


@dataclass(frozen=True)
class ReanalysisCompleted:
    run_id: str
    operation_id: str
    expected_revision: int
    card: ResultHistoryCardSnapshot
    review_payload: ReviewDisplayPayload | None = None
    error_payload: ErrorDisplayPayload | None = None

    def __post_init__(self) -> None:
        _require_exact_instance(self, ReanalysisCompleted)
        _require_nonempty_string(self.run_id, "run_id")
        _require_nonempty_string(self.operation_id, "operation_id")
        _require_revision(
            self.expected_revision,
            "expected revision (expected_revision)",
        )
        card = _validate_new_terminal_card(self.card)
        if card.run_id != self.run_id:
            raise ResultHistoryError(
                "event run_id does not match replacement card"
            )
        _validate_reanalysis_outcome_values(
            card,
            self.review_payload,
            self.error_payload,
        )


@dataclass(frozen=True)
class ReanalysisCancelled:
    run_id: str
    operation_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        _require_exact_instance(self, ReanalysisCancelled)
        _require_nonempty_string(self.run_id, "run_id")
        _require_nonempty_string(self.operation_id, "operation_id")
        _require_revision(
            self.expected_revision,
            "expected revision (expected_revision)",
        )


@dataclass(frozen=True)
class ActiveRunRemoved:
    run_id: str

    def __post_init__(self) -> None:
        _require_exact_instance(self, ActiveRunRemoved)
        _require_nonempty_string(self.run_id, "run_id")


ResultHistoryEvent: TypeAlias = (
    ActiveRunChanged
    | RunTerminated
    | TerminalResultReplaced
    | ReanalysisStarted
    | ReanalysisCompleted
    | ReanalysisCancelled
    | ActiveRunRemoved
)


@dataclass(frozen=True)
class ResultHistorySnapshot:
    revision: int
    follow_latest: bool
    selected_run_id: str | None
    active_cards: tuple[ResultHistoryCardSnapshot, ...]
    terminal_cards: tuple[ResultHistoryCardSnapshot, ...]

    def __post_init__(self) -> None:
        _require_exact_instance(self, ResultHistorySnapshot)
        _require_revision(self.revision, "revision")
        if type(self.follow_latest) is not bool:
            raise ResultHistoryError("follow_latest must be a bool")
        if self.selected_run_id is not None:
            _require_nonempty_string(
                self.selected_run_id,
                "selected_run_id",
            )
        if type(self.active_cards) is not tuple:
            raise ResultHistoryError("active_cards must be an exact tuple")
        if type(self.terminal_cards) is not tuple:
            raise ResultHistoryError("terminal_cards must be an exact tuple")
        if not all(
            type(card) is ResultHistoryCardSnapshot
            for card in self.active_cards
        ):
            raise ResultHistoryError(
                "active_cards must contain exact card values"
            )
        if not all(
            type(card) is ResultHistoryCardSnapshot
            for card in self.terminal_cards
        ):
            raise ResultHistoryError(
                "terminal_cards must contain exact card values"
            )
        if len(self.terminal_cards) > _MAX_TERMINAL_RESULTS:
            raise ResultHistoryError(
                "terminal_cards cannot contain more than six results"
            )
        for card in self.active_cards:
            try:
                _validate_active_card_value(card)
            except ResultHistoryError as exc:
                raise ResultHistoryError(
                    f"active zone card is invalid: {exc}"
                ) from exc
        for card in self.terminal_cards:
            try:
                _validate_terminal_zone_card(card)
            except ResultHistoryError as exc:
                raise ResultHistoryError(
                    f"terminal zone card is invalid: {exc}"
                ) from exc
        run_ids = [
            card.run_id
            for card in (*self.active_cards, *self.terminal_cards)
        ]
        if len(set(run_ids)) != len(run_ids):
            raise ResultHistoryError("duplicate run_id in snapshot")
        cached_ids = set(run_ids)
        if (
            self.selected_run_id is not None
            and self.selected_run_id not in cached_ids
        ):
            raise ResultHistoryError("dangling selected_run_id in snapshot")
        if (
            self.follow_latest
            and cached_ids
            and self.selected_run_id is None
        ):
            raise ResultHistoryError(
                "locked selection must identify a cached result card"
            )


@dataclass(frozen=True)
class ResultHistoryTransition:
    snapshot: ResultHistorySnapshot
    evicted_run_id: str | None = None
    selection_changed: bool = False
    notice: str | None = None
    changed: bool = False

    def __post_init__(self) -> None:
        _require_exact_instance(self, ResultHistoryTransition)
        if type(self.snapshot) is not ResultHistorySnapshot:
            raise ResultHistoryError(
                "snapshot must be an exact ResultHistorySnapshot"
            )
        if self.evicted_run_id is not None:
            _require_nonempty_string(
                self.evicted_run_id,
                "evicted_run_id",
            )
        if type(self.selection_changed) is not bool:
            raise ResultHistoryError("selection_changed must be a bool")
        if self.notice is not None and type(self.notice) is not str:
            raise ResultHistoryError("notice must be a string or None")
        if type(self.changed) is not bool:
            raise ResultHistoryError("changed must be a bool")
        if not self.changed and (
            self.selection_changed
            or self.evicted_run_id is not None
            or self.notice is not None
        ):
            raise ResultHistoryError(
                "changed must be true when transition metadata changes"
            )
        cached_ids = {
            card.run_id
            for card in (
                *self.snapshot.active_cards,
                *self.snapshot.terminal_cards,
            )
        }
        if (
            self.evicted_run_id is not None
            and self.evicted_run_id in cached_ids
        ):
            raise ResultHistoryError(
                "evicted run must be absent from the snapshot"
            )
        if self.notice is not None:
            if (
                self.notice != _EVICTION_NOTICE
                or self.evicted_run_id is None
                or not self.selection_changed
                or self.snapshot.follow_latest
            ):
                raise ResultHistoryError(
                    "notice is inconsistent with an evicted selection"
                )


@dataclass(frozen=True)
class _ReanalysisBackup:
    card: ResultHistoryCardSnapshot
    payload: ResultDisplayPayload


def _clone_config_summary(
    config: object,
) -> FrozenConfigSummary:
    if type(config) is not FrozenConfigSummary:
        raise ResultHistoryError(
            "card config must be an exact FrozenConfigSummary"
        )
    return FrozenConfigSummary(
        config_id=config.config_id,
        name=config.name,
        fault_types=tuple(config.fault_types),
        details=tuple(
            ConfigSummaryField(label=item.label, value=item.value)
            for item in config.details
        ),
    )


def _clone_card(card: object) -> ResultHistoryCardSnapshot:
    if type(card) is not ResultHistoryCardSnapshot:
        raise ResultHistoryError(
            "card must be an exact ResultHistoryCardSnapshot"
        )
    return ResultHistoryCardSnapshot(
        run_id=card.run_id,
        config=_clone_config_summary(card.config),
        sn=card.sn,
        trigger_source=card.trigger_source,
        started_at=card.started_at,
        terminated_at=card.terminated_at,
        phase=card.phase,
        conclusion=card.conclusion,
        reason=card.reason,
        is_active_run=card.is_active_run,
        manual_judgment_allowed=card.manual_judgment_allowed,
        manual_reanalysis_allowed=card.manual_reanalysis_allowed,
        revision=card.revision,
        operation_id=card.operation_id,
        fault_type=card.fault_type,
        manual_judgment_revision=card.manual_judgment_revision,
    )


def _copy_payload_value(payload: object) -> ResultDisplayPayload:
    if type(payload) not in (ReviewDisplayPayload, ErrorDisplayPayload):
        raise ResultHistoryError(
            "payload must be an exact display payload wrapper"
        )
    try:
        copied = deepcopy(payload)
    except Exception as exc:
        raise ResultHistoryError("display payload copy failed") from exc
    if type(copied) is ReviewDisplayPayload:
        return ReviewDisplayPayload(
            review=copied.review,
            dashboard_config=copied.dashboard_config,
        )
    if type(copied) is ErrorDisplayPayload:
        return ErrorDisplayPayload(
            error_category=copied.error_category,
            reason=copied.reason,
            context=tuple(
                ConfigSummaryField(label=item.label, value=item.value)
                for item in copied.context
            ),
        )
    raise ResultHistoryError("display payload copy returned an invalid type")


def _prepare_event(event: object) -> ResultHistoryEvent:
    event_type = type(event)
    if event_type not in {
        ActiveRunChanged,
        RunTerminated,
        TerminalResultReplaced,
        ReanalysisStarted,
        ReanalysisCompleted,
        ReanalysisCancelled,
        ActiveRunRemoved,
    }:
        raise ResultHistoryError(
            f"value must be an exact event, got {event_type.__name__}"
        )
    if event_type is ActiveRunChanged:
        return ActiveRunChanged(card=_clone_card(event.card))
    if event_type is RunTerminated:
        card = _clone_card(event.card)
        payload = _validate_payload_variants(
            event.review_payload,
            event.error_payload,
        )
        owned = _copy_payload_value(payload)
        return RunTerminated(
            card=card,
            review_payload=(
                owned if type(owned) is ReviewDisplayPayload else None
            ),
            error_payload=(
                owned if type(owned) is ErrorDisplayPayload else None
            ),
        )
    if event_type is TerminalResultReplaced:
        card = _clone_card(event.card)
        payload = _validate_payload_variants(
            event.review_payload,
            event.error_payload,
        )
        owned = _copy_payload_value(payload)
        return TerminalResultReplaced(
            run_id=event.run_id,
            expected_revision=event.expected_revision,
            card=card,
            review_payload=(
                owned if type(owned) is ReviewDisplayPayload else None
            ),
            error_payload=(
                owned if type(owned) is ErrorDisplayPayload else None
            ),
        )
    if event_type is ReanalysisStarted:
        return ReanalysisStarted(
            run_id=event.run_id,
            operation_id=event.operation_id,
            expected_revision=event.expected_revision,
        )
    if event_type is ReanalysisCompleted:
        card = _clone_card(event.card)
        payload = _validate_reanalysis_outcome_values(
            card,
            event.review_payload,
            event.error_payload,
        )
        owned = _copy_payload_value(payload)
        return ReanalysisCompleted(
            run_id=event.run_id,
            operation_id=event.operation_id,
            expected_revision=event.expected_revision,
            card=card,
            review_payload=(
                owned if type(owned) is ReviewDisplayPayload else None
            ),
            error_payload=(
                owned if type(owned) is ErrorDisplayPayload else None
            ),
        )
    if event_type is ReanalysisCancelled:
        return ReanalysisCancelled(
            run_id=event.run_id,
            operation_id=event.operation_id,
            expected_revision=event.expected_revision,
        )
    return ActiveRunRemoved(run_id=event.run_id)


class ResultHistoryStore:
    MAX_TERMINAL_RESULTS = _MAX_TERMINAL_RESULTS
    EVICTION_NOTICE = _EVICTION_NOTICE

    def __init__(self) -> None:
        self._lock = RLock()
        self._active_cards: dict[str, ResultHistoryCardSnapshot] = {}
        self._active_order: list[str] = []
        self._terminal_cards: dict[str, ResultHistoryCardSnapshot] = {}
        self._terminal_order: list[str] = []
        self._terminal_sequences: dict[str, int] = {}
        self._display_payloads: dict[str, ResultDisplayPayload] = {}
        self._reanalysis_backups: dict[str, _ReanalysisBackup] = {}
        self._completion_sequence = 0
        self._revision = 0
        self._follow_latest = True
        self._selected_run_id: str | None = None

    def apply(self, event: ResultHistoryEvent) -> ResultHistoryTransition:
        prepared_event = _prepare_event(event)
        retired_refs: list[object] = []
        with self._lock:
            event_type = type(prepared_event)
            if event_type is ActiveRunChanged:
                transition = self._apply_active_changed(prepared_event)
            elif event_type is RunTerminated:
                transition = self._apply_terminated(
                    prepared_event,
                    retired_refs,
                )
            elif event_type is TerminalResultReplaced:
                transition = self._apply_terminal_replaced(
                    prepared_event,
                    retired_refs,
                )
            elif event_type is ReanalysisStarted:
                transition = self._apply_reanalysis_started(prepared_event)
            elif event_type is ReanalysisCompleted:
                transition = self._apply_reanalysis_completed(
                    prepared_event,
                    retired_refs,
                )
            elif event_type is ReanalysisCancelled:
                transition = self._apply_reanalysis_cancelled(
                    prepared_event,
                    retired_refs,
                )
            else:
                transition = self._apply_active_removed(prepared_event)
        retired_refs.clear()
        return transition

    def snapshot(self) -> ResultHistorySnapshot:
        with self._lock:
            return self._snapshot_locked()

    def display_payload(self, run_id: str) -> ResultDisplayPayload | None:
        self._validate_run_id(run_id)
        with self._lock:
            payload = self._display_payloads.get(run_id)
        if payload is None:
            return None
        return _copy_payload_value(payload)

    def select_run(self, run_id: str) -> ResultHistoryTransition:
        self._validate_run_id(run_id)
        with self._lock:
            if (
                run_id not in self._active_cards
                and run_id not in self._terminal_cards
            ):
                raise ResultHistoryError(f"run {run_id!r} is not cached")
            if self._selected_run_id == run_id:
                return self._unchanged_transition_locked()
            self._selected_run_id = run_id
            self._revision += 1
            return ResultHistoryTransition(
                snapshot=self._snapshot_locked(),
                selection_changed=True,
                changed=True,
            )

    def set_follow_latest(self, enabled: bool) -> ResultHistoryTransition:
        if type(enabled) is not bool:
            raise ResultHistoryError("follow_latest must be a bool")
        with self._lock:
            old_selection = self._selected_run_id
            desired_selection = (
                self._top_run_id_locked()
                if enabled
                else self._selected_run_id
            )
            if (
                self._follow_latest == enabled
                and self._selected_run_id == desired_selection
            ):
                return self._unchanged_transition_locked()
            self._follow_latest = enabled
            self._selected_run_id = desired_selection
            self._revision += 1
            return ResultHistoryTransition(
                snapshot=self._snapshot_locked(),
                selection_changed=self._selected_run_id != old_selection,
                changed=True,
            )

    def _apply_active_changed(
        self, event: ActiveRunChanged
    ) -> ResultHistoryTransition:
        card = event.card
        if card.run_id in self._terminal_cards:
            raise ResultHistoryError(f"run {card.run_id!r} is already terminal")
        is_new_run = card.run_id not in self._active_cards
        previous = self._active_cards.get(card.run_id)
        if previous == card:
            return self._unchanged_transition_locked()
        if is_new_run:
            self._active_order.insert(0, card.run_id)
        self._active_cards[card.run_id] = card
        old_selection = self._selected_run_id
        if self._follow_latest and is_new_run:
            self._selected_run_id = card.run_id
        self._revision += 1
        return ResultHistoryTransition(
            snapshot=self._snapshot_locked(),
            selection_changed=self._selected_run_id != old_selection,
            changed=True,
        )

    def _apply_terminated(
        self,
        event: RunTerminated,
        retired_refs: list[object],
    ) -> ResultHistoryTransition:
        card = event.card
        if card.run_id in self._terminal_cards:
            raise ResultHistoryError(f"run {card.run_id!r} is already terminal")
        payload: ResultDisplayPayload = (
            event.review_payload
            if event.review_payload is not None
            else event.error_payload
        )

        old_selection = self._selected_run_id
        was_active = card.run_id in self._active_cards
        self._active_cards.pop(card.run_id, None)
        if card.run_id in self._active_order:
            self._active_order.remove(card.run_id)
        self._terminal_cards[card.run_id] = card
        self._terminal_order.insert(0, card.run_id)
        self._completion_sequence += 1
        self._terminal_sequences[card.run_id] = self._completion_sequence
        self._display_payloads[card.run_id] = payload
        if self._follow_latest and (
            not was_active or old_selection == card.run_id
        ):
            self._selected_run_id = card.run_id

        evicted_run_id = None
        notice = None
        if len(self._terminal_order) > self.MAX_TERMINAL_RESULTS:
            evicted_run_id = self._terminal_order.pop()
            self._terminal_cards.pop(evicted_run_id)
            self._terminal_sequences.pop(evicted_run_id)
            retired_refs.append(
                self._display_payloads.pop(evicted_run_id)
            )
            backup = self._reanalysis_backups.pop(evicted_run_id, None)
            if backup is not None:
                retired_refs.append(backup)
            if (
                not self._follow_latest
                and old_selection == evicted_run_id
            ):
                self._selected_run_id = (
                    self._terminal_order[0] if self._terminal_order else None
                )
                notice = self.EVICTION_NOTICE
        self._revision += 1
        return ResultHistoryTransition(
            snapshot=self._snapshot_locked(),
            evicted_run_id=evicted_run_id,
            selection_changed=self._selected_run_id != old_selection,
            notice=notice,
            changed=True,
        )

    def _apply_active_removed(
        self, event: ActiveRunRemoved
    ) -> ResultHistoryTransition:
        if event.run_id not in self._active_cards:
            raise ResultHistoryError(f"active run {event.run_id!r} is not cached")
        old_selection = self._selected_run_id
        self._active_cards.pop(event.run_id)
        self._active_order.remove(event.run_id)
        if old_selection == event.run_id:
            self._selected_run_id = (
                self._top_run_id_locked()
                if self._follow_latest
                else (
                    self._terminal_order[0]
                    if self._terminal_order
                    else None
                )
            )
        self._revision += 1
        return ResultHistoryTransition(
            snapshot=self._snapshot_locked(),
            selection_changed=self._selected_run_id != old_selection,
            changed=True,
        )

    def _apply_terminal_replaced(
        self,
        event: TerminalResultReplaced,
        retired_refs: list[object],
    ) -> ResultHistoryTransition:
        current = self._terminal_for_cas(event.run_id, event.expected_revision)
        if current.operation_id is not None:
            raise ResultHistoryError(
                "terminal replacement is unavailable during a reanalysis operation"
            )
        payload: ResultDisplayPayload = (
            event.review_payload
            if event.review_payload is not None
            else event.error_payload
        )
        replacement_card = replace(
            event.card,
            revision=current.revision + 1,
            operation_id=None,
        )
        retired_refs.append(self._display_payloads[event.run_id])
        self._terminal_cards[event.run_id] = replacement_card
        self._display_payloads[event.run_id] = payload
        self._revision += 1
        return self._changed_transition_locked()

    def _apply_reanalysis_started(
        self, event: ReanalysisStarted
    ) -> ResultHistoryTransition:
        current = self._terminal_for_cas(event.run_id, event.expected_revision)
        if current.operation_id is not None:
            raise ResultHistoryError("a reanalysis operation is already active")
        if event.run_id in self._reanalysis_backups:
            raise ResultHistoryError("a reanalysis backup already exists")
        if not current.manual_reanalysis_allowed:
            raise ResultHistoryError("reanalysis is not allowed for this result")
        payload = self._display_payloads.get(event.run_id)
        if payload is None:
            raise ResultHistoryError(
                f"terminal run {event.run_id!r} has no display payload"
            )
        self._reanalysis_backups[event.run_id] = _ReanalysisBackup(
            card=current,
            payload=payload,
        )
        self._terminal_cards[event.run_id] = replace(
            current,
            phase=ResultPhase.REANALYZING,
            manual_judgment_allowed=False,
            manual_reanalysis_allowed=False,
            revision=current.revision + 1,
            operation_id=event.operation_id,
        )
        self._revision += 1
        return self._changed_transition_locked()

    def _apply_reanalysis_completed(
        self,
        event: ReanalysisCompleted,
        retired_refs: list[object],
    ) -> ResultHistoryTransition:
        current = self._terminal_for_cas(event.run_id, event.expected_revision)
        self._validate_current_operation(current, event.operation_id)
        if event.run_id not in self._reanalysis_backups:
            raise ResultHistoryError("reanalysis operation backup is missing")
        payload: ResultDisplayPayload = (
            event.review_payload
            if event.review_payload is not None
            else event.error_payload
        )
        replacement_card = replace(
            event.card,
            revision=current.revision + 1,
            operation_id=None,
        )
        retired_refs.append(self._display_payloads[event.run_id])
        retired_refs.append(self._reanalysis_backups[event.run_id])
        self._terminal_cards[event.run_id] = replacement_card
        self._display_payloads[event.run_id] = payload
        self._reanalysis_backups.pop(event.run_id)
        self._revision += 1
        return self._changed_transition_locked()

    def _apply_reanalysis_cancelled(
        self,
        event: ReanalysisCancelled,
        retired_refs: list[object],
    ) -> ResultHistoryTransition:
        current = self._terminal_for_cas(event.run_id, event.expected_revision)
        self._validate_current_operation(current, event.operation_id)
        backup = self._reanalysis_backups.get(event.run_id)
        if backup is None:
            raise ResultHistoryError("reanalysis operation backup is missing")
        restored_card = replace(
            backup.card,
            revision=current.revision + 1,
            operation_id=None,
        )
        retired_refs.append(self._display_payloads[event.run_id])
        retired_refs.append(backup)
        self._terminal_cards[event.run_id] = restored_card
        self._display_payloads[event.run_id] = backup.payload
        self._reanalysis_backups.pop(event.run_id)
        self._revision += 1
        return self._changed_transition_locked()

    def _terminal_for_cas(
        self, run_id: str, expected_revision: int
    ) -> ResultHistoryCardSnapshot:
        current = self._terminal_cards.get(run_id)
        if current is None:
            raise ResultHistoryError(f"terminal run {run_id!r} is not cached")
        if current.revision != expected_revision:
            raise ResultHistoryError(
                f"card revision mismatch for run {run_id!r}: "
                f"expected {expected_revision}, current {current.revision}"
            )
        return current

    def _validate_replacement_card(
        self, run_id: str, card: ResultHistoryCardSnapshot
    ) -> None:
        if card.run_id != run_id:
            raise ResultHistoryError("event run_id does not match replacement card")
        self._validate_terminal_card(card)

    def _validate_current_operation(
        self,
        current: ResultHistoryCardSnapshot,
        operation_id: str,
    ) -> None:
        if current.operation_id is None:
            raise ResultHistoryError("no reanalysis operation is active")
        if current.operation_id != operation_id:
            raise ResultHistoryError("reanalysis operation identity mismatch")
        if current.phase is not ResultPhase.REANALYZING:
            raise ResultHistoryError("reanalysis operation phase is invalid")

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if type(run_id) is not str or not run_id.strip():
            raise ResultHistoryError("run_id must be a non-empty string")

    def _snapshot_locked(self) -> ResultHistorySnapshot:
        return ResultHistorySnapshot(
            revision=self._revision,
            follow_latest=self._follow_latest,
            selected_run_id=self._selected_run_id,
            active_cards=tuple(
                self._active_cards[run_id] for run_id in self._active_order
            ),
            terminal_cards=tuple(
                self._terminal_cards[run_id] for run_id in self._terminal_order
            ),
        )

    def _top_run_id_locked(self) -> str | None:
        if self._active_order:
            return self._active_order[0]
        if self._terminal_order:
            return self._terminal_order[0]
        return None

    def _unchanged_transition_locked(self) -> ResultHistoryTransition:
        return ResultHistoryTransition(snapshot=self._snapshot_locked())

    def _changed_transition_locked(self) -> ResultHistoryTransition:
        return ResultHistoryTransition(
            snapshot=self._snapshot_locked(),
            changed=True,
        )
