"""致命异常兜底：保证「启动失败」永远有一条玩家看得见的出口。

为什么需要
----------
启动器打包成 **windowed** exe（``console=False``）。Windows 下这类进程没有
控制台、``sys.stderr`` 是 ``None``，PyInstaller 的
``disable_windowed_traceback=False`` 也只能把 traceback 写到一个没人看的
地方。后果是：

    启动期任何未捕获异常 → 进程静默退出 → 玩家看到「双击没反应」

2026-11-14 实测复现：把 ``config/launcher.json`` 写成半截 JSON，
打包版以 ``rc=1`` 退出，**没有窗口、没有提示、日志里也没有那一行**。

设计要点
--------
1. **先写日志文件，再想办法弹窗**。日志是唯一在所有失败模式下都还能落地的
   通道（Qt 起不来时也不能依赖 Qt）。
2. **弹窗按「有没有 Qt」分两条路**：能用 Qt 就用 QMessageBox（样式统一），
   不能就用 Win32 ``MessageBoxW``（零依赖，一定弹得出来）。
3. **绝不吞掉异常**：写完日志、弹完窗之后仍然返回失败码，让退出码如实反映
   问题，方便用户按 Ctrl+C 之外的方式排查。
4. **不递归**：兜底自身出错时只往日志里追加一行，不再尝试弹窗，避免
   异常处理器自己炸出二级异常把原始信息盖掉。
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

#: 兜底模块自己的日志文件名。刻意不依赖 ``app.core.logging``：
#: 那个模块本身就可能在「配置读不出来」时还没初始化好。
_FALLBACK_LOG = "crash.log"


def _root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _log_dir() -> Path:
    """崩溃日志目录：``<root>/logs``。

    用的是与正常日志同一个目录——玩家报问题时只需要看一个地方。
    建不出来（安装目录只读）时退到临时目录，绝不因为写不了日志再崩一次。
    """
    target = _root() / "logs"
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        import tempfile

        return Path(tempfile.gettempdir())


def write_crash_log(exc: BaseException, *, context: str = "") -> Path | None:
    """把异常详情追加到 ``logs/crash.log``，返回日志路径（失败返回 ``None``）。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = [
        "=" * 70,
        f"[{stamp}] {context or '启动期未捕获异常'}",
        f"python  : {sys.version.split()[0]}",
        f"frozen  : {getattr(sys, 'frozen', False)}",
        f"exe     : {sys.executable}",
        "-" * 70,
    ]
    body = traceback.format_exception(type(exc), exc, exc.__traceback__)
    payload = "\n".join(header) + "\n" + "".join(body) + "\n"
    try:
        path = _log_dir() / _FALLBACK_LOG
        with path.open("a", encoding="utf-8") as f:
            f.write(payload)
        return path
    except OSError:
        return None


def _message(exc: BaseException, log_path: Path | None) -> str:
    name = type(exc).__name__
    text = str(exc).strip() or "（无详细信息）"
    lines = [
        "启动器启动失败。",
        "",
        f"{name}: {text}",
        "",
    ]
    if log_path is not None:
        lines += [f"详细记录已写入：{log_path}", ""]
    lines += [
        "可以这样处理：",
        "1. 删除 config/launcher.json 后重新打开（会恢复默认设置，",
        "   已装的扩展不受影响）；",
        "2. 如果仍然打不开，请把上面那个日志文件发给开发者。",
    ]
    return "\n".join(lines)


def show_error(exc: BaseException, *, context: str = "") -> None:
    """记录 + 尽最大努力弹一个框。

    两条路都失败（例如无桌面会话、自动化环境）时静默返回，调用方照常
    返回失败码——不能因为「提示不出来」而让进程卡在这里。
    """
    log_path = write_crash_log(exc, context=context)
    text = _message(exc, log_path)

    # 路 1：Qt 可用且已有 QApplication —— 风格统一，可选中复制。
    if _try_qt(text):
        return
    # 路 2：Win32 原生弹窗，零依赖。
    if _try_win32(text):
        return
    # 路 3：都没有（headless / 无桌面）时写 stderr（可能为 None）。
    try:
        if sys.stderr is not None:
            sys.stderr.write(text + "\n")
    except Exception:  # noqa: BLE001 - 兜底自身不许再抛
        pass


def _try_qt(text: str) -> bool:
    """有 QApplication 就用 QMessageBox。没有就返回 False，不主动创建。"""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except Exception:  # noqa: BLE001 - 没装 / 导入失败都走 Win32
        return False
    app = QApplication.instance()
    if app is None:
        return False
    try:
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("启动器启动失败")
        box.setText(text)
        box.exec()
        return True
    except Exception:  # noqa: BLE001
        return False


def _try_win32(text: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        MB_OK = 0x00000000
        MB_ICONERROR = 0x00000010
        MB_TOPMOST = 0x00040000
        ctypes.windll.user32.MessageBoxW(
            None, text, "启动器启动失败", MB_OK | MB_ICONERROR | MB_TOPMOST
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def install_excepthook() -> None:
    """把 ``sys.excepthook`` 换成「写日志 + 弹窗」版。

    只覆盖主线程未捕获异常。Qt 事件循环里的异常走
    :func:`install_qt_message_handler`——两者缺一不可：前者管启动期
    （还没进事件循环），后者管运行期（槽函数里抛）。
    """
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        # KeyboardInterrupt 是用户主动中断，不该弹窗打扰
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        show_error(exc, context="未捕获异常")

    sys.excepthook = hook


def install_qt_message_handler() -> None:
    """让 Qt 槽函数里抛的异常也进崩溃日志。

    PySide6 默认把槽函数里的异常打到 stderr —— 而 windowed exe 的
    stderr 是 ``None``，等于整条信息蒸发。这里接管 ``qInstallMessageHandler``，
    把 QtCriticalMsg 及以上追加到同一个 ``crash.log``。
    """
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # noqa: BLE001
        return

    def handler(mode, context, message):
        # 只关心严重级别；QtDebug/QtInfo 走正常日志，不进崩溃文件
        if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            try:
                path = _log_dir() / _FALLBACK_LOG
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                where = ""
                if context is not None and getattr(context, "file", ""):
                    where = f" ({context.file}:{context.line})"
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"[{stamp}] [Qt] {message}{where}\n")
            except Exception:  # noqa: BLE001 - 写日志失败不能反过来炸 Qt
                pass
        # 继续走默认行为（有控制台时仍能看到）
        try:
            if sys.stderr is not None:
                sys.stderr.write(f"[Qt] {message}\n")
        except Exception:  # noqa: BLE001
            pass

    try:
        qInstallMessageHandler(handler)
    except Exception:  # noqa: BLE001
        pass


def guard(entry, *args, **kwargs):
    """包住一个入口函数：异常时弹窗 + 返回失败码，而不是让进程静默死。

    用法（``build_launcher.py`` / ``app.main``）::

        raise SystemExit(fatal.guard(main))
    """
    try:
        return entry(*args, **kwargs)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 这里就是要兜住一切
        show_error(exc, context="启动失败")
        return 1