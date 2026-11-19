"""界面体检：按指定尺寸逐页截图，并输出布局诊断（溢出 / 重叠 / 被压扁）。

用法::

    python tools/ui_check.py                 # 默认 1180x760 与 980x640 两套尺寸
    python tools/ui_check.py --sizes 1180x760

产出：
    logs/ui-check/<尺寸>/<页面>.png   截图
    logs/ui-check/report.json         诊断数据（溢出、重叠、零高度控件）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QScrollArea,
    QWidget,
)

from app.core import config as cfg_mod  # noqa: E402
from app.ui.pyside import theme  # noqa: E402
from app.ui.pyside.assets import chevron  # noqa: E402
from app.ui.pyside.fonts import ensure_cjk  # noqa: E402
from app.ui.pyside.main_window import MainWindow, NAV_ITEMS  # noqa: E402


def _settle(app: QApplication, times: int = 5) -> None:
    for _ in range(times):
        app.processEvents()


def _scroll_overflow(page) -> int:
    """页面内所有 QScrollArea 的内容高度 - 可视高度（>0 表示需要滚动才看全）。"""
    worst = 0
    for area in page.findChildren(QScrollArea):
        widget = area.widget()
        if widget is None:
            continue
        need = widget.sizeHint().height()
        have = area.viewport().height()
        if need > have:
            worst = max(worst, need - have)
    return worst


def _clipped_labels(page) -> list[str]:
    """检查带省略号的 QLabel：实际文本与显示文本不一致即被截断。"""
    out: list[str] = []
    for label in page.findChildren(QLabel):
        try:
            full = label.text()
        except RuntimeError:  # 控件已被销毁
            continue
        if not full:
            continue
        fm = label.fontMetrics()
        avail = label.contentsRect().width()
        if avail <= 0:
            continue
        name = label.objectName() or label.__class__.__name__
        if label.wordWrap():
            # 开了换行就不存在水平截断，改判「换行后需要的高度 > 实际高度」
            need = label.heightForWidth(avail)
            have = label.contentsRect().height()
            if need - have > 6:
                out.append(f"{name} 换行后需{need}实{have}: {full[:48]}")
            continue
        shown = fm.elidedText(full, Qt.TextElideMode.ElideRight, avail)
        if shown != full and len(full) > 4:
            out.append(f"{name}: {full[:48]}")
    return out[:12]


def _squeezed(page) -> list[str]:
    """容器被压扁：布局需要的尺寸 > 实际分配到的尺寸（这就是「显示不全」的根因）。

    只报差值明显的（> 6px），避免把正常的伸缩当成问题。
    """
    out: list[str] = []
    for widget in page.findChildren(QWidget):
        lay = widget.layout()
        if lay is None:
            continue
        need_h = lay.sizeHint().height()
        have_h = widget.height()
        need_w = lay.sizeHint().width()
        have_w = widget.width()
        name = widget.objectName() or type(widget).__name__
        if name.startswith("qt_"):  # Qt 内部容器，不是我们写的布局，跳过
            continue
        if need_h - have_h > 6:
            out.append(f"{name} 高 需{need_h} 实{have_h} (缺{need_h - have_h})")
        if need_w - have_w > 6:
            out.append(f"{name} 宽 需{need_w} 实{have_w} (缺{need_w - have_w})")
    return out[:14]


def _outside(page) -> list[str]:
    """子控件矩形超出页面可视区（被窗口边缘裁掉）。"""
    out: list[str] = []
    page_rect = page.rect()
    for widget in page.findChildren(QWidget):
        if not widget.isVisible() or widget.window() is not page.window():
            continue
        geo = widget.geometry()
        if geo.width() <= 0 or geo.height() <= 0:
            continue
        parent = widget.parentWidget()
        if parent is None or parent is page:
            continue
        # 只看直接挂在页面级容器下的顶层控件，避免逐层重复上报
        if parent.parentWidget() is not None and parent.parentWidget() is not page:
            continue
        over_bottom = geo.bottom() - page_rect.bottom()
        over_right = geo.right() - page_rect.right()
        if over_bottom > 4 or over_right > 4:
            out.append(
                f"{widget.objectName() or type(widget).__name__} "
                f"超出下{over_bottom}px 右{over_right}px"
            )
    return out[:12]


def _overlaps(page) -> list[str]:
    """同层兄弟控件矩形重叠（错位最典型的量化信号）。"""
    out: list[str] = []
    parent = page
    kids = [
        w
        for w in parent.children()
        if hasattr(w, "geometry") and w.isWidgetType() and w.isVisible()
    ]
    for i in range(len(kids)):
        for j in range(i + 1, len(kids)):
            a: QRect = kids[i].geometry()
            b: QRect = kids[j].geometry()
            inter = a.intersected(b)
            if inter.width() > 4 and inter.height() > 4:
                out.append(
                    f"{kids[i].objectName() or type(kids[i]).__name__}"
                    f" ∩ {kids[j].objectName() or type(kids[j]).__name__}"
                    f" = {inter.width()}x{inter.height()}"
                )
    return out[:10]


def main() -> int:
    sizes: list[str] = []
    argv = sys.argv[1:]
    for i, item in enumerate(argv):
        if item == "--sizes" and i + 1 < len(argv):
            sizes = [s.strip() for s in argv[i + 1].split(",") if s.strip()]
    if not sizes:
        sizes = ["1180x760", "980x640"]

    app = QApplication(sys.argv[:1])
    app.setApplicationName("机甲旋风启动器")
    palette = theme.DARK
    app.setStyleSheet(theme.qss(palette, chevron(palette.text_dim)))
    ensure_cjk(app)

    cfg = cfg_mod.load()
    window = MainWindow(cfg)
    out_root = ROOT / "logs" / "ui-check"
    report: list[dict] = []

    for size in sizes:
        try:
            w_str, h_str = size.lower().split("x")
            w, h = int(w_str), int(h_str)
        except ValueError:
            print(f"跳过非法尺寸：{size}")
            continue
        window.resize(w, h)
        window.show()
        _settle(app)

        dir_out = out_root / size
        dir_out.mkdir(parents=True, exist_ok=True)
        for key, _text, _icon in NAV_ITEMS:
            window._switch(key)
            _settle(app)
            bodies = getattr(window, "page_bodies", {})
            page = bodies.get(key, window.pages[key])
            shot = dir_out / f"{key}.png"
            window.grab().save(str(shot), "PNG")
            report.append(
                {
                    "size": size,
                    "page": key,
                    "shot": str(shot),
                    "widgets": len(page.findChildren(object)),
                    "overflow_px": _scroll_overflow(page),
                    "clipped": _clipped_labels(page),
                    "squeezed": _squeezed(page),
                    "outside": _outside(page),
                    "overlaps": _overlaps(page),
                }
            )

    window.close()
    _settle(app)

    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for row in report:
        bad = bool(
            row["overflow_px"] > 0
            or row["clipped"]
            or row["squeezed"]
            or row["outside"]
            or row["overlaps"]
        )
        print(
            f"[{row['size']}] {row['page']:<12} widgets={row['widgets']:<4} "
            f"overflow={row['overflow_px']:<5} clipped={len(row['clipped'])} "
            f"squeezed={len(row['squeezed'])} outside={len(row['outside'])} "
            f"overlap={len(row['overlaps'])}{'  <== 需关注' if bad else ''}"
        )
        for item in row["squeezed"]:
            print(f"      挤压: {item}")
        for item in row["outside"]:
            print(f"      越界: {item}")
        for item in row["clipped"]:
            print(f"      截断: {item}")
    print(f"\n诊断数据：{out_root / 'report.json'}")
    print(f"截图目录：{out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
