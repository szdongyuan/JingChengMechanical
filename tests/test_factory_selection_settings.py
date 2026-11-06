from __future__ import annotations

import app.service.factory_selection_settings as factory_selection_settings_module
from app.service.factory_selection_settings import FactorySelectionSettings
from PyQt6.QtCore import QSettings
import pytest


class _FakeSettings:
    def __init__(self):
        self._values: dict[str, object] = {}

    def value(self, key: str, default=None):
        return self._values.get(key, default)

    def setValue(self, key: str, value) -> None:
        self._values[key] = value

    def sync(self) -> None:
        pass

    def remove(self, key: str) -> None:
        self._values.pop(key, None)


def test_selection_lease_rejects_committed_settings_save() -> None:
    raw = _FakeSettings()
    settings = FactorySelectionSettings(raw)
    assert settings.save_selected_config_ids((101,)) is None
    generation, ids = settings.load_committed_selection()
    lease = settings.acquire_selection_lease(generation, ids)

    assert settings.save_selected_config_ids((202,)) == (
        "当前测试使用的配置选择已固定，暂时无法更改。"
    )
    assert settings.load_committed_selection() == (generation, ids)
    settings.release_selection_lease(lease)


def test_selection_lease_allows_nonempty_subset_of_prepared_configs() -> None:
    raw = _FakeSettings()
    settings = FactorySelectionSettings(raw)
    assert settings.save_selected_config_ids((101, 202)) is None
    generation, ids = settings.load_committed_selection()
    lease = settings.acquire_selection_lease(generation, ids)

    assert settings.save_selected_config_ids((202,)) is None
    next_generation, next_ids = settings.load_committed_selection()
    assert next_generation == generation + 1
    assert next_ids == (202,)
    assert settings.save_selected_config_ids(()) == (
        "请至少保留一个配置；空选择不会保存。"
    )
    assert settings.load_committed_selection() == (
        next_generation,
        next_ids,
    )
    settings.release_selection_lease(lease)


def test_selection_lease_generation_and_release_are_exact() -> None:
    raw = _FakeSettings()
    settings = FactorySelectionSettings(raw)
    assert settings.save_selected_config_ids((303, 101)) is None
    generation, ids = settings.load_committed_selection()

    with pytest.raises(ValueError, match="authoritative selection"):
        settings.acquire_selection_lease(generation + 1, ids)
    lease = settings.acquire_selection_lease(generation, ids)
    with pytest.raises(ValueError, match="active"):
        settings.acquire_selection_lease(generation, ids)
    with pytest.raises(ValueError, match="lease"):
        settings.release_selection_lease(object())
    settings.release_selection_lease(lease)
    with pytest.raises(ValueError, match="lease"):
        settings.release_selection_lease(lease)
    next_lease = settings.acquire_selection_lease(generation, ids)
    assert next_lease is not lease
    settings.release_selection_lease(next_lease)


def test_selection_lease_is_process_local_and_exclusive_across_holders() -> None:
    raw = _FakeSettings()
    first_holder = FactorySelectionSettings(raw)
    second_holder = FactorySelectionSettings(raw)
    assert first_holder.save_selected_config_ids((101, 202)) is None
    generation, ids = first_holder.load_committed_selection()
    lease = first_holder.acquire_selection_lease(generation, ids)

    try:
        with pytest.raises(ValueError, match="active"):
            second_holder.acquire_selection_lease(generation, ids)
        assert second_holder.save_selected_config_ids((303,)) == (
            "当前测试使用的配置选择已固定，暂时无法更改。"
        )
        with pytest.raises(ValueError, match="lease"):
            second_holder.release_selection_lease(object())
    finally:
        first_holder.release_selection_lease(lease)

    next_lease = second_holder.acquire_selection_lease(generation, ids)
    assert next_lease is not lease
    second_holder.release_selection_lease(next_lease)


class _ExplodingSettings:
    def value(self, key: str, default=None):
        return default

    def setValue(self, key: str, value) -> None:
        raise RuntimeError("boom")

    def sync(self) -> None:
        raise AssertionError("sync should not run after a failed set")


