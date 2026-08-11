from __future__ import annotations

import json
import logging
import math
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal

from peewee import IntegrityError

from app.service.analysis.limit_tables import (
    normalize_limit_tables_for_storage,
    validate_limit_tables,
)
from app.service.current_meter_service import (
    normalize_current_meter_config,
    validate_current_meter_config,
)
from app.service.fault_types import (
    decode_fault_types_json,
    encode_fault_types_json,
    normalize_fault_types,
)
from app.service.golden_sample_service import GoldenSampleService, StagedGoldenSample
from app.service.result_dashboard_layout import (
    DEFAULT_RESULT_DASHBOARD_LAYOUT,
    ResultDashboardLayoutError,
    normalize_result_dashboard_layout,
    normalize_result_dashboard_layout_for_display,
    serialize_result_dashboard_layout,
)
from consts.time_consts import now_str
from model.channel_config_table import ChannelConfigTable
from model.input_hardware_table import InputHardwareTable
from model.plc_table import PlcTable
from model.test_config_table import TestConfigTable

logger = logging.getLogger(__name__)

_INVALID_INPUT_HARDWARE_ERROR = (
    "\u5fc5\u987b\u9009\u62e9\u6709\u6548\u7684\u8f93\u5165\u786c\u4ef6\u8bbe\u5907"
)
_UNAVAILABLE_INPUT_HARDWARE_ERROR = (
    "\u6240\u9009\u8f93\u5165\u786c\u4ef6\u8bbe\u5907\u4e0d\u53ef\u7528"
)
_UNRESOLVED_MANAGED_COUNT = object()

# Default values for new channel configs
_CHANNEL_DEFAULTS = {
    "stft_nfft": 1024,
    "fft_nfft": 4096,
    "stft_overlap": 0.5,
    "stft_color_scale_enabled": False,
    "stft_color_scale_min_db": -120.0,
    "stft_color_scale_max_db": 0.0,
    "stft_score_enabled": True,
    "stft_score_summary_mode": "percentile",
    "stft_score_percentile": 10.0,
    "stft_score_loudness_reference_sone": 10.0,
    "stft_score_modulation_reference": 0.20,
    "stft_score_harmonic_ratio_reference": 0.25,
    "stft_score_frequency_instability_reference": 0.05,
    "stft_score_harmonic_start_order": 2,
    "stft_score_harmonic_end_order": 100,
    "spl_weighting": "A",
    "spl_time_constant": 0.125,
    "spl_lower_enabled": False,
    "spl_upper_enabled": False,
    "spl_lower_limit": 40.0,
    "spl_upper_limit": 85.0,
    "spl_average_lower_enabled": False,
    "spl_average_lower_limit": 40.0,
    "spl_average_upper_enabled": False,
    "spl_average_upper_limit": 85.0,
    "loudness_unit": "sone",
    "loudness_lower_enabled": False,
    "loudness_upper_enabled": False,
    "loudness_lower_limit": 0.0,
    "loudness_upper_limit": 60.0,
    "loudness_average_lower_enabled": False,
    "loudness_average_lower_limit": 0.0,
    "loudness_average_upper_enabled": False,
    "loudness_average_upper_limit": 60.0,
    "fft_x_axis_scale": "linear",
    "fft_weighting": "Z",
    "fft_band_limits": "[]",
    "oct_weighting": "A",
    "oct_band_limits": "[]",
    "ai_config": "{}",
    "golden_sample_path": None,
    "golden_sample_label": None,
    "golden_sample_source_channel": None,
    "golden_sample_start_sample": None,
    "golden_sample_num_samples": None,
    "golden_sample_start_ms": None,
    "golden_sample_duration_ms": None,
    "golden_sample_sample_rate": None,
    "fft_golden_mode": "off",
    "fft_show_golden_overlay": False,
    "oct_golden_mode": "off",
    "oct_show_golden_overlay": False,
}

_CHANNEL_LIMIT_GROUPS = (
    (
        "SPL",
        "spl_lower_enabled",
        "spl_lower_limit",
        "spl_upper_enabled",
        "spl_upper_limit",
        False,
    ),
    (
        "平均SPL",
        "spl_average_lower_enabled",
        "spl_average_lower_limit",
        "spl_average_upper_enabled",
        "spl_average_upper_limit",
        False,
    ),
    (
        "响度",
        "loudness_lower_enabled",
        "loudness_lower_limit",
        "loudness_upper_enabled",
        "loudness_upper_limit",
        True,
    ),
    (
        "平均响度",
        "loudness_average_lower_enabled",
        "loudness_average_lower_limit",
        "loudness_average_upper_enabled",
        "loudness_average_upper_limit",
        True,
    ),
)

_CHANNEL_LIMIT_ENABLED_FIELDS = tuple(
    field
    for _label, lower_enabled, _lower_value, upper_enabled, _upper_value, _non_negative in _CHANNEL_LIMIT_GROUPS
    for field in (lower_enabled, upper_enabled)
)

_CHANNEL_LIMIT_VALUE_FIELDS = tuple(
    field
    for _label, _lower_enabled, lower_value, _upper_enabled, upper_value, _non_negative in _CHANNEL_LIMIT_GROUPS
    for field in (lower_value, upper_value)
)

_GOLDEN_BINDING_FIELDS = (
    "golden_sample_path",
    "golden_sample_label",
    "golden_sample_source_channel",
    "golden_sample_start_sample",
    "golden_sample_num_samples",
    "golden_sample_start_ms",
    "golden_sample_duration_ms",
    "golden_sample_sample_rate",
)
_VALID_NFFT = [256, 512, 1024, 2048, 4096, 8192]
_VALID_FFT_NFFT = [256, 512, 1024, 2048, 4096, 8192]
_VALID_OVERLAP = [0.25, 0.5, 0.75]
_VALID_WEIGHTING = ["A", "C", "Z"]
_VALID_LOUDNESS_UNITS = ["sone", "phon"]
_VALID_GOLDEN_MODES = {"off", "absolute", "delta"}
_VALID_FFT_X_AXIS_SCALES = {"linear", "log"}
_VALID_STFT_SCORE_SUMMARY_MODES = {"percentile", "mean", "min"}
MONITOR_GAIN_DEFAULT_DB = 0.0
MONITOR_GAIN_MIN_DB = -60.0
MONITOR_GAIN_MAX_DB = 35.0
_MONITOR_CONFIG_FIELDS = ("monitor_channel_index", "monitor_gain_db")
_CURRENT_METER_SOURCES = frozenset({"modbus_tcp", "plc"})
_CURRENT_METER_CONFIG_FIELDS = (
    "current_meter_enabled",
    "current_meter_source",
    "current_meter_host",
    "current_meter_port",
    "current_meter_unit_id",
    "current_meter_register_type",
    "current_meter_register_address",
    "current_meter_register_count",
    "current_meter_data_type",
    "current_meter_byte_order",
    "current_meter_word_order",
    "current_meter_scale",
    "current_meter_offset",
    "current_meter_poll_interval_ms",
    "current_meter_timeout_ms",
)
_CONFIG_LOAD_FIELD_NAMES = (
    "config_id",
    "config_name",
    "record_duration",
    "analysis_duration",
    "analysis_window_offset_ms",
    "allow_incomplete_recording_analysis",
    "scan_triggers_test",
    "plc",
    "input_hardware_device_id",
    "input_device_name",
    "input_device_hostapi",
    "input_device_samplerate",
    "output_device_name",
    "output_device_hostapi",
    "output_device_samplerate",
    "selected_channels",
    "fault_types_json",
    "vibration_view_channel_index",
    "result_dashboard_layout",
    *_MONITOR_CONFIG_FIELDS,
    *_CURRENT_METER_CONFIG_FIELDS,
)


@dataclass(frozen=True)
class PendingConfigSave:
    config_id: int
    promoted_paths: tuple[str, ...] = ()
    old_paths: tuple[str, ...] = ()
    staged_paths: tuple[str, ...] = ()
    golden_bindings_by_channel: tuple[tuple[int, dict], ...] = ()


def _normalize_current_meter_with_source(config: dict | None) -> dict:
    source_config = config if isinstance(config, dict) else {}
    source = str(source_config.get("source") or "").strip().lower()
    if source not in _CURRENT_METER_SOURCES:
        source = "modbus_tcp"
    normalized = normalize_current_meter_config(source_config)
    normalized["source"] = source
    return normalized


