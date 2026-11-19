"""PySide6 界面入口。

正常启动::

    _build_tools\\venv\\Scripts\\python.exe -m app.ui.pyside.app

自检（离屏渲染，不需要显示器，产出每页截图）::

    _build_tools\\venv\\Scripts\\python.exe -m app.ui.pyside.app --selftest
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 持有 QTranslator 引用：被 GC 掉的话翻译会立刻失效（Qt 只存指针）
_TRANSLATOR = None


def _install_qt_translator(app) -> str:
    """让 Qt 自带的按钮文案显示中文。

    ``QMessageBox`` 的标准按钮（Yes/No/OK/Cancel）文案由 Qt 翻译文件决定，
    默认是英文——中文界面里弹出一个 "Yes"/"No" 的框，既突兀也不像成品。
    加载 ``qtbase_zh_CN`` 后自动变成「是 / 否 / 确定 / 取消」。

    打包时由 ``机甲旋风兼容启动器.spec`` 把这两个 .qm 带进
    ``PySide6/translations``；找不到就静默跳过（功能不受影响，只是按钮是英文）。
    """
    global _TRANSLATOR
    from PySide6.QtCore import QLibraryInfo, QTranslator

    base = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(app)
    for name in ("qtbase_zh_CN", "qt_zh_CN"):
        if translator.load(name, base):
            app.installTranslator(translator)
            _TRANSLATOR = translator
            return name
    return ""


def _set_taskbar_identity() -> None:
    """给进程一个显式的 AppUserModelID，让任务栏按本程序而不是解释器分组。

    不设的话：从命令行跑（``python.exe -m app.ui.pyside.app``）时，任务栏
    会把窗口归到 Python 名下、显示 Python 的图标；打包后虽然通常能从 exe
    内嵌资源取到图标，但显式设一次更稳，也不依赖 Windows 的路径推断。

    必须在建窗口之前调用——窗口一旦创建，任务栏按钮就已经归属好了。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "JJXF.CompatLauncher"
        )
    except Exception:  # noqa: BLE001 - 拿不到不影响启动，只是分组可能不理想
        pass


