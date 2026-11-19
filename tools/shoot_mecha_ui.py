#!/usr/bin/env python
"""离屏渲染启动器窗口，截 HUB 态与功能页态两张图。

为什么用离屏（``QT_QPA_PLATFORM=offscreen``）：
不需要真人点，也不会弹窗抢焦点、改真实配置。**但要注意**
``MainWindow.__init__`` 会读 config，所以这里先把 ``ui.status_panel_hidden``
按需要写好再构造 —— 否则截不到想要的那一态。

用法::

    python tools/shoot_mecha_ui.py

输出 ``assets/ui/mecha/v2/_real_hub.png`` 和 ``_real_page.png``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = ROOT / "assets" / "ui" / "mecha" / "v2"


def _load_cjk_fonts() -> None:
    """离屏平台默认不带中文字体，中文会全渲染成方框。

    必须显式把 Windows 字体注册进 QFontDatabase。实测不加这一步，
    截图里所有汉字都是 ``□□□``，会误判成「界面文字坏了」。
    """
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        p = Path(r"C:\Windows\Fonts") / name
        if p.is_file():
            QFontDatabase.addApplicationFont(str(p))


def build(hidden: bool):
    """构造主窗口，强制把面板显隐设成 ``hidden``。"""
    from app.ui.pyside import main_window as mw
    import app.core.config as cfg_mod

    cfg = cfg_mod.load()
    cfg.setdefault("ui", {})["status_panel_hidden"] = bool(hidden)
    win = mw.MainWindow(cfg)
    win.resize(1180, 760)
    return win


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    app = QApplication.instance() or QApplication([])
    _load_cjk_fonts()

    for hidden, name in ((True, "_real_hub.png"), (False, "_real_page.png")):
        win = build(hidden)
        win.show()
        # 多跑几轮事件循环 + 强制重新排版：
        # 只 processEvents() 不够 —— setFixedHeight 之后布局要等一次
        # 真正的 layout pass 才会重排，否则 grab() 抓到的是旧帧
        # （踩过：改了折叠高度，截图却和改之前一模一样）。
        for _ in range(12):
            app.processEvents()
        w = win.centralWidget() if hasattr(win, "centralWidget") else win
        if w is not None and w.layout() is not None:
            w.layout().activate()
        for _ in range(6):
            app.processEvents()
        _ = QTimer
        pix = win.grab()
        dst = OUT / name
        pix.save(str(dst))
        print(f"  {'HUB' if hidden else '功能页'} -> {dst.name}  {pix.width()}x{pix.height()}")
        win.close()
        win.deleteLater()
        app.processEvents()

    # ---- 交互验证：HUB 态点「游戏库」应切到功能页
    #
    # 这是用户需求的核心（「点游戏库按钮才出现详情页」），必须实测，
    # 不能只看两态截图分别对不对。
    win = build(True)
    win.show()
    for _ in range(4):
        app.processEvents()
    before = win.content_stack.currentIndex()
    win.nav_buttons["library"].click()
    for _ in range(4):
        app.processEvents()
    after = win.content_stack.currentIndex()
    ok = before == 1 and after == 0
    print(
        f"\n交互验证：HUB(index={before}) → 点游戏库 → 功能页(index={after})"
        f"  {'✓ 通过' if ok else '✗ 失败'}"
    )
    pix = win.grab()
    pix.save(str(OUT / "_real_after_click.png"))
    win.close()
    win.deleteLater()
    app.processEvents()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())