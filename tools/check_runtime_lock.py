"""验证运行期锁：真实启动游戏 → 扩展启停必须被拒 → 退出后恢复。

不用「造一个假进程名」来测，而是真的用启动器拉起 4.6，因为要验证的正是
「玩家点了启动、游戏在跑」这个真实状态。

注意：输出必须写文件，不能用 PIPE 不读（管道写满会把游戏卡死，踩过）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["JJXF_ROOT"] = str(ROOT / "dist" / "机甲旋风兼容启动器")
sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import runtime_lock, runner  # noqa: E402
from app.core.ext import enable as ext_enable  # noqa: E402
from app.core.ext.service import ExtensionService  # noqa: E402

RUN_DIR = ROOT / "builds" / "_lock_test" / "run46"


def main() -> int:
    cfg = cfg_mod.load()
    run_dir = RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)

    # 先用真实流程组一份 packset，再以窗口模式起进程（headless 会立刻退出）
    res = ext_enable.build_for_launch(cfg, "4.6", run_dir)
    base = cfg["baselines"]["jjxf.v1.45"]["path"]
    packs = [{"path": base, "offset": 104599040, "role": "baseline"}]
    for p in res.packs:
        packs.append({"path": str(Path(p["path"]).resolve()), "offset": 0, "role": p["role"]})
    (run_dir / "packset.json").write_text(
        json.dumps({"schema": 1, "packs": packs}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cmd = runner.build_command(
        cfg, "4.6", packset=run_dir / "packset.json",
        main_scene="res://scenes/start_page.tscn", smoke=False, headless=False,
        log_path=run_dir / "mount.json", mods_path=run_dir / "mods.json",
    )
    env, _ = runner.build_env(cfg, "locktest46")
    log_file = (run_dir / "game.log").open("wb")
    proc = subprocess.Popen(cmd, cwd=str(Path(cmd[0]).parent), env=env,
                            stdout=log_file, stderr=subprocess.STDOUT)

    report: dict[str, object] = {}
    try:
        # 等游戏窗口起来（最多 25 秒）
        for _ in range(50):
            time.sleep(0.5)
            if runtime_lock.is_game_running():
                break
            if proc.poll() is not None:
                break

        running = runtime_lock.is_game_running()
        pids = runtime_lock.running_engine_pids()
        report["game_running"] = running
        report["pids"] = pids
        report["describe"] = runtime_lock.describe_running()

        svc = ExtensionService(cfg)
        target = "mch.sell_all"
        before = bool((svc.get(target) or {}).get("enabled", True))

        blocked = False
        message = ""
        if running:
            try:
                svc.set_enabled(target, not before)
            except runtime_lock.ModLockedError as exc:
                blocked = True
                message = str(exc).splitlines()[0]
            except Exception as exc:  # noqa: BLE001
                message = f"非预期异常：{exc}"
        report["set_enabled_blocked"] = blocked
        report["block_message"] = message
        after = bool((svc.get(target) or {}).get("enabled", True))
        report["state_unchanged"] = before == after
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_file.close()

    # 退出后应恢复可操作
    for _ in range(20):
        if not runtime_lock.is_game_running():
            break
        time.sleep(0.5)
    report["after_exit_running"] = runtime_lock.is_game_running()
    svc = ExtensionService(cfg)
    target = "mch.sell_all"
    orig = bool((svc.get(target) or {}).get("enabled", True))
    try:
        svc.set_enabled(target, not orig)
        svc.set_enabled(target, orig)   # 立刻还原，别留下副作用
        report["after_exit_modifiable"] = True
    except Exception as exc:  # noqa: BLE001
        report["after_exit_modifiable"] = f"仍被拒：{exc}"

    print(json.dumps(report, ensure_ascii=False, indent=1))
    ok = (
        report.get("game_running") is True
        and report.get("set_enabled_blocked") is True
        and report.get("state_unchanged") is True
        and report.get("after_exit_running") is False
        and report.get("after_exit_modifiable") is True
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())