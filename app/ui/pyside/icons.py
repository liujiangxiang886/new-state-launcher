"""矢量图标：全部用 QPainter 现画，不依赖任何图片资源。

好处是打包时不用带 assets，也能按主题色实时换色、按 DPR 渲染清晰。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _grid(p: QPainter, r: QRectF) -> None:
    w = r.width() / 2.6
    gap = r.width() / 6
    for row in range(2):
        for col in range(2):
            x = r.left() + col * (w + gap)
            y = r.top() + row * (w + gap)
            p.drawRoundedRect(QRectF(x, y, w, w), 1.6, 1.6)


def _puzzle(p: QPainter, r: QRectF) -> None:
    """扩展图标：一块带凸起的方块。"""
    path = QPainterPath()
    w, h = r.width(), r.height()
    path.addRoundedRect(QRectF(r.left(), r.top(), w, h), 2.2, 2.2)
    p.drawPath(path)
    knob = QRectF(r.center().x() - w * 0.16, r.center().y() - h * 0.16, w * 0.32, h * 0.32)
    p.drawEllipse(knob)


def _box(p: QPainter, r: QRectF) -> None:
    """打包图标：纸箱轮廓 + 中线。"""
    top = QRectF(r.left(), r.top() + r.height() * 0.12, r.width(), r.height() * 0.26)
    body = QRectF(r.left(), r.top() + r.height() * 0.28, r.width(), r.height() * 0.62)
    p.drawRoundedRect(top, 1.5, 1.5)
    p.drawRoundedRect(body, 1.8, 1.8)
    p.drawLine(QPointF(r.center().x(), body.top()), QPointF(r.center().x(), body.bottom()))


def _gear(p: QPainter, r: QRectF) -> None:
    c = r.center()
    outer = r.width() * 0.30
    inner = r.width() * 0.12
    p.drawEllipse(c, outer, outer)
    p.drawEllipse(c, inner, inner)
    import math

    for i in range(8):
        a = math.pi * 2 * i / 8
        p.drawLine(
            QPointF(c.x() + math.cos(a) * outer, c.y() + math.sin(a) * outer),
            QPointF(c.x() + math.cos(a) * (outer + r.width() * 0.14), c.y() + math.sin(a) * (outer + r.width() * 0.14)),
        )


def _pulse(p: QPainter, r: QRectF) -> None:
    """诊断图标：心电折线。"""
    path = QPainterPath()
    x0, y0 = r.left(), r.center().y()
    w, h = r.width(), r.height() * 0.34
    path.moveTo(x0, y0)
    path.lineTo(x0 + w * 0.22, y0)
    path.lineTo(x0 + w * 0.34, y0 - h)
    path.lineTo(x0 + w * 0.46, y0 + h * 0.8)
    path.lineTo(x0 + w * 0.58, y0 - h * 0.4)
    path.lineTo(x0 + w * 0.70, y0)
    path.lineTo(x0 + w, y0)
    p.drawPath(path)


def _book(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(QRectF(r.left(), r.top(), r.width(), r.height()), 1.8, 1.8)
    p.drawLine(QPointF(r.center().x(), r.top() + 2), QPointF(r.center().x(), r.bottom() - 2))
    for i in range(2):
        y = r.top() + r.height() * (0.32 + i * 0.24)
        p.drawLine(QPointF(r.left() + 3, y), QPointF(r.center().x() - 3, y))
        p.drawLine(QPointF(r.center().x() + 3, y), QPointF(r.right() - 3, y))


def _gamepad(p: QPainter, r: QRectF) -> None:
    body = QRectF(r.left(), r.top() + r.height() * 0.26, r.width(), r.height() * 0.52)
    p.drawRoundedRect(body, body.height() / 2, body.height() / 2)
    cx, cy = r.center().x(), r.center().y()
    p.drawLine(QPointF(cx - r.width() * 0.22, cy), QPointF(cx - r.width() * 0.08, cy))
    p.drawLine(QPointF(cx - r.width() * 0.15, cy - r.height() * 0.09), QPointF(cx - r.width() * 0.15, cy + r.height() * 0.09))
    p.drawEllipse(QPointF(cx + r.width() * 0.18, cy - r.height() * 0.05), 1.6, 1.6)
    p.drawEllipse(QPointF(cx + r.width() * 0.28, cy + r.height() * 0.07), 1.6, 1.6)


def _play(p: QPainter, r: QRectF) -> None:
    path = QPainterPath()
    path.moveTo(r.left() + r.width() * 0.28, r.top() + r.height() * 0.14)
    path.lineTo(r.left() + r.width() * 0.84, r.center().y())
    path.lineTo(r.left() + r.width() * 0.28, r.bottom() - r.height() * 0.14)
    path.closeSubpath()
    p.fillPath(path, p.pen().color())
    p.drawPath(path)


def _folder(p: QPainter, r: QRectF) -> None:
    path = QPainterPath()
    path.addRoundedRect(QRectF(r.left(), r.top() + r.height() * 0.22, r.width(), r.height() * 0.58), 1.8, 1.8)
    p.drawPath(path)
    p.drawLine(QPointF(r.left(), r.top() + r.height() * 0.34), QPointF(r.left() + r.width() * 0.34, r.top() + r.height() * 0.34))
    p.drawLine(QPointF(r.left() + r.width() * 0.34, r.top() + r.height() * 0.34), QPointF(r.left() + r.width() * 0.42, r.top() + r.height() * 0.22))


def _refresh(p: QPainter, r: QRectF) -> None:
    p.drawArc(QRectF(r.left() + 2, r.top() + 2, r.width() - 4, r.height() - 4), 40 * 16, 280 * 16)
    import math

    c = r.center()
    a = math.radians(40)
    tip = QPointF(c.x() + math.cos(a) * (r.width() / 2 - 2), c.y() - math.sin(a) * (r.height() / 2 - 2))
    p.drawLine(tip, QPointF(tip.x() - r.width() * 0.10, tip.y() - r.height() * 0.02))
    p.drawLine(tip, QPointF(tip.x() + r.width() * 0.04, tip.y() + r.height() * 0.10))


def _check(p: QPainter, r: QRectF) -> None:
    p.drawLine(QPointF(r.left() + r.width() * 0.18, r.center().y()), QPointF(r.left() + r.width() * 0.42, r.bottom() - r.height() * 0.22))
    p.drawLine(QPointF(r.left() + r.width() * 0.42, r.bottom() - r.height() * 0.22), QPointF(r.right() - r.width() * 0.16, r.top() + r.height() * 0.22))


_DRAWERS: dict[str, Callable[[QPainter, QRectF], None]] = {
    "library": _grid,
    "puzzle": _puzzle,
    "box": _box,
    "gear": _gear,
    "pulse": _pulse,
    "book": _book,
    "gamepad": _gamepad,
    "play": _play,
    "folder": _folder,
    "refresh": _refresh,
    "check": _check,
}


def pixmap(name: str, color: str = "#9aa3ae", size: int = 20, width: float = 1.7, dpr: float = 2.0) -> QPixmap:
    painter_fn = _DRAWERS.get(name, _grid)
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    pad = size * 0.14
    rect = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    painter_fn(p, rect)
    p.end()
    return px


def icon(name: str, color: str = "#9aa3ae", size: int = 20, width: float = 1.7) -> QIcon:
    return QIcon(pixmap(name, color, size, width))
