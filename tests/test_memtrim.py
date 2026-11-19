"""启动前「释放系统内存」（working set trim）的回归测试。

两个必须守住的行为：

1. **绝不碰游戏进程**。4.7 剧情版的 7GB 是每帧都在读的热数据，腾走等于
   每帧硬缺页，只会更卡。这条红线靠名字前缀兜住，写死在这里。
2. **默认关闭**。收益随机器状态波动，机械硬盘上还会让玩家觉得卡，不能
   自作主张替所有人开。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import memtrim  # noqa: E402
from app.core import runner  # noqa: E402
from app.core import runtime_lock  # noqa: E402


# ---------------------------------------------------------------- 保护名单


@pytest.mark.parametrize("name", ["jjxf47.exe", "jjxf46.exe", "JJXF47.EXE"])
def test_game_processes_are_never_trimmed(name: str) -> None:
    """游戏进程是红线：名字大小写不同也只是同一个引擎。"""
    assert memtrim._should_skip(name.lower(), 4242, set()) is True


@pytest.mark.parametrize(
    "name",
    ["system", "csrss.exe", "winlogon.exe", "dwm.exe", "audiodg.exe"],
)
def test_system_processes_are_never_trimmed(name: str) -> None:
    """系统关键进程 + 桌面合成器不碰（dwm 腾空会让拖窗口掉帧）。"""
    assert memtrim._should_skip(name, 888, set()) is True


def test_explicit_pid_exclusion() -> None:
    """调用方传进来的 pid 必须排除（自己的 pid、多开的游戏实例）。"""
    assert memtrim._should_skip("chrome.exe", 777, {777}) is True
    assert memtrim._should_skip("chrome.exe", 778, {777}) is False


def test_pid_below_5_is_skipped() -> None:
    """0/4 号是内核态「进程」，没有可腾的工作集。"""
    assert memtrim._should_skip("anything.exe", 4, set()) is True


def test_unnamed_process_is_skipped() -> None:
    """拿不到映像名说明权限不够，宁可不碰。"""
    assert memtrim._should_skip("", 1234, set()) is True


# ---------------------------------------------------------------- 报告


def test_human_readable() -> None:
    assert memtrim.human(512) == "512 B"
    assert memtrim.human(2048) == "2.0 KB"
    assert memtrim.human(3 * 1024 * 1024) == "3.0 MB"
    assert memtrim.human(5 * 1024**3) == "5.0 GB"


def test_summary_when_nothing_to_do() -> None:
    report = memtrim.TrimReport(ok=True, processes=0, skipped=12)
    assert "没有可腾的进程" in report.summary()
    assert "12" in report.summary()


def test_summary_reports_both_numbers() -> None:
    """``freed`` 与「可用物理内存增量」不是一回事，两个都要露出来。

    现场实测：工作集减了 1.1 GB，可用物理内存只涨了 0.3 GB —— 剩下的页
    进了待机列表。只报一个数字会让玩家/作者对不上账。
    """
    report = memtrim.TrimReport(
        ok=True,
        freed=1024**3,
        before_avail=7 * 1024**3,
        after_avail=8 * 1024**3,
        processes=30,
    )
    text = report.summary()
    assert "1.0 GB" in text and "7.0 GB" in text and "8.0 GB" in text
    assert report.gained == 1024**3


def test_unavailable_platform_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memtrim.os, "name", "posix")
    report = memtrim.trim_working_sets()
    assert report.ok is False and "Windows" in report.reason


# ---------------------------------------------------------------- 真机空跑


@pytest.mark.skipif(os.name != "nt", reason="Windows 专用 API")
def test_dry_run_lists_processes_without_trimming() -> None:
    """dry_run 只读工作集，不能调用 EmptyWorkingSet。"""
    before = memtrim.available_physical()
    report = memtrim.trim_working_sets(dry_run=True)
    assert report.ok is True
    assert report.reason == "试运行（未实际执行）"
    assert report.processes > 0, "至少应该有几个可腾的大进程"
    assert report.freed == 0, "试运行不允许产生释放量"
    assert report.estimated > 0, "试运行要给出「最多能腾多少」的上限估算"
    # 系统里别的进程一直在申请/释放内存，可用量本来就会小幅抖动，
    # 这里只要求没有「成百 MB 级」的跃升（那才是真的腾过）。
    assert abs(memtrim.available_physical() - before) < 256 * 1024 * 1024


@pytest.mark.skipif(os.name != "nt", reason="Windows 专用 API")
def test_dry_run_respects_min_bytes() -> None:
    """阈值设成天文数字后，什么都不该被选中。"""
    report = memtrim.trim_working_sets(dry_run=True, min_bytes=10**15)
    assert report.processes == 0


# ---------------------------------------------------------------- 接入点


def test_runner_excludes_game_pids(monkeypatch: pytest.MonkeyPatch) -> None:
    """``runner.trim_working_set_before_launch`` 必须把游戏实例 pid 传下去。"""
    monkeypatch.setattr(runtime_lock, "running_engine_pids", lambda: [5001, 5002])
    captured: dict[str, set[int]] = {}

    def fake_trim(**kwargs):
        captured.update(kwargs)
        return memtrim.TrimReport(ok=True)

    monkeypatch.setattr(memtrim, "trim_working_sets", fake_trim)
    runner.trim_working_set_before_launch()
    exclude = captured["exclude_pids"]
    assert {5001, 5002} <= exclude, "多开的游戏实例不能被腾"
    assert os.getpid() in exclude, "启动器自己也不能被腾"


# ---------------------------------------------------------------- 界面接入


def test_ui_trims_only_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """界面启动路径：开关关着就一次都不许调，开着才调。

    界面走 QProcess，**不经过** ``runner.launch`` —— 这是立绘那次踩过的坑，
    任何「启动前要做的事」都得在这里再挂一次，否则玩家路径上不生效。
    """
    pytest.importorskip("PySide6")
    from unittest import mock

    from PySide6.QtCore import QCoreApplication

    from app.core import config as cfg_mod
    from app.core import profiles as profiles_mod
    from app.ui.pyside import workers

    QCoreApplication.instance() or QCoreApplication([])
    calls = {"trim": 0}
    monkeypatch.setattr(
        workers.runner, "purge_shader_cache_ex", lambda *a, **k: (0, "没有缓存目录")
    )
    monkeypatch.setattr(
        workers.runner, "trim_working_set_before_launch",
        lambda: calls.__setitem__("trim", calls["trim"] + 1)
        or memtrim.TrimReport(ok=True, freed=1024, processes=3),
    )
    monkeypatch.setattr(
        profiles_mod, "snapshot",
        lambda name=None, **kw: {"profile": name, "path": str(tmp_path), "backup": "", "files": {}},
    )

    def _start(cfg: dict) -> None:
        gp = workers.GameProcess()
        with mock.patch.object(workers.QProcess, "start", lambda self: None):
            gp.start(
                cfg, "4.7",
                packset=str(tmp_path / "x.pck"),
                main_scene="res://scenes/start_page.tscn",
                profile="default",
                log_path=tmp_path / "mount_report.json",
            )

    off = cfg_mod.load()
    (off.setdefault("runtime", {}))["trim_working_set"] = False
    _start(off)
    assert calls["trim"] == 0, "默认关闭时一次都不能调"

    on = cfg_mod.load()
    (on.setdefault("runtime", {}))["trim_working_set"] = True
    _start(on)
    assert calls["trim"] == 1, "开启后界面启动要腾一次内存"

