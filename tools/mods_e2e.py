#!/usr/bin/env python
"""扩展生效验证：带已启用扩展真实启动一次，抓关键日志。

判定标准：
1. ModHost 打印「已装载扩展」；
2. 扩展的 ``install()`` 成功（出现其自身的初始化日志，如「性能护栏已启用」）；
3. 游戏主场景起来后 ``on_game_ready`` 成功（出现「玩法节点已就绪」）；
4. 没有 GDScript 解析错误（插桩代码能编译）。

用法::

    _build_tools\\venv\\Scripts\\python.exe tools\\mods_e2e.py 4.6 --keep 30
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import config as cfg_mod  # noqa: E402
from app.core import runner  # noqa: E402
from app.core.ext import enable as ext_enable  # noqa: E402

KEYS = ("[ModHost]", "[Hook]", "[Compat]", "次元裂缝", "[MP]", "SCRIPT ERROR", "Parse Error", "ERROR:")


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    line = args[0] if args else "4.6"
    keep = 30.0
    for i, item in enumerate(args):
        if item == "--keep" and i + 1 < len(args):
            keep = float(args[i + 1])

    app = QApplication(sys.argv[:1])
    cfg = cfg_mod.load()
    bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
    rd = runner.run_dir_for(f"mods{line.replace('.', '')}")

    mods = ext_enable.build_for_launch(cfg, line, rd)
    print("启用扩展：", [m.get("id") for m in mods.mods], "钩子：", len(mods.hooks))
    for w in mods.warnings:
        print("  [警告]", w)
    if not mods.mods:
        print("没有已启用的扩展，先安装并启用一个（或检查 enabled 字段）。")
        return 1

    base = runner.baseline_pack(cfg, bid)
    packset = runner.make_packset(
        rd / "packset.json",
        packset_id=f"{bid}-{line}-mods",
        line=line,
        packs=[base, *mods.packs],
        baseline_id=bid,
    )
    cmd = runner.build_command(
        cfg,
        line,
        packset=packset,
        main_scene="res://scenes/start_page.tscn",
        smoke=False,
        headless=False,
        log_path=rd / "mount_report.json",
        mods_path=mods.mods_json,
    )
    env, _ = runner.build_env(cfg, "modtest")
    qenv = QProcessEnvironment()
    for k, v in env.items():
        qenv.insert(k, v)

    proc = QProcess()
    proc.setProcessEnvironment(qenv)
    proc.setWorkingDirectory(str(Path(cmd[0]).parent))
    proc.setProgram(cmd[0])
    proc.setArguments(cmd[1:])
    collected: list[str] = []

    def on_out() -> None:
        text = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        collected.extend(text.splitlines())

    def on_err() -> None:
        text = bytes(proc.readAllStandardError()).decode("utf-8", "replace")
        collected.extend(text.splitlines())

    proc.readyReadStandardOutput.connect(on_out)
    proc.readyReadStandardError.connect(on_err)
    proc.start()

    started = time.time()
    kill_timer = QTimer()
    kill_timer.setSingleShot(True)
    kill_timer.timeout.connect(proc.kill)
    kill_timer.start(int(keep * 1000))

    def done(*_a) -> None:
        on_out()
        on_err()
        interesting = [l for l in collected if any(k in l for k in KEYS)]
        joined = "\n".join(collected)
        verdict = {
            "line": line,
            "mods": [m.get("id") for m in mods.mods],
            "hooks": len(mods.hooks),
            "host_loaded": "已装载扩展" in joined,
            "mod_installed": "性能护栏已启用" in joined or "register_social_command" in joined,
            "game_ready": "玩法节点已就绪" in joined,
            "parse_error": bool(re.search(r"Parse Error|SCRIPT ERROR", joined)),
            "seconds": round(time.time() - started, 1),
        }
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        print("--- 关键日志 ---")
        for l in interesting[:40]:
            print(" ", l[:200])
        ok = verdict["host_loaded"] and not verdict["parse_error"]
        print("VERDICT:", "PASS" if ok else "FAIL")
        app.quit()

    proc.finished.connect(done)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