class _FreshStatusQSettings:
    Status = QSettings.Status
    _backing: dict[str, object] = {}
    _instances: list["_FreshStatusQSettings"] = []
    _sync_outcomes: list[tuple[QSettings.Status, bool]] = []

    @classmethod
    def reset(cls, *, sync_outcomes: list[tuple[QSettings.Status, bool]]) -> None:
        cls._backing = {}
        cls._instances = []
        cls._sync_outcomes = list(sync_outcomes)

    def __init__(self, organization: str, application: str):
        del organization, application
        self.__class__._instances.append(self)
        self._status = self.Status.NoError
        self._pending = dict(self.__class__._backing)

    def value(self, key: str, default=None):
        return self.__class__._backing.get(key, default)

    def setValue(self, key: str, value) -> None:
        self._pending[key] = value

    def sync(self) -> None:
        if self.__class__._sync_outcomes:
            self._status, should_commit = self.__class__._sync_outcomes.pop(0)
        else:
            self._status, should_commit = (self.Status.NoError, True)
        if should_commit:
            self.__class__._backing.clear()
            self.__class__._backing.update(self._pending)

    def status(self):
        return self._status


class _SharedBackingSettings:
    def __init__(
        self,
        *,
        backing: dict[str, object],
        sync_outcomes: list[tuple[QSettings.Status, bool]],
    ):
        self._backing = backing
        self._pending = dict(backing)
        self._sync_outcomes = sync_outcomes
        self._status = QSettings.Status.NoError

    def value(self, key: str, default=None):
        return self._backing.get(key, default)

    def setValue(self, key: str, value) -> None:
        self._pending[key] = value

    def sync(self) -> None:
        if self._sync_outcomes:
            self._status, should_commit = self._sync_outcomes.pop(0)
        else:
            self._status, should_commit = (QSettings.Status.NoError, True)
        if should_commit:
            self._backing.clear()
            self._backing.update(self._pending)

    def status(self):
        return self._status

    def remove(self, key: str) -> None:
        self._pending.pop(key, None)


class _WrongPayloadOnFailedSyncSettings:
    def __init__(
        self,
        *,
        backing: dict[str, object],
        committed_slot_payload: str,
    ):
        self._backing = backing
        self._pending = dict(backing)
        self._committed_slot_payload = committed_slot_payload
        self._status = QSettings.Status.NoError
        self._sync_calls = 0

    def value(self, key: str, default=None):
        return self._backing.get(key, default)

    def setValue(self, key: str, value) -> None:
        self._pending[key] = value

    def sync(self) -> None:
        self._sync_calls += 1
        if self._sync_calls == 1:
            self._status = QSettings.Status.NoError
            self._backing.clear()
            self._backing.update(self._pending)
            return

        self._status = QSettings.Status.AccessError
        self._backing.clear()
        self._backing.update(self._pending)
        self._backing[
            "factory/selected_config_ids/slot_b"
        ] = self._committed_slot_payload

    def status(self):
        return self._status


class _WrongCommitRecordOnFailedSyncSettings:
    def __init__(
        self,
        *,
        backing: dict[str, object],
        wrong_commit_payload: str,
    ):
        self._backing = backing
        self._pending = dict(backing)
        self._wrong_commit_payload = wrong_commit_payload
        self._status = QSettings.Status.NoError
        self._failed_commit_written = False
        self._last_written_key: str | None = None

    def value(self, key: str, default=None):
        return self._backing.get(key, default)

    def setValue(self, key: str, value) -> None:
        self._last_written_key = key
        self._pending[key] = value

    def sync(self) -> None:
        self._backing.clear()
        self._backing.update(self._pending)
        if (
            self._last_written_key == "factory/selected_config_ids/commit_a"
            and not self._failed_commit_written
            and self._backing.get("factory/selected_config_ids/slot_b") is not None
        ):
            self._status = QSettings.Status.AccessError
            self._backing[
                "factory/selected_config_ids/commit_a"
            ] = self._wrong_commit_payload
            self._failed_commit_written = True
            return
        self._status = QSettings.Status.NoError

    def status(self):
        return self._status


