from __future__ import annotations

import json
from dataclasses import dataclass

from PyQt6.QtCore import QSettings


@dataclass(frozen=True)
class _PersistedSelection:
    generation: int
    config_ids: tuple[int, ...]
    slot_key: str


@dataclass(frozen=True)
class _CommittedSelection:
    generation: int
    config_ids: tuple[int, ...]
    slot_key: str
    data_slot_key: str | None


@dataclass(frozen=True)
class _AuthoritativeSelection:
    generation: int
    config_ids: tuple[int, ...]
    data_slot_key: str | None
    commit_slot_key: str | None


class FactorySelectionSettings:
    SETTINGS_KEY = "factory/selected_config_ids"
    # Alternate data slots keep the prior candidate available during a later write.
    _SLOT_KEYS = (
        "factory/selected_config_ids/slot_a",
        "factory/selected_config_ids/slot_b",
    )
    # Alternate commit slots keep the last authoritative selection intact if
    # updating the commit record fails after a data slot was written.
    _COMMIT_SLOT_KEYS = (
        "factory/selected_config_ids/commit_a",
        "factory/selected_config_ids/commit_b",
    )

    def __init__(self, settings: QSettings | None = None):
        self._settings = settings

    def _open_settings(self) -> QSettings:
        if self._settings is not None:
            return self._settings
        return QSettings("JingChen", "JingChenMechanical")

    def _open_verification_settings(self) -> QSettings:
        if self._settings is not None:
            return self._settings
        return self._open_settings()

    def _load_legacy_config_ids(self, settings: QSettings) -> tuple[int, ...]:
        legacy_selection = self._load_valid_legacy_config_ids(settings)
        if legacy_selection is None:
            return ()
        return legacy_selection

    def _load_valid_legacy_config_ids(
        self,
        settings: QSettings,
    ) -> tuple[int, ...] | None:
        missing = object()
        raw_value = settings.value(self.SETTINGS_KEY, missing)
        if raw_value is missing:
            return None
        return self._decode_legacy_config_ids(raw_value)

    def _decode_legacy_config_ids(self, raw_value: object) -> tuple[int, ...] | None:
        if raw_value in (None, ""):
            return None

        try:
            decoded = json.loads(raw_value)
        except (TypeError, ValueError):
            return None

        if not isinstance(decoded, list):
            return None

        try:
            return tuple(int(config_id) for config_id in decoded)
        except (TypeError, ValueError):
            return None

    def _decode_config_ids(self, raw_value: object) -> tuple[int, ...]:
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

    def _load_slot_selection(
        self,
        settings: QSettings,
        slot_key: str,
    ) -> _PersistedSelection | None:
        raw_value = settings.value(slot_key, "")
        if raw_value in (None, ""):
            return None

        try:
            decoded = json.loads(raw_value)
        except (TypeError, ValueError):
            return None

        if not isinstance(decoded, dict):
            return None

        generation = decoded.get("generation")
        config_ids = decoded.get("selected_config_ids")
        if not isinstance(generation, int) or generation < 1:
            return None
        if not isinstance(config_ids, list):
            return None

        try:
            normalized_config_ids = tuple(int(config_id) for config_id in config_ids)
        except (TypeError, ValueError):
            return None

        return _PersistedSelection(
            generation=generation,
            config_ids=normalized_config_ids,
            slot_key=slot_key,
        )

    def _load_current_selection(
        self,
        settings: QSettings,
    ) -> _PersistedSelection | None:
        selections = [
            selection
            for slot_key in self._SLOT_KEYS
            if (
                selection := self._load_slot_selection(
                    settings,
                    slot_key,
                )
            )
            is not None
        ]
        if not selections:
            return None
        return max(selections, key=lambda selection: selection.generation)

    def _load_commit_selection(
        self,
        settings: QSettings,
        slot_key: str,
    ) -> _CommittedSelection | None:
        raw_value = settings.value(slot_key, "")
        if raw_value in (None, ""):
            return None

        try:
            decoded = json.loads(raw_value)
        except (TypeError, ValueError):
            return None

        if not isinstance(decoded, dict):
            return None

        generation = decoded.get("generation")
        config_ids = decoded.get("selected_config_ids")
        data_slot_key = decoded.get("data_slot_key")
        if not isinstance(generation, int) or generation < 0:
            return None
        if not isinstance(config_ids, list):
            return None
        if data_slot_key not in (*self._SLOT_KEYS, None, ""):
            return None

        try:
            normalized_config_ids = tuple(int(config_id) for config_id in config_ids)
        except (TypeError, ValueError):
            return None

        return _CommittedSelection(
            generation=generation,
            config_ids=normalized_config_ids,
            slot_key=slot_key,
            data_slot_key=data_slot_key or None,
        )

    def _load_current_committed_selection(
        self,
        settings: QSettings,
    ) -> _CommittedSelection | None:
        committed_selections = [
            committed_selection
            for slot_key in self._COMMIT_SLOT_KEYS
            if (
                committed_selection := self._load_commit_selection(
                    settings,
                    slot_key,
                )
            )
            is not None
            and self._is_valid_committed_selection(
                settings,
                committed_selection,
            )
        ]
        if not committed_selections:
            return None
        return max(
            committed_selections,
            key=lambda committed_selection: committed_selection.generation,
        )

    def _is_valid_committed_selection(
        self,
        settings: QSettings,
        committed_selection: _CommittedSelection,
    ) -> bool:
        if committed_selection.data_slot_key is None:
            return (
                committed_selection.generation == 0
                and committed_selection.config_ids == ()
            )

        persisted_selection = self._load_slot_selection(
            settings,
            committed_selection.data_slot_key,
        )
        return (
            persisted_selection is not None
            and persisted_selection.generation == committed_selection.generation
            and persisted_selection.config_ids == committed_selection.config_ids
        )

    def _serialize_slot_selection(
        self,
        *,
        generation: int,
        config_ids: tuple[int, ...],
    ) -> str:
        return json.dumps(
            {
                "generation": generation,
                "selected_config_ids": list(config_ids),
            },
            separators=(",", ":"),
        )

    def _serialize_commit_selection(
        self,
        *,
        generation: int,
        config_ids: tuple[int, ...],
        data_slot_key: str | None,
    ) -> str:
        return json.dumps(
            {
                "generation": generation,
                "selected_config_ids": list(config_ids),
                "data_slot_key": data_slot_key,
            },
            separators=(",", ":"),
        )

    def _next_slot_key(self, current_selection: _PersistedSelection | None) -> str:
        if current_selection is None:
            return self._SLOT_KEYS[0]
        if current_selection.slot_key == self._SLOT_KEYS[0]:
            return self._SLOT_KEYS[1]
        return self._SLOT_KEYS[0]

    def _next_commit_slot_key(self, current_slot_key: str | None) -> str:
        if current_slot_key is None:
            return self._COMMIT_SLOT_KEYS[0]
        if current_slot_key == self._COMMIT_SLOT_KEYS[0]:
            return self._COMMIT_SLOT_KEYS[1]
        return self._COMMIT_SLOT_KEYS[0]

    def _verify_data_slot(
        self,
        verification_settings: QSettings,
        *,
        slot_key: str,
        generation: int,
        config_ids: tuple[int, ...],
    ) -> bool:
        persisted_selection = self._load_slot_selection(
            verification_settings,
            slot_key,
        )
        return (
            persisted_selection is not None
            and persisted_selection.generation == generation
            and persisted_selection.config_ids == config_ids
        )

    def _verify_commit_slot(
        self,
        verification_settings: QSettings,
        *,
        slot_key: str,
        generation: int,
        config_ids: tuple[int, ...],
        data_slot_key: str | None,
    ) -> bool:
        committed_selection = self._load_commit_selection(
            verification_settings,
            slot_key,
        )
        return (
            committed_selection is not None
            and committed_selection.generation == generation
            and committed_selection.config_ids == config_ids
            and committed_selection.data_slot_key == data_slot_key
            and self._is_valid_committed_selection(
                verification_settings,
                committed_selection,
            )
        )

    def _write_data_selection(
        self,
        settings: QSettings,
        verification_settings: QSettings,
        *,
        generation: int,
        config_ids: tuple[int, ...],
        current_data_slot_key: str | None,
    ) -> _PersistedSelection | None:
        current_selection = (
            None
            if current_data_slot_key is None
            else _PersistedSelection(
                generation=1,
                config_ids=(),
                slot_key=current_data_slot_key,
            )
        )
        slot_key = self._next_slot_key(current_selection)
        raw_value = self._serialize_slot_selection(
            generation=generation,
            config_ids=config_ids,
        )

        try:
            settings.setValue(slot_key, raw_value)
            settings.sync()
        except Exception:
            return None

        if not self._verify_data_slot(
            verification_settings,
            slot_key=slot_key,
            generation=generation,
            config_ids=config_ids,
        ):
            return None

        return _PersistedSelection(
            generation=generation,
            config_ids=config_ids,
            slot_key=slot_key,
        )

    def _write_commit_selection(
        self,
        settings: QSettings,
        verification_settings: QSettings,
        *,
        current_selection: _AuthoritativeSelection | None,
        generation: int,
        config_ids: tuple[int, ...],
        data_slot_key: str | None,
    ) -> _CommittedSelection | None:
        slot_key = self._next_commit_slot_key(
            None if current_selection is None else current_selection.commit_slot_key
        )
        raw_value = self._serialize_commit_selection(
            generation=generation,
            config_ids=config_ids,
            data_slot_key=data_slot_key,
        )

        try:
            settings.setValue(slot_key, raw_value)
            settings.sync()
        except Exception:
            return None

        if not self._verify_commit_slot(
            verification_settings,
            slot_key=slot_key,
            generation=generation,
            config_ids=config_ids,
            data_slot_key=data_slot_key,
        ):
            return None

        return _CommittedSelection(
            generation=generation,
            config_ids=config_ids,
            slot_key=slot_key,
            data_slot_key=data_slot_key,
        )

    def _load_authoritative_selection(
        self,
        settings: QSettings,
    ) -> _AuthoritativeSelection:
        committed_selection = self._load_current_committed_selection(settings)
        if committed_selection is not None:
            return _AuthoritativeSelection(
                generation=committed_selection.generation,
                config_ids=committed_selection.config_ids,
                data_slot_key=committed_selection.data_slot_key,
                commit_slot_key=committed_selection.slot_key,
            )

        legacy_selection = self._load_valid_legacy_config_ids(settings)
        if legacy_selection is not None:
            return _AuthoritativeSelection(
                generation=0,
                config_ids=legacy_selection,
                data_slot_key=None,
                commit_slot_key=None,
            )

        return _AuthoritativeSelection(
            generation=(
                0
                if (persisted_selection := self._load_current_selection(settings)) is None
                else persisted_selection.generation
            ),
            config_ids=(
                ()
                if persisted_selection is None
                else persisted_selection.config_ids
            ),
            data_slot_key=(
                None
                if persisted_selection is None
                else persisted_selection.slot_key
            ),
            commit_slot_key=None,
        )

    def _ensure_authoritative_selection(
        self,
        settings: QSettings,
        verification_settings: QSettings,
    ) -> _AuthoritativeSelection | None:
        authoritative_selection = self._load_authoritative_selection(settings)
        if authoritative_selection.commit_slot_key is not None:
            return authoritative_selection

        return self._bootstrap_authoritative_selection(
            settings,
            verification_settings,
            authoritative_selection,
        )

    def _bootstrap_authoritative_selection(
        self,
        settings: QSettings,
        verification_settings: QSettings,
        authoritative_selection: _AuthoritativeSelection,
    ) -> _AuthoritativeSelection | None:
        current_selection = authoritative_selection
        if (
            current_selection.data_slot_key is None
            and current_selection.config_ids
        ):
            persisted_selection = self._write_data_selection(
                settings,
                verification_settings,
                generation=1,
                config_ids=current_selection.config_ids,
                current_data_slot_key=None,
            )
            if persisted_selection is None:
                return None
            current_selection = _AuthoritativeSelection(
                generation=persisted_selection.generation,
                config_ids=persisted_selection.config_ids,
                data_slot_key=persisted_selection.slot_key,
                commit_slot_key=None,
            )

        committed_selection = self._write_commit_selection(
            settings,
            verification_settings,
            current_selection=None,
            generation=current_selection.generation,
            config_ids=current_selection.config_ids,
            data_slot_key=current_selection.data_slot_key,
        )
        if committed_selection is None:
            return None

        return _AuthoritativeSelection(
            generation=committed_selection.generation,
            config_ids=committed_selection.config_ids,
            data_slot_key=committed_selection.data_slot_key,
            commit_slot_key=committed_selection.slot_key,
        )

    def load_selected_config_ids(self) -> tuple[int, ...]:
        settings = self._open_settings()
        return self._load_authoritative_selection(settings).config_ids

    def save_selected_config_ids(self, config_ids: tuple[int, ...]) -> str | None:
        settings = self._open_settings()
        verification_settings = self._open_verification_settings()
        current_selection = self._ensure_authoritative_selection(
            settings,
            verification_settings,
        )
        if current_selection is None:
            return "配置保存失败"

        generation = current_selection.generation + 1
        normalized_config_ids = tuple(config_ids)
        persisted_selection = self._write_data_selection(
            settings,
            verification_settings,
            generation=generation,
            config_ids=normalized_config_ids,
            current_data_slot_key=current_selection.data_slot_key,
        )
        if persisted_selection is None:
            return "配置保存失败"

        committed_selection = self._write_commit_selection(
            settings,
            verification_settings,
            current_selection=current_selection,
            generation=generation,
            config_ids=normalized_config_ids,
            data_slot_key=persisted_selection.slot_key,
        )
        if committed_selection is None:
            return "配置保存失败"
        return None
