"""运行时生成的小资源（QSS 需要图片文件，不能直接用 QPainter 画的 pixmap）。

QSS 一旦接管 QComboBox 的外观，Qt 就不再画默认下拉箭头，必须由样式表给出
``::down-arrow`` 的图片。这里在缓存目录里生成一张极小的三角形 PNG，
按主题色区分，避免往仓库里塞二进制。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap, QPolygonF


def _cache_dir() -> Path:
    from ...core import paths

    d = paths.CACHE / "ui"
    d.mkdir(parents=True, exist_ok=True)
    return d


def chevron(color: str, size: int = 10) -> Path:
    """生成一个朝下的实心三角，返回 PNG 绝对路径（正斜杠，供 QSS 使用）。"""
    tag = hashlib.md5(f"{color}-{size}".encode()).hexdigest()[:8]
    path = _cache_dir() / f"chevron-{tag}.png"
    if path.exists():
        return path.as_posix()

    dpr = 2
    px = QPixmap(size * dpr, size * dpr)
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawPolygon(
        QPolygonF(
            [
                QPointF(1.0, 3.0),
                QPointF(size - 1.0, 3.0),
                QPointF(size / 2.0, size - 2.0),
            ]
        )
    )
    p.end()
    px.save(str(path), "PNG")
    return path.as_posix()