class _LegacyBootstrapWrongSlotSettings:
    def __init__(
        self,
        *,
        backing: dict[str, object],
        wrong_slot_payload: str,
    ):
        self._backing = backing
        self._pending = dict(backing)
        self._wrong_slot_payload = wrong_slot_payload
        self._status = QSettings.Status.NoError
        self._failed_bootstrap_write = False
        self._last_written_key: str | None = None

    def value(self, key: str, default=None):
        return self._backing.get(key, default)

    def setValue(self, key: str, value) -> None:
        self._last_written_key = key
        self._pending[key] = value

    def sync(self) -> None:
        self._backing.clear()
        self._backing.update(self._pending)
        if (
            self._last_written_key == "factory/selected_config_ids/slot_a"
            and not self._failed_bootstrap_write
        ):
            self._status = QSettings.Status.AccessError
            self._backing[
                "factory/selected_config_ids/slot_a"
            ] = self._wrong_slot_payload
            self._failed_bootstrap_write = True
            return
        self._status = QSettings.Status.NoError

    def status(self):
        return self._status


def test_factory_selection_settings_round_trips_ordered_ids():
    store = FactorySelectionSettings(settings=_FakeSettings())

    assert store.load_selected_config_ids() == ()
    assert store.save_selected_config_ids((303, 101, 202)) is None
    assert store.load_selected_config_ids() == (303, 101, 202)


def test_factory_selection_settings_returns_error_when_save_fails():
    store = FactorySelectionSettings(settings=_ExplodingSettings())

    error = store.save_selected_config_ids((101, 202))

    assert error == "配置保存失败"


def test_factory_selection_settings_uncommitted_status_error_keeps_last_successful_selection():
    backing: dict[str, object] = {}
    store = FactorySelectionSettings(
        settings=_SharedBackingSettings(
            backing=backing,
            sync_outcomes=[
                (QSettings.Status.NoError, True),
                (QSettings.Status.NoError, True),
                (QSettings.Status.NoError, True),
                (QSettings.Status.AccessError, False),
            ],
        )
    )

    assert store.save_selected_config_ids((303, 101)) is None
    assert store.save_selected_config_ids((303, 101, 202)) == "配置保存失败"
    assert store.load_selected_config_ids() == (303, 101)

    reopened_store = FactorySelectionSettings(
        settings=_SharedBackingSettings(
            backing=backing,
            sync_outcomes=[],
        )
    )

    assert reopened_store.load_selected_config_ids() == (303, 101)


def test_factory_selection_settings_failed_verification_quarantines_newer_decodable_slot():
    backing: dict[str, object] = {}
    store = FactorySelectionSettings(
        settings=_WrongPayloadOnFailedSyncSettings(
            backing=backing,
            committed_slot_payload='{"generation":2,"selected_config_ids":[999]}',
        )
    )

    assert store.save_selected_config_ids((303, 101)) is None
    assert store.save_selected_config_ids((303, 101, 202)) == "配置保存失败"
    assert store.load_selected_config_ids() == (303, 101)

    reopened_store = FactorySelectionSettings(
        settings=_SharedBackingSettings(
            backing=backing,
            sync_outcomes=[],
        )
    )

    assert reopened_store.load_selected_config_ids() == (303, 101)


def test_factory_selection_settings_ignores_decodable_commit_record_that_mismatches_data_slot():
    backing: dict[str, object] = {}
    store = FactorySelectionSettings(
        settings=_WrongCommitRecordOnFailedSyncSettings(
            backing=backing,
            wrong_commit_payload='{"generation":2,"selected_config_ids":[999],"data_slot_key":"factory/selected_config_ids/slot_b"}',
        )
    )

    assert store.save_selected_config_ids((303, 101)) is None
    assert store.save_selected_config_ids((303, 101, 202)) == "配置保存失败"
    assert store.load_selected_config_ids() == (303, 101)

    reopened_store = FactorySelectionSettings(
        settings=_SharedBackingSettings(
            backing=backing,
            sync_outcomes=[],
        )
    )

    assert reopened_store.load_selected_config_ids() == (303, 101)


