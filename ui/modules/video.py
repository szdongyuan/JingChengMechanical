from __future__ import annotations

import cv2
import time

from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget


class CameraThread(QThread):
    frame_ready = pyqtSignal(object)
    opened = pyqtSignal()
    failed = pyqtSignal(str)
    disconnected = pyqtSignal(str)

    def __init__(self, camera_index=0):
        super().__init__()
        self.camera_index = camera_index
        self.running = False
        self.cap = None

    def run(self):
        self.running = True
        self.cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.failed.emit("摄像头打开失败")
            return

        # 设置一个常用预览分辨率
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.opened.emit()

        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                self.disconnected.emit("摄像头已断开")
                break
            # 更新图像
            self.frame_ready.emit(frame)
            time.sleep(0.03)

        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def stop(self):
        self.running = False


class VideoModule(QWidget):
    module_id = "video"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.camera_thread = None

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
        self.lb.setScaledContents(False)
        self.lb.setStyleSheet(
            "background:#f8fafc; border:1px dashed #d1d5db; border-radius:10px;"
        )
        self.lb.setOpenExternalLinks(False)
        self.lb.linkActivated.connect(self.refresh_camera)
        lay.addWidget(self.lb, 1)

    def start_camera(self):
        # Windows 下优先用 DirectShow 打开 USB
        self.stop_camera()
        self.camera_thread = CameraThread(0)
        self.camera_thread.frame_ready.connect(self.update_frame)
        self.camera_thread.opened.connect(self.on_camera_opened)
        self.camera_thread.failed.connect(self.on_camera_failed)
        self.camera_thread.disconnected.connect(self.on_camera_disconnected)
        self.camera_thread.start()

    def stop_camera(self):
        if self.camera_thread is not None:
            self.camera_thread.stop()
            self.camera_thread.wait()
            self.camera_thread = None

    @pyqtSlot()
    def on_camera_opened(self):
        self.services.bus.log.emit("摄像头打开成功")

    @pyqtSlot(str)
    def on_camera_failed(self, message):
        self.services.bus.log.emit("摄像头打开失败")
        self.set_text(message)

    @pyqtSlot(str)
    def on_camera_disconnected(self, message):
        self.services.bus.log.emit("摄像头已断开")
        self.set_text(message)

    @pyqtSlot(object)
    def update_frame(self, frame):
        if frame is None:
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
        pixmap = QPixmap.fromImage(image)
        scaled = pixmap.scaled(
            self.lb.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.lb.setPixmap(scaled)

    def set_text(self, message):
        self.lb.clear()
        self.lb.setText(f'{message}, 点击 <a href="retry" style="color:#2563eb;text-decoration:none;">重试</a>')

    def refresh_camera(self, _link=None):
        self.start_camera()

    # def closeEvent(self, event):
    #     # 关闭页面时释放摄像头
    #     self.stop_camera()
    #     super().closeEvent(event)
