"""字体兜底。

正常情况下 Windows 平台自带字体库，QSS 里的 "Microsoft YaHei UI" 就能命中。
但离屏渲染（``QT_QPA_PLATFORM=offscreen``）等场景下 ``QFontDatabase`` 可能是空的，
所有中文会变成方框——既影响截图验证，也影响极端环境下的可用性。

这里在启动时检查一次：没有可用的 CJK 字体就显式从系统字体目录加载。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

#: 常见中文字体，按偏好排序（ttc 优先，字形更现代）
CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    r"C:\Windows\Fonts\Deng.ttf",      # 等线
]

#: 判定"字体库里有中文"的几个家族名
CJK_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun", "DengXian")


def ensure_cjk(app: QApplication) -> str:
    """确保有可用中文字体，返回最终选用的家族名（可能为空串）。"""
    families = set(QFontDatabase.families())
    if any(name in families for name in CJK_FAMILIES):
        chosen = next(name for name in CJK_FAMILIES if name in families)
        app.setFont(QFont(chosen, 9))
        return chosen

    loaded: list[str] = []
    for path in CANDIDATES:
        if not Path(path).exists():
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        if font_id >= 0:
            loaded.extend(QFontDatabase.applicationFontFamilies(font_id))

    families = set(QFontDatabase.families())
    for name in CJK_FAMILIES:
        if name in families:
            app.setFont(QFont(name, 9))
            return name
    if loaded:
        app.setFont(QFont(loaded[0], 9))
        return loaded[0]
    return ""