def test_factory_selection_settings_valid_legacy_selection_outranks_orphan_bootstrap_slot():
    backing: dict[str, object] = {
        FactorySelectionSettings.SETTINGS_KEY: "[303,101]",
    }
    store = FactorySelectionSettings(
        settings=_LegacyBootstrapWrongSlotSettings(
            backing=backing,
            wrong_slot_payload='{"generation":1,"selected_config_ids":[999]}',
        )
    )

    assert store.save_selected_config_ids((303, 101, 202)) == "配置保存失败"
    assert store.load_selected_config_ids() == (303, 101)

    reopened_store = FactorySelectionSettings(
        settings=_SharedBackingSettings(
            backing=backing,
            sync_outcomes=[],
        )
    )

    assert reopened_store.load_selected_config_ids() == (303, 101)


def test_factory_selection_settings_committed_status_error_is_treated_as_success():
    backing: dict[str, object] = {}
    store = FactorySelectionSettings(
        settings=_SharedBackingSettings(
            backing=backing,
            sync_outcomes=[
                (QSettings.Status.NoError, True),
                (QSettings.Status.NoError, True),
                (QSettings.Status.NoError, True),
                (QSettings.Status.AccessError, True),
                (QSettings.Status.AccessError, True),
            ],
        )
    )

    assert store.save_selected_config_ids((303, 101)) is None
    assert store.save_selected_config_ids((303, 101, 202)) is None
    assert store.load_selected_config_ids() == (303, 101, 202)

    reopened_store = FactorySelectionSettings(
        settings=_SharedBackingSettings(
            backing=backing,
            sync_outcomes=[],
        )
    )

    assert reopened_store.load_selected_config_ids() == (303, 101, 202)


def test_factory_selection_settings_default_usage_treats_committed_status_error_as_success(
    monkeypatch,
):
    _FreshStatusQSettings.reset(
        sync_outcomes=[
            (QSettings.Status.AccessError, True),
        ]
    )
    monkeypatch.setattr(
        factory_selection_settings_module,
        "QSettings",
        _FreshStatusQSettings,
    )
    store = FactorySelectionSettings()

    assert store.save_selected_config_ids((101, 202)) is None
    assert store.load_selected_config_ids() == (101, 202)

    reopened_store = FactorySelectionSettings()
    assert reopened_store.load_selected_config_ids() == (101, 202)


def test_pending_selection_distinguishes_missing_from_empty_and_can_be_cleared():
    raw = _FakeSettings()
    store = FactorySelectionSettings(raw)

    assert store.load_pending_selected_config_ids() is None
    assert store.save_pending_selected_config_ids(()) is None
    assert store.load_pending_selected_config_ids() == ()
    assert store.save_pending_selected_config_ids((303, 101)) is None
    assert store.load_pending_selected_config_ids() == (303, 101)

    store.clear_pending_selected_config_ids()

    assert store.load_pending_selected_config_ids() is None


@pytest.mark.parametrize(
    "invalid_ids",
    [
        (101, 101),
        (True,),
        ("101",),
    ],
)
def test_pending_selection_rejects_duplicates_and_non_exact_integers(
    invalid_ids,
):
    raw = _FakeSettings()
    store = FactorySelectionSettings(raw)
    assert store.save_pending_selected_config_ids((303,)) is None

    error = store.save_pending_selected_config_ids(invalid_ids)

    assert error == "下次启动配置选择无效"
    assert store.load_pending_selected_config_ids() == (303,)


def test_pending_selection_reports_write_failure_in_chinese():
    store = FactorySelectionSettings(settings=_ExplodingSettings())

    assert (
        store.save_pending_selected_config_ids((101, 202))
        == "下次启动配置保存失败"
    )


@pytest.mark.parametrize(
    "sync_outcome",
    [
        (QSettings.Status.AccessError, True),
        (QSettings.Status.NoError, False),
    ],
)
def test_pending_selection_reports_sync_or_readback_failure(sync_outcome):
    store = FactorySelectionSettings(
        settings=_SharedBackingSettings(
            backing={},
            sync_outcomes=[sync_outcome],
        )
    )

    assert (
        store.save_pending_selected_config_ids((101, 202))
        == "下次启动配置保存失败"
    )


