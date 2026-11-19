"""程序入口：图形界面（PySide6）与命令行。

    python -m app.main pyside       # 图形界面（默认）
    python -m app.main cli stat     # 命令行
    python -m app.main              # 不带参数时等同 pyside

打包入口见 ``build_launcher.py``（PyInstaller 以脚本方式调用 :func:`main`）。
"""

from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 可用后端。图形界面是主路径，CLI 保留给脚本与排障。
BACKENDS = ("auto", "pyside", "cli")


def _run_pyside(argv: list[str]) -> int:
    from .ui.pyside.app import run

    return run(argv)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    # 一次性迁移：打包时若漏改 ``profiles.default``，玩家机上的档位名会停在
    # 开发期那个（如 ``smoke46``）。迁移会把目录一起改名 —— 只改配置不改
    # 目录的话，玩家进游戏会发现角色"消失"了。
    #
    # 放在最前面、并且吃掉所有异常：迁移失败最多是名字难看，
    # 不该把启动器本身卡在启动阶段。
    try:
        from .core import profiles as profiles_mod

        profiles_mod.migrate_legacy_default()
    except Exception:  # noqa: BLE001
        pass

    if args and args[0] == "cli":
        from . import cli

        return cli.main(args[1:])

    if args and args[0] in ("pyside", "auto", "--gui"):
        return _guarded_pyside(args[1:])

    # 诊断入口：冻结 exe 也要能跑，所以必须在「未知参数 → 用法」之前分流。
    if args and args[0] in ("--selftest", "--edition-info"):
        return _guarded_pyside(args)

    if not args:
        # 双击 exe / 直接 python -m app.main：进图形界面
        return _guarded_pyside([])

    print(
        "用法：\n"
        "  python -m app.main            图形界面\n"
        "  python -m app.main pyside     图形界面\n"
        "  python -m app.main cli --help 命令行用法",
        file=sys.stderr,
    )
    return 2


def _guarded_pyside(argv: list[str]) -> int:
    """图形界面的统一入口：任何未捕获异常都变成「日志 + 弹窗」。

    为什么必须包：打包版是 windowed exe，``sys.stderr`` 为 ``None``，
    异常直接让进程静默退出，玩家看到的是「双击没反应」。兜底实现在
    :mod:`app.fatal`，这里只负责挂上去并保证退出码如实反映失败。
    """
    from . import fatal

    fatal.install_excepthook()
    return fatal.guard(_run_pyside, argv)


if __name__ == "__main__":
    raise SystemExit(main())
