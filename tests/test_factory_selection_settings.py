from __future__ import annotations

import app.service.factory_selection_settings as factory_selection_settings_module
from app.service.factory_selection_settings import FactorySelectionSettings
from PyQt6.QtCore import QSettings


class _FakeSettings:
    def __init__(self):
        self._values: dict[str, object] = {}

    def value(self, key: str, default=None):
        return self._values.get(key, default)

    def setValue(self, key: str, value) -> None:
        self._values[key] = value

    def sync(self) -> None:
        pass


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
