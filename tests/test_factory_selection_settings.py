from __future__ import annotations

from app.service.factory_selection_settings import FactorySelectionSettings


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


def test_factory_selection_settings_round_trips_ordered_ids():
    store = FactorySelectionSettings(settings=_FakeSettings())

    assert store.load_selected_config_ids() == ()
    assert store.save_selected_config_ids((303, 101, 202)) is None
    assert store.load_selected_config_ids() == (303, 101, 202)


def test_factory_selection_settings_returns_error_when_save_fails():
    store = FactorySelectionSettings(settings=_ExplodingSettings())

    error = store.save_selected_config_ids((101, 202))

    assert error == "配置保存失败"
