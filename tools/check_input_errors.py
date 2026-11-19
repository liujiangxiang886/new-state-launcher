"""窗口模式 + 真实键盘输入：抓「只在有输入时才炸」的扩展错误。

为什么单独需要它：``check_game_window.py`` 只开窗、等几秒、关窗，
**不产生任何输入事件**。而很多扩展的坑就埋在 ``_input`` / ``_unhandled_input``
里——实测 ``mch.sell_all`` 的 ``_input`` 首行是 ``if not _dlg.visible``，
而 ``_dlg`` 要到 ``on_game_ready`` 才创建；只要玩家按一次键就刷一条
``Invalid access to property or key 'visible' on a base object of type 'Nil'``。
这类错误不模拟输入就永远测不出来。

做法：开窗 → 抢前台 → 用 SendInput 连续发方向键/空格/ESC/字母 →
收集完整 stderr → 断言没有 SCRIPT ERROR。存档走独立 profile。

用法::

    python tools/check_input_errors.py 4.6
    python tools/check_input_errors.py 4.6 --keys 40
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import runner  # noqa: E402
from app.core.ext import enable as enable_mod  # noqa: E402

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

#: 一串「不会真的伤到存档」的按键：方向键、空格、字母、ESC
#: 不按回车/删除这类会确认弹窗、删东西的键。
VK_POOL = (0x25, 0x26, 0x27, 0x28, 0x20, 0x41, 0x42, 0x1B)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def send_key(vk: int) -> None:
    for flags in (0, KEYEVENTF_KEYUP):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def find_window(pid: int) -> int:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid_owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_owner))
        if pid_owner.value == pid:
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if rect.right > rect.left and rect.bottom > rect.top:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def run_line(cfg: dict, line: str, wait: float, key_count: int) -> dict:
    rd = runner.run_dir_for(f"input{line.replace('.', '')}")
    launch = enable_mod.build_for_launch(cfg, line, rd)
    bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
    # 基线必须排第一：不挂基线，游戏自己的 scripts/*.gd 全都找不到，
    # autoload 逐个实例化失败，于是后面所有 SCRIPT ERROR 都是我们自己
    # 造成的假阳性——会把「工具写错」误报成「启动器有 bug」。
    packs = [runner.baseline_pack(cfg, bid), *launch.packs]
    packset = runner.make_packset(
        rd / "packset.json",
        packset_id=f"{bid}-{line}-input",
        line=line,
        packs=packs,
        baseline_id=bid,
    )
    mount_report = rd / "mount_report.json"
    cmd = runner.build_command(
        cfg,
        line,
        packset=packset,
        main_scene="res://scenes/start_page.tscn",
        smoke=False,
        headless=False,
        log_path=mount_report,
        mods_path=launch.mods_json,
    )
    env, _ = runner.build_env(cfg, "gamediag")

    # stdout/stderr 必须落文件：给窗口进程接没人读的管道会在 64KB 后阻塞
    log_path = rd / "input.log"
    with log_path.open("wb") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(cmd[0]).parent),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    hwnd = 0
    deadline = time.time() + wait
    while time.time() < deadline:
        hwnd = find_window(proc.pid)
        if hwnd:
            break
        time.sleep(0.2)

    out = {"line": line, "window": bool(hwnd), "run_dir": rd.as_posix(), "errors": []}
    if not hwnd:
        proc.terminate()
        out["errors"] = ["窗口未出现"]
        return out

    time.sleep(1.5)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    for i in range(key_count):
        if proc.poll() is not None:
            break
        send_key(VK_POOL[i % len(VK_POOL)])
        time.sleep(0.06)

    time.sleep(2.0)
    if proc.poll() is None:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    text = log_path.read_text(encoding="utf-8", errors="replace")
    errors = [ln.strip() for ln in text.splitlines() if "SCRIPT ERROR" in ln]
    out["errors"] = errors[:6]
    out["ok"] = not errors
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lines", nargs="+", choices=[cfg_mod.BUILTIN_LINE])
    ap.add_argument("--wait", type=float, default=45.0)
    ap.add_argument("--keys", type=int, default=30)
    args = ap.parse_args(argv)

    cfg = cfg_mod.load()
    failed = 0
    for line in args.lines:
        print(f"\n=== {line} 窗口 + 键盘输入 ===")
        res = run_line(cfg, line, args.wait, args.keys)
        print(f"窗口={res['window']}  运行目录={res['run_dir']}")
        if res["window"]:
            print(f"发送 {args.keys} 次按键后收集到 {len(res['errors'])} 条脚本错误")
            for e in res["errors"]:
                print("  !! " + e)
        print(("[PASS] " if res.get("ok") else "[FAIL] ") +
              ("输入期间无脚本错误" if res.get("ok") else "输入期间存在脚本错误"))
        failed += 0 if res.get("ok") else 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
