"""「引擎 exe 与底座 pck 同名配对」的启动前检查。

为什么要有
----------

Godot 的**模板 exe 不支持 ``--main-pack``**（编译时没开
``disable_path_overrides``），它靠「与 exe 同名的 .pck」自动配对。
一旦对不上，引擎会弹一个**模态 ALERT**：

    Error: Couldn't load project data at path "...". Is the .pck file missing?

那个弹窗会**卡住进程**，玩家看到的就是「双击游戏就弹一个看不懂的英文框」。

在启动器里先查一遍、给中文提示，比让玩家去猜引擎的英文报错强得多。

**本文件还锁住一条测试纪律**：测试里跑引擎**不许弹窗**。
弹窗是模态的，会把测试进程卡死 —— 本次开发中就因为「exe 与 pck 异名」
反复弹窗、卡住、逼得我把 pck 改成同名。所以下面有一条专门的守卫测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import runner  # noqa: E402


def test_real_host_dirs_are_paired(tmp_path: Path, monkeypatch) -> None:
    """真实发布目录里，exe 与 pck 必须同名配对。

    这条断言直接盯着「玩家双击游戏会不会弹英文框」。
    """
    import app.core.paths as paths

    checked = 0
    problems: list[str] = []
    for line in ("4.6",):
        host = paths.ROOT / "runtime" / f"host{line.replace('.', '')}"
        if not host.is_dir():
            continue
        exe = next(host.glob("*.exe"), None)
        pkcs = list(host.glob("*.pck"))
        if exe is None or not pkcs:
            continue
        checked += 1
        if not any(p.stem == exe.stem for p in pkcs):
            problems.append(
                f"host{line.replace('.', '')}：{exe.name} 没有同名 pck"
                f"（现有：{[p.name for p in pkcs]}）"
            )
    assert checked == 0 or not problems, (
        "引擎与底座包没同名配对 —— 玩家双击游戏会弹英文 ALERT：\n  "
        + "\n  ".join(problems)
    )


def test_check_host_pairing_detects_renamed_pck(tmp_path: Path, monkeypatch) -> None:
    """pck 被改名时，检查必须给出**中文**提示。"""
    import app.core.paths as paths

    host = tmp_path / "runtime" / "host46"
    host.mkdir(parents=True)
    (host / "jjxf46.exe").write_bytes(b"x")
    (host / "wrong.pck").write_bytes(b"x")
    monkeypatch.setattr(paths, "ROOT", tmp_path)

    msg = runner.check_host_pairing("4.6")
    assert msg, "pck 改名后检查没有报出来"
    assert "同名配对" in msg, msg
    assert "jjxf46.pck" in msg, msg
    # 必须是人话，不能把引擎的英文报错原样丢给玩家
    assert "Couldn't load project data" not in msg, msg


def test_check_host_pairing_passes_when_paired(tmp_path: Path, monkeypatch) -> None:
    """同名配对时检查必须静默返回空串（不能每次启动都弹一句废话）。"""
    import app.core.paths as paths

    host = tmp_path / "runtime" / "host46"
    host.mkdir(parents=True)
    (host / "jjxf46.exe").write_bytes(b"x")
    (host / "jjxf46.pck").write_bytes(b"x")
    monkeypatch.setattr(paths, "ROOT", tmp_path)

    assert runner.check_host_pairing("4.6") == ""


def test_check_host_pairing_ignores_missing_files(tmp_path: Path, monkeypatch) -> None:
    """文件缺失时不由这里报 —— 那件事归 ``host_paths()``，提示更准。"""
    import app.core.paths as paths

    (tmp_path / "runtime").mkdir(parents=True)
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    assert runner.check_host_pairing("4.6") == ""


# ---------------------------------------------------------------- 测试纪律


def test_test_harness_never_triggers_the_pck_alert() -> None:
    """**测试跑引擎时不许弹「pck missing」框。**

    守卫方式：扫测试目录里所有「起引擎」的代码，确认
    ``exe`` 与 ``pck`` **同名**。

    踩过的坑：写 ``godot.exe`` 配 ``check.pck``，引擎不挂载 pck、
    还弹模态 ALERT（会把测试进程卡死）。而且「没挂载」= 脚本压根没被
    load = 检查结论是**假阳性** —— 比不检查更坏，因为它让人以为查过了。
    """
    import re

    bad: list[str] = []
    for path in sorted((ROOT / "tests").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        # 找 PckWriter(...) 的目标名 与 复制 exe 的目标名
        pck_names = set(re.findall(r'PckWriter\(\s*case\s*/\s*"([^"]+)"', src))
        exe_names = set(re.findall(r'copyfile\([^)]*exe[^)]*,\s*(?:case\s*/\s*)?"([^"]+)"', src))
        if not pck_names or not exe_names:
            continue
        # 归一成 stem 比较（godot.exe 与 godot.pck 是同名的）
        pck_stems = {n.rsplit(".", 1)[0] for n in pck_names}
        exe_stems = {n.rsplit(".", 1)[0] for n in exe_names}
        if pck_stems and exe_stems and not (pck_stems & exe_stems):
            bad.append(f"{path.name}: exe {sorted(exe_stems)} vs pck {sorted(pck_stems)}")
    assert not bad, (
        "测试里的 exe 与 pck 不同名 —— 会弹模态 ALERT 并让检查结论变成假阳性：\n  "
        + "\n  ".join(bad)
    )
