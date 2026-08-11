"""Read-only production Hardware and analysis prerequisite gate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.service.analysis.golden_reference import (
    FrozenGoldenArtifact,
    freeze_golden_artifact,
)
from app.service.analysis.process_executor import ProcessIsolatedAnalysisExecutor
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.factory_selection_settings import FactorySelectionSettings
from app.service.golden_sample_service import GoldenSampleService
from app.service.managed_input_registry import ManagedInputRegistry
from app.service.managed_input_resident_adapter import ResidentManagedInputAdapter
from app.service.managed_input_sounddevice_backend import (
    MonotonicSoundDeviceAudioBackend,
    PYTHON_SYSTEM_MONOTONIC_DOMAIN,
    validate_python_system_monotonic_authority,
)
from model.channel_config_table import ChannelConfigTable
from model.input_hardware_table import InputHardwareTable
from model.managed_audio_input_table import ManagedAudioInputTable
from model.test_config_table import TestConfigTable


class ProductionPrerequisiteError(RuntimeError):
    """Raised when immutable production facts cannot be prepared."""


@dataclass(frozen=True, slots=True)
class FrozenChannelPrerequisite:
    channel_index: int
    values: Mapping[str, object]
    golden_artifact: FrozenGoldenArtifact | None = None


@dataclass(frozen=True, slots=True)
class FrozenManagedInputPrerequisite:
    hardware_id: int
    alias: str
    hardware_revision: int
    stable_backend_uid: str | None
    descriptor_device_name: str
    descriptor_hostapi_name: str
    descriptor_sample_rate_hz: float
    descriptor_input_channels: int
    calibration_revision: int
    calibration: tuple[tuple[int, bool, int, float | None], ...]
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FrozenConfigPrerequisite:
    config_id: int
    input_hardware_device_id: int
    selected_channels: tuple[int, ...]
    values: Mapping[str, object]
    channels: tuple[FrozenChannelPrerequisite, ...]
    managed_input: FrozenManagedInputPrerequisite


class PreparedProductionPrerequisites:
    def __init__(
        self,
        *,
        selection_settings: FactorySelectionSettings,
        lease: object,
        selection_generation: int,
        selected_config_ids: tuple[int, ...],
        configs: tuple[FrozenConfigPrerequisite, ...],
        managed_inputs: tuple[FrozenManagedInputPrerequisite, ...],
        golden_artifacts: tuple[FrozenGoldenArtifact, ...],
        monotonic_authority: object,
    ) -> None:
        self._selection_settings = selection_settings
        self._lease = lease
        self._closed = False
        self.selection_generation = selection_generation
        self.selected_config_ids = selected_config_ids
        self.configs = configs
        self.managed_inputs = managed_inputs
        self.golden_artifacts = golden_artifacts
        self.monotonic_authority = monotonic_authority
        self.monotonic_domain = PYTHON_SYSTEM_MONOTONIC_DOMAIN

    def validate_activation(self) -> None:
        actual = self._selection_settings.load_committed_selection()
        expected = (self.selection_generation, self.selected_config_ids)
        if actual != expected:
            raise ProductionPrerequisiteError(
                "The committed production selection changed after prepare."
            )

    def build_resident_adapter(
        self,
        *,
        backend: object,
        level_meter,
        max_capture_frames: int,
        startup_stream_timeout_s: float = 5.0,
        fault_close_timeout_s: float = 5.0,
        shutdown_timeout_s: float = 5.0,
        calibration_wait_margin_s: float = 1.0,
    ) -> ResidentManagedInputAdapter:
        if len(self.managed_inputs) > 6:
            raise ProductionPrerequisiteError(
                "Production supports at most six managed input streams."
            )
        monotonic_ns = getattr(self.monotonic_authority, "monotonic_ns", None)
        if not callable(monotonic_ns):
            raise ProductionPrerequisiteError(
                "The monotonic authority must provide monotonic_ns()."
            )
        adapter = ResidentManagedInputAdapter(
            backend=backend,
            level_meter=level_meter,
            frames_per_buffer=512,
            max_capture_frames=max_capture_frames,
            startup_stream_timeout_s=float(startup_stream_timeout_s),
            fault_close_timeout_s=float(fault_close_timeout_s),
            shutdown_timeout_s=float(shutdown_timeout_s),
            calibration_wait_margin_s=float(calibration_wait_margin_s),
            monotonic_ns=monotonic_ns,
        )
        return adapter

    def build_sounddevice_audio_backend(
        self,
        sounddevice_module: object | None = None,
    ) -> MonotonicSoundDeviceAudioBackend:
        return MonotonicSoundDeviceAudioBackend(
            sounddevice_module,
            self.monotonic_authority,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._selection_settings.release_selection_lease(self._lease)
        self._closed = True

    def build_analysis_executor(
        self,
        *,
        judgment_engine: object | None = None,
        capture_active=None,
        process_count: int = 2,
    ) -> object:
        artifacts = {
            (item.config_id, item.channel_index): item
            for item in self.golden_artifacts
        }
        if judgment_engine is not None:
            return ProductionAnalysisExecutor(
                judgment_engine=judgment_engine,
                frozen_golden_artifacts=artifacts,
            )
        return ProcessIsolatedAnalysisExecutor(
            frozen_golden_artifacts=artifacts,
            capture_active=capture_active,
            process_count=process_count,
        )


class ProductionPrerequisites:
    def __init__(
        self,
        selection_settings: FactorySelectionSettings,
        *,
        golden_sample_service: GoldenSampleService | None = None,
    ) -> None:
        self._selection_settings = selection_settings
        self._golden_samples = (
            golden_sample_service or GoldenSampleService()
        )

    def prepare(
        self,
        *,
        monotonic_authority: object,
    ) -> PreparedProductionPrerequisites:
        try:
            validate_python_system_monotonic_authority(monotonic_authority)
        except (TypeError, ValueError) as exc:
            raise ProductionPrerequisiteError(
                "Production requires the Python/system monotonic clock domain."
            ) from exc
        existing_config_ids = tuple(
            int(row.config_id)
            for row in TestConfigTable.select().order_by(
                TestConfigTable.config_id
            )
        )
        self._selection_settings.reconcile_for_startup(
            existing_config_ids
        )
        generation, selected_ids = (
            self._selection_settings.load_committed_selection()
        )
        lease = self._selection_settings.acquire_selection_lease(
            generation,
            selected_ids,
        )
        try:
            managed = self._freeze_managed_inputs(selected_ids)
            configs = self._freeze_configs(selected_ids, managed)
            golden = tuple(
                channel.golden_artifact
                for config in configs
                for channel in config.channels
                if channel.golden_artifact is not None
            )
            return PreparedProductionPrerequisites(
                selection_settings=self._selection_settings,
                lease=lease,
                selection_generation=generation,
                selected_config_ids=selected_ids,
                configs=configs,
                managed_inputs=tuple(
                    managed[key] for key in sorted(managed)
                ),
                golden_artifacts=golden,
                monotonic_authority=monotonic_authority,
            )
        except Exception:
            self._selection_settings.release_selection_lease(lease)
            raise

    @staticmethod
    def _freeze_managed_inputs(
        selected_config_ids: tuple[int, ...],
    ) -> dict[int, FrozenManagedInputPrerequisite]:
        frozen: dict[int, FrozenManagedInputPrerequisite] = {}
        referenced_hardware_ids = {
            int(row.input_hardware_device_id)
            for row in TestConfigTable.select().where(
                TestConfigTable.config_id.in_(selected_config_ids)
            )
            if type(row.input_hardware_device_id) is int
            and row.input_hardware_device_id > 0
        }
        rows = tuple(
            InputHardwareTable.select()
            .where(InputHardwareTable.hardware_id.in_(referenced_hardware_ids))
            .order_by(InputHardwareTable.hardware_id)
        )
        if not rows:
            if InputHardwareTable.select().exists():
                return {}
            return ProductionPrerequisites._freeze_previous_managed_inputs(
                referenced_hardware_ids
            )
        for row in rows:
            values = dict(row.__data__)
            try:
                raw_calibration = json.loads(row.mic_cal or "{}")
            except (TypeError, ValueError):
                raw_calibration = {}
            if not isinstance(raw_calibration, dict):
                raw_calibration = {}
            calibration = []
            for index in range(int(row.channel_count)):
                entry = raw_calibration.get(f"In{index + 1}", {})
                if not isinstance(entry, dict):
                    entry = {}
                enabled = bool(entry.get("is_selected", 1))
                try:
                    correction = float(entry.get("deviation_value", 0.0))
                except (TypeError, ValueError):
                    correction = 0.0
                calibration.append(
                    (
                        index,
                        enabled,
                        1 if enabled else 0,
                        correction if enabled else None,
                    )
                )
            hardware_id = int(row.hardware_id)
            frozen[hardware_id] = FrozenManagedInputPrerequisite(
                hardware_id=hardware_id,
                alias=str(row.alias),
                hardware_revision=1,
                stable_backend_uid=None,
                descriptor_device_name=str(row.device_name),
                descriptor_hostapi_name=str(row.hostapi),
                descriptor_sample_rate_hz=float(row.samplerate),
                descriptor_input_channels=int(row.channel_count),
                calibration_revision=1,
                calibration=tuple(calibration),
                values=MappingProxyType(values),
            )
        return frozen

    @staticmethod
    def _freeze_previous_managed_inputs(
        referenced_hardware_ids: set[int],
    ) -> dict[int, FrozenManagedInputPrerequisite]:
        """Keep isolated tests and pre-hardware-management databases readable."""
        frozen: dict[int, FrozenManagedInputPrerequisite] = {}
        rows = tuple(
            ManagedAudioInputTable.select().where(
                ManagedAudioInputTable.hardware_id.in_(referenced_hardware_ids)
            )
        )

        class FrozenManagedTable:
            @staticmethod
            def select():
                return rows

        validator = object.__new__(ManagedInputRegistry)
        validator._table = FrozenManagedTable
        try:
            records = ManagedInputRegistry._load_rows(validator)
        except Exception as exc:
            if str(exc) == "managed input row count exceeds capacity":
                raise ProductionPrerequisiteError(
                    "Production supports at most six managed input rows."
                ) from exc
            raise ProductionPrerequisiteError(
                "A managed input row or calibration is invalid."
            ) from exc
        rows_by_id = {int(row.hardware_id): row for row in rows}
        for record in records:
            hardware_id = record.hardware_id.value
            values = dict(rows_by_id[hardware_id].__data__)
            frozen[hardware_id] = FrozenManagedInputPrerequisite(
                hardware_id=hardware_id,
                alias=record.alias,
                hardware_revision=record.hardware_revision,
                stable_backend_uid=record.stable_backend_uid,
                descriptor_device_name=record.descriptor.device_name,
                descriptor_hostapi_name=record.descriptor.hostapi_name,
                descriptor_sample_rate_hz=record.descriptor.sample_rate_hz,
                descriptor_input_channels=record.descriptor.input_channels,
                calibration_revision=record.calibration_revision,
                calibration=record.calibration_channels,
                values=MappingProxyType(values),
            )
        return frozen

    def _freeze_configs(
        self,
        selected_ids: tuple[int, ...],
        managed: dict[int, FrozenManagedInputPrerequisite],
    ) -> tuple[FrozenConfigPrerequisite, ...]:
        if not selected_ids:
            raise ProductionPrerequisiteError(
                "Production requires at least one selected config."
            )
        if len(set(selected_ids)) != len(selected_ids):
            raise ProductionPrerequisiteError(
                "The committed production selection contains duplicate IDs."
            )
        frozen: list[FrozenConfigPrerequisite] = []
        for config_id in selected_ids:
            matches = tuple(
                TestConfigTable.select().where(
                    TestConfigTable.config_id == config_id
                )
            )
            if len(matches) != 1:
                raise ProductionPrerequisiteError(
                    f"Selected config {config_id} is missing or duplicated."
            )
            row = matches[0]
            values = dict(row.__data__)
            values["plc_id"] = values.pop("plc", None)
            hardware_id = values.get("input_hardware_device_id")
            if type(hardware_id) is not int or hardware_id not in managed:
                raise ProductionPrerequisiteError(
                    f"Selected config {config_id} has no exact managed input."
                )
            try:
                raw_selected = json.loads(values["selected_channels"])
            except (TypeError, ValueError) as exc:
                raise ProductionPrerequisiteError(
                    f"Selected config {config_id} has invalid channels."
                ) from exc
            if (
                type(raw_selected) is not list
                or not raw_selected
                or any(type(index) is not int or index < 0 for index in raw_selected)
                or len(set(raw_selected)) != len(raw_selected)
            ):
                raise ProductionPrerequisiteError(
                    f"Selected config {config_id} has invalid channels."
                )
            selected = tuple(raw_selected)
            vibration_channel = values.get("vibration_view_channel_index")
            hardware = managed[hardware_id]
            configured_rate = values.get("input_device_samplerate")
            if (
                type(configured_rate) not in (int, float)
                or float(configured_rate)
                != hardware.descriptor_sample_rate_hz
            ):
                raise ProductionPrerequisiteError(
                    f"Selected config {config_id} sample rate does not match Hardware."
                )
            if any(
                index >= hardware.descriptor_input_channels
                or not hardware.calibration[index][1]
                for index in selected
            ):
                raise ProductionPrerequisiteError(
                    f"Selected config {config_id} uses an untrusted channel."
                )
            rows = tuple(
                ChannelConfigTable.select()
                .where(ChannelConfigTable.config == config_id)
                .order_by(ChannelConfigTable.channel_index)
            )
            by_index = {
                int(channel.channel_index): channel for channel in rows
            }
            if not set(selected).issubset(by_index):
                raise ProductionPrerequisiteError(
                    f"Selected config {config_id} channel rows do not match."
                )
            channels: list[FrozenChannelPrerequisite] = []
            for index in selected:
                channel_values = dict(by_index[index].__data__)
                if (
                    str(channel_values.get("spl_weighting") or "")
                    .strip()
                    .upper()
                    not in {"A", "C", "Z"}
                ):
                    raise ProductionPrerequisiteError(
                        f"Selected config {config_id} channel {index} "
                        "has unsupported SPL weighting."
                    )
                golden_enabled = any(
                    str(channel_values.get(key) or "off").lower() != "off"
                    for key in ("fft_golden_mode", "oct_golden_mode")
                )
                artifact = None
                if golden_enabled:
                    try:
                        artifact = freeze_golden_artifact(
                            config_id=int(config_id),
                            channel_index=index,
                            channel_config=channel_values,
                            runtime_sample_rate=hardware.descriptor_sample_rate_hz,
                            analysis_duration=float(values["analysis_duration"]),
                            managed_root=self._golden_samples.managed_root(),
                        )
                    except Exception as exc:
                        raise ProductionPrerequisiteError(
                            f"Selected config {config_id} golden artifact is invalid."
                        ) from exc
                channels.append(
                    FrozenChannelPrerequisite(
                        channel_index=index,
                        values=MappingProxyType(channel_values),
                        golden_artifact=artifact,
                    )
                )
            frozen.append(
                FrozenConfigPrerequisite(
                    config_id=int(config_id),
                    input_hardware_device_id=hardware_id,
                    selected_channels=selected,
                    values=MappingProxyType(values),
                    channels=tuple(channels),
                    managed_input=hardware,
                )
            )
        return tuple(frozen)


__all__ = [
    "PreparedProductionPrerequisites",
    "ProductionPrerequisiteError",
    "ProductionPrerequisites",
]
