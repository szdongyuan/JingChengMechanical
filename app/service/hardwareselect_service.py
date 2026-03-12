from __future__ import annotations
import os

# 必须在 import sounddevice 之前设置（开启 ASIO 枚举）
os.environ.setdefault("SD_ENABLE_ASIO", "1")
import sounddevice as sd

from model.device_table import DeviceTable


class HardwareSelectService:
    """
    设备服务：
    - 刷新/枚举 sounddevice 设备与 hostapi（驱动后端）
    - 读取/保存已选输入/输出设备到 DB（device_table.id=1）
    """

    # -------- sounddevice：刷新/查询 --------
    @staticmethod
    def refresh_available_device():
        """强制重置 PortAudio（设备热插拔后常用）。"""
        sd._terminate()
        sd._initialize()


    @staticmethod
    def query_devices():
        """返回设备列表（list[dict]）。"""
        return list(sd.query_devices())

    @staticmethod
    def query_hostapis():
        """返回 hostapi 列表（list[dict]）。"""
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
        sd.default.device = (in_id, out_id)

    # -------- 按 hostapi 分组（参考工程）--------
    def get_device_info_grouped(self, *, refresh: bool = False) -> dict:
        """
        返回：
        {
          "WASAPI": {"input":[...], "output":[...]},
          "ASIO":   {"input":[...], "output":[...]},
          ...
        }
        """
        devices, hostapis = self.query_devices_and_hostapis(refresh=refresh)
        host_dict: dict[str, dict[str, list[dict]]] = {}

        for api in hostapis:
            api_name = api.get("name", "N/A")
            api_input: list[dict] = []
            api_output: list[dict] = []

            for device_id in api.get("devices") or []:
                try:
                    device = devices[device_id]
                except Exception:
                    continue
                if device.get("max_input_channels", 0) > 0:
                    api_input.append(device)
                if device.get("max_output_channels", 0) > 0:
                    api_output.append(device)

            host_dict[api_name] = {"input": api_input, "output": api_output}

        return host_dict

    # -------- DB：读取/保存选择 --------
    @staticmethod
    def load_io_devices() -> dict:
        """读取输入/输出设备信息（device_table.id=1）。"""
        row = DeviceTable.select().where(DeviceTable.id == 1).first()
        if not row:
            return {}
        return {
            "input": {
                "id": row.input_selected_device_id,
                "name": row.input_selected_device_name,
                "type": row.input_selected_device_type,
                "samplerate": row.input_selected_device_samplerate,
                "channel_count": row.input_channel_count,
            },
            "output": {
                "id": row.output_selected_device_id,
                "name": row.output_selected_device_name,
                "type": row.output_selected_device_type,
                "samplerate": row.output_selected_device_samplerate,
                "channel_count": row.output_channel_count,
            },
        }

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

    def save_io_devices(
        self,
        input_device_id: int,
        output_device_id: int,
        *,
        devices: list[dict] | None = None,
        hostapis: list[dict] | None = None,
        sync_sounddevice_default: bool = True,
    ) -> tuple[dict, dict]:
        """
        保存输入/输出设备到 DB，并可选同步 sd.default.device。
        返回 (in_dev, out_dev) 两个 dict（已整理为 DB 结构）。
        """
        if devices is None or hostapis is None:
            devices, hostapis = self.query_devices_and_hostapis(refresh=False)

        in_dev = self._device_to_saved(input_device_id, devices, hostapis, kind="input")
        out_dev = self._device_to_saved(output_device_id, devices, hostapis, kind="output")

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
            )
            .where(DeviceTable.id == 1)
            .execute()
        )
        if updated == 0:
            raise RuntimeError("device_table 中未找到 id=1 的配置行")

        if sync_sounddevice_default:
            try:
                self.set_default_device(int(input_device_id), int(output_device_id))
            except Exception:
                pass

        return in_dev, out_dev

