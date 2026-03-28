from __future__ import annotations

import json

from PyQt6.QtCore import QSettings


class FactorySelectionSettings:
    SETTINGS_KEY = "factory/selected_config_ids"

    def __init__(self, settings: QSettings | None = None):
        self._settings = settings or QSettings("JingChen", "JingChenMechanical")

    def load_selected_config_ids(self) -> tuple[int, ...]:
        raw_value = self._settings.value(self.SETTINGS_KEY, "")
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
        try:
            self._settings.setValue(self.SETTINGS_KEY, json.dumps(list(config_ids)))
            self._settings.sync()
        except Exception:
            return "配置保存失败"
        status = getattr(self._settings, "status", None)
        if callable(status) and status() != QSettings.Status.NoError:
            return "配置保存失败"
        return None
