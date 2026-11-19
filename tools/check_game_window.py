"""真实窗口启动验证：「点开始游戏」能不能真的出画面。

为什么不能只信 headless 冒烟：用户报的是**窗口模式**进不去。headless 与窗口
模式的命令只差一个 ``--headless``，路径解析逻辑相同，但那只是推理；这个工具
直接开窗，用四条硬证据判定：

1. 游戏进程真的创建了可见窗口（EnumWindows 按 pid 过滤，标题为空也算）；
2. ``mount_report.json`` 里 ``ok=true`` 且挂载条目数对得上；
3. ``mod_report.json`` 里每个声明的扩展都 ``loaded``；
4. 日志里没有 GDScript 解析/运行错误。

**输出必须写文件，不能接管道。** 实测（4.6）给窗口进程接一个没人读的
stdout 管道，写满 64KB 后子进程会阻塞在写日志上，窗口永远出不来——
这会把「能进游戏」误判成「卡死」。所以这里 stdout/stderr 一律落到
``run_dir/window.log``，跑完再读。

跑完给窗口发 ``WM_CLOSE``（等同点右上角），走正常退出链路，不留下孤儿进程。
存档走独立 profile，不碰玩家存档。

用法::

    python tools/check_game_window.py 4.6
    python tools/check_game_window.py 4.6 --wait 40
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
from app.core import smoke as smoke_mod  # noqa: E402
from app.core.ext import enable as enable_mod  # noqa: E402

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010


def find_window(pid: int) -> tuple[int, str, int, int]:
    """按 pid 找第一个可见且有尺寸的顶层窗口，返回 (hwnd, 标题, 宽, 高)。

    标题可能为空（Godot 建窗后、设置标题前），所以**不能用标题判断有没有窗口**。
    """
    found: list[tuple[int, str, int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w >= 320 and h >= 240:
            found.append((int(hwnd), buf.value, w, h))
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else (0, "", 0, 0)


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def check_line(cfg: dict, line: str, wait: float) -> dict:
    rd = runner.run_dir_for(f"win{line.replace('.', '')}")
    mods = enable_mod.build_for_launch(cfg, line, rd)
    expected = [str(m.get("id")) for m in mods.mods]
    bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
    packs = [runner.baseline_pack(cfg, bid), *mods.packs]
    packset = runner.make_packset(
        rd / "packset.json",
        packset_id=f"{bid}-{line}-window",
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
        mods_path=mods.mods_json,
    )
    env, prof_dir = runner.build_env(cfg, "gamediag")
    log_path = rd / "window.log"

    out: dict = {
        "line": line,
        "expected": expected,
        "packs": len(packs),
        "run_dir": str(rd),
        "profile": str(prof_dir),
        "hwnd": 0,
        "window": "",
        "size": "",
        "seconds": 0.0,
        "mount_ok": False,
        "mounted": 0,
        "mods_loaded": [],
        "mods_failed": [],
        "errors": [],
    }
    started = time.time()
    # 关键：输出落文件，绝不用无人读取的 PIPE（会写满阻塞，见模块 docstring）
    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(cmd[0]).parent),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        hwnd, title, w, h = 0, "", 0, 0
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            hwnd, title, w, h = find_window(proc.pid)
            if hwnd:
                break
            time.sleep(0.5)

        # 窗口出现后再给最多 20 秒，等主场景把 mount_report / mod_report 写完
        if hwnd:
            for _ in range(40):
                if (rd / "mount_report.json").is_file() and (rd / "mod_report.json").is_file():
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
            time.sleep(1.5)

    out["seconds"] = round(time.time() - started, 1)
    out["hwnd"] = hwnd
    out["window"] = title
    out["size"] = f"{w}x{h}" if hwnd else ""

    mr = _json(mount_report)
    out["mount_ok"] = bool(mr.get("ok"))
    out["mounted"] = len(mr.get("mounted") or [])
    out["errors"] = [str(e) for e in (mr.get("errors") or [])][:5]
    loaded, failed, _reasons, events = smoke_mod.read_mod_report(rd / "mod_report.json")
    out["mods_loaded"] = loaded
    out["mods_failed"] = failed
    out["game_events"] = events

    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    exited = False
    for _ in range(30):
        if proc.poll() is not None:
            exited = True
            break
        time.sleep(0.5)
    if not exited:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    out["exited"] = proc.poll() is not None
    out["returncode"] = proc.returncode

    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    out["script_errors"] = smoke_mod.script_errors_from_stderr(text)
    out["log_tail"] = [ln for ln in text.splitlines() if ln.strip()][-8:]
    return out


def report(data: dict) -> bool:
    expected = set(data["expected"])
    loaded = set(data["mods_loaded"])
    missing = sorted(expected - loaded)
    checks = [
        ("窗口已出现", bool(data["hwnd"]), f"{data['window']!r} {data['size']}".strip()),
        ("挂载报告 ok", data["mount_ok"], f"mounted={data['mounted']} errors={data['errors']}"),
        ("扩展全部装载", not missing and not data["mods_failed"], f"缺 {missing} 失败 {data['mods_failed']}"),
        ("无脚本错误", not data["script_errors"], str(data["script_errors"][:2])),
        ("进程正常退出", data.get("exited", False), f"rc={data.get('returncode')}"),
    ]
    print(f"\n=== {data['line']} 窗口启动 ===")
    print(f"覆盖层 {data['packs']} 个，声明入口 {len(expected)} 个，已装载 {len(loaded)} 个")
    print(f"窗口：{data['window']!r} {data['size']}  用时 {data['seconds']} 秒")
    print(f"运行目录：{data['run_dir']}")
    failed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
        failed += 0 if ok else 1
    if data.get("log_tail"):
        print("日志末尾：")
        for ln in data["log_tail"]:
            print("  " + ln[:180])
    return failed == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="窗口模式启动验证")
    parser.add_argument("lines", nargs="+", choices=[cfg_mod.BUILTIN_LINE])
    parser.add_argument("--wait", type=float, default=45.0, help="等窗口出现的秒数")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    cfg = cfg_mod.load()
    results = [check_line(cfg, line, args.wait) for line in args.lines]
    ok = all(report(r) for r in results)
    if args.json:
        target = Path(args.json)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"schema": 1, "reports": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print("\n结论：" + ("窗口模式可进入" if ok else "窗口模式仍有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
