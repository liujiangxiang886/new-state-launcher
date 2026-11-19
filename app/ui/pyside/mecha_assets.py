"""机甲风外壳的位图资源定位。

**为什么不用 base64 内联**（和 ``creator_asset.py`` 不同）：
那张创作者横幅只有 403x96、约 15KB，内联很划算。这里 7 张素材共
3.5MB（光圆形按钮就 1.6MB），内联会让 .py 变成几 MB 的一行字符串，
既难读又拖慢编辑器。onedir 打包下把目录放进 ``_internal`` 更干净。

路径优先级（和 ``main_window._icon_candidates`` 同构）：

1. ``sys._MEIPASS/app/ui/pyside/assets/mecha/`` —— PyInstaller onedir，
   数据文件在 ``_internal`` 下，``_MEIPASS`` 指向它
2. 源码目录 ``app/ui/pyside/assets/mecha/`` —— 开发态
3. ``paths.ROOT`` 下同名相对路径 —— 便携版/绿色版解压目录

发布脚本按目录复制整个 ``_internal``，所以只要 spec 的 datas 带上
这个目录，发布包自动包含，不需要额外白名单。
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QPixmap

#: 素材名 → 文件名
FILES = {
    "logo": "logo.png",
    "btn_bar": "btn_bar.png",
    "round": "round.png",
    "panel": "panel.png",
    "background": "background.jpg",
    "avatar": "avatar.png",
    "bilibili": "bilibili.png",
}

_REL = Path("app") / "ui" / "pyside" / "assets" / "mecha"


def asset_dir() -> Path | None:
    """返回第一个真实存在的素材目录，找不到返回 None。"""
    cands: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(Path(meipass) / _REL)
    cands.append(Path(__file__).resolve().parent / "assets" / "mecha")
    try:
        from ....core import paths

        cands.append(paths.ROOT / _REL)
    except Exception:  # noqa: BLE001 - 早期导入失败不该拦住启动
        pass
    for c in cands:
        if c.is_dir() and (c / FILES["round"]).is_file():
            return c
    return None


@lru_cache(maxsize=16)
def path(key: str) -> Path | None:
    """按素材 key 取路径；缺失时返回 None（调用方需能优雅降级）。"""
    filename = FILES.get(key)
    base = asset_dir()
    if not filename or base is None:
        return None
    p = base / filename
    return p if p.is_file() else None


@lru_cache(maxsize=16)
def pixmap(key: str) -> QPixmap:
    """按素材 key 取位图；缺失返回空 QPixmap（``isNull()`` 为真）。

    为什么返回空图而不是抛异常：素材是**纯装饰**，缺失时界面必须还能用。
    所有绘制点都检查 ``isNull()`` 后走降级路径（纯色/文字按钮）。
    """
    p = path(key)
    if p is None:
        return QPixmap()
    px = QPixmap(str(p))
    return px if not px.isNull() else QPixmap()


def available() -> bool:
    """整套素材是否齐全（7 张都在）。"""
    return all(path(k) is not None for k in FILES)


def scaled(key: str, width: int) -> QPixmap:
    """按宽度等比缩放，保持平滑。空图原样返回。"""
    px = pixmap(key)
    if px.isNull() or width <= 0:
        return px
    from PySide6.QtCore import Qt

    return px.scaledToWidth(
        width, Qt.TransformationMode.SmoothTransformation
    )
