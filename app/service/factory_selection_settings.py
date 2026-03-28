from __future__ import annotations

import json

from PyQt6.QtCore import QSettings


class FactorySelectionSettings:
    SETTINGS_KEY = "factory/selected_config_ids"

    def __init__(self, settings: QSettings | None = None):
        self._settings = settings

    def _open_settings(self) -> QSettings:
        if self._settings is not None:
            return self._settings
        return QSettings("JingChen", "JingChenMechanical")

    def load_selected_config_ids(self) -> tuple[int, ...]:
        settings = self._open_settings()
        raw_value = settings.value(self.SETTINGS_KEY, "")
        if raw_value in (None, ""):
            return ()

        try:
            decoded = json.loads(raw_value)
        except (TypeError, ValueError):
            return ()

        if not isinstance(decoded, list):
            return ()

        try:
            return tuple(int(config_id) for config_id in decoded)
        except (TypeError, ValueError):
            return ()

    def save_selected_config_ids(self, config_ids: tuple[int, ...]) -> str | None:
        settings = self._open_settings()
        try:
            settings.setValue(self.SETTINGS_KEY, json.dumps(list(config_ids)))
            settings.sync()
        except Exception:
            return "配置保存失败"
        status = getattr(settings, "status", None)
        if callable(status) and status() != QSettings.Status.NoError:
            return "配置保存失败"
        return None
