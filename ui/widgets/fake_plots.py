from __future__ import annotations

import math

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget


class FakeLinePlot(QWidget):
    """不依赖第三方库的占位折线图，用来把 UI 先做漂亮。"""

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        r = self.rect().adjusted(10, 10, -10, -10)
        p.fillRect(r, QColor("#ffffff"))

        # grid
        p.setPen(QPen(QColor("#eef2f7"), 1))
        for i in range(1, 6):
            x = r.left() + i * r.width() / 6
            p.drawLine(int(x), r.top(), int(x), r.bottom())
        for i in range(1, 4):
            y = r.top() + i * r.height() / 4
            p.drawLine(r.left(), int(y), r.right(), int(y))

        pts = []
        n = 140
        for i in range(n):
            t = i / (n - 1)
            x = r.left() + t * r.width()
            yv = 0.55 + 0.25 * math.sin(10 * t) + 0.12 * math.sin(41 * t)
            y = r.bottom() - max(0.05, min(0.95, yv)) * r.height()
            pts.append((x, y))

        # fill under
        grad = QLinearGradient(r.left(), r.top(), r.left(), r.bottom())
        grad.setColorAt(0.0, QColor(124, 131, 255, 80))
        grad.setColorAt(1.0, QColor(124, 131, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)

        poly_pts = [(pts[0][0], r.bottom()), *pts, (pts[-1][0], r.bottom())]
        poly = QPolygonF([QRectF(x, y, 1, 1).center() for x, y in poly_pts])
        p.drawPolygon(poly)

        # line
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#7c83ff"), 2))
        for i in range(1, len(pts)):
            p.drawLine(
                int(pts[i - 1][0]),
                int(pts[i - 1][1]),
                int(pts[i][0]),
                int(pts[i][1]),
            )


class FakeHeatmap(QWidget):
    """占位热力图（用于 STFT 频谱图）。"""

    def paintEvent(self, e):
        p = QPainter(self)
        r = self.rect().adjusted(10, 10, -10, -10)
        p.fillRect(r, QColor("#ffffff"))

        cols = 70
        rows = 26
        cw = r.width() / cols
        rh = r.height() / rows

        for y in range(rows):
            for x in range(cols):
                t = x / max(1, cols - 1)
                v = 0.5 + 0.5 * math.sin(10 * t + y * 0.3)
                p.fillRect(
                    int(r.left() + x * cw),
                    int(r.top() + y * rh),
                    int(cw + 1),
                    int(rh + 1),
                    _palette(v),
                )

        p.setPen(QPen(QColor("#e5e7eb"), 1))
        p.drawRect(r)


class FakeBars(QWidget):
    """占位柱状图（用于倍频程）。"""

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        r = self.rect().adjusted(10, 10, -10, -10)
        p.fillRect(r, QColor("#ffffff"))

        n = 10
        bw = r.width() / (n * 1.4)
        gap = bw * 0.4

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#f59e0b"))
        for i in range(n):
            t = i / (n - 1)
            h = (0.35 + 0.55 * abs(math.sin(2.4 * t + 0.2))) * r.height()
            x = r.left() + i * (bw + gap)
            y = r.bottom() - h
            p.drawRoundedRect(QRectF(x, y, bw, h), 4, 4)

        p.setPen(QPen(QColor("#e5e7eb"), 1))
        p.drawRect(r)


def _palette(v: float) -> QColor:
    v = max(0.0, min(1.0, v))
    if v < 0.5:
        t = v / 0.5
        return QColor(int(10 + 20 * t), int(40 + 170 * t), int(120 + 120 * t))
    if v < 0.8:
        t = (v - 0.5) / 0.3
        return QColor(int(30 + 220 * t), int(210 + 30 * t), int(240 - 200 * t))
    t = (v - 0.8) / 0.2
    return QColor(250, int(240 - 160 * t), 40)

