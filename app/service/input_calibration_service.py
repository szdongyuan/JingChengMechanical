from __future__ import annotations

import json
import os

# 必须在 import sounddevice 之前设置（开启 ASIO 枚举）
os.environ.setdefault("SD_ENABLE_ASIO", "1")
import sounddevice as sd  # type: ignore


from consts.running_consts import base_dir
from base.utils import get_config_content



class InputCalibrationService:
    """
    输入校准服务：录音 -> SPL/偏差计算 -> 持久化（JSON）。
    约定：
    - UI 只负责展示/交互
    - 录音/计算/读写配置放到 service
    """
    def __init__(self, bus=None):
        self.bus = bus

    # -------- mic calibration config (channel list + deviation) --------
    def load_mic_calibration(self) -> dict:
        ok, data = get_config_content("mic_calibration.json")
        if ok and isinstance(data, dict):
            return data

        # fallback：兼容当前仓库里已有的 config/mic_calibration.json
        # （有些运行方式下 resources/config 可能还没准备好）
        fallback_path = os.path.join(base_dir, "config", "mic_calibration.json")
        try:
            if os.path.exists(fallback_path):
                with open(fallback_path, "r", encoding="utf-8") as f:
                    data2 = json.load(f) or {}
                return data2 if isinstance(data2, dict) else {}
        except Exception:
            return {}

        return {}
