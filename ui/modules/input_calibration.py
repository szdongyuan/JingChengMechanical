from __future__ import annotations


from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget, QApplication,
)

from app.service.log_manager import LogManager
from base.audio_thd_frequency_response_analysis import AudioThdFrequencyResponseAnalysis
from consts.style import combo_qss


class InputCalibrationModule(QWidget):

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.logger = LogManager.set_log_handler("输入校准")

        self.init_ui()
        self.init_fun()

    # ---------------- UI ----------------
    def init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # 标准声压
        gb_std = QGroupBox("标准声压")
        gb_std.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        std_lay = QHBoxLayout(gb_std)
        std_lay.setContentsMargins(56, 10, 56, 10)
        std_lay.setSpacing(18)

        self.rb_94 = QRadioButton("94 dB")
        self.rb_114 = QRadioButton("114 dB")
        self.rb_94.setChecked(True)

        self.std_group = QButtonGroup(self)
        self.std_group.addButton(self.rb_94, 94)
        self.std_group.addButton(self.rb_114, 114)

        std_lay.addWidget(self.rb_94)
        std_lay.addSpacing(80)
        std_lay.addWidget(self.rb_114)
        std_lay.addStretch(1)

        # 录制状态
        gb_state = QGroupBox("录制状态")
        gb_state.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        state_lay = QFormLayout(gb_state)
        state_lay.setContentsMargins(56, 10, 56, 10)
        state_lay.setHorizontalSpacing(120)
        state_lay.setVerticalSpacing(8)
        state_lay.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        state_lay.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self.record_state = QLabel("待录制")
        self.record_state.setObjectName("Muted")
        self.record_state.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.record_state.setStyleSheet("color: rgb(255, 170, 0);")

        record_state_label = QLabel("录制状态 : ")
        state_lay.addRow(record_state_label, self.record_state)

        # 校准结果
        gb_result = QGroupBox("校准结果")
        gb_result.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        result_lay = QVBoxLayout(gb_result)
        result_lay.setContentsMargins(56, 10, 56, 10)
        result_lay.setSpacing(10)

        self.combo_channel = QComboBox()
        self.combo_channel.setStyleSheet(combo_qss)
        # 太窄会把 “In1” 截成 “In”
        self.combo_channel.setFixedWidth(70)

        # 声压偏差：最终校准结果（标准声压 - 测得平均 SPL），用于展示与写入 mic_calibration.json
        self.deviation_lineedit = QLineEdit()
        self.deviation_lineedit.setPlaceholderText("—")
        self.deviation_lineedit.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.deviation_lineedit.setMinimumHeight(34)
        self.deviation_lineedit.setFixedWidth(90)

        self.save_button = QPushButton("保存")
        self.save_button.setFixedWidth(72)
        self.save_button.setMinimumHeight(34)
        self.save_button.setObjectName("Primary")

        # 第一行：声压偏差 + 值
        row_dev_lay = QHBoxLayout()
        row_dev_lay.setContentsMargins(0, 0, 0, 0)
        row_dev_lay.setSpacing(14)

        spl_deviation_label = QLabel("声压偏差 :")
        spl_deviation_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        spl_deviation_label.setFixedWidth(92)

        row_dev_lay.addWidget(spl_deviation_label)
        # 让输入框整体往右移动一点
        row_dev_lay.addSpacing(40)
        row_dev_lay.addWidget(self.deviation_lineedit)
        row_dev_lay.addStretch(1)

        # 第二行：空出和上面 label 相同宽度，再放 In 和 保存
        row_action_lay = QHBoxLayout()
        row_action_lay.setContentsMargins(0, 0, 0, 0)
        row_action_lay.setSpacing(14)
        row_action_lay.addWidget(self.combo_channel)
        # 让“保存”按钮右移一点（不顶到最右侧）
        row_action_lay.addSpacing(70)
        row_action_lay.addWidget(self.save_button)
        row_action_lay.addStretch(1)

        result_lay.addLayout(row_dev_lay)
        result_lay.addLayout(row_action_lay)

        root.addWidget(gb_std)
        root.addWidget(gb_state)
        root.addWidget(gb_result)

        # 底部按钮
        bottom = QWidget()
        bottom_lay = QHBoxLayout(bottom)
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        bottom_lay.setSpacing(12)

        self.cal_button = QPushButton("校准")
        self.reset_button = QPushButton("重置")
        # 使用 Primary 样式
        self.cal_button.setObjectName("Primary")
        self.reset_button.setObjectName("Primary")
        self.cal_button.setFixedWidth(160)
        self.reset_button.setFixedWidth(160)
        self.cal_button.setMinimumHeight(34)
        self.reset_button.setMinimumHeight(34)

        bottom_lay.addStretch(1)
        bottom_lay.addWidget(self.cal_button)
        bottom_lay.addWidget(self.reset_button)
        bottom_lay.addStretch(1)
        root.addStretch(1)
        root.addWidget(bottom)

        self.init_channel_combobox()

    def init_fun(self):
        self.cal_button.clicked.connect(self.input_calibration)
        self.reset_button.clicked.connect(self.clicked_reset_button)
        self.save_button.clicked.connect(self.save_mic_deviation_value)
        self.services.bus.refresh_channels.connect(self.init_channel_combobox)

    def init_channel_combobox(self):
        self.combo_channel.clear()
        device_info = self.services.hss_service.load_io_devices()
        input_info = device_info.get("input", {})
        raw = input_info.get("channel_count")
        if not raw:
            return
        channel_count = int(raw)
        for i in range(1, channel_count + 1):
            self.combo_channel.addItem(f"In{i}", i - 1)

    def save_mic_deviation_value(self):
        selected_channel = self.combo_channel.currentText()
        deviation_lineedit_text = self.deviation_lineedit.text()
        if not deviation_lineedit_text:
            QMessageBox.warning(self, "警告", "请先校准Mic！")
            return

        flag, data = self.services.input_calibration_service.save_mic_cal_data(
            selected_channel, deviation_lineedit_text
        )
        if not flag:
            QMessageBox.warning(self, "错误", data)
            return
        self.logger.info(f"成功保存 {selected_channel} 的偏差值 {data} Pa/V")
        QMessageBox.information(self, "成功", f"保存成功！{selected_channel} 的偏差值 {data} Pa/V ！")

    def clicked_reset_button(self):
        self.record_state.setText("待录制")
        self.record_state.setStyleSheet("color: rgb(255, 170, 0);")
        self.deviation_lineedit.clear()

    def input_calibration(self):
        # 进入“录制中”状态
        self.record_state.setText("录制中")
        self.record_state.setStyleSheet("color: green;")
        QApplication.processEvents()

        prolong = 1
        device_info = self.services.hss_service.load_io_devices()
        samplerate = int(device_info["input"]["samplerate"])
        channels_counts = int(device_info["input"]["channel_count"])
        seconds = 5
        recorded_dict = {"channels": channels_counts,
                         "sample_rate": samplerate,
                         "num_frames": seconds * samplerate,
                         "prolong_frames": int(prolong * samplerate)
                         }
        # 获取下拉框通道数据
        channel_number_text = self.combo_channel.currentText()
        mic_cal = (device_info.get("mic_cal") or {})
        channel_index = int(mic_cal.get(channel_number_text, {}).get("binding_index", 0))
        # 计算平均声压级（SPL）
        flag, average_spl = AudioThdFrequencyResponseAnalysis.calculate_average_spl(recorded_dict, channel_index)
        if not flag:
            self.record_state.setText("待录制")
            self.record_state.setStyleSheet("color: rgb(255, 170, 0);")
            self.logger.error(average_spl)
            QMessageBox.critical(self, "录音错误", f"{average_spl}")
            return
        deviation_value = self.calculate_deviation(average_spl)
        # 计算偏差值
        self.deviation_lineedit.setText(f"{deviation_value} Pa/V")
        self.record_state.setText("待录制")
        self.record_state.setStyleSheet("color: rgb(255, 170, 0);")

    def calculate_deviation(self, average_value):
        """
        计算偏差值
        """
        if self.rb_94.isChecked():
            deviation_value = average_value
        else:
            deviation_value = average_value * 10
        return deviation_value


