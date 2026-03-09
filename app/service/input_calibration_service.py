from __future__ import annotations


class InputCalibrationService:
    """
    输入校准服务（占位骨架）。

    约定：
    - UI 只负责展示/交互
    - 校准计算、参数读写、通道映射等逻辑逐步放到这里
    """

    def __init__(self, bus=None):
        self.bus = bus

    def load(self) -> dict:
        """读取校准配置。后续你接入 DB/JSON 后再实现。"""
        return {}

    def save(self, data: dict):
        """保存校准配置。后续你接入 DB/JSON 后再实现。"""
        _ = data

