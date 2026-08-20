from __future__ import annotations

import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
import snap7
import snap7.util

from app.service.log_manager import LogManager


class PlcService(QObject):
    """
    用 python-snap7 轮询 PLC 开始信号。

    逻辑：
    1. 程序启动时先连接一次 PLC。
    2. 用 QTimer 周期读取 PLC 的开始位。
    3. 当该位从 0 变成 1 时，触发开始回调。
    """
    # 连接结果通知界面信号，包含“连接 PLC”的结果是True or False
    connect_result = pyqtSignal(bool, str)
    # 连接状态通知界面信号
    state_changed = pyqtSignal(str)
    read_time_ms = 60000
    limit_field = (
        "res_product_model_offset",
        "res_spl_limit_up_offset",
        "res_spl_limit_down_offset",
        "res_loudness_limit_up_offset",
        "res_loudness_limit_down_offset",
        "res_vibrate_limit_up_offset",
        "res_vibrate_limit_down_offset",
        "res_ai_score_limit_down_offset",
    )

    def __init__(self,
        bus,
        on_start_requested,
        ip: str,
        rack: int = 0,
        slot: int = 1,
        db_number: int = 1,
        poll_interval_ms = 200,
        reconnect_interval_ms = 3000,
        auto_reset_bit = True,
        field_offsets_dict = None,
        plc_id = None,
    ):
        # 初始化 PLC 服务，保存连接参数、轮询参数和运行时状态。
        super().__init__()
        self.bus = bus
        self.logger = LogManager.set_log_handler("core")
        self._on_start_requested = on_start_requested
        self._plc_id = plc_id
        self._field_offsets_dict = field_offsets_dict

        # PLC 连接参数(IP，机架号，槽号）
        self._ip = ip
        self._rack = rack
        self._slot = slot

        # 数据块号DB
        self._db_number = int(db_number)
        # 轮询周期
        self._poll_interval_ms = int(poll_interval_ms)
        # 重连周期
        self._reconnect_interval_ms = int(reconnect_interval_ms)

        # 触发开始后，是否把 PLC 里的开始位自动写回 0
        self._auto_reset_bit = bool(auto_reset_bit)

        # 与PLC 通信的客户端实例
        self.plc_client = None
        self.timer = None
        self.value_timer = None
        self._last_value = None
        self._reconnect_timer_ms = 0  # 重连计时器
        self._last_connect_error = ""

    @pyqtSlot()
    def start_plc_service(self):
        # 启动 PLC 监听；如果已经启动过，就直接返回，避免重复创建定时器。
        self.logger.info(f"启动 PLC{self._plc_id} 服务")
        if self.timer is not None:
            return
        self.state_changed.emit("连接中")
        # 启动定时器前 先尝试连一次 PLC；失败也启动定时器，后面继续自动重连。
        self._connect_once()

        # 创建轮询定时器，按固定周期读取 PLC 开始位。
        self.timer = QTimer(self)
        self.timer.setInterval(self._poll_interval_ms)
        self.timer.timeout.connect(self.poll_once)
        self.timer.start()

        if self.plc_client is None:
            self.state_changed.emit("重连中")
            return

        self.logger.info(f"PLC{self._plc_id}开始监听: ip={self._ip}, rack={self._rack}, slot={self._slot}, "
            f"DB{self._db_number}")
        self.bus.log.emit(
            f"PLC{self._plc_id}开始监听: ip={self._ip}, rack={self._rack}, slot={self._slot}, "
            f"DB{self._db_number}"
        )
        # self._start_value_timer()

    @pyqtSlot()
    def stop_plc_service(self):
        self.logger.info(f"停止 PLC 服务")
        # 停止监听，销毁定时器并主动断开 PLC 连接。
        if self.timer is not None:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        if self.value_timer is not None:
            self.value_timer.stop()
            self.value_timer.deleteLater()
            self.value_timer = None
        self._disconnect()
        self.state_changed.emit("未连接")
        self._last_value = None
        self.bus.log.emit(f"PLC{self._plc_id}停止监听")
        self.logger.info(f"PLC{self._plc_id}停止监听")

    def _connect_once(self):
        """
        连接一次plc
        """
        self.logger.info(f"开始尝试连接 PLC {self._plc_id}")
        # 尝试建立一次 PLC 连接；如果当前已经连上，就不重复连接。
        if self.plc_client is not None:
            self.logger.info("已有 PLC 客户端，不重复连接")
            return

        try:
            client = snap7.client.Client()
            client.connect(self._ip, self._rack, self._slot)
            self.logger.info(f"PLC{self._plc_id}连接参数: ip={self._ip}, rack={self._rack}, slot={self._slot}")
            self.logger.info(f"PLC{self._plc_id}地址参数: db_number数据块号={self._db_number}")
            self.bus.log.emit(f"PLC{self._plc_id}连接参数: ip={self._ip}, rack={self._rack}, slot={self._slot}")
            self.bus.log.emit(f"PLC{self._plc_id}地址参数: db_number数据块号={self._db_number}")
            self.plc_client = client
            self._reconnect_timer_ms = 0
            self._last_connect_error = ""
            self.state_changed.emit("已连接")
            self.logger.info(f"PLC{self._plc_id}连接成功: {self._ip}")
            self.bus.log.emit(f"PLC{self._plc_id}连接成功: {self._ip}")
            self.connect_result.emit(True, "")
        except Exception as exc:
            self.plc_client = None
            self._last_connect_error = str(exc)
            self.state_changed.emit("重连中")
            self.logger.error(f"PLC{self._plc_id}连接失败: {exc}")
            self.bus.log.emit(f"PLC{self._plc_id}连接失败: {exc}")

    def _disconnect(self):
        # 断开当前 PLC 连接，并清空 plc_client，供后续重连使用。
        if self.plc_client is None:
            return
        try:
            self.plc_client.disconnect()
        except Exception:
            pass
        self.plc_client = None
        if self.timer is not None:
            self.state_changed.emit("重连中")
        else:
            self.state_changed.emit("未连接")

    def _try_reconnect(self):
        self.logger.info(f"{self._plc_id}触发重新连接")
        # 按重连间隔尝试重新连接，避免每次轮询失败都立刻重连。
        if self.timer is None:
            return

        # 不要每次轮询都立刻重连，按配置的重连间隔节流。
        self._reconnect_timer_ms += self._poll_interval_ms
        if self._reconnect_timer_ms < self._reconnect_interval_ms:
            return
        self._reconnect_timer_ms = 0
        self._connect_once()

    def _read_start_bit(self) -> bool | None:
        # 读取 PLC 中配置的开始位；读失败时返回 None，并断开等待重连。
        if self.plc_client is None:
            return None

        try:
            byte_index, bit_index = self.parse_bit_offset(self._field_offsets_dict["start_signal_offset"])
            bytes = self.plc_client.db_read(self._db_number, byte_index, 1)
            # 取第几位
            data = snap7.util.get_bool(bytes, 0, bit_index)
            return bool(data)
        except Exception as exc:
            self._disconnect()
            self.bus.log.emit(f"PLC{self._plc_id}读取失败: {exc}")
            self.logger.error(f"PLC{self._plc_id}读取失败: {exc}")
            return None

    def write_plc_value(self, offset, type, value):
        if type == "string":
            byte_index = int(str(offset).split(".", 1)[0])
            data = bytearray(256)
            snap7.util.set_string(data, 0, str(value), 254)
            self.plc_client.db_write(self._db_number, byte_index, data)
            return True
        if type == "bool":
            byte_index, bit_index = self.parse_bit_offset(offset)
            data = bytearray(self.plc_client.db_read(self._db_number, byte_index, 1))
            snap7.util.set_bool(data, 0, bit_index, bool(value))
            self.plc_client.db_write(self._db_number, byte_index, data)
            return True
        if type == "real":
            byte_index = int(str(offset).split(".", 1)[0])
            data = bytearray(4)
            snap7.util.set_real(data, 0, float(value))
            self.plc_client.db_write(self._db_number, byte_index, data)
            return True
        return False

    def _write_testing_signal(self, testing):
        offset = self._field_offsets_dict.get("res_testing_signal_offset")
        if not offset:
            self.logger.warning(f"PLC {self._plc_id} 未找到偏移配置: res_testing_signal_offset")
            return
        try:
            self.write_plc_value(offset, "bool", testing)
        except Exception as exc:
            self.bus.log.emit(f"PLC {self._plc_id} 写入测试中信号失败: {exc}")
            self.logger.error(f"PLC 写入测试中信号失败: {exc}")

    def last_write_test_result_to_plc(self, plc_id, test_result):
        """
        最后按照plcid来写入对应的plc
        test_result = {
                "finished": True,
                "error_code": "0",
                "sn_code": "01010101",
                "result": True,
                "score": 86.5,
                "spl": 65.2,
                "loudness": 12.3,
                "vibrate": 0.8,
                "ai_score": 91.0,
                }
        """
        if plc_id != self._plc_id:
            return
        print(f"id:{plc_id},{self._plc_id}, result:{test_result}")
        if self.plc_client is None:
            return
        plc_values = [
            ("res_error_code_offset", "string", test_result.get("error_code")),
            ("res_sn_code_offset", "string", test_result.get("sn_code")),
            ("res_result_offset", "bool", test_result.get("test_result")),
            ("res_score_offset", "real", test_result.get("score")),
            ("res_spl_offset", "real", test_result.get("spl")),
            ("res_loudness_offset", "real", test_result.get("loudness")),
            ("res_vibrate_data_offset", "real", test_result.get("vibrate")),
            ("res_ai_score_offset", "real", test_result.get("ai_score")),
        ]
        try:
            for field_name, type, value in plc_values:
                offset = self._field_offsets_dict.get(field_name)
                if not offset:
                    self.logger.warning(f"PLC {self._plc_id} 未找到偏移配置: {field_name}")
                    continue
                self.write_plc_value(offset, type, value)
            # 最后写结束信号
            if test_result.get("finished", False):
                finish_offset = self._field_offsets_dict.get("res_finish_signal_offset")
                if finish_offset:
                    self.write_plc_value(finish_offset, "bool", True)
                self._write_testing_signal(False)
            self.bus.log.emit(f"PLC {self._plc_id} 写入测试结果完成")
        except Exception as exc:
            self.bus.log.emit(f"PLC {self._plc_id} 写入测试结果失败: {exc}")
            self.logger.error(f"PLC 写入测试结果失败: {exc}")

    def last_write_limit_to_plc(self, plc_id, limit_values):
        """
        按 plc_id 写入当前 PLC 的机种和限值。
        limit_values = {
            "res_product_model_offset": "xxx",
            "res_spl_limit_up_offset": 0.0,
            "res_spl_limit_down_offset": 0.0,
            "res_loudness_limit_up_offset": 0.0,
            "res_loudness_limit_down_offset": 0.0,
            "res_vibrate_limit_up_offset": 0.0,
            "res_vibrate_limit_down_offset": 0.0,
            "res_ai_score_limit_down_offset": 0.0,
        }
        """
        if plc_id != self._plc_id:
            return
        if self.plc_client is None:
            return

        field_types = {
            "res_product_model_offset": "string",
            "res_spl_limit_up_offset": "real",
            "res_spl_limit_down_offset": "real",
            "res_loudness_limit_up_offset": "real",
            "res_loudness_limit_down_offset": "real",
            "res_vibrate_limit_up_offset": "real",
            "res_vibrate_limit_down_offset": "real",
            "res_ai_score_limit_down_offset": "real",
        }

        try:
            for field_name in self.limit_field:
                if field_name not in limit_values:
                    continue
                offset = self._field_offsets_dict.get(field_name)
                if not offset:
                    self.logger.warning(f"PLC {self._plc_id} 未找到偏移配置: {field_name}")
                    continue
                self.write_plc_value(offset, field_types[field_name], limit_values.get(field_name))

            self.bus.log.emit(f"PLC {self._plc_id} 写入限值完成")
        except Exception as exc:
            self.bus.log.emit(f"PLC {self._plc_id} 写入限值失败: {exc}")
            self.logger.error(f"PLC 写入限值失败: {exc}")

    def read_offset_value(self, offset, type):
        if type == "string":
            byte_index = int(str(offset).split(".", 1)[0])
            data = self.plc_client.db_read(self._db_number, byte_index, 256)
            current_len = data[1]
            if current_len <= 0:
                return ""
            raw = bytes(data[2:2 + current_len])
            return raw.decode("ascii", errors="ignore").strip()

        if type == "bool":
            byte_index, bit_index = self.parse_bit_offset(offset)
            data = self.plc_client.db_read(self._db_number, byte_index, 1)
            return snap7.util.get_bool(data, 0, bit_index)

        if type == "real":
            byte_index = int(str(offset).split(".", 1)[0])
            data = self.plc_client.db_read(self._db_number, byte_index, 4)
            return snap7.util.get_real(data, 0)

        if type == "int":
            byte_index = int(str(offset).split(".", 1)[0])
            data = self.plc_client.db_read(self._db_number, byte_index, 2)
            return snap7.util.get_int(data, 0)

        return None

    def read_plc_values(self, *field_names) -> dict:
        values = {}
        string_fields = {
            "sn_code_offset",
            "product_model_offset",
            "res_error_code_offset",
            "res_sn_code_offset",
            "res_product_model_offset",
        }
        bool_fields = {
            "start_signal_offset",
            "res_finish_signal_offset",
            "res_result_offset",
        }
        real_fields = {
            "res_score_offset",
            "res_spl_offset",
            "res_loudness_offset",
            "res_vibrate_data_offset",
            "res_ai_score_offset",
            "res_spl_limit_up_offset",
            "res_spl_limit_down_offset",
            "res_loudness_limit_up_offset",
            "res_loudness_limit_down_offset",
            "res_vibrate_limit_up_offset",
            "res_vibrate_limit_down_offset",
            "res_ai_score_limit_down_offset",
        }
        int_fields = {
            "rotation_speed_offset",
        }

        for field_name in field_names:
            offset = self._field_offsets_dict.get(field_name)
            if not offset:
                self.logger.warning(f"PLC {self._plc_id} 未找到偏移配置: {field_name}")
                continue

            if field_name in string_fields:
                value = self.read_offset_value(offset, "string")
            elif field_name in bool_fields:
                value = self.read_offset_value(offset, "bool")
            elif field_name in real_fields:
                value = self.read_offset_value(offset, "real")
            elif field_name in int_fields:
                value = self.read_offset_value(offset, "int")
            else:
                continue

            # 字符串为空或读取失败时不放进结果；False 和 0.0 是有效值，需要保留。
            if value == "" or value is None:
                continue
            values[field_name] = value
        return values

    def read_plc_limit(self):
        # PLC 连接成功后读取一次机种和各项上下限，后续测试可直接按 plc_id 取用。
        values = self.read_plc_values(*self.limit_field)
        self.bus.plc_limit_values_dict[self._plc_id] = values
        self.bus.log.emit(f"PLC {self._plc_id} 读取限值完成: {values}")

    def _start_value_timer(self):
        if self.value_timer is not None:
            return
        self.value_timer = QTimer(self)
        self.value_timer.setInterval(self.read_time_ms)
        self.value_timer.timeout.connect(self.read_plc_limit)
        self.value_timer.start()

    def _emit_plc_workorder(self):
        payload = {}
        values = self.read_plc_values(
            "sn_code_offset",
            "rotation_speed_offset",
        )
        if values.get("sn_code_offset"):
            payload["sn"] = values["sn_code_offset"]
        if values.get("rotation_speed_offset") is not None:
            payload["rotation_speed"] = values["rotation_speed_offset"]
        if self._plc_id is not None:
            display_payload = dict(payload)
            display_payload["plc_id"] = self._plc_id
            self.bus.plc_workorder_changed.emit(display_payload)
        return payload

    def poll_once(self):
        """
        轮询一次
        """
        # 单次轮询逻辑：未连接则尝试重连，已连接则读取位并判断是否出现上升沿。
        if self.plc_client is None:
            self._try_reconnect()
            return

        v = self._read_start_bit()
        if v is None:
            return

        if self._last_value is None:
            # 第一次读到值时只记录，不触发“开始”。
            self._last_value = v
            return

        # 只在 0 -> 1 时触发一次，避免 PLC 保持 1 时重复触发。
        rising = (self._last_value is False) and (v is True)
        self._last_value = v

        if not rising:
            # 未检测到信号
            return
        self.bus.log.emit(f"PLC {self._plc_id} 检测到开始信号")
        self.logger.info(f"PLC {self._plc_id} 检测到开始信号")
        self._write_testing_signal(True)
        trigger_monotonic_ns = time.monotonic_ns()
        workorder = self._emit_plc_workorder()
        print(workorder)
        try:
            self._on_start_requested(
                self._plc_id,
                workorder,
                trigger_monotonic_ns,
            )
        except Exception as exc:
            self.bus.log.emit(f"PLC{self._plc_id}开始回调失败: {exc}")
            self.logger.error(f"PLC{self._plc_id}开始回调失败: {exc}")

        # 如果需要，触发后自动清零，方便 PLC 下次再次置 1 触发。
        if self._auto_reset_bit:
            self.write_plc_value(self._field_offsets_dict["start_signal_offset"],"bool",False)

    def parse_bit_offset(self, offset):
        byte_text, bit_text = str(offset).split(".", 1)
        return int(byte_text), int(bit_text)