@pytest.mark.parametrize(
    "prior_raw",
    [
        '[303,101]',
        None,
    ],
)
def test_pending_selection_post_write_failure_restores_exact_prior_raw_value(
    prior_raw,
):
    key = FactorySelectionSettings.PENDING_SETTINGS_KEY
    backing = {} if prior_raw is None else {key: prior_raw}
    raw = _SharedBackingSettings(
        backing=backing,
        sync_outcomes=[
            (QSettings.Status.AccessError, True),
            (QSettings.Status.NoError, True),
        ],
    )
    store = FactorySelectionSettings(settings=raw)

    error = store.save_pending_selected_config_ids((202,))

    assert error == "下次启动配置保存失败"
    missing = object()
    assert raw.value(key, missing) == (
        missing if prior_raw is None else prior_raw
    )
    assert store.load_pending_selected_config_ids() == (
        None if prior_raw is None else (303, 101)
    )


def test_pending_selection_failed_rollback_reconciles_durable_requested_value():
    key = FactorySelectionSettings.PENDING_SETTINGS_KEY
    backing = {key: '[101]'}
    raw = _SharedBackingSettings(
        backing=backing,
        sync_outcomes=[
            (QSettings.Status.AccessError, True),
            (QSettings.Status.AccessError, False),
        ],
    )
    store = FactorySelectionSettings(settings=raw)

    error = store.save_pending_selected_config_ids((202,))

    assert error is None
    assert store.load_pending_selected_config_ids() == (202,)


def test_reconcile_for_startup_prefers_pending_and_prunes_missing_duplicates():
    raw = _FakeSettings()
    store = FactorySelectionSettings(raw)
    assert store.save_selected_config_ids((101, 202)) is None
    raw.setValue(
        FactorySelectionSettings.PENDING_SETTINGS_KEY,
        "[303,303,999,202]",
    )

    selected = store.reconcile_for_startup((202, 303, 404))

    assert selected == (303, 202)
    assert store.load_selected_config_ids() == (303, 202)
    assert store.load_pending_selected_config_ids() is None


def test_reconcile_for_startup_falls_back_to_first_existing_config():
    raw = _FakeSettings()
    store = FactorySelectionSettings(raw)
    assert store.save_selected_config_ids((999,)) is None

    selected = store.reconcile_for_startup((404, 303))

    assert selected == (404,)
    assert store.load_selected_config_ids() == (404,)


def test_reconcile_for_startup_keeps_generation_when_selection_is_unchanged():
    raw = _FakeSettings()
    store = FactorySelectionSettings(raw)
    assert store.save_selected_config_ids((303, 101)) is None
    generation, _ = store.load_committed_selection()
    assert store.save_pending_selected_config_ids((303, 101)) is None

    selected = store.reconcile_for_startup((101, 303))

    assert selected == (303, 101)
    assert store.load_committed_selection() == (generation, (303, 101))
    assert store.load_pending_selected_config_ids() is None


@pytest.mark.parametrize(
    "existing_ids",
    [
        (101, 101),
        (0,),
        (-1,),
        (True,),
        ("101",),
    ],
)
def test_reconcile_for_startup_rejects_invalid_existing_ids(existing_ids):
    store = FactorySelectionSettings(_FakeSettings())

    with pytest.raises(ValueError, match="existing config IDs"):
        store.reconcile_for_startup(existing_ids)


def test_reconcile_for_startup_keeps_pending_when_commit_fails(monkeypatch):
    raw = _FakeSettings()
    store = FactorySelectionSettings(raw)
    assert store.save_selected_config_ids((101,)) is None
    assert store.save_pending_selected_config_ids((202,)) is None
    monkeypatch.setattr(
        store,
        "_save_selected_config_ids_locked",
        lambda config_ids: "配置保存失败",
    )

    with pytest.raises(RuntimeError, match="配置保存失败"):
        store.reconcile_for_startup((101, 202))

    assert store.load_pending_selected_config_ids() == (202,)
