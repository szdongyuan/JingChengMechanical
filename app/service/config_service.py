from __future__ import annotations

import json

from peewee import IntegrityError

from model.base_mode import db
from model.test_config_table import TestConfigTable
from model.channel_config_table import ChannelConfigTable
from model.device_table import DeviceTable
from consts.time_consts import now_str

# Default values for new channel configs
_CHANNEL_DEFAULTS = {
    "stft_nfft": 1024,
    "stft_overlap": 0.5,
    "spl_weighting": "A",
    "spl_time_constant": 0.125,
    "spl_lower_limit": 40.0,
    "spl_upper_limit": 85.0,
    "fft_band_limits": "[]",
    "oct_weighting": "A",
    "oct_band_limits": "[]",
    "ai_config": "{}",
}

_VALID_NFFT = [256, 512, 1024, 2048, 4096, 8192]
_VALID_OVERLAP = [0.25, 0.5, 0.75]
_VALID_WEIGHTING = ["A", "C", "Z"]


class ConfigService:
    def __init__(self, bus):
        self.bus = bus

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
            "scan_triggers_test": bool(cfg.scan_triggers_test),
            "trigger_type": cfg.trigger_type,
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
                    "stft_overlap": ch.stft_overlap,
                    "spl_weighting": ch.spl_weighting,
                    "spl_time_constant": ch.spl_time_constant,
                    "spl_upper_limit": ch.spl_upper_limit,
                    "spl_lower_limit": ch.spl_lower_limit,
                    "fft_band_limits": self._parse_json(ch.fft_band_limits, []),
                    "oct_weighting": ch.oct_weighting,
                    "oct_band_limits": self._parse_json(ch.oct_band_limits, []),
                    "ai_config": self._parse_json(ch.ai_config, {}),
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
            with db.atomic():
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
        errors = self.validate(data)
        if errors:
            raise ValueError("\n".join(errors))

        config_id = data["config_id"]
        if not TestConfigTable.select().where(TestConfigTable.config_id == config_id).exists():
            raise ValueError("配置不存在或已被删除")
        channels = data.get("channels", [])

        try:
            with db.atomic():
                TestConfigTable.update(
                    config_name=data["config_name"].strip(),
                    record_duration=data["record_duration"],
                    analysis_duration=data["analysis_duration"],
                    scan_triggers_test=data["scan_triggers_test"],
                    trigger_type=data.get("trigger_type"),
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
                for ch in channels:
                    ChannelConfigTable.create(
                        config=config_id,
                        channel_index=ch["channel_index"],
                        stft_nfft=ch["stft_nfft"],
                        stft_overlap=ch["stft_overlap"],
                        spl_weighting=ch["spl_weighting"],
                        spl_time_constant=ch["spl_time_constant"],
                        spl_upper_limit=ch["spl_upper_limit"],
                        spl_lower_limit=ch["spl_lower_limit"],
                        fft_band_limits=json.dumps(ch["fft_band_limits"]),
                        oct_weighting=ch["oct_weighting"],
                        oct_band_limits=json.dumps(ch["oct_band_limits"]),
                        ai_config=json.dumps(ch.get("ai_config", {})),
                    )
        except IntegrityError:
            raise ValueError(f"配置名称「{data['config_name']}」已存在")

        self.bus.config_changed.emit()
        return config_id

    def delete_config(self, config_id: int) -> bool:
        with db.atomic():
            count = (
                TestConfigTable.delete()
                .where(TestConfigTable.config_id == config_id)
                .execute()
            )
        if count > 0:
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
        if not isinstance(rd, (int, float)) or rd <= 0:
            errors.append("录音时长必须大于 0")
        if not isinstance(ad, (int, float)) or ad <= 0:
            errors.append("分析时长必须大于 0")
        elif isinstance(rd, (int, float)) and rd > 0 and ad > rd:
            errors.append("分析时长不能超过录音时长")

        if not isinstance(data.get("scan_triggers_test"), bool):
            errors.append("扫码触发必须为布尔值")

        input_dev = data.get("input_device_name")
        sel_ch = data.get("selected_channels", [])
        if input_dev and not sel_ch:
            errors.append("已选择输入设备但未选择任何通道")

        channels = data.get("channels", [])
        if not isinstance(channels, list):
            errors.append("通道数据格式无效")
            return errors

        seen_idx = set()
        for i, ch in enumerate(channels):
            if not isinstance(ch, dict):
                errors.append(f"通道 {i + 1}: 数据格式无效")
                continue
            idx = ch.get("channel_index")
            if not isinstance(idx, int):
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
            if ch.get("oct_weighting") not in _VALID_WEIGHTING:
                errors.append(f"{prefix}: 倍频程计权模式无效")

            for label, key in [("FFT", "fft_band_limits"), ("倍频程", "oct_band_limits")]:
                bands = ch.get(key, [])
                if not isinstance(bands, list):
                    errors.append(f"{prefix}: {label} 频带数据格式无效")
                    continue
                for j, b in enumerate(bands):
                    if not isinstance(b, dict):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行格式无效")
                        continue
                    freq = b.get("freq", 0)
                    lo = b.get("lower", 0)
                    hi = b.get("upper", 0)
                    if not isinstance(freq, (int, float)) or isinstance(freq, bool) or freq <= 0:
                        errors.append(f"{prefix}: {label} 第 {j+1} 行频率必须为正数")
                    if not isinstance(lo, (int, float)) or isinstance(lo, bool):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行下限必须为数值")
                    elif not isinstance(hi, (int, float)) or isinstance(hi, bool):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行上限必须为数值")
                    elif lo >= hi:
                        errors.append(
                            f"{prefix}: {label} 第 {j+1} 行下限必须小于上限"
                        )
        return errors

    @staticmethod
    def _parse_json(text: str, default):
        try:
            result = json.loads(text) if text else default
            if not isinstance(result, type(default)):
                return default
            return result
        except (json.JSONDecodeError, TypeError):
            return default