def run(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    # 构建期版本自检：把本 exe 烧进去的 edition 与可用档位写成一个 JSON。
    # 冻结成 windowed exe 后没有 stdout，所以走文件；发布前用它核对
    # 「玩家版只有两档 / 开发者版三档」确实生效，而不是只看包里躺着的常量。
    if "--edition-info" in argv:
        from ... import edition

        from . import features

        index = argv.index("--edition-info")
        target = Path(argv[index + 1]) if index + 1 < len(argv) else ROOT / "logs" / "edition-info.json"
        payload = {
            "edition": edition.EDITION,
            "label": edition.label(),
            "modes": list(features.available_modes()),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    selftest = "--selftest" in argv
    shots_dir = ROOT / "logs" / "ui-shots"
    for i, item in enumerate(argv):
        if item == "--shots" and i + 1 < len(argv):
            shots_dir = Path(argv[i + 1])
        if item == "--theme" and i + 1 < len(argv):
            argv[i + 1] = argv[i + 1]

    if selftest:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from ... import fatal
    from ...core import config as cfg_mod
    from . import colors, theme
    from .assets import chevron
    from .fonts import ensure_cjk
    from .main_window import MainWindow

    # Qt 槽函数里抛的异常默认只写 stderr —— windowed exe 下 stderr 是 None，
    # 等于整条信息蒸发。必须在 Qt 起来之前接管，才能覆盖建窗口期的报错。
    fatal.install_qt_message_handler()

    # 必须在 QApplication 之前：Qt 初始化时会向任务栏注册按钮，
    # 那时 AppUserModelID 已经生效，图标才会是本程序的。
    _set_taskbar_identity()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("机甲旋风兼容启动器")
    palette_name = "light" if "--light" in argv else "dark"
    palette = theme.LIGHT if palette_name == "light" else theme.DARK
    # 先设调色板：QSS 没管到的容器（滚动区里的页面容器、日志抽屉等）
    # 会回退到调色板，默认调色板是浅色，会露出大片浅灰底。
    app.setPalette(theme.qpalette(palette))
    app.setStyleSheet(theme.qss(palette, chevron(palette.text_dim)))
    # 自绘控件的取色（徽标、日志、表格前景）跟着所选主题走，
    # 必须在建窗口之前设好，否则它们会用默认深色配色。
    colors.set_palette(palette)
    translator = _install_qt_translator(app)
    family = ensure_cjk(app)
    if selftest:
        print(f"[selftest] 字体：{family or '（未找到中文字体，中文会显示为方框）'}")
        print(f"[selftest] 翻译：{translator or '（未加载，标准按钮会是英文）'}")

    cfg = cfg_mod.load()
    # console=False：打包成 windowed exe 后 sys.stderr 是 None，
    # 挂 StreamHandler 会直接抛 AttributeError；界面日志走文件即可。
    # 必须在建窗口前初始化，否则窗口构造期间打的日志不会被落盘。
    from ...core import logging as logging_mod

    logging_mod.setup(str(cfg.get("log_level") or "INFO"), console=False)

    # 把 edition / build id 写进日志第一屏：用户报问题时，先看他跑的是哪一次构建。
    from ... import __version__, edition

    logging_mod.get("app").info(
        "启动器 v%s · %s · build %s",
        __version__,
        edition.label(),
        edition.build_id(),
    )

    # 启动清扫：把上次运行留下的工作期缓存收一遍（失败现场留 7 天 / 200 MB）。
    # 放在建窗口之前，界面日志区还没建好也不会丢——写的是文件日志。
    try:
        from ...core import housekeeping

        report = housekeeping.sweep()
        if report.removed or report.failures:
            logging_mod.get("housekeeping").info("启动清扫：%s", report.message())
    except Exception as exc:  # noqa: BLE001 - 清扫失败绝不能挡住启动
        logging_mod.get("housekeeping").warning("启动清扫失败：%s", exc)

    # 首启须知：必须在 ``MainWindow`` **之前**。
    #
    # 先建主界面再弹的话，用户点「不同意」会看见主窗口先闪一下再被销毁 ——
    # 既难看，也不符合「不同意即禁止使用」的语义。
    #
    # selftest 必须跳过：否则 ``--selftest`` 会卡在这个模态框上，
    # 自动截图与 CI 全部挂住。
    if not selftest and not _notice_accepted(cfg):
        if not _ask_notice(cfg):
            logging_mod.get("app").info("用户未同意使用前须知，退出。")
            return 0          # 正常退出码：不是崩溃，是用户选择
        cfg.setdefault("ui", {})["notice_ack"] = _notice_version()
        try:
            cfg_mod.save(cfg)
        except OSError as exc:
            # 保存失败不该挡住使用：下次启动会再问一遍而已
            logging_mod.get("app").warning("保存须知确认失败：%s", exc)

    window = MainWindow(cfg)
    window.show()

    if not selftest:
        _warn_if_mixed_install(window)
        return app.exec()

    return _selftest(app, window, shots_dir)


def _notice_version() -> str:
    """当前条款版本号。与对话框里的一致，单独取值避免多一层 import。"""
    from .notice_dialog import NOTICE_VERSION

    return NOTICE_VERSION


def _notice_accepted(cfg: dict) -> bool:
    """用户是否已同意**当前版本**的使用前须知。

    存的是版本字符串：``"1"`` = 同意过第 1 版。条款升级到 ``"2"`` 后，
    老用户会重新看到新条款 —— 这是用字符串而非 bool 的全部意义。
    """
    acked = str((cfg.get("ui") or {}).get("notice_ack") or "")
    return acked == _notice_version()


def _ask_notice(cfg: dict) -> bool:
    """弹首启须知，返回用户是否同意。

    独立成函数而不是内联，是因为 ``app.py::main`` 已经很长，
    再塞一段 20 行的对话框逻辑会看不清启动主流程。
    """
    from PySide6.QtWidgets import QDialog

    from .notice_dialog import FirstRunNotice

    dlg = FirstRunNotice()
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    dlg.deleteLater()
    return accepted


def _warn_if_mixed_install(parent) -> None:
    """包根 BUILD_INFO 与 exe 内烧的 build id 不一致时，明确报「混合安装」。

    解压工具遇到同名目录是合并覆盖，不会先清空。新 exe 配旧 ``_internal``
    （或反过来）能启动，但行为会随机回到上一版——这是用户最难自查的一类
    问题。与其让它带病运行，不如在启动时把两个 build id 摆出来。
    """
    from ... import edition

    info_path = ROOT / "BUILD_INFO.json"
    if not info_path.is_file():
        return
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    expected = str(info.get("build_id") or "")
    actual = edition.build_id()
    if not expected or actual in ("dev", "unknown") or expected == actual:
        return

    from PySide6.QtWidgets import QMessageBox

    QMessageBox.warning(
        parent,
        "检测到混合安装",
        "这个文件夹里的主程序和版本说明不是同一次构建，"
        "通常是解压时直接覆盖了旧目录。\n\n"
        f"说明文件：{expected}\n"
        f"实际运行：{actual}\n\n"
        "请把整个旧文件夹删掉，再重新解压本次发布包。",
    )


def _selftest(app, window, shots_dir: Path) -> int:
    """离屏跑一遍所有页面，抓图并打印结构信息，作为可重复的界面回归手段。"""
    from .main_window import NAV_ITEMS

    shots_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        app.processEvents()

    report: dict[str, object] = {
        "window": {"title": window.windowTitle(), "size": [window.width(), window.height()]},
        "pages": [],
        "shots": [],
    }
    for key, _text, _icon in NAV_ITEMS:
        window._switch(key)
        for _ in range(3):
            app.processEvents()
        page = window.pages[key]
        shot = shots_dir / f"{key}.png"
        window.grab().save(str(shot), "PNG")
        widgets = len(page.findChildren(object))
        report["pages"].append({"key": key, "widgets": widgets})  # type: ignore[union-attr]
        report["shots"].append(str(shot))

    report["log_lines"] = window.log_view.blockCount() if hasattr(window, "log_view") else 0
    print(json.dumps(report, ensure_ascii=False, indent=2))
    window.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
