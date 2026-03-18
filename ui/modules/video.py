from __future__ import annotations

import cv2

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget


class VideoModule(QWidget):
    module_id = "video"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.cap = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_frame)

        self.init_ui()
        self.start_camera()

    def init_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.lb = QLabel("暂无画面")
        self.lb.setObjectName("Muted")
        self.lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.lb.setMinimumHeight(220)
        # 让画面铺满预览区域
        self.lb.setScaledContents(True)
        self.lb.setStyleSheet(
            "background:#f8fafc; border:1px dashed #d1d5db; border-radius:10px;"
        )
        self.lb.setOpenExternalLinks(False)
        self.lb.linkActivated.connect(self.refresh_camera)
        lay.addWidget(self.lb, 1)

    def start_camera(self):
        # Windows 下优先用 DirectShow 打开 USB
        self.timer.stop()
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)

        if self.cap.isOpened():
            self.services.bus.log.emit("[Video] 摄像头打开成功")
        else:
            self.services.bus.log.emit("[Video] 摄像头打开失败")
            self.set_text("摄像头打开失败")
            return

        # 设置一个常用预览分辨率
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.timer.start(30)

    def update_frame(self):
        if self.cap is None:
            return
        ret, frame = self.cap.read()
        if not ret:
            self.timer.stop()
            self.cap.release()
            self.cap = None
            self.set_text("摄像头已断开")
            return

        # OpenCV 是 BGR，Qt 显示前要转成 RGB
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame.shape
        bytes_per_line = ch * w
        image = QImage(
            frame.data,
            w,
            h,
            bytes_per_line,
            QImage.Format.Format_RGB888,
        )

        self.lb.setText("")
        self.lb.setPixmap(QPixmap.fromImage(image))

    def set_text(self, message):
        self.lb.clear()
        self.lb.setText(f'{message}, 点击 <a href="retry" style="color:#2563eb;text-decoration:none;">重试</a>')

    def refresh_camera(self, _link=None):
        self.start_camera()

    def closeEvent(self, event):
        # 关闭页面时释放摄像头
        self.timer.stop()
        if self.cap is not None:
            self.cap.release()
        super().closeEvent(event)