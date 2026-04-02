from __future__ import annotations

import json
import os

# 必须在 import sounddevice 之前设置（开启 ASIO 枚举）
os.environ.setdefault("SD_ENABLE_ASIO", "1")
import sounddevice as sd

from app.service.log_manager import LogManager
from model.device_table import DeviceTable


class HardwareSelectService:
    """
    设备服务：
    - 刷新/枚举 sounddevice 设备与 hostapi（驱动后端）
    - 读取/保存已选输入/监听设备到 DB（device_table.id=1）
    """
    logger = LogManager.set_log_handler("硬件选择")

    # -------- sounddevice：刷新/查询 --------
    @staticmethod
    def refresh_available_device():
        """强制重置 PortAudio（设备热插拔后常用）。"""
        sd._terminate()
        sd._initialize()


    @staticmethod
    def query_devices():
        """返回设备列表（list[dict]）。
        [
            {'name': 'xxx', 'index': 0, 'hostapi': 0, 'max_input_channels': 2, 'max_output_channels': 0, 。。'default_samplerate': 44100.0},
            {'name': 'xxx', 'index': 1, 'hostapi': 0, 'max_input_channels': 2, 'max_output_channels': 0, 。。'default_samplerate': 44100.0},
            {'name': 'xxx', 'index': 2, 'hostapi': 0, 'max_input_channels': 2, 'max_output_channels': 0, 。。'default_samplerate': 44100.0},
            {'name': 'xxx', 'index': 3, 'hostapi': 0, 'max_input_channels': 2, 'max_output_channels': 0, 。。'default_samplerate': 44100.0},
            {'name': 'xxx', 'index': 4, 'hostapi': 0, 'max_input_channels': 2, 'max_output_channels': 0, 。。'default_samplerate': 44100.0},
        ]
        """
        return list(sd.query_devices())

    @staticmethod
    def query_hostapis():
        """返回 hostapi 列表（list[dict]）。
            [
                {'name': 'MME', 'devices': [0, 1, 2, 3, 4, 5, 6], ...}
                {'name': 'Windows DirectSound', 'devices': [7, 8, 9, 10, 11, 12, 13], ...}
                {'name': 'Windows WASAPI', 'devices': [14, 15, 16, 17, 18], ...}
                {'name': 'Windows WDM-KS', 'devices': [19, 20, 21, 22, 23], ...}
            ]
        """
        return list(sd.query_hostapis())

    def query_devices_and_hostapis(self, *, refresh: bool = False) -> tuple[list[dict], list[dict]]:
        if refresh:
            self.refresh_available_device()
        return self.query_devices(), self.query_hostapis()

    @staticmethod
    def get_default_device_ids() -> tuple[int | None, int | None]:
        """返回 (default_input_id, default_output_id)。"""
        try:
            in_id, out_id = sd.default.device
            return in_id, out_id
        except Exception:
            return None, None

    @staticmethod
    def set_default_device(in_id: int, out_id: int):
        """
        设置默认设备
        """
        sd.default.device = (in_id, out_id)


    # -------- DB：读取/保存选择 --------
    @staticmethod
    def _saved_device_payload(
        *,
        device_id,
        name,
        device_type,
        samplerate,
        channel_count,
    ) -> dict | None:
        if any(value in (None, "") for value in (name, device_type, samplerate, channel_count)):
            return None
        return {
            "id": device_id,
            "name": name,
            "type": device_type,
            "samplerate": samplerate,
            "channel_count": channel_count,
        }

    @staticmethod
    def load_io_devices() -> dict:
        """读取输入/监听设备信息（device_table.id=1），空白单例行视为未保存设备。"""
        row = DeviceTable.select().where(DeviceTable.id == 1).first()
        if not row:
            return {}
        input_payload = HardwareSelectService._saved_device_payload(
            device_id=row.input_selected_device_id,
            name=row.input_selected_device_name,
            device_type=row.input_selected_device_type,
            samplerate=row.input_selected_device_samplerate,
            channel_count=row.input_channel_count,
        )
        output_payload = HardwareSelectService._saved_device_payload(
            device_id=row.output_selected_device_id,
            name=row.output_selected_device_name,
            device_type=row.output_selected_device_type,
            samplerate=row.output_selected_device_samplerate,
            channel_count=row.output_channel_count,
        )
        mic_cal = json.loads(row.mic_cal) if row.mic_cal else {}

        payload = {}
        if input_payload:
            payload["input"] = input_payload
        if output_payload:
            payload["output"] = output_payload
        if mic_cal:
            payload["mic_cal"] = mic_cal
        return payload

    @staticmethod
    def save_mic_cal(mic_cal_dict):
        """保存通道配置到 DB（device_table.id=1 的 mic_cal）。"""
        mic_cal_str = json.dumps(mic_cal_dict, ensure_ascii=False, indent=4)
        updated = (
            DeviceTable.update(mic_cal=mic_cal_str)
            .where(DeviceTable.id == 1)
            .execute()
        )
        if updated > 0:
            HardwareSelectService.logger.info(f"保存通道配置成功: {mic_cal_dict}")
        return updated > 0

    @staticmethod
    def _hostapi_name(hostapis: list[dict], hostapi_index: int) -> str:
        try:
            return hostapis[hostapi_index]["name"]
        except Exception:
            return "N/A"

    def _device_to_saved(
        self,
        dev_index: int,
        devices: list[dict],
        hostapis: list[dict],
        *,
        kind: str,
    ) -> dict:
        """
        把 sounddevice 的“设备信息 dict”整理成“可存入 DB 的 dict”。

        参数说明：
        - dev_index: 设备 id（就是 sd.query_devices() 里每个设备的 device["index"]）
        - devices: sd.query_devices() 返回的设备列表
        - hostapis: sd.query_hostapis() 返回的 hostapi 列表（用于把 hostapi 索引翻译为名称）
        - kind: "input" 或 "output"（决定 channel_count 取输入通道还是输出通道）

        返回示例：
        {
          "id": 12,
          "name": "UMC ASIO Driver",
          "type": "ASIO",
          "samplerate": "44100",
          "channel_count": 2
        }
        """
        device = next((d for d in devices if d.get("index") == dev_index), None)
        if not device:
            # 找不到设备时，返回空壳结构，避免上层保存时崩溃
            return {
                "id": dev_index,
                "name": "",
                "type": "",
                "samplerate": "",
                "channel_count": 0,
            }

        sr = device.get("default_samplerate", "")
        if isinstance(sr, float):
            sr = str(int(sr))

        if kind == "input":
            ch = int(device.get("max_input_channels", 0) or 0)
        else:
            ch = int(device.get("max_output_channels", 0) or 0)

        return {
            # id：用于保存/恢复选择（下拉框 itemData 也用这个）
            "id": dev_index,
            # name：设备名称（用于 UI 展示、也保存到 DB 方便查看）
            "name": device.get("name", ""),
            # type：驱动后端名称（MME/WASAPI/ASIO/...）
            "type": self._hostapi_name(hostapis, int(device.get("hostapi", 0) or 0)),
            # samplerate：默认采样率（字符串化，便于直接展示）
            "samplerate": f"{sr}" if sr else "N/A",
            # channel_count：输入/输出通道数（由 kind 决定）
            "channel_count": ch,
        }

    def save_device_info(
        self,
        input_device_id: int,
        output_device_id: int,
        *,
        devices: list[dict] | None = None,
        hostapis: list[dict] | None = None,
        sync_sounddevice_default: bool = True,
    ) -> tuple[dict, dict]:
        """
        保存输入/监听设备到 DB，并可选同步 sd.default.device。
        返回 (in_dev, out_dev) 两个 dict（已整理为 DB 结构）。
        """
        if devices is None or hostapis is None:
            devices, hostapis = self.query_devices_and_hostapis(refresh=False)

        in_dev = self._device_to_saved(input_device_id, devices, hostapis, kind="input")
        out_dev = self._device_to_saved(output_device_id, devices, hostapis, kind="output")
        mic_cal_dict = {
            f"In{i}": {"deviation_value": "0", "binding_index": i - 1, "is_selected": 1}
            for i in range(1, int(in_dev.get("channel_count") or 1) + 1)
        }
        mic_cal_str = json.dumps(mic_cal_dict, ensure_ascii=False, indent=4)
        updated = (
            DeviceTable.update(
                input_selected_device_id=in_dev.get("id"),
                input_selected_device_name=in_dev.get("name"),
                input_selected_device_type=in_dev.get("type"),
                input_selected_device_samplerate=in_dev.get("samplerate"),
                input_channel_count=in_dev.get("channel_count"),
                output_selected_device_id=out_dev.get("id"),
                output_selected_device_name=out_dev.get("name"),
                output_selected_device_type=out_dev.get("type"),
                output_selected_device_samplerate=out_dev.get("samplerate"),
                output_channel_count=out_dev.get("channel_count"),
                mic_cal=mic_cal_str,
            )
            .where(DeviceTable.id == 1)
            .execute()
        )
        if updated == 0:
            raise RuntimeError("device_table 中未找到 id=1 的配置行")

        # 设置默认设备
        if sync_sounddevice_default:
            self.set_default_device(int(input_device_id), int(output_device_id))

        return in_dev, out_dev

    def restore_default_device_from_db(self, device_info, devices, hostapis):
        """
        根据数据库的设备来恢复当前设备
        """
        matched_in = self.find_matched_device(device_info.get("input"), "input", devices, hostapis)
        matched_out = self.find_matched_device(device_info.get("output"), "output", devices, hostapis)
        if matched_in is None or matched_out is None:
            return False
        in_id = int(matched_in["index"])
        out_id = int(matched_out["index"])
        self.set_default_device(in_id, out_id)
        return True

    @staticmethod
    def find_matched_device(dev_info, kind, devices, hostapis):
        if not dev_info:
            return None
        for dev in devices:
            hostapi_name = hostapis[dev["hostapi"]]["name"]
            samplerate = str(int(dev["default_samplerate"]))
            if kind == "input":
                channel_count = int(dev.get("max_input_channels", 0))
            else:
                channel_count = int(dev.get("max_output_channels", 0))
            if (
                    dev["name"] == dev_info["name"]
                    and hostapi_name == dev_info["type"]
                    and samplerate == dev_info["samplerate"]
                    and channel_count == int(dev_info.get("channel_count") or 0)
            ):
                return dev
        return None

