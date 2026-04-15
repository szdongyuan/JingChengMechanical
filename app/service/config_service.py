from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal

from peewee import IntegrityError

from app.service.golden_sample_service import GoldenSampleService, StagedGoldenSample
from consts.time_consts import now_str
from model.channel_config_table import ChannelConfigTable
from model.device_table import DeviceTable
from model.plc_table import PlcTable
from model.test_config_table import TestConfigTable

logger = logging.getLogger(__name__)

# Default values for new channel configs
_CHANNEL_DEFAULTS = {
    "stft_nfft": 1024,
    "fft_nfft": 4096,
    "stft_overlap": 0.5,
    "spl_weighting": "A",
    "spl_time_constant": 0.125,
    "spl_lower_limit": 40.0,
    "spl_upper_limit": 85.0,
    "loudness_unit": "sone",
    "loudness_lower_limit": 0.0,
    "loudness_upper_limit": 60.0,
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


@dataclass(frozen=True)
class PendingConfigSave:
    config_id: int
    promoted_paths: tuple[str, ...] = ()
    old_paths: tuple[str, ...] = ()
    staged_paths: tuple[str, ...] = ()
    golden_bindings_by_channel: tuple[tuple[int, dict], ...] = ()


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
        cfg = TestConfigTable.get_by_id(config_id)
        result = {
            "config_id": cfg.config_id,
            "config_name": cfg.config_name,
            "record_duration": cfg.record_duration,
            "analysis_duration": cfg.analysis_duration,
            "analysis_window_offset_ms": cfg.analysis_window_offset_ms,
            "allow_incomplete_recording_analysis": bool(
                getattr(cfg, "allow_incomplete_recording_analysis", False)
            ),
            "scan_triggers_test": bool(cfg.scan_triggers_test),
            "plc_id": cfg.plc_id,
            "input_device_name": cfg.input_device_name,
            "input_device_hostapi": cfg.input_device_hostapi,
            "input_device_samplerate": cfg.input_device_samplerate,
            "output_device_name": cfg.output_device_name,
            "output_device_hostapi": cfg.output_device_hostapi,
            "output_device_samplerate": cfg.output_device_samplerate,
            "selected_channels": self._parse_json(cfg.selected_channels, []),
            "channels": [],
        }
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
                    "spl_weighting": ch.spl_weighting,
                    "spl_time_constant": ch.spl_time_constant,
                    "spl_upper_limit": ch.spl_upper_limit,
                    "spl_lower_limit": ch.spl_lower_limit,
                    "loudness_unit": ch.loudness_unit
                    or _CHANNEL_DEFAULTS["loudness_unit"],
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
        return result

    def create_config(self, config_name: str) -> int:
        name = config_name.strip()
        if not name:
            raise ValueError("配置名称不能为空")
        if TestConfigTable.select().where(TestConfigTable.config_name == name).exists():
            raise ValueError(f"配置名称「{name}」已存在")
        try:
            with TestConfigTable._meta.database.atomic():
                cfg = TestConfigTable.create(
                    config_name=name,
                    created_time=now_str(),
                    updated_time=now_str(),
                )
        except IntegrityError:
            raise ValueError(f"配置名称「{name}」已存在")
        self.bus.config_changed.emit()
        return cfg.config_id

    def save_config(self, data: dict) -> int:
        pending = self.begin_save_config(data)
        self.commit_pending_save(pending)
        return pending.config_id

    def _raise_save_integrity_error(self, exc: IntegrityError, data: dict) -> None:
        error_text = str(exc).lower()
        if (
            data.get("plc_id") is not None
            and "foreign key constraint failed" in error_text
        ):
            raise ValueError("PLC 不存在或已被删除") from exc
        raise ValueError(f"配置名称「{data['config_name']}」已存在") from exc

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

        analysis_window_offset_ms = int(data.get("analysis_window_offset_ms", 0))
        try:
            with TestConfigTable._meta.database.atomic():
                TestConfigTable.update(
                    config_name=data["config_name"].strip(),
                    record_duration=data["record_duration"],
                    analysis_duration=data["analysis_duration"],
                    analysis_window_offset_ms=analysis_window_offset_ms,
                    allow_incomplete_recording_analysis=bool(
                        data.get("allow_incomplete_recording_analysis", False)
                    ),
                    scan_triggers_test=data["scan_triggers_test"],
                    plc=data.get("plc_id"),
                    input_device_name=data.get("input_device_name"),
                    input_device_hostapi=data.get("input_device_hostapi"),
                    input_device_samplerate=data.get("input_device_samplerate"),
                    output_device_name=data.get("output_device_name"),
                    output_device_hostapi=data.get("output_device_hostapi"),
                    output_device_samplerate=data.get("output_device_samplerate"),
                    selected_channels=json.dumps(data.get("selected_channels", [])),
                    updated_time=now_str(),
                ).where(TestConfigTable.config_id == config_id).execute()
                ChannelConfigTable.delete().where(
                    ChannelConfigTable.config == config_id
                ).execute()
                for channel in channels:
                    ChannelConfigTable.create(
                        config=config_id,
                        **self._channel_db_values(channel),
                    )
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

    def get_channel_count(self) -> int:
        try:
            row = DeviceTable.get_by_id(1)
            return int(row.input_channel_count)
        except Exception:
            return 0

    def validate(self, data: dict) -> list[str]:
        errors = []
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

        errors.extend(self.validate_factory_runtime(data))

        input_dev = data.get("input_device_name")
        sel_ch = data.get("selected_channels", [])
        if input_dev and not sel_ch:
            errors.append("已选择输入设备但未选择任何通道")

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
            if ch.get("stft_overlap") not in _VALID_OVERLAP:
                errors.append(f"{prefix}: STFT 重叠率无效")
            if ch.get("spl_weighting") not in _VALID_WEIGHTING:
                errors.append(f"{prefix}: SPL 计权模式无效")
            tc = ch.get("spl_time_constant", 0)
            if not isinstance(tc, (int, float)) or isinstance(tc, bool) or tc <= 0:
                errors.append(f"{prefix}: SPL 时间常数必须大于 0")
            sl = ch.get("spl_lower_limit", 0)
            su = ch.get("spl_upper_limit", 0)
            if not isinstance(sl, (int, float)) or isinstance(sl, bool):
                errors.append(f"{prefix}: SPL 下限必须为数值")
            elif not isinstance(su, (int, float)) or isinstance(su, bool):
                errors.append(f"{prefix}: SPL 上限必须为数值")
            elif sl >= su:
                errors.append(f"{prefix}: SPL 下限必须小于上限")
            loudness_unit = ch.get(
                "loudness_unit", _CHANNEL_DEFAULTS["loudness_unit"]
            )
            if loudness_unit not in _VALID_LOUDNESS_UNITS:
                errors.append(f"{prefix}: 响度单位无效")
            loudness_lower = ch.get(
                "loudness_lower_limit", _CHANNEL_DEFAULTS["loudness_lower_limit"]
            )
            loudness_upper = ch.get(
                "loudness_upper_limit", _CHANNEL_DEFAULTS["loudness_upper_limit"]
            )
            if not self._is_numeric(loudness_lower):
                errors.append(f"{prefix}: 响度下限必须为数值")
            elif loudness_lower < 0:
                errors.append(f"{prefix}: 响度下限必须大于等于 0")
            elif not self._is_numeric(loudness_upper):
                errors.append(f"{prefix}: 响度上限必须为数值")
            elif loudness_upper < 0:
                errors.append(f"{prefix}: 响度上限必须大于等于 0")
            elif loudness_lower >= loudness_upper:
                errors.append(f"{prefix}: 响度下限必须小于上限")
            if ch.get("oct_weighting") not in _VALID_WEIGHTING:
                errors.append(f"{prefix}: 倍频程计权模式无效")

            for label, key in [("FFT", "fft_band_limits"), ("倍频程", "oct_band_limits")]:
                bands = ch.get(key, [])
                if not isinstance(bands, list):
                    errors.append(f"{prefix}: {label} 频带数据格式无效")
                    continue
                for j, band in enumerate(bands):
                    if not isinstance(band, dict):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行格式无效")
                        continue
                    freq = band.get("freq", 0)
                    lo = band.get("lower", 0)
                    hi = band.get("upper", 0)
                    if not isinstance(freq, (int, float)) or isinstance(freq, bool) or freq <= 0:
                        errors.append(f"{prefix}: {label} 第 {j+1} 行频率必须为正数")
                    if not isinstance(lo, (int, float)) or isinstance(lo, bool):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行下限必须为数值")
                    elif not isinstance(hi, (int, float)) or isinstance(hi, bool):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行上限必须为数值")
                    elif lo >= hi:
                        errors.append(f"{prefix}: {label} 第 {j+1} 行下限必须小于上限")

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

    def validate_factory_runtime(self, data: dict) -> list[str]:
        errors = []
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

        return errors

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

    def _channel_db_values(self, channel: dict) -> dict:
        return {
            "channel_index": channel["channel_index"],
            "stft_nfft": channel["stft_nfft"],
            "fft_nfft": channel.get("fft_nfft", _CHANNEL_DEFAULTS["fft_nfft"]),
            "stft_overlap": channel["stft_overlap"],
            "spl_weighting": channel["spl_weighting"],
            "spl_time_constant": channel["spl_time_constant"],
            "spl_upper_limit": channel["spl_upper_limit"],
            "spl_lower_limit": channel["spl_lower_limit"],
            "loudness_unit": channel.get(
                "loudness_unit", _CHANNEL_DEFAULTS["loudness_unit"]
            ),
            "loudness_lower_limit": channel.get(
                "loudness_lower_limit",
                _CHANNEL_DEFAULTS["loudness_lower_limit"],
            ),
            "loudness_upper_limit": channel.get(
                "loudness_upper_limit",
                _CHANNEL_DEFAULTS["loudness_upper_limit"],
            ),
            "fft_band_limits": json.dumps(channel["fft_band_limits"]),
            "oct_weighting": channel["oct_weighting"],
            "oct_band_limits": json.dumps(channel["oct_band_limits"]),
            "ai_config": json.dumps(channel.get("ai_config", {})),
            "golden_sample_path": self._normalize_optional_text(
                channel.get("golden_sample_path")
            ),
            "golden_sample_label": self._normalize_optional_text(
                channel.get("golden_sample_label")
            ),
            "golden_sample_source_channel": channel.get("golden_sample_source_channel"),
            "golden_sample_start_sample": channel.get("golden_sample_start_sample"),
            "golden_sample_num_samples": channel.get("golden_sample_num_samples"),
            "golden_sample_start_ms": channel.get("golden_sample_start_ms"),
            "golden_sample_duration_ms": channel.get("golden_sample_duration_ms"),
            "golden_sample_sample_rate": channel.get("golden_sample_sample_rate"),
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
