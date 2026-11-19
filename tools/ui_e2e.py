#!/usr/bin/env python
"""界面端到端回归：用 UI 的启动路径真拉起游戏，验证挂载成功后自动收尾。

它会短暂打开真实游戏窗口（不是离屏），跑完自动结束进程并退出，用于人工回归：

    _build_tools\\venv\\Scripts\\python.exe tools\\ui_e2e.py 4.6
    _build_tools\\venv\\Scripts\\python.exe tools\\ui_e2e.py 4.6 --keep 40

判定标准（全部满足才算通过）：

1. ``QProcess`` 成功启动（无 FailedToStart）；
2. 运行目录下的 ``mount_report.json`` 出现且 ``ok=true``；
3. 进程被正常结束，``finished`` 摘要里 ``killed`` 或 ``ok`` 至少一个成立。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import config as cfg_mod  # noqa: E402
from app.ui.pyside import theme  # noqa: E402
from app.ui.pyside.assets import chevron  # noqa: E402
from app.ui.pyside.fonts import ensure_cjk  # noqa: E402
from app.ui.pyside.main_window import MainWindow  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    line = args[0] if args else "4.6"
    keep = 45.0
    for i, item in enumerate(args):
        if item == "--keep" and i + 1 < len(args):
            keep = float(args[i + 1])

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss(theme.DARK, chevron(theme.DARK.text_dim)))
    ensure_cjk(app)

    cfg = cfg_mod.load()
    window = MainWindow(cfg)
    window.show()

    result: dict[str, object] = {"line": line, "started": False, "mounted": False, "finished": None}
    state = {"stop_sent": False}
    started_at = time.time()

    def trigger() -> None:
        window._launch(line)
        result["started"] = window.proc.running

    def watch() -> None:
        run_dirs = sorted((ROOT / "builds").glob(f"run{line.replace('.', '')}-*"), key=lambda p: p.stat().st_mtime)
        report = run_dirs[-1] / "mount_report.json" if run_dirs else None
        mounted = False
        if report and report.exists():
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
                mounted = bool(data.get("ok")) and bool(data.get("mounted"))
            except (OSError, json.JSONDecodeError):
                mounted = False
        result["mounted"] = mounted

        elapsed = time.time() - started_at
        if mounted and window.proc.running and not state["stop_sent"]:
            state["stop_sent"] = True
            window.log("挂载成功，准备结束进程（回归测试）。", "ok")
            window.proc.stop()
            return
        if not window.proc.running and elapsed > 3:
            return
        if elapsed > keep and not state["stop_sent"]:
            state["stop_sent"] = True
            window.log(f"达到 {keep:.0f}s 上限，结束进程。", "warn")
            window.proc.stop()

    def on_finished(summary: dict) -> None:
        result["finished"] = summary
        result["log_tail"] = window.log_view.toPlainText().splitlines()[-6:]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        ok = bool(result["mounted"]) and summary.get("returncode") is not None
        print("VERDICT:", "PASS" if ok else "FAIL")
        QTimer.singleShot(200, app.quit)

    window.proc.finished.connect(on_finished)

    QTimer.singleShot(600, trigger)
    timer = QTimer()
    timer.timeout.connect(watch)
    timer.start(500)

    app.exec()
    return 0 if result.get("mounted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
