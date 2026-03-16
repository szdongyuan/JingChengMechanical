from __future__ import annotations

import json
import re

from model.device_table import DeviceTable


class InputCalibrationService:
    """
    输入校准服务：录音 -> SPL/偏差计算 -> 持久化（DB: device_table.mic_cal）。
    约定：
    - UI 只负责展示/交互
    - 录音/计算/读写配置放到 service
    """
    def __init__(self, bus=None):
        self.bus = bus

    def save_mic_cal_data(self, selected_channel, deviation_lineedit_text):
        # 只允许 int/float（可带正负号、小数点），允许可选的 "Pa/V" 后缀
        text = (deviation_lineedit_text or "").strip()
        if text.endswith("Pa/V"):
            text = text[:-4].strip()
        if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text):
            return False, "偏差值只能是数字（int/float）"
        deviation_match = text

        row = DeviceTable.select().where(DeviceTable.id == 1).first()
        if not row:
            return False, "device_table 中未找到 id=1 的配置行"

        mic_cal_dict = json.loads(row.mic_cal or "{}")
        if selected_channel not in mic_cal_dict:
            return False, f"mic_cal 中未找到 {selected_channel}"
        mic_cal_dict[selected_channel]["deviation_value"] = deviation_match

        mic_cal_str = json.dumps(mic_cal_dict, ensure_ascii=False, indent=4)
        updated = (
            DeviceTable.update(mic_cal=mic_cal_str)
            .where(DeviceTable.id == 1)
            .execute()
        )
        if updated == 0:
            return False, "保存失败：device_table.id=1 未更新"
        return True, deviation_match



