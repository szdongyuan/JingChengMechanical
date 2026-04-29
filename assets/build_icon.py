"""Render assets/logo.svg to assets/logo.ico (multi-size)."""
from pathlib import Path

from PIL import Image
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication


HERE = Path(__file__).resolve().parent
SVG_PATH = HERE / "logo.svg"
PNG_DIR = HERE / "_png"
ICO_PATH = HERE / "logo.ico"
SIZES = (16, 32, 48, 64, 128, 256)


def render_png(size: int) -> Path:
    renderer = QSvgRenderer(str(SVG_PATH))
    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    PNG_DIR.mkdir(exist_ok=True)
    out = PNG_DIR / f"logo_{size}.png"
    image.save(str(out), "PNG")
    return out


def main() -> None:
    app = QApplication.instance() or QApplication([])
    pngs = [render_png(size) for size in SIZES]
    base = Image.open(pngs[-1])
    base.save(ICO_PATH, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {ICO_PATH}")


if __name__ == "__main__":
    main()
