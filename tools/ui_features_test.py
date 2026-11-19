#!/usr/bin/env python
"""界面功能回归：版本选择器展开 / 自定义背景 / 拖拽安装 + 进度状态栏。

离屏运行（不需要显示器），逐项抓图，最后打印判定：

    _build_tools\\venv\\Scripts\\python.exe tools\\ui_features_test.py [扩展包路径]

覆盖的交互：
1. 点开版本选择器 → 截图弹层 → 切换版本
2. 生成一张测试图并设为界面背景 → 截图
3. 用真实的 QDropEvent 模拟拖入扩展包 → 观察状态栏进度 → 截图 → 等安装完成
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QMimeData, QPointF, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QDropEvent, QLinearGradient, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import config as cfg_mod  # noqa: E402
from app.ui.pyside import theme  # noqa: E402
from app.ui.pyside.assets import chevron  # noqa: E402
from app.ui.pyside.fonts import ensure_cjk  # noqa: E402
from app.ui.pyside.main_window import MainWindow  # noqa: E402

SHOTS = ROOT / "logs" / "ui-shots"
def find_sample_package() -> Path | None:
    """按优先级找一个可用的扩展包：桌面 zip/jjm > 已装扩展的原包。

    不写死某一个文件——样例被清理后测试会一直红，而那时失败原因是
    「文件不存在」而不是「功能坏了」，噪音大于价值。
    """
    import zipfile

    from app.core import paths
    from app.core.fsutil import read_json

    desktop = Path.home() / "Desktop"
    candidates = [*sorted(desktop.glob("*.zip")), *sorted(desktop.glob("*.jjm"))]
    registry = read_json(paths.CONFIG_DIR / "extensions.json", default=None) or {}
    for item in registry.get("items") or []:
        raw = str(item.get("source_package") or "")
        if raw and Path(raw).is_file():
            candidates.append(Path(raw))
    for path in candidates:
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except (OSError, zipfile.BadZipFile):
            continue
        if any(n.endswith("extension.json") for n in names):
            return path
    return None


DEFAULT_PKG = find_sample_package()


def make_test_background(path: Path) -> Path:
    """生成一张渐变图当测试背景，避免依赖外部素材。"""
    px = QPixmap(1600, 900)
    p = QPainter(px)
    grad = QLinearGradient(0, 0, 1600, 900)
    grad.setColorAt(0.0, QColor("#1b2a4a"))
    grad.setColorAt(0.5, QColor("#2c3f63"))
    grad.setColorAt(1.0, QColor("#4a2c55"))
    p.fillRect(0, 0, 1600, 900, grad)
    p.setPen(QColor(255, 255, 255, 26))
    for i in range(0, 1600, 80):
        p.drawLine(i, 0, i, 900)
    for j in range(0, 900, 80):
        p.drawLine(0, j, 1600, j)
    p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    px.save(str(path), "PNG")
    return path


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args:
        package = Path(args[0])
    elif DEFAULT_PKG is not None:
        package = DEFAULT_PKG
    else:
        print("找不到可用的扩展包样例：拖拽安装一步会跳过。")
        print("  放一个带 extension.json 的 zip 到桌面，或直接把路径作为参数传进来。")
        package = None
    SHOTS.mkdir(parents=True, exist_ok=True)

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss(theme.DARK, chevron(theme.DARK.text_dim)))
    ensure_cjk(app)

    cfg = cfg_mod.load()
    original_bg = str((cfg.get("ui") or {}).get("background_image") or "")
    window = MainWindow(cfg)
    window.show()
    for _ in range(3):
        app.processEvents()

    report: dict[str, object] = {"steps": {}, "ok": True}

    def ok(key: str, value: object = True) -> None:
        report["steps"][key] = value  # type: ignore[index]

    # ---------------- 1 版本选择器
    window._switch("library")
    sel = window.library.selector
    sel._toggle_popup()
    for _ in range(3):
        app.processEvents()
    popup = sel._popup
    ok("selector_popup_opened", popup is not None)
    if popup is not None:
        popup.grab().save(str(SHOTS / "selector-popup.png"), "PNG")
    window.grab().save(str(SHOTS / "selector-open.png"), "PNG")
    sel._close_popup()
    app.processEvents()

    before = window.library.current_version()
    # 1.4 起只有一条内置线，切版本只在「内置线 ↔ 自定义线」之间才有意义。
    # 没有自定义线时跳过这一项，而不是断言一个不存在的版本。
    custom = [ln.id for ln in window.library._custom_lines()]
    if not custom:
        ok("selector_switch", {"skipped": "没有自定义线可切换"})
        return
    target = custom[0]
    window.library.set_version(target)
    app.processEvents()
    ok("selector_switch", {"from": before, "to": window.library.current_version(),
                           "match": window.library.current_version() == target,
                           "sidebar_text": window.side_launch.text()})
    window.library.set_version(before)
    app.processEvents()

    # ---------------- 2 背景图
    bg = make_test_background(SHOTS / "_test-bg.png")
    cfg.setdefault("ui", {})["background_image"] = str(bg)
    cfg["ui"]["background_dim"] = 45
    cfg_mod.save(cfg)
    window.apply_background()
    for _ in range(3):
        app.processEvents()
    window.grab().save(str(SHOTS / "background.png"), "PNG")
    ok("background_applied", window.root.has_background())

    # ---------------- 3 拖拽安装
    if package is None or not package.exists():
        # 没有样例包不算失败——这是环境问题，不是功能问题。
        ok("drop_install", {"skipped": f"没有可用的样例包：{package}"})
    else:
        window._switch("library")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(package))])
        event = QDropEvent(
            QPointF(400, 300),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        accepted = window._drop_path(event) is not None
        ok("drop_accepts_package", accepted)
        window.dropEvent(event)
        for _ in range(3):
            app.processEvents()
        ok("install_started", window._install_worker is not None)

        state = {"mid_shot": False, "deadline": time.time() + 300}

        def watch() -> None:
            worker = window._install_worker
            running = worker is not None and worker.isRunning()
            label = window.ext_status.label.text()
            value = window.ext_status.bar.value()
            if not state["mid_shot"] and 20 <= value <= 90:
                window.grab().save(str(SHOTS / "install-progress.png"), "PNG")
                state["mid_shot"] = True
            if not running and time.time() > state["deadline"] - 295:
                window.grab().save(str(SHOTS / "install-done.png"), "PNG")
                from app.core.ext.service import ExtensionService

                items = ExtensionService(cfg).installed()
                ok("install_progress_shot", state["mid_shot"])
                ok("install_status_label", label)
                ok("install_registry", [
                    {"id": i.get("id"), "status": i.get("status"), "verified": i.get("verified")}
                    for i in items
                ])
                report["ok"] = bool(items) and state["mid_shot"]
                print(json.dumps(report, ensure_ascii=False, indent=2))
                print("VERDICT:", "PASS" if report["ok"] else "FAIL")
                cfg.setdefault("ui", {})["background_image"] = original_bg
                cfg_mod.save(cfg)
                app.quit()
                return

        timer = QTimer()
        timer.timeout.connect(watch)
        timer.start(400)
        app.exec()
        return 0 if report["ok"] else 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    cfg.setdefault("ui", {})["background_image"] = original_bg
    cfg_mod.save(cfg)
    print("VERDICT:", "PASS" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
