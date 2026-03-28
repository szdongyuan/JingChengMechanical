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


class _StatusFailingSettings(_FakeSettings):
    def __init__(self):
        super().__init__()
        self._status = QSettings.Status.NoError

    def sync(self) -> None:
        self._status = QSettings.Status.AccessError

    def status(self):
        return self._status


class _FreshStatusQSettings:
    Status = QSettings.Status
    _values: dict[str, object] = {}
    _instances: list["_FreshStatusQSettings"] = []
    _sync_statuses: list[QSettings.Status] = []

    @classmethod
    def reset(cls, *, sync_statuses: list[QSettings.Status]) -> None:
        cls._values = {}
        cls._instances = []
        cls._sync_statuses = list(sync_statuses)

    def __init__(self, organization: str, application: str):
        del organization, application
        instance_index = len(self.__class__._instances)
        self.__class__._instances.append(self)
        self._status = self.Status.NoError
        if instance_index < len(self.__class__._sync_statuses):
            self._sync_status = self.__class__._sync_statuses[instance_index]
        else:
            self._sync_status = self.Status.NoError

    def value(self, key: str, default=None):
        return self.__class__._values.get(key, default)

    def setValue(self, key: str, value) -> None:
        self.__class__._values[key] = value

    def sync(self) -> None:
        self._status = self._sync_status

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


def test_factory_selection_settings_returns_error_when_sync_status_is_not_no_error():
    store = FactorySelectionSettings(settings=_StatusFailingSettings())

    error = store.save_selected_config_ids((101, 202))

    assert error == "配置保存失败"


def test_factory_selection_settings_default_usage_recovers_after_transient_status_error(
    monkeypatch,
):
    _FreshStatusQSettings.reset(
        sync_statuses=[
            QSettings.Status.AccessError,
            QSettings.Status.NoError,
            QSettings.Status.NoError,
        ]
    )
    monkeypatch.setattr(
        factory_selection_settings_module,
        "QSettings",
        _FreshStatusQSettings,
    )
    store = FactorySelectionSettings()

    assert store.save_selected_config_ids((101, 202)) == "配置保存失败"
    assert store.save_selected_config_ids((303,)) is None
    assert store.load_selected_config_ids() == (303,)