class ConfigService:
    def __init__(self, bus, golden_sample_service: GoldenSampleService | None = None):
        self.bus = bus
        self._golden_samples = golden_sample_service or GoldenSampleService()
        self.golden_sample_service = self._golden_samples

    def validate_golden_binding(
        self,
        *,
        channel: dict,
        staged_binding: StagedGoldenSample | dict | None,
        analysis_duration: float,
        sample_rate,
    ) -> list[str]:
        errors = self._validate_golden_channel(
            prefix="",
            channel=channel,
            staged_binding=(
                StagedGoldenSample.from_mapping(staged_binding)
                if staged_binding is not None
                else None
            ),
            analysis_duration=analysis_duration,
            sample_rate=sample_rate,
        )
        cleaned_errors: list[str] = []
        for error in errors:
            if error.startswith(": "):
                cleaned_errors.append(error[2:])
            else:
                cleaned_errors.append(error)
        return cleaned_errors

    def list_configs(self) -> list[dict]:
        rows = (
            TestConfigTable.select(
                TestConfigTable.config_id, TestConfigTable.config_name
            )
            .order_by(TestConfigTable.config_id)
            .dicts()
        )
        return list(rows)

    def load_config(self, config_id: int) -> dict:
        available_columns = self._config_table_columns()
        fields = [
            TestConfigTable._meta.fields[field_name]
            for field_name in _CONFIG_LOAD_FIELD_NAMES
            if field_name == "fault_types_json"
            or self._config_field_exists(field_name, available_columns)
        ]
        cfg = (
            TestConfigTable.select(*fields)
            .where(TestConfigTable.config_id == config_id)
            .dicts()
            .first()
        )
        if cfg is None:
            raise TestConfigTable.DoesNotExist

        selected_channels = self._parse_json(cfg.get("selected_channels"), [])
        fault_types = decode_fault_types_json(cfg["fault_types_json"])
        result = {
            "config_id": cfg["config_id"],
            "config_name": cfg["config_name"],
            "record_duration": cfg.get("record_duration", 5.0),
            "analysis_duration": cfg.get("analysis_duration", 3.0),
            "analysis_window_offset_ms": cfg.get("analysis_window_offset_ms", 0),
            "allow_incomplete_recording_analysis": bool(
                cfg.get("allow_incomplete_recording_analysis", False)
            ),
            "scan_triggers_test": bool(cfg.get("scan_triggers_test", False)),
            "plc_id": cfg.get("plc"),
            "input_device_name": cfg.get("input_device_name"),
            "input_device_hostapi": cfg.get("input_device_hostapi"),
            "input_device_samplerate": cfg.get("input_device_samplerate"),
            "output_device_name": cfg.get("output_device_name"),
            "output_device_hostapi": cfg.get("output_device_hostapi"),
            "output_device_samplerate": cfg.get("output_device_samplerate"),
            "selected_channels": selected_channels,
            "fault_types": list(fault_types),
            "vibration_view_channel_index": cfg.get("vibration_view_channel_index"),
            "monitor_channel_index": None,
            "monitor_gain_db": self._normalize_monitor_gain(
                cfg.get("monitor_gain_db")
            ),
            "current_meter": self._normalize_current_meter_from_row(cfg),
            "channels": [],
        }
        if cfg.get("input_hardware_device_id") is not None:
            result["input_hardware_device_id"] = cfg.get(
                "input_hardware_device_id"
            )
        ch_rows = (
            ChannelConfigTable.select()
            .where(ChannelConfigTable.config == config_id)
            .order_by(ChannelConfigTable.channel_index)
        )
        for ch in ch_rows:
            result["channels"].append(
                {
                    "channel_index": ch.channel_index,
                    "stft_nfft": ch.stft_nfft,
                    "fft_nfft": (
                        ch.fft_nfft
                        if ch.fft_nfft is not None
                        else _CHANNEL_DEFAULTS["fft_nfft"]
                    ),
                    "stft_overlap": ch.stft_overlap,
                    "stft_color_scale_enabled": bool(
                        getattr(
                            ch,
                            "stft_color_scale_enabled",
                            _CHANNEL_DEFAULTS["stft_color_scale_enabled"],
                        )
                    ),
                    "stft_color_scale_min_db": self._finite_float_or_default(
                        getattr(ch, "stft_color_scale_min_db", None),
                        _CHANNEL_DEFAULTS["stft_color_scale_min_db"],
                    ),
                    "stft_color_scale_max_db": self._finite_float_or_default(
                        getattr(ch, "stft_color_scale_max_db", None),
                        _CHANNEL_DEFAULTS["stft_color_scale_max_db"],
                    ),
                    "stft_score_enabled": bool(
                        getattr(
                            ch,
                            "stft_score_enabled",
                            _CHANNEL_DEFAULTS["stft_score_enabled"],
                        )
                    ),
                    "stft_score_summary_mode": self._normalize_stft_score_summary_mode(
                        getattr(
                            ch,
                            "stft_score_summary_mode",
                            _CHANNEL_DEFAULTS["stft_score_summary_mode"],
                        )
                    ),
                    "stft_score_percentile": self._finite_float_or_default(
                        getattr(ch, "stft_score_percentile", None),
                        _CHANNEL_DEFAULTS["stft_score_percentile"],
                    ),
                    "stft_score_loudness_reference_sone": self._finite_float_or_default(
                        getattr(ch, "stft_score_loudness_reference_sone", None),
                        _CHANNEL_DEFAULTS["stft_score_loudness_reference_sone"],
                    ),
                    "stft_score_modulation_reference": self._finite_float_or_default(
                        getattr(ch, "stft_score_modulation_reference", None),
                        _CHANNEL_DEFAULTS["stft_score_modulation_reference"],
                    ),
                    "stft_score_harmonic_ratio_reference": self._finite_float_or_default(
                        getattr(ch, "stft_score_harmonic_ratio_reference", None),
                        _CHANNEL_DEFAULTS["stft_score_harmonic_ratio_reference"],
                    ),
                    "stft_score_frequency_instability_reference": self._finite_float_or_default(
                        getattr(
                            ch,
                            "stft_score_frequency_instability_reference",
                            None,
                        ),
                        _CHANNEL_DEFAULTS[
                            "stft_score_frequency_instability_reference"
                        ],
                    ),
                    "stft_score_harmonic_start_order": self._int_or_default(
                        getattr(ch, "stft_score_harmonic_start_order", None),
                        _CHANNEL_DEFAULTS["stft_score_harmonic_start_order"],
                    ),
                    "stft_score_harmonic_end_order": self._int_or_default(
                        getattr(ch, "stft_score_harmonic_end_order", None),
                        _CHANNEL_DEFAULTS["stft_score_harmonic_end_order"],
                    ),
                    "spl_weighting": ch.spl_weighting,
                    "spl_time_constant": ch.spl_time_constant,
                    "spl_lower_enabled": bool(
                        getattr(ch, "spl_lower_enabled", _CHANNEL_DEFAULTS["spl_lower_enabled"])
                    ),
                    "spl_upper_enabled": bool(
                        getattr(ch, "spl_upper_enabled", _CHANNEL_DEFAULTS["spl_upper_enabled"])
                    ),
                    "spl_upper_limit": ch.spl_upper_limit,
                    "spl_lower_limit": ch.spl_lower_limit,
                    "spl_average_lower_enabled": bool(
                        getattr(
                            ch,
                            "spl_average_lower_enabled",
                            _CHANNEL_DEFAULTS["spl_average_lower_enabled"],
                        )
                    ),
                    "spl_average_lower_limit": getattr(
                        ch,
                        "spl_average_lower_limit",
                        _CHANNEL_DEFAULTS["spl_average_lower_limit"],
                    ),
                    "spl_average_upper_enabled": bool(
                        getattr(
                            ch,
                            "spl_average_upper_enabled",
                            _CHANNEL_DEFAULTS["spl_average_upper_enabled"],
                        )
                    ),
                    "spl_average_upper_limit": getattr(
                        ch,
                        "spl_average_upper_limit",
                        _CHANNEL_DEFAULTS["spl_average_upper_limit"],
                    ),
                    "loudness_unit": ch.loudness_unit
                    or _CHANNEL_DEFAULTS["loudness_unit"],
                    "loudness_lower_enabled": bool(
                        getattr(ch, "loudness_lower_enabled", _CHANNEL_DEFAULTS["loudness_lower_enabled"])
                    ),
                    "loudness_upper_enabled": bool(
                        getattr(ch, "loudness_upper_enabled", _CHANNEL_DEFAULTS["loudness_upper_enabled"])
                    ),
                    "loudness_lower_limit": (
                        ch.loudness_lower_limit
                        if ch.loudness_lower_limit is not None
                        else _CHANNEL_DEFAULTS["loudness_lower_limit"]
                    ),
                    "loudness_upper_limit": (
                        ch.loudness_upper_limit
                        if ch.loudness_upper_limit is not None
                        else _CHANNEL_DEFAULTS["loudness_upper_limit"]
                    ),
                    "loudness_average_lower_enabled": bool(
                        getattr(
                            ch,
                            "loudness_average_lower_enabled",
                            _CHANNEL_DEFAULTS["loudness_average_lower_enabled"],
                        )
                    ),
                    "loudness_average_lower_limit": getattr(
                        ch,
                        "loudness_average_lower_limit",
                        _CHANNEL_DEFAULTS["loudness_average_lower_limit"],
                    ),
                    "loudness_average_upper_enabled": bool(
                        getattr(
                            ch,
                            "loudness_average_upper_enabled",
                            _CHANNEL_DEFAULTS["loudness_average_upper_enabled"],
                        )
                    ),
                    "loudness_average_upper_limit": getattr(
                        ch,
                        "loudness_average_upper_limit",
                        _CHANNEL_DEFAULTS["loudness_average_upper_limit"],
                    ),
                    "fft_x_axis_scale": self._normalize_fft_x_axis_scale(
                        getattr(
                            ch,
                            "fft_x_axis_scale",
                            _CHANNEL_DEFAULTS["fft_x_axis_scale"],
                        )
                    ),
                    "fft_weighting": self._normalize_weighting(
                        getattr(
                            ch,
                            "fft_weighting",
                            _CHANNEL_DEFAULTS["fft_weighting"],
                        )
                    ),
                    "fft_band_limits": self._parse_json(ch.fft_band_limits, []),
                    "oct_weighting": ch.oct_weighting,
                    "oct_band_limits": self._parse_json(ch.oct_band_limits, []),
                    "ai_config": self._parse_json(ch.ai_config, {}),
                    "golden_sample_path": self._normalize_optional_text(
                        ch.golden_sample_path
                    ),
                    "golden_sample_label": self._normalize_optional_text(
                        ch.golden_sample_label
                    ),
                    "golden_sample_source_channel": ch.golden_sample_source_channel,
                    "golden_sample_start_sample": ch.golden_sample_start_sample,
                    "golden_sample_num_samples": ch.golden_sample_num_samples,
                    "golden_sample_start_ms": ch.golden_sample_start_ms,
                    "golden_sample_duration_ms": ch.golden_sample_duration_ms,
                    "golden_sample_sample_rate": ch.golden_sample_sample_rate,
                    "fft_golden_mode": ch.fft_golden_mode
                    or _CHANNEL_DEFAULTS["fft_golden_mode"],
                    "fft_show_golden_overlay": bool(ch.fft_show_golden_overlay),
                    "oct_golden_mode": ch.oct_golden_mode
                    or _CHANNEL_DEFAULTS["oct_golden_mode"],
                    "oct_show_golden_overlay": bool(ch.oct_show_golden_overlay),
                }
            )
        # Legacy fallback: infer selected_channels from existing channel rows
        if not result["selected_channels"] and result["channels"]:
            result["selected_channels"] = [
                ch["channel_index"] for ch in result["channels"]
            ]
        (
            result["result_dashboard_layout"],
            result_dashboard_layout_error,
        ) = self._normalize_loaded_result_dashboard_layout(
            cfg.get("result_dashboard_layout"),
            selected_channels=result["selected_channels"],
        )
        if result_dashboard_layout_error is not None:
            result["_result_dashboard_layout_error"] = result_dashboard_layout_error
        managed_channel_count, _ = self._resolve_managed_channel_count(result)
        result["monitor_channel_index"] = self._resolve_monitor_channel_index(
            cfg.get("monitor_channel_index"),
            selected_channels=result["selected_channels"],
            managed_channel_count=managed_channel_count,
        )
        return result

    def load_config_for_result_dashboard(self, config_id: int) -> dict:
        result = self.load_config(config_id)
        result["result_dashboard_layout"] = normalize_result_dashboard_layout_for_display(
            result.get("result_dashboard_layout"),
            selected_channels=result.get("selected_channels", []),
        )
        result.pop("_result_dashboard_layout_error", None)
        return result

    def _normalize_loaded_result_dashboard_layout(
        self,
        raw_layout,
        *,
        selected_channels: list,
    ) -> tuple[object, str | None]:
        try:
            return (
                normalize_result_dashboard_layout(
                    raw_layout,
                    selected_channels=selected_channels,
                ),
                None,
            )
        except ResultDashboardLayoutError as exc:
            return raw_layout, f"结果展示布局：{exc}"

    @staticmethod
    def _selected_channels_for_layout_validation(selected_channels) -> list[int]:
        if not isinstance(selected_channels, (list, tuple)):
            return []
        normalized = []
        for channel in selected_channels:
            if isinstance(channel, int) and not isinstance(channel, bool):
                normalized.append(channel)
        return normalized

    def create_config(self, config_name: str) -> int:
        name = config_name.strip()
        if not name:
            raise ValueError("配置名称不能为空")
        if TestConfigTable.select().where(TestConfigTable.config_name == name).exists():
            raise ValueError(f"配置名称「{name}」已存在")
        try:
            with TestConfigTable._meta.database.atomic():
                row_values = {
                    "config_name": name,
                    "record_duration": 5.0,
                    "analysis_duration": 3.0,
                    "allow_incomplete_recording_analysis": False,
                    "scan_triggers_test": False,
                    "selected_channels": "[]",
                    "fault_types_json": "[]",
                    "result_dashboard_layout": serialize_result_dashboard_layout(
                        DEFAULT_RESULT_DASHBOARD_LAYOUT
                    ),
                    "analysis_window_offset_ms": 0,
                    "created_time": now_str(),
                    "updated_time": now_str(),
                }
                self._add_current_meter_row_values(
                    row_values,
                    current_meter=_normalize_current_meter_with_source(None),
                    available_columns=self._config_table_columns(),
                )
                config_id = self._insert_config_row_values(row_values)
        except IntegrityError:
            raise ValueError(f"配置名称「{name}」已存在")
        self.bus.config_changed.emit()
        return int(config_id)

    def save_config(self, data: dict) -> int:
        pending = self.begin_save_config(data)
        self.commit_pending_save(pending)
        return pending.config_id

    def reserve_import_config_id(self) -> int:
        row = (
            TestConfigTable.select(TestConfigTable.config_id)
            .order_by(TestConfigTable.config_id.desc())
            .first()
        )
        if row is None:
            return 1
        return int(row.config_id) + 1

    def create_imported_config(
        self,
        payload: dict,
        *,
        reserved_config_id: int,
        golden_asset_paths_by_channel: dict[int, dict] | None = None,
    ) -> int:
        if (
            not isinstance(reserved_config_id, int)
            or isinstance(reserved_config_id, bool)
            or reserved_config_id <= 0
        ):
            raise ValueError("导入配置 ID 无效")

        imported_paths = self._collect_owned_import_asset_paths(
            golden_asset_paths_by_channel
        )
        try:
            prepared = self.prepare_import_payload(
                payload,
                golden_asset_paths_by_channel=golden_asset_paths_by_channel,
            )
            prepared["config_id"] = reserved_config_id
            errors = self.validate(prepared)
            if errors:
                raise ValueError("\n".join(errors))
            if TestConfigTable.select().where(
                TestConfigTable.config_id == reserved_config_id
            ).exists():
                raise ValueError("导入配置 ID 已被占用")

            with TestConfigTable._meta.database.atomic():
                row_values = self._config_row_values(prepared)
                row_values.update(
                    {
                        "config_id": reserved_config_id,
                        "created_time": now_str(),
                        "updated_time": now_str(),
                    }
                )
                self._insert_config_row_values(row_values)
                self._replace_channel_rows(
                    config_id=reserved_config_id,
                    channels=prepared["channels"],
                )
        except IntegrityError as exc:
            self._safe_rollback(imported_paths)
            self._raise_save_integrity_error(exc, prepared)
        except Exception:
            self._safe_rollback(imported_paths)
            raise

        self.bus.config_changed.emit()
        return reserved_config_id

    def overwrite_imported_config(
        self,
        config_id: int,
        payload: dict,
        *,
        golden_asset_paths_by_channel: dict[int, dict] | None = None,
    ) -> int:
        prepared = self.prepare_import_payload(
            payload,
            golden_asset_paths_by_channel=golden_asset_paths_by_channel,
        )
        prepared["config_id"] = config_id
        imported_paths = self._collect_owned_import_asset_paths(
            golden_asset_paths_by_channel
        )
        try:
            pending = self.begin_save_config(prepared)
        except Exception:
            self._safe_rollback(imported_paths)
            raise
        self.commit_pending_save(pending)
        return pending.config_id

    def prepare_import_payload(
        self,
        payload: dict,
        golden_asset_paths_by_channel: dict[int, dict] | None = None,
    ) -> dict:
        source = deepcopy(payload or {})
        if "fault_types" not in source:
            raise ValueError("fault_types is required")
        fault_types = normalize_fault_types(source["fault_types"])
        channels = source.get("channels", [])
        if not isinstance(channels, list):
            channels = []

        prepared_channels: list[dict] = []
        for channel in channels:
            if not isinstance(channel, dict):
                prepared_channels.append(channel)
                continue
            channel_index = channel.get("channel_index")
            restored_binding = None
            if isinstance(channel_index, int) and not isinstance(channel_index, bool):
                restored_binding = (golden_asset_paths_by_channel or {}).get(channel_index)
            prepared_channels.append(
                self._normalize_import_channel(
                    channel,
                    restored_binding=restored_binding,
                )
            )

        selected_channels = source.get("selected_channels")
        if not isinstance(selected_channels, list):
            selected_channels = [
                channel.get("channel_index")
                for channel in prepared_channels
                if isinstance(channel, dict)
                and isinstance(channel.get("channel_index"), int)
                and not isinstance(channel.get("channel_index"), bool)
            ]
        monitor_gain_db = source.get("monitor_gain_db", MONITOR_GAIN_DEFAULT_DB)
        if monitor_gain_db is None:
            monitor_gain_db = MONITOR_GAIN_DEFAULT_DB
        try:
            result_dashboard_layout = normalize_result_dashboard_layout(
                source.get("result_dashboard_layout"),
                selected_channels=selected_channels,
            )
        except ResultDashboardLayoutError as exc:
            raise ValueError(f"结果展示布局：{exc}") from exc

        return {
            "config_name": str(source.get("config_name", "")).strip(),
            "record_duration": source.get("record_duration"),
            "analysis_duration": source.get("analysis_duration"),
            "analysis_window_offset_ms": source.get("analysis_window_offset_ms", 0),
            "allow_incomplete_recording_analysis": source.get(
                "allow_incomplete_recording_analysis",
                False,
            ),
            "scan_triggers_test": source.get("scan_triggers_test", False),
            "plc_id": None,
            "fault_types": list(fault_types),
            "input_hardware_device_id": source.get("input_hardware_device_id"),
            "input_device_name": source.get("input_device_name"),
            "input_device_hostapi": source.get("input_device_hostapi"),
            "input_device_samplerate": source.get("input_device_samplerate"),
            "output_device_name": source.get("output_device_name"),
            "output_device_hostapi": source.get("output_device_hostapi"),
            "output_device_samplerate": source.get("output_device_samplerate"),
            "selected_channels": selected_channels,
            "vibration_view_channel_index": source.get(
                "vibration_view_channel_index"
            ),
            "monitor_channel_index": source.get("monitor_channel_index"),
            "monitor_gain_db": monitor_gain_db,
            "result_dashboard_layout": result_dashboard_layout,
            "current_meter": _normalize_current_meter_with_source(
                source.get("current_meter")
            ),
            "channels": prepared_channels,
        }

    def _raise_save_integrity_error(self, exc: IntegrityError, data: dict) -> None:
        error_text = str(exc).lower()
        if "foreign key constraint failed" in error_text:
            config_id = data.get("config_id")
            if (
                isinstance(config_id, int)
                and not isinstance(config_id, bool)
                and not TestConfigTable.select().where(
                    TestConfigTable.config_id == config_id
                ).exists()
            ):
                raise ValueError("配置不存在或已被删除") from exc

            plc_id = data.get("plc_id")
            if (
                isinstance(plc_id, int)
                and not isinstance(plc_id, bool)
                and not PlcTable.select().where(PlcTable.id == plc_id).exists()
            ):
                raise ValueError("PLC 不存在或已被删除") from exc

            raise ValueError("配置保存失败，关联数据无效") from exc

        if "unique constraint failed" in error_text:
            raise ValueError(f"配置名称「{data['config_name']}」已存在") from exc

        raise ValueError("配置保存失败，数据约束冲突") from exc

    def begin_save_config(self, data: dict) -> PendingConfigSave:
        errors = self.validate(data)
        if errors:
            raise ValueError("\n".join(errors))

        config_id = data["config_id"]
        if not TestConfigTable.select().where(TestConfigTable.config_id == config_id).exists():
            raise ValueError("配置不存在或已被删除")

        staged_by_channel = self._parse_golden_editor_state(
            data.get("golden_editor_state")
        )
        channels = [dict(channel) for channel in data.get("channels", [])]
        staged_channel_errors = self._validate_staged_channel_mapping(
            channels=channels,
            staged_by_channel=staged_by_channel,
        )
        if staged_channel_errors:
            raise ValueError("\n".join(staged_channel_errors))
        promoted_paths: list[str] = []
        cleanup_after_commit = self._collect_replaced_paths(config_id, channels)
        staged_paths = [sample.staged_path for sample in staged_by_channel.values()]

        if staged_by_channel:
            promoted = self._golden_samples.promote_staged_samples(
                config_id=config_id,
                staged_by_channel=staged_by_channel,
            )
            promoted_paths = [
                meta["golden_sample_path"] for meta in promoted.values()
            ]
            self._apply_promoted_bindings(channels, promoted)
            final_payload = dict(data)
            final_payload["channels"] = channels
            final_payload.pop("golden_editor_state", None)
            post_promotion_errors = self.validate(final_payload)
            if post_promotion_errors:
                self._safe_rollback(promoted_paths)
                raise ValueError("\n".join(post_promotion_errors))
            cleanup_after_commit = self._collect_replaced_paths(config_id, channels)

        try:
            with TestConfigTable._meta.database.atomic():
                TestConfigTable.update(
                    **self._config_row_values(data),
                    updated_time=now_str(),
                ).where(TestConfigTable.config_id == config_id).execute()
                self._replace_channel_rows(config_id=config_id, channels=channels)
        except IntegrityError as exc:
            self._safe_rollback(promoted_paths)
            self._raise_save_integrity_error(exc, data)
        except Exception:
            self._safe_rollback(promoted_paths)
            raise

        return PendingConfigSave(
            config_id=config_id,
            promoted_paths=tuple(promoted_paths),
            old_paths=tuple(cleanup_after_commit),
            staged_paths=tuple(staged_paths),
            golden_bindings_by_channel=tuple(
                (
                    channel["channel_index"],
                    {
                        field: channel.get(field)
                        for field in _GOLDEN_BINDING_FIELDS
                    },
                )
                for channel in channels
                if isinstance(channel.get("channel_index"), int)
                and not isinstance(channel.get("channel_index"), bool)
            ),
        )

    def commit_pending_save(self, pending: PendingConfigSave | None) -> None:
        if pending is None:
            return
        self._post_commit_cleanup(
            old_paths=list(pending.old_paths),
            staged_paths=list(pending.staged_paths),
        )
        self.bus.config_changed.emit()

    def rollback_pending_save(self, pending: PendingConfigSave | None) -> None:
        if pending is None:
            return
        self._safe_rollback(list(pending.promoted_paths))

    def delete_config(self, config_id: int) -> bool:
        cleanup_paths = self._collect_managed_config_paths(config_id)
        with TestConfigTable._meta.database.atomic():
            count = (
                TestConfigTable.delete()
                .where(TestConfigTable.config_id == config_id)
                .execute()
            )
        if count > 0:
            self._post_commit_cleanup(old_paths=cleanup_paths, staged_paths=[])
            self.bus.config_changed.emit()
            return True
        return False

    @staticmethod
    def _resolve_managed_channel_count(
        data: dict,
    ) -> tuple[int | None, str | None]:
        hardware_id = data.get("input_hardware_device_id")
        if type(hardware_id) is not int or hardware_id <= 0:
            return None, _INVALID_INPUT_HARDWARE_ERROR
        try:
            row = InputHardwareTable.get_by_id(hardware_id)
        except Exception:
            return None, _UNAVAILABLE_INPUT_HARDWARE_ERROR
        channel_count = row.channel_count
        if type(channel_count) is not int or channel_count <= 0:
            return None, _UNAVAILABLE_INPUT_HARDWARE_ERROR
        return channel_count, None

    def validate(self, data: dict) -> list[str]:
        errors = []
        if "fault_types" not in data:
            errors.append("fault_types is required")
        else:
            try:
                normalize_fault_types(data["fault_types"])
            except ValueError as exc:
                errors.append(str(exc))
        name = data.get("config_name", "").strip()
        if not name:
            errors.append("配置名称不能为空")
        else:
            q = TestConfigTable.select().where(TestConfigTable.config_name == name)
            config_id = data.get("config_id")
            if config_id is not None:
                q = q.where(TestConfigTable.config_id != config_id)
            if q.exists():
                errors.append(f"配置名称「{name}」已存在")

        rd = data.get("record_duration", 0)
        ad = data.get("analysis_duration", 0)
        if not isinstance(rd, (int, float)) or isinstance(rd, bool) or rd <= 0:
            errors.append("录音时长必须大于 0")
        if not isinstance(ad, (int, float)) or isinstance(ad, bool) or ad <= 0:
            errors.append("分析时长必须大于 0")
        elif isinstance(rd, (int, float)) and not isinstance(rd, bool) and rd > 0 and ad > rd:
            errors.append("分析时长不能超过录音时长")

        if not isinstance(data.get("scan_triggers_test"), bool):
            errors.append("扫码触发必须为布尔值")
        if not isinstance(
            data.get("allow_incomplete_recording_analysis", False), bool
        ):
            errors.append("允许分析不完整录音必须为布尔值")
        plc_id = data.get("plc_id")
        if plc_id is not None:
            if not isinstance(plc_id, int) or isinstance(plc_id, bool):
                errors.append("PLC 绑定无效")
            elif not PlcTable.select().where(PlcTable.id == plc_id).exists():
                errors.append("PLC 不存在或已被删除")

        managed_count_result = self._resolve_managed_channel_count(data)
        managed_channel_count, managed_input_error = managed_count_result
        if managed_input_error is not None:
            errors.append(managed_input_error)
        errors.extend(
            self.validate_factory_runtime(
                data,
                _managed_count_result=managed_count_result,
            )
        )
        current_meter = _normalize_current_meter_with_source(data.get("current_meter"))
        if current_meter["enabled"] and current_meter["source"] == "plc":
            if plc_id is None:
                errors.append("PLC 电流源需要绑定有效 PLC")
        else:
            errors.extend(validate_current_meter_config(current_meter))

        selected_channels_for_layout = self._selected_channels_for_layout_validation(
            data.get("selected_channels", [])
        )
        try:
            normalize_result_dashboard_layout(
                data.get("result_dashboard_layout"),
                selected_channels=selected_channels_for_layout,
            )
        except ResultDashboardLayoutError as exc:
            errors.append(f"结果展示布局：{exc}")

        input_dev = data.get("input_device_name")
        sel_ch = data.get("selected_channels", [])
        if input_dev and not sel_ch:
            errors.append("已选择输入设备但未选择任何通道")

        monitor_channel = data.get("monitor_channel_index")
        if monitor_channel is not None:
            if (
                not isinstance(monitor_channel, int)
                or isinstance(monitor_channel, bool)
                or monitor_channel < 0
            ):
                errors.append("监听通道必须为不小于 0 的整数")
            else:
                if (
                    managed_channel_count is not None
                    and monitor_channel >= managed_channel_count
                ):
                    errors.append("监听通道必须小于输入通道数")

        monitor_gain = data.get("monitor_gain_db", MONITOR_GAIN_DEFAULT_DB)
        if monitor_gain is None:
            monitor_gain = MONITOR_GAIN_DEFAULT_DB
        if not self._is_numeric(monitor_gain) or not math.isfinite(float(monitor_gain)):
            errors.append("监听增益必须为有限数值")
        elif not MONITOR_GAIN_MIN_DB <= float(monitor_gain) <= MONITOR_GAIN_MAX_DB:
            errors.append(
                f"监听增益必须在 {MONITOR_GAIN_MIN_DB:g} 到 {MONITOR_GAIN_MAX_DB:g} dB 之间"
            )
        channels = data.get("channels", [])
        if not isinstance(channels, list):
            errors.append("通道数据格式无效")
            return errors

        staged_by_channel = self._parse_golden_editor_state(
            data.get("golden_editor_state")
        )
        sample_rate = data.get("input_device_samplerate")

        seen_idx = set()
        for i, ch in enumerate(channels):
            if not isinstance(ch, dict):
                errors.append(f"通道 {i + 1}: 数据格式无效")
                continue
            idx = ch.get("channel_index")
            if not isinstance(idx, int) or isinstance(idx, bool):
                errors.append(f"通道 {i + 1}: 通道序号无效")
                continue
            prefix = f"通道 {idx + 1}"
            if idx < 0:
                errors.append(f"{prefix}: 通道序号无效")
            if idx in seen_idx:
                errors.append(f"{prefix}: 通道序号重复")
            seen_idx.add(idx)

            if ch.get("stft_nfft") not in _VALID_NFFT:
                errors.append(f"{prefix}: STFT FFT 点数无效")
            if ch.get("fft_nfft", _CHANNEL_DEFAULTS["fft_nfft"]) not in _VALID_FFT_NFFT:
                errors.append(f"{prefix}: FFT 分辨率无效")
            if (
                ch.get(
                    "fft_x_axis_scale",
                    _CHANNEL_DEFAULTS["fft_x_axis_scale"],
                )
                not in _VALID_FFT_X_AXIS_SCALES
            ):
                errors.append(f"{prefix}: FFT 横轴模式无效")
            if (
                ch.get("fft_weighting", _CHANNEL_DEFAULTS["fft_weighting"])
                not in _VALID_WEIGHTING
            ):
                errors.append(f"{prefix}: FFT 计权模式无效")
            if ch.get("stft_overlap") not in _VALID_OVERLAP:
                errors.append(f"{prefix}: STFT 重叠率无效")
            fixed_color_scale_enabled = ch.get(
                "stft_color_scale_enabled",
                _CHANNEL_DEFAULTS["stft_color_scale_enabled"],
            )
            if "stft_color_scale_enabled" in ch and not isinstance(
                fixed_color_scale_enabled,
                bool,
            ):
                errors.append(f"{prefix}: STFT 固定色阶开关必须为布尔值")
            elif fixed_color_scale_enabled:
                min_db = self._finite_float_or_none(
                    ch.get("stft_color_scale_min_db")
                )
                max_db = self._finite_float_or_none(
                    ch.get("stft_color_scale_max_db")
                )
                if min_db is None or max_db is None:
                    errors.append(f"{prefix}: STFT 色阶上下限必须为有限数值")
                elif min_db >= max_db:
                    errors.append(f"{prefix}: STFT 色阶下限必须小于上限")
            errors.extend(self._validate_stft_score_fields(prefix, ch))
            if ch.get("spl_weighting") not in _VALID_WEIGHTING:
                errors.append(f"{prefix}: SPL 计权模式无效")
            tc = ch.get("spl_time_constant", 0)
            if not isinstance(tc, (int, float)) or isinstance(tc, bool) or tc <= 0:
                errors.append(f"{prefix}: SPL 时间常数必须大于 0")
            loudness_unit = ch.get(
                "loudness_unit", _CHANNEL_DEFAULTS["loudness_unit"]
            )
            if loudness_unit not in _VALID_LOUDNESS_UNITS:
                errors.append(f"{prefix}: 响度单位无效")
            errors.extend(self._validate_channel_limit_groups(prefix, ch))
            if ch.get("oct_weighting") not in _VALID_WEIGHTING:
                errors.append(f"{prefix}: 倍频程计权模式无效")

            for label, key in [("FFT", "fft_band_limits"), ("倍频程", "oct_band_limits")]:
                errors.extend(
                    f"{prefix}: {message}"
                    for message in self._validate_channel_limit_tables(
                        ch.get(key, []),
                        label=label,
                    )
                )

            errors.extend(
                self._validate_golden_channel(
                    prefix=prefix,
                    channel=ch,
                    staged_binding=staged_by_channel.get(idx),
                    analysis_duration=ad,
                    sample_rate=sample_rate,
                )
            )
        errors.extend(
            self._validate_staged_channel_mapping(
                channels=channels,
                staged_by_channel=staged_by_channel,
            )
        )
        return errors

    def _validate_vibration_view_channel(
        self,
        data: dict,
        *,
        managed_channel_count: int | None,
    ) -> list[str]:
        vibration_channel = data.get("vibration_view_channel_index")
        if vibration_channel is None:
            return []
        if (
            not isinstance(vibration_channel, int)
            or isinstance(vibration_channel, bool)
            or vibration_channel < 0
        ):
            return ["振动视图通道必须为不小于 0 的整数"]

        if (
            managed_channel_count is not None
            and vibration_channel >= managed_channel_count
        ):
            return ["振动视图通道必须小于输入通道数"]

        selected_channels = data.get("selected_channels", [])
        if isinstance(selected_channels, list) and vibration_channel not in selected_channels:
            return ["振动视图通道必须来自已选择通道"]
        return []

    def validate_factory_runtime(
        self,
        data: dict,
        *,
        _managed_count_result=_UNRESOLVED_MANAGED_COUNT,
    ) -> list[str]:
        errors = []
        if _managed_count_result is _UNRESOLVED_MANAGED_COUNT:
            _managed_count_result = self._resolve_managed_channel_count(data)
            if _managed_count_result[1] is not None:
                errors.append(_managed_count_result[1])
        managed_channel_count = _managed_count_result[0]
        selected_channels = data.get("selected_channels", [])
        if isinstance(selected_channels, (list, tuple)) and len(selected_channels) > 2:
            errors.append("最多选择两个通道")
        offset = data.get("analysis_window_offset_ms", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            errors.append("分析窗起始偏移必须为不小于 0 的整数")
        else:
            rd = data.get("record_duration", 0)
            ad = data.get("analysis_duration", 0)
            has_valid_rd = (
                isinstance(rd, (int, float)) and not isinstance(rd, bool) and rd > 0
            )
            has_valid_ad = (
                isinstance(ad, (int, float)) and not isinstance(ad, bool) and ad > 0
            )
            if has_valid_rd and has_valid_ad:
                window_end_ms = Decimal(offset) + (Decimal(str(ad)) * Decimal("1000"))
                record_duration_ms = Decimal(str(rd)) * Decimal("1000")
                if window_end_ms > record_duration_ms:
                    errors.append("分析窗起始偏移加分析时长不能超过录音时长")

        errors.extend(
            self._validate_vibration_view_channel(
                data,
                managed_channel_count=managed_channel_count,
            )
        )
        return errors

    def _validate_stft_score_fields(self, prefix: str, channel: dict) -> list[str]:
        errors: list[str] = []
        if "stft_score_enabled" in channel and not isinstance(
            channel["stft_score_enabled"],
            bool,
        ):
            errors.append(f"{prefix}: STFT 评分启用必须为布尔值")
        summary_mode = channel.get(
            "stft_score_summary_mode",
            _CHANNEL_DEFAULTS["stft_score_summary_mode"],
        )
        if not (
            isinstance(summary_mode, str)
            and summary_mode in _VALID_STFT_SCORE_SUMMARY_MODES
        ):
            errors.append(f"{prefix}: STFT 评分汇总方式无效")

        percentile = channel.get(
            "stft_score_percentile",
            _CHANNEL_DEFAULTS["stft_score_percentile"],
        )
        if not self._is_finite_number(percentile) or not (
            0.0 < float(percentile) <= 100.0
        ):
            errors.append(f"{prefix}: STFT 评分分位数必须大于0且小于等于100")

        for field, label in (
            ("stft_score_loudness_reference_sone", "响度参考值"),
            ("stft_score_modulation_reference", "响度变化参考值"),
            ("stft_score_harmonic_ratio_reference", "谐波占比参考值"),
            ("stft_score_frequency_instability_reference", "频率不稳定参考值"),
        ):
            if not self._is_positive_finite(
                channel.get(field, _CHANNEL_DEFAULTS[field])
            ):
                errors.append(f"{prefix}: STFT 评分{label}必须为正数")

        start_order = channel.get(
            "stft_score_harmonic_start_order",
            _CHANNEL_DEFAULTS["stft_score_harmonic_start_order"],
        )
        end_order = channel.get(
            "stft_score_harmonic_end_order",
            _CHANNEL_DEFAULTS["stft_score_harmonic_end_order"],
        )
        if not isinstance(start_order, int) or isinstance(start_order, bool):
            errors.append(f"{prefix}: STFT 评分谐波起始阶次必须为整数")
        if not isinstance(end_order, int) or isinstance(end_order, bool):
            errors.append(f"{prefix}: STFT 评分谐波结束阶次必须为整数")
        if (
            isinstance(start_order, int)
            and not isinstance(start_order, bool)
            and start_order < 2
        ):
            errors.append(f"{prefix}: STFT 评分谐波起始阶次必须大于等于2")
        if (
            isinstance(start_order, int)
            and not isinstance(start_order, bool)
            and isinstance(end_order, int)
            and not isinstance(end_order, bool)
            and end_order < start_order
        ):
            errors.append(f"{prefix}: STFT 评分谐波结束阶次必须大于等于起始阶次")
        return errors

    def _validate_channel_limit_groups(self, prefix: str, channel: dict) -> list[str]:
        errors: list[str] = []
        for (
            label,
            lower_enabled_key,
            lower_value_key,
            upper_enabled_key,
            upper_value_key,
            non_negative,
        ) in _CHANNEL_LIMIT_GROUPS:
            lower_label = self._limit_bound_label(label, "下限")
            upper_label = self._limit_bound_label(label, "上限")
            lower_enabled = channel.get(lower_enabled_key, _CHANNEL_DEFAULTS[lower_enabled_key])
            upper_enabled = channel.get(upper_enabled_key, _CHANNEL_DEFAULTS[upper_enabled_key])
            if not isinstance(lower_enabled, bool):
                errors.append(f"{prefix}: {lower_label}启用必须为布尔值")
            if not isinstance(upper_enabled, bool):
                errors.append(f"{prefix}: {upper_label}启用必须为布尔值")
            lower_value = channel.get(lower_value_key, _CHANNEL_DEFAULTS[lower_value_key])
            upper_value = channel.get(upper_value_key, _CHANNEL_DEFAULTS[upper_value_key])
            lower_valid = True
            upper_valid = True
            if lower_enabled is True:
                if not self._is_numeric(lower_value):
                    errors.append(f"{prefix}: {lower_label}必须为数值")
                    lower_valid = False
                elif non_negative and lower_value < 0:
                    errors.append(f"{prefix}: {lower_label}必须大于等于 0")
                    lower_valid = False
            if upper_enabled is True:
                if not self._is_numeric(upper_value):
                    errors.append(f"{prefix}: {upper_label}必须为数值")
                    upper_valid = False
                elif non_negative and upper_value < 0:
                    errors.append(f"{prefix}: {upper_label}必须大于等于 0")
                    upper_valid = False
            if (
                lower_enabled is True
                and upper_enabled is True
                and lower_valid
                and upper_valid
                and lower_value >= upper_value
            ):
                errors.append(f"{prefix}: {lower_label}必须小于上限")
        return errors

    def _validate_channel_limit_tables(self, raw_limits, *, label: str) -> list[str]:
        return [
            self._channel_limit_table_validation_message(message)
            for message in validate_limit_tables(raw_limits, label=label)
        ]

    @staticmethod
    def _channel_limit_table_validation_message(message: str) -> str:
        return (
            message.replace("类型必须是 upper 或 lower", "类型必须是上限或下限")
            .replace(" points 必须是列表", "频点列表必须是列表")
        )

    @staticmethod
    def _limit_bound_label(label: str, bound: str) -> str:
        separator = " " if "SPL" in label else ""
        return f"{label}{separator}{bound}"

    def _validate_golden_channel(
        self,
        *,
        prefix: str,
        channel: dict,
        staged_binding: StagedGoldenSample | None,
        analysis_duration: float,
        sample_rate,
    ) -> list[str]:
        errors: list[str] = []
        fft_mode = channel.get("fft_golden_mode", _CHANNEL_DEFAULTS["fft_golden_mode"])
        oct_mode = channel.get("oct_golden_mode", _CHANNEL_DEFAULTS["oct_golden_mode"])
        fft_overlay = channel.get(
            "fft_show_golden_overlay",
            _CHANNEL_DEFAULTS["fft_show_golden_overlay"],
        )
        oct_overlay = channel.get(
            "oct_show_golden_overlay",
            _CHANNEL_DEFAULTS["oct_show_golden_overlay"],
        )

        if fft_mode not in _VALID_GOLDEN_MODES:
            errors.append(f"{prefix}: FFT 黄金模式无效")
        if oct_mode not in _VALID_GOLDEN_MODES:
            errors.append(f"{prefix}: 倍频程黄金模式无效")
        if not isinstance(fft_overlay, bool):
            errors.append(f"{prefix}: FFT 黄金曲线显示开关无效")
        if not isinstance(oct_overlay, bool):
            errors.append(f"{prefix}: 倍频程黄金曲线显示开关无效")
        if fft_mode == "delta" and fft_overlay is True:
            errors.append(f"{prefix}: FFT Delta 模式不允许显示黄金曲线")
        if oct_mode == "delta" and oct_overlay is True:
            errors.append(f"{prefix}: 倍频程 Delta 模式不允许显示黄金曲线")

        binding_errors: list[str] = []
        binding_present = staged_binding is not None or self._channel_has_binding(channel)
        if staged_binding is not None:
            binding_errors.extend(
                self._golden_samples.validate_staged_binding(
                    staged_binding,
                    analysis_duration=analysis_duration,
                    sample_rate=sample_rate,
                )
            )
        elif self._channel_has_binding(channel):
            binding_errors.extend(
                self._golden_samples.validate_committed_binding(
                    channel_config=channel,
                    analysis_duration=analysis_duration,
                    sample_rate=sample_rate,
                )
            )

        for error in binding_errors:
            errors.append(f"{prefix}: {error}")

        binding_valid = binding_present and not binding_errors
        if fft_mode != "off" and not binding_valid:
            errors.append(f"{prefix}: FFT 黄金模式需要有效的黄金样本绑定")
        if fft_overlay is True and not binding_valid:
            errors.append(f"{prefix}: FFT黄金曲线显示需要有效的黄金样本绑定")
        if oct_mode != "off" and not binding_valid:
            errors.append(f"{prefix}: 倍频程黄金模式需要有效的黄金样本绑定")
        if oct_overlay is True and not binding_valid:
            errors.append(f"{prefix}: 倍频程黄金曲线显示需要有效的黄金样本绑定")
        return errors

    def _parse_golden_editor_state(
        self,
        raw_state,
    ) -> dict[int, StagedGoldenSample]:
        if not isinstance(raw_state, dict):
            return {}
        state: dict[int, StagedGoldenSample] = {}
        for raw_index, payload in raw_state.items():
            if isinstance(raw_index, int) and not isinstance(raw_index, bool):
                index = raw_index
            else:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
            state[index] = StagedGoldenSample.from_mapping(payload)
        return state

    def _apply_promoted_bindings(
        self,
        channels: list[dict],
        promoted_by_channel: dict[int, dict],
    ) -> None:
        for channel in channels:
            channel_index = channel.get("channel_index")
            if channel_index in promoted_by_channel:
                channel.update(promoted_by_channel[channel_index])

    def _validate_staged_channel_mapping(
        self,
        *,
        channels: list[dict],
        staged_by_channel: dict[int, StagedGoldenSample],
    ) -> list[str]:
        if not staged_by_channel:
            return []
        valid_indexes = {
            channel.get("channel_index")
            for channel in channels
            if isinstance(channel, dict)
            and isinstance(channel.get("channel_index"), int)
            and not isinstance(channel.get("channel_index"), bool)
        }
        errors: list[str] = []
        for channel_index in sorted(set(staged_by_channel) - valid_indexes):
            errors.append(
                f"暂存黄金样本通道 {channel_index + 1} 缺少对应通道配置"
            )
        return errors

    def _collect_replaced_paths(
        self,
        config_id: int,
        channels: list[dict],
    ) -> list[str]:
        existing_paths = {
            row.channel_index: self._normalize_optional_text(row.golden_sample_path)
            for row in ChannelConfigTable.select(
                ChannelConfigTable.channel_index,
                ChannelConfigTable.golden_sample_path,
            ).where(ChannelConfigTable.config == config_id)
        }
        new_paths = {
            channel.get("channel_index"): self._normalize_optional_text(
                channel.get("golden_sample_path")
            )
            for channel in channels
            if isinstance(channel.get("channel_index"), int)
            and not isinstance(channel.get("channel_index"), bool)
        }

        cleanup: list[str] = []
        for channel_index, old_path in existing_paths.items():
            if (
                old_path
                and old_path != new_paths.get(channel_index)
                and self._golden_samples.is_managed_path(old_path)
            ):
                cleanup.append(old_path)
        return cleanup

    def _collect_managed_config_paths(self, config_id: int) -> list[str]:
        paths: list[str] = []
        for row in ChannelConfigTable.select(ChannelConfigTable.golden_sample_path).where(
            ChannelConfigTable.config == config_id
        ):
            path = self._normalize_optional_text(row.golden_sample_path)
            if path and self._golden_samples.is_managed_path(path):
                paths.append(path)
        return paths

    def _collect_imported_channel_paths(
        self,
        channels: list[dict],
        *,
        exclude_paths: set[str] | None = None,
    ) -> list[str]:
        excluded = set(exclude_paths or ())
        paths: list[str] = []
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            path = self._normalize_optional_text(channel.get("golden_sample_path"))
            if (
                path
                and path not in excluded
                and self._golden_samples.is_managed_path(path)
                and not self._golden_samples.is_managed_staged_path(path)
            ):
                paths.append(path)
        return paths

    def _collect_owned_import_asset_paths(
        self,
        golden_asset_paths_by_channel: dict[int, dict] | None,
    ) -> list[str]:
        if not isinstance(golden_asset_paths_by_channel, dict):
            return []

        paths: list[str] = []
        for binding in golden_asset_paths_by_channel.values():
            if not isinstance(binding, dict):
                continue
            path = self._normalize_optional_text(binding.get("golden_sample_path"))
            if (
                path
                and self._golden_samples.is_managed_path(path)
                and not self._golden_samples.is_managed_staged_path(path)
            ):
                paths.append(path)
        return paths

    def _normalize_import_channel(
        self,
        channel: dict,
        *,
        restored_binding: dict | None,
    ) -> dict:
        normalized = {
            "channel_index": channel.get("channel_index"),
            "stft_nfft": channel.get("stft_nfft", _CHANNEL_DEFAULTS["stft_nfft"]),
            "fft_nfft": channel.get("fft_nfft", _CHANNEL_DEFAULTS["fft_nfft"]),
            "stft_overlap": channel.get(
                "stft_overlap",
                _CHANNEL_DEFAULTS["stft_overlap"],
            ),
            **self._normalized_stft_color_scale_fields(channel),
            **self._normalized_stft_score_fields(channel),
            "spl_weighting": channel.get(
                "spl_weighting",
                _CHANNEL_DEFAULTS["spl_weighting"],
            ),
            "spl_time_constant": channel.get(
                "spl_time_constant",
                _CHANNEL_DEFAULTS["spl_time_constant"],
            ),
            "spl_lower_enabled": channel.get(
                "spl_lower_enabled",
                _CHANNEL_DEFAULTS["spl_lower_enabled"],
            ),
            "spl_upper_enabled": channel.get(
                "spl_upper_enabled",
                _CHANNEL_DEFAULTS["spl_upper_enabled"],
            ),
            "spl_upper_limit": channel.get(
                "spl_upper_limit",
                _CHANNEL_DEFAULTS["spl_upper_limit"],
            ),
            "spl_lower_limit": channel.get(
                "spl_lower_limit",
                _CHANNEL_DEFAULTS["spl_lower_limit"],
            ),
            "spl_average_lower_enabled": channel.get(
                "spl_average_lower_enabled",
                _CHANNEL_DEFAULTS["spl_average_lower_enabled"],
            ),
            "spl_average_lower_limit": channel.get(
                "spl_average_lower_limit",
                _CHANNEL_DEFAULTS["spl_average_lower_limit"],
            ),
            "spl_average_upper_enabled": channel.get(
                "spl_average_upper_enabled",
                _CHANNEL_DEFAULTS["spl_average_upper_enabled"],
            ),
            "spl_average_upper_limit": channel.get(
                "spl_average_upper_limit",
                _CHANNEL_DEFAULTS["spl_average_upper_limit"],
            ),
            "loudness_unit": channel.get(
                "loudness_unit",
                _CHANNEL_DEFAULTS["loudness_unit"],
            ),
            "loudness_lower_enabled": channel.get(
                "loudness_lower_enabled",
                _CHANNEL_DEFAULTS["loudness_lower_enabled"],
            ),
            "loudness_upper_enabled": channel.get(
                "loudness_upper_enabled",
                _CHANNEL_DEFAULTS["loudness_upper_enabled"],
            ),
            "loudness_lower_limit": channel.get(
                "loudness_lower_limit",
                _CHANNEL_DEFAULTS["loudness_lower_limit"],
            ),
            "loudness_upper_limit": channel.get(
                "loudness_upper_limit",
                _CHANNEL_DEFAULTS["loudness_upper_limit"],
            ),
            "loudness_average_lower_enabled": channel.get(
                "loudness_average_lower_enabled",
                _CHANNEL_DEFAULTS["loudness_average_lower_enabled"],
            ),
            "loudness_average_lower_limit": channel.get(
                "loudness_average_lower_limit",
                _CHANNEL_DEFAULTS["loudness_average_lower_limit"],
            ),
            "loudness_average_upper_enabled": channel.get(
                "loudness_average_upper_enabled",
                _CHANNEL_DEFAULTS["loudness_average_upper_enabled"],
            ),
            "loudness_average_upper_limit": channel.get(
                "loudness_average_upper_limit",
                _CHANNEL_DEFAULTS["loudness_average_upper_limit"],
            ),
            "fft_x_axis_scale": self._normalize_fft_x_axis_scale(
                channel.get(
                    "fft_x_axis_scale",
                    _CHANNEL_DEFAULTS["fft_x_axis_scale"],
                )
            ),
            "fft_weighting": self._normalize_weighting(
                channel.get(
                    "fft_weighting",
                    _CHANNEL_DEFAULTS["fft_weighting"],
                )
            ),
            "fft_band_limits": deepcopy(channel.get("fft_band_limits", [])),
            "oct_weighting": channel.get(
                "oct_weighting",
                _CHANNEL_DEFAULTS["oct_weighting"],
            ),
            "oct_band_limits": deepcopy(channel.get("oct_band_limits", [])),
            "ai_config": deepcopy(channel.get("ai_config", {})),
            "fft_golden_mode": channel.get(
                "fft_golden_mode",
                _CHANNEL_DEFAULTS["fft_golden_mode"],
            ),
            "fft_show_golden_overlay": channel.get(
                "fft_show_golden_overlay",
                _CHANNEL_DEFAULTS["fft_show_golden_overlay"],
            ),
            "oct_golden_mode": channel.get(
                "oct_golden_mode",
                _CHANNEL_DEFAULTS["oct_golden_mode"],
            ),
            "oct_show_golden_overlay": channel.get(
                "oct_show_golden_overlay",
                _CHANNEL_DEFAULTS["oct_show_golden_overlay"],
            ),
        }

        if restored_binding is None:
            self._clear_golden_binding(normalized)
        else:
            for field in _GOLDEN_BINDING_FIELDS:
                normalized[field] = restored_binding.get(field)
        self._normalize_channel_limit_storage(normalized)
        return normalized

    def _channel_db_values(self, channel: dict) -> dict:
        normalized_channel = dict(channel)
        self._normalize_channel_limit_storage(normalized_channel)
        self._normalize_stft_color_scale_storage(normalized_channel)
        self._normalize_stft_score_storage(normalized_channel)
        return {
            "channel_index": normalized_channel["channel_index"],
            "stft_nfft": normalized_channel["stft_nfft"],
            "fft_nfft": normalized_channel.get("fft_nfft", _CHANNEL_DEFAULTS["fft_nfft"]),
            "stft_overlap": normalized_channel["stft_overlap"],
            "stft_color_scale_enabled": normalized_channel[
                "stft_color_scale_enabled"
            ],
            "stft_color_scale_min_db": normalized_channel[
                "stft_color_scale_min_db"
            ],
            "stft_color_scale_max_db": normalized_channel[
                "stft_color_scale_max_db"
            ],
            "stft_score_enabled": normalized_channel["stft_score_enabled"],
            "stft_score_summary_mode": normalized_channel[
                "stft_score_summary_mode"
            ],
            "stft_score_percentile": normalized_channel["stft_score_percentile"],
            "stft_score_loudness_reference_sone": normalized_channel[
                "stft_score_loudness_reference_sone"
            ],
            "stft_score_modulation_reference": normalized_channel[
                "stft_score_modulation_reference"
            ],
            "stft_score_harmonic_ratio_reference": normalized_channel[
                "stft_score_harmonic_ratio_reference"
            ],
            "stft_score_frequency_instability_reference": normalized_channel[
                "stft_score_frequency_instability_reference"
            ],
            "stft_score_harmonic_start_order": normalized_channel[
                "stft_score_harmonic_start_order"
            ],
            "stft_score_harmonic_end_order": normalized_channel[
                "stft_score_harmonic_end_order"
            ],
            "spl_weighting": normalized_channel["spl_weighting"],
            "spl_time_constant": normalized_channel["spl_time_constant"],
            "spl_lower_enabled": normalized_channel["spl_lower_enabled"],
            "spl_upper_enabled": normalized_channel["spl_upper_enabled"],
            "spl_upper_limit": normalized_channel["spl_upper_limit"],
            "spl_lower_limit": normalized_channel["spl_lower_limit"],
            "spl_average_lower_enabled": normalized_channel["spl_average_lower_enabled"],
            "spl_average_lower_limit": normalized_channel["spl_average_lower_limit"],
            "spl_average_upper_enabled": normalized_channel["spl_average_upper_enabled"],
            "spl_average_upper_limit": normalized_channel["spl_average_upper_limit"],
            "loudness_unit": normalized_channel.get(
                "loudness_unit", _CHANNEL_DEFAULTS["loudness_unit"]
            ),
            "loudness_lower_enabled": normalized_channel["loudness_lower_enabled"],
            "loudness_upper_enabled": normalized_channel["loudness_upper_enabled"],
            "loudness_lower_limit": normalized_channel["loudness_lower_limit"],
            "loudness_upper_limit": normalized_channel["loudness_upper_limit"],
            "loudness_average_lower_enabled": normalized_channel["loudness_average_lower_enabled"],
            "loudness_average_lower_limit": normalized_channel["loudness_average_lower_limit"],
            "loudness_average_upper_enabled": normalized_channel["loudness_average_upper_enabled"],
            "loudness_average_upper_limit": normalized_channel["loudness_average_upper_limit"],
            "fft_x_axis_scale": self._normalize_fft_x_axis_scale(
                normalized_channel.get(
                    "fft_x_axis_scale",
                    _CHANNEL_DEFAULTS["fft_x_axis_scale"],
                )
            ),
            "fft_weighting": self._normalize_weighting(
                normalized_channel.get(
                    "fft_weighting",
                    _CHANNEL_DEFAULTS["fft_weighting"],
                )
            ),
            "fft_band_limits": json.dumps(normalized_channel["fft_band_limits"]),
            "oct_weighting": normalized_channel["oct_weighting"],
            "oct_band_limits": json.dumps(normalized_channel["oct_band_limits"]),
            "ai_config": json.dumps(normalized_channel.get("ai_config", {})),
            "golden_sample_path": self._normalize_optional_text(
                normalized_channel.get("golden_sample_path")
            ),
            "golden_sample_label": self._normalize_optional_text(
                normalized_channel.get("golden_sample_label")
            ),
            "golden_sample_source_channel": normalized_channel.get("golden_sample_source_channel"),
            "golden_sample_start_sample": normalized_channel.get("golden_sample_start_sample"),
            "golden_sample_num_samples": normalized_channel.get("golden_sample_num_samples"),
            "golden_sample_start_ms": normalized_channel.get("golden_sample_start_ms"),
            "golden_sample_duration_ms": normalized_channel.get("golden_sample_duration_ms"),
            "golden_sample_sample_rate": normalized_channel.get("golden_sample_sample_rate"),
            "fft_golden_mode": normalized_channel.get(
                "fft_golden_mode",
                _CHANNEL_DEFAULTS["fft_golden_mode"],
            ),
            "fft_show_golden_overlay": normalized_channel.get(
                "fft_show_golden_overlay",
                _CHANNEL_DEFAULTS["fft_show_golden_overlay"],
            ),
            "oct_golden_mode": normalized_channel.get(
                "oct_golden_mode",
                _CHANNEL_DEFAULTS["oct_golden_mode"],
            ),
            "oct_show_golden_overlay": normalized_channel.get(
                "oct_show_golden_overlay",
                _CHANNEL_DEFAULTS["oct_show_golden_overlay"],
            ),
        }

    def _config_row_values(self, data: dict) -> dict:
        analysis_window_offset_ms = int(data.get("analysis_window_offset_ms", 0))
        values = {
            "config_name": data["config_name"].strip(),
            "record_duration": data["record_duration"],
            "analysis_duration": data["analysis_duration"],
            "analysis_window_offset_ms": analysis_window_offset_ms,
            "allow_incomplete_recording_analysis": bool(
                data.get("allow_incomplete_recording_analysis", False)
            ),
            "scan_triggers_test": data["scan_triggers_test"],
            "plc": data.get("plc_id"),
            "input_device_name": data.get("input_device_name"),
            "input_device_hostapi": data.get("input_device_hostapi"),
            "input_device_samplerate": data.get("input_device_samplerate"),
            "output_device_name": data.get("output_device_name"),
            "output_device_hostapi": data.get("output_device_hostapi"),
            "output_device_samplerate": data.get("output_device_samplerate"),
            "selected_channels": json.dumps(data.get("selected_channels", [])),
            "fault_types_json": encode_fault_types_json(data["fault_types"]),
        }
        available_columns = self._config_table_columns()
        if "fault_types_json" not in available_columns:
            raise ValueError("test_config_table is missing fault_types_json")
        if self._config_field_exists(
            "input_hardware_device_id", available_columns
        ):
            values["input_hardware_device_id"] = data.get(
                "input_hardware_device_id"
            )
        if self._config_field_exists("monitor_channel_index", available_columns):
            values["monitor_channel_index"] = data.get("monitor_channel_index")
        if self._config_field_exists("monitor_gain_db", available_columns):
            values["monitor_gain_db"] = self._normalize_monitor_gain(
                data.get("monitor_gain_db", MONITOR_GAIN_DEFAULT_DB)
            )
        if self._config_field_exists(
            "vibration_view_channel_index",
            available_columns,
        ):
            values["vibration_view_channel_index"] = data.get(
                "vibration_view_channel_index"
            )
        if self._config_field_exists("result_dashboard_layout", available_columns):
            values["result_dashboard_layout"] = serialize_result_dashboard_layout(
                data.get("result_dashboard_layout")
            )
        current_meter = _normalize_current_meter_with_source(data.get("current_meter"))
        self._add_current_meter_row_values(
            values,
            current_meter=current_meter,
            available_columns=available_columns,
        )
        return values

    def _add_current_meter_row_values(
        self,
        values: dict,
        *,
        current_meter: dict,
        available_columns: set[str],
    ) -> None:
        field_map = {
            "current_meter_enabled": "enabled",
            "current_meter_source": "source",
            "current_meter_host": "host",
            "current_meter_port": "port",
            "current_meter_unit_id": "unit_id",
            "current_meter_register_type": "register_type",
            "current_meter_register_address": "register_address",
            "current_meter_register_count": "register_count",
            "current_meter_data_type": "data_type",
            "current_meter_byte_order": "byte_order",
            "current_meter_word_order": "word_order",
            "current_meter_scale": "scale",
            "current_meter_offset": "offset",
            "current_meter_poll_interval_ms": "poll_interval_ms",
            "current_meter_timeout_ms": "timeout_ms",
        }
        for field_name, config_key in field_map.items():
            if self._config_field_exists(field_name, available_columns):
                values[field_name] = current_meter[config_key]

    def _normalize_current_meter_from_row(self, row: dict) -> dict:
        return _normalize_current_meter_with_source(
            {
                "enabled": row.get("current_meter_enabled", False),
                "source": row.get("current_meter_source", "modbus_tcp"),
                "host": row.get("current_meter_host"),
                "port": row.get("current_meter_port"),
                "unit_id": row.get("current_meter_unit_id"),
                "register_type": row.get("current_meter_register_type"),
                "register_address": row.get("current_meter_register_address"),
                "register_count": row.get("current_meter_register_count"),
                "data_type": row.get("current_meter_data_type"),
                "byte_order": row.get("current_meter_byte_order"),
                "word_order": row.get("current_meter_word_order"),
                "scale": row.get("current_meter_scale"),
                "offset": row.get("current_meter_offset"),
                "poll_interval_ms": row.get("current_meter_poll_interval_ms"),
                "timeout_ms": row.get("current_meter_timeout_ms"),
            }
        )

    def _insert_config_row_values(self, values: dict) -> int:
        available_columns = self._config_table_columns()
        if (
            "fault_types_json" in values
            and "fault_types_json" not in available_columns
        ):
            raise ValueError("test_config_table is missing fault_types_json")
        filtered_values = {
            field_name: value
            for field_name, value in values.items()
            if self._config_field_exists(field_name, available_columns)
        }
        if not filtered_values:
            raise ValueError("配置数据为空")

        columns = []
        params = []
        for field_name, value in filtered_values.items():
            field = TestConfigTable._meta.fields[field_name]
            columns.append(field.column_name)
            params.append(value)

        table_name = TestConfigTable._meta.table_name
        column_sql = ", ".join(f'"{column}"' for column in columns)
        placeholder_sql = ", ".join("?" for _ in columns)
        cursor = TestConfigTable._meta.database.execute_sql(
            f'INSERT INTO "{table_name}" ({column_sql}) VALUES ({placeholder_sql})',
            params,
        )
        config_id = filtered_values.get("config_id")
        if config_id is not None:
            return int(config_id)
        return int(cursor.lastrowid)

    @staticmethod
    def _config_table_columns() -> set[str]:
        cursor = TestConfigTable._meta.database.execute_sql(
            f"PRAGMA table_info({TestConfigTable._meta.table_name})"
        )
        return {row[1] for row in cursor.fetchall()}

    @staticmethod
    def _config_field_exists(field_name: str, available_columns: set[str]) -> bool:
        field = TestConfigTable._meta.fields[field_name]
        return field.column_name in available_columns

    def _resolve_monitor_channel_index(
        self,
        value,
        *,
        selected_channels: list,
        managed_channel_count: int | None,
    ) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        for channel_index in selected_channels:
            if (
                isinstance(channel_index, int)
                and not isinstance(channel_index, bool)
                and channel_index >= 0
            ):
                return channel_index
        if managed_channel_count is not None:
            return 0
        return None

    def _normalize_monitor_gain(self, value) -> float:
        if value is None:
            return MONITOR_GAIN_DEFAULT_DB
        if self._is_numeric(value) and math.isfinite(float(value)):
            return float(value)
        return MONITOR_GAIN_DEFAULT_DB

    @staticmethod
    def _normalize_fft_x_axis_scale(value) -> str:
        text = str(value or "").strip().lower()
        return text if text in _VALID_FFT_X_AXIS_SCALES else "linear"

    @staticmethod
    def _normalize_weighting(value) -> str:
        text = str(value or "").strip().upper()
        return text if text in _VALID_WEIGHTING else "Z"

    def _normalized_stft_color_scale_fields(self, channel: dict) -> dict:
        enabled = channel.get(
            "stft_color_scale_enabled",
            _CHANNEL_DEFAULTS["stft_color_scale_enabled"],
        )
        if enabled is True:
            return {
                "stft_color_scale_enabled": True,
                "stft_color_scale_min_db": channel.get(
                    "stft_color_scale_min_db"
                ),
                "stft_color_scale_max_db": channel.get(
                    "stft_color_scale_max_db"
                ),
            }
        if enabled is not False:
            return {
                "stft_color_scale_enabled": enabled,
                "stft_color_scale_min_db": channel.get(
                    "stft_color_scale_min_db"
                ),
                "stft_color_scale_max_db": channel.get(
                    "stft_color_scale_max_db"
                ),
            }
        return {
            "stft_color_scale_enabled": False,
            "stft_color_scale_min_db": self._finite_float_or_default(
                channel.get("stft_color_scale_min_db"),
                _CHANNEL_DEFAULTS["stft_color_scale_min_db"],
            ),
            "stft_color_scale_max_db": self._finite_float_or_default(
                channel.get("stft_color_scale_max_db"),
                _CHANNEL_DEFAULTS["stft_color_scale_max_db"],
            ),
        }

    def _normalize_stft_color_scale_storage(self, channel: dict) -> None:
        normalized = self._normalized_stft_color_scale_fields(channel)
        if normalized["stft_color_scale_enabled"]:
            for field in (
                "stft_color_scale_min_db",
                "stft_color_scale_max_db",
            ):
                value = normalized[field]
                if self._is_finite_number(value):
                    normalized[field] = float(value)
        channel.update(normalized)

    @staticmethod
    def _normalize_stft_score_summary_mode(value) -> str:
        if isinstance(value, str) and value in _VALID_STFT_SCORE_SUMMARY_MODES:
            return value
        return _CHANNEL_DEFAULTS["stft_score_summary_mode"]

    def _normalized_stft_score_fields(self, channel: dict) -> dict:
        return {
            field: channel[field] if field in channel else _CHANNEL_DEFAULTS[field]
            for field in (
                "stft_score_enabled",
                "stft_score_summary_mode",
                "stft_score_percentile",
                "stft_score_loudness_reference_sone",
                "stft_score_modulation_reference",
                "stft_score_harmonic_ratio_reference",
                "stft_score_frequency_instability_reference",
                "stft_score_harmonic_start_order",
                "stft_score_harmonic_end_order",
            )
        }

    def _normalize_stft_score_storage(self, channel: dict) -> None:
        normalized = self._normalized_stft_score_fields(channel)
        normalized["stft_score_enabled"] = (
            normalized["stft_score_enabled"] is True
        )
        normalized["stft_score_summary_mode"] = (
            self._normalize_stft_score_summary_mode(
                normalized["stft_score_summary_mode"]
            )
        )
        for field in (
            "stft_score_percentile",
            "stft_score_loudness_reference_sone",
            "stft_score_modulation_reference",
            "stft_score_harmonic_ratio_reference",
            "stft_score_frequency_instability_reference",
        ):
            normalized[field] = self._finite_float_or_default(
                normalized[field],
                _CHANNEL_DEFAULTS[field],
            )
        for field in (
            "stft_score_harmonic_start_order",
            "stft_score_harmonic_end_order",
        ):
            normalized[field] = self._int_or_default(
                normalized[field],
                _CHANNEL_DEFAULTS[field],
            )
        channel.update(normalized)

    def _finite_float_or_default(self, value, default: float) -> float:
        value = self._finite_float_or_none(value)
        if value is None:
            return float(default)
        return value

    def _finite_float_or_none(self, value) -> float | None:
        if self._is_finite_number(value):
            return float(value)
        return None

    @staticmethod
    def _int_or_default(value, default: int) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return int(value)
        return int(default)

    def _replace_channel_rows(self, *, config_id: int, channels: list[dict]) -> None:
        ChannelConfigTable.delete().where(ChannelConfigTable.config == config_id).execute()
        for channel in channels:
            ChannelConfigTable.create(
                config=config_id,
                **self._channel_db_values(channel),
            )

    def _post_commit_cleanup(
        self,
        *,
        old_paths: list[str],
        staged_paths: list[str],
    ) -> None:
        managed_old_paths = [
            path for path in old_paths if self._golden_samples.is_managed_path(path)
        ]
        for label, paths, cleaner in (
            ("旧黄金样本", managed_old_paths, self._golden_samples.cleanup_paths),
            ("黄金样本暂存文件", staged_paths, self._golden_samples.discard_staged_samples),
        ):
            if not paths:
                continue
            try:
                cleaner(paths)
            except Exception as exc:
                logger.warning("ConfigService post-commit %s cleanup failed: %s", label, exc)

    def _safe_rollback(self, promoted_paths: list[str]) -> None:
        if not promoted_paths:
            return
        try:
            self._golden_samples.rollback_promoted_samples(promoted_paths)
        except Exception as exc:
            logger.warning("ConfigService promoted golden rollback failed: %s", exc)

    @staticmethod
    def _channel_has_binding(channel: dict) -> bool:
        for key in _GOLDEN_BINDING_FIELDS:
            value = channel.get(key)
            if isinstance(value, str):
                if value.strip():
                    return True
            elif value is not None:
                return True
        return False

    @staticmethod
    def _clear_golden_binding(channel: dict) -> None:
        for key in _GOLDEN_BINDING_FIELDS:
            channel[key] = None
        channel["fft_golden_mode"] = "off"
        channel["fft_show_golden_overlay"] = False
        channel["oct_golden_mode"] = "off"
        channel["oct_show_golden_overlay"] = False

    def _normalize_channel_limit_storage(self, channel: dict) -> None:
        for field in _CHANNEL_LIMIT_ENABLED_FIELDS:
            channel[field] = channel.get(field, _CHANNEL_DEFAULTS[field]) is True
        for field in _CHANNEL_LIMIT_VALUE_FIELDS:
            value = channel.get(field, _CHANNEL_DEFAULTS[field])
            if self._is_numeric(value):
                channel[field] = float(value)
            else:
                channel[field] = float(_CHANNEL_DEFAULTS[field])
        for label, key in [("FFT", "fft_band_limits"), ("倍频程", "oct_band_limits")]:
            raw_limits = channel.get(key, [])
            if validate_limit_tables(raw_limits, label=label):
                channel[key] = deepcopy(raw_limits)
            else:
                channel[key] = normalize_limit_tables_for_storage(
                    raw_limits,
                    label=label,
                )

    @staticmethod
    def _parse_json(text: str, default):
        try:
            result = json.loads(text) if text else default
            if not isinstance(result, type(default)):
                return default
            return result
        except (json.JSONDecodeError, TypeError):
            return default

    @staticmethod
    def _normalize_optional_text(value):
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text or None

    @staticmethod
    def _is_numeric(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @staticmethod
    def _is_finite_number(value) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    def _is_positive_finite(self, value) -> bool:
        return self._is_finite_number(value) and float(value) > 0.0
