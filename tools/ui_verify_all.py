"""离屏 UI 自检：真的点一下「整体校验」，验证按钮 → 线程 → 日志 的完整链路。

为什么需要这个：核心函数 ``verify_line`` 有单测，界面自检 ``ui_check.py``
只验排版。但用户报的故障恰恰发生在**两者之间**——按钮回调里按路径加载
``tools/verify_mods.py``，打包后该文件不存在，于是点一下必崩，而核心函数
和排版检查都发现不了。

本脚本按用户的操作顺序走一遍：造窗口 → 切到扩展页 → 调 ``_verify_all()``
→ 等 worker 结束 → 断言日志里出现结论行与事件订阅行。

用法::

    QT_QPA_PLATFORM=offscreen python tools/ui_verify_all.py
    QT_QPA_PLATFORM=offscreen python tools/ui_verify_all.py --line 4.6
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import config as cfg_mod  # noqa: E402
from app.ui.pyside.pages.extensions import ExtensionsPage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", default="4.6", choices=["4.6"])
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv)
    cfg = cfg_mod.load()
    lines: list[tuple[str, str]] = []

    def collect(text: str, level: str = "info") -> None:
        lines.append((level, str(text)))
        print(f"[{level}] {text}")

    page = ExtensionsPage(cfg, collect)
    page.set_current_line(args.line)
    page.resize(1180, 760)
    page.show()

    # 确认按钮存在且可点
    assert page.verify_all_btn.isEnabled(), "整体校验按钮一开始就是禁用的"
    print(f"--- 点击「整体校验」，目标线 {args.line} ---")
    page._verify_all()

    # 手动转事件循环等 worker 结束（不能阻塞在 worker.wait()，
    # 否则 done 信号排不到主线程，_on_verify_all_done 永远不执行）
    worker = getattr(page, "_verify_worker", None)
    if worker is None:
        print("!! 没有创建 worker，按钮回调没跑起来")
        return 1

    started = time.time()
    while worker.isRunning() and time.time() - started < args.timeout:
        app.processEvents()
        time.sleep(0.05)
    # 再转几轮，把排队的 done/finished 信号消化掉
    for _ in range(200):
        app.processEvents()
        time.sleep(0.01)
        if page.verify_all_btn.isEnabled():
            break

    text = "\n".join(t for _l, t in lines)
    failures: list[str] = []

    if "整体校验通过" not in text and "整体校验未通过" not in text:
        failures.append("日志里没有出现整体校验的结论行")
    if "整体校验失败" in text:
        failures.append("整体校验抛异常了（按钮链路没走通）")
    if "FileNotFoundError" in text:
        failures.append("仍然出现 FileNotFoundError（tools/ 依赖没清掉）")
    if "已订阅游戏事件" not in text:
        failures.append("没有输出「已订阅游戏事件」——事件总线没生效")
    if not page.verify_all_btn.isEnabled():
        failures.append("校验结束后按钮没有恢复可用")

    print("\n=== 结果 ===")
    if failures:
        for f in failures:
            print("!! " + f)
        return 1
    print("通过：按钮链路、结论行、事件订阅、按钮复位 全部正常")
    print(f"耗时约 {time.time() - started:.1f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())