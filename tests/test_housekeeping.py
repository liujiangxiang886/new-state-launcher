"""工作期缓存清理：成功即删 + 启动清扫。

这两条策略是 1.4 的需求「工作期间产生的缓存物要自动及时清理」的落地：
``builds/`` 下每次启动/验证/重建都会开目录，以前只增不减。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.core import housekeeping, paths


def _mk(path: Path, *, age_days: float = 0.0, size: int = 0) -> Path:
    """建一个目录并把它（含内容）的时间拨到 ``age_days`` 天前。"""
    path.mkdir(parents=True, exist_ok=True)
    if size:
        (path / "blob.bin").write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_sweep_drops_stale_transient_keeps_fresh() -> None:
    stale = _mk(paths.BUILDS / "_rebuild_46", age_days=2)
    fresh = _mk(paths.BUILDS / "_verify_47", age_days=0.01)

    report = housekeeping.sweep()

    assert not stale.exists(), "两小时前的重建目录该被清掉"
    assert fresh.exists(), "刚建的目录可能是另一个实例在用，不能删"
    assert "_rebuild_46" in report.removed
    assert report.bytes >= 0


def test_sweep_keeps_recent_failed_run_dirs() -> None:
    old = _mk(paths.BUILDS / "run46-s1-20250101-000000", age_days=8)
    recent = _mk(paths.BUILDS / "run46-s1-20250102-000000", age_days=2)

    report = housekeeping.sweep()

    assert not old.exists(), "超过 7 天的失败现场应清掉"
    assert recent.exists(), "7 天内的失败现场要留着排查"
    assert report.kept == 1


def test_sweep_enforces_size_budget_oldest_first() -> None:
    oldest = _mk(paths.BUILDS / "run46-s1-20240601-000000", age_days=3, size=100 * 1024)
    middle = _mk(paths.BUILDS / "run46-s1-20240602-000000", age_days=2, size=100 * 1024)
    newest = _mk(paths.BUILDS / "run46-s1-20240603-000000", age_days=1, size=100 * 1024)

    housekeeping.sweep(max_bytes=250 * 1024)

    assert not oldest.exists(), "超预算时从最旧的删"
    assert middle.exists() and newest.exists()
    assert sum(1 for p in (oldest, middle, newest) if p.exists()) == 2


def test_cleanup_run_only_on_success() -> None:
    ok_dir = _mk(paths.BUILDS / "run47-s1-20240604-000000")
    bad_dir = _mk(paths.BUILDS / "run47-s1-20240605-000000")

    assert housekeeping.cleanup_run(ok_dir, ok=True) is True
    assert not ok_dir.exists(), "正常退出后应立刻删掉本次工作目录"

    assert housekeeping.cleanup_run(bad_dir, ok=False) is False
    assert bad_dir.exists(), "异常退出要保留现场（mount_report.json 在里面）"


def test_cleanup_run_tolerates_missing_dir() -> None:
    gone = paths.BUILDS / "run46-s1-19990101-000000"
    assert housekeeping.cleanup_run(gone, ok=True) is False
    assert housekeeping.cleanup_run(None, ok=True) is False


def test_orphan_instrumented_cache_is_removed() -> None:
    cache = paths.CACHE / "instrumented"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "left.pck").write_bytes(b"x")          # 只有 pck，元数据丢了
    (cache / "right.pck").write_bytes(b"x")
    (cache / "right.json").write_text("{}", encoding="utf-8")

    removed = housekeeping.cleanup_orphan_cache()

    assert removed == 1
    assert not (cache / "left.pck").exists()
    assert (cache / "right.pck").exists() and (cache / "right.json").exists()


def test_ext_src_is_never_swept() -> None:
    # 这里放的是用户装的扩展包原件，重装要用，多老都不能删
    src = _mk(paths.BUILDS / "_ext_src", age_days=30)

    housekeeping.sweep(keep_days=1)

    assert src.exists()


def test_install_workdirs_are_never_swept() -> None:
    # 扩展源码在里面，钩子重扫（rescan_runtime_hooks）要靠它
    inst = _mk(paths.BUILDS / "_install_mch.sell_all_46", age_days=30)

    housekeeping.sweep(keep_days=1)

    assert inst.exists()


def test_sweep_dry_run_touches_nothing() -> None:
    stale = _mk(paths.BUILDS / "_bundle_demo", age_days=5)

    report = housekeeping.sweep(dry_run=True)

    assert stale.exists(), "dry-run 不该删任何东西"
    assert "_bundle_demo" in report.removed, "但要把它列进计划里"


def test_run_dir_pattern_matches_runner_output() -> None:
    from app.core import runner

    tag = "run46-s1"
    made = runner.run_dir_for(tag)
    names = [p.name for p in housekeeping.run_dirs()]
    assert made.name in names, f"runner 生成的目录名要能被清扫识别：{made.name}"


def test_sweep_ignores_dev_workspaces() -> None:
    # tools/ 下的联调工作区（ctx-real 之类）不是工作期缓存，不能碰
    dev = _mk(paths.BUILDS / "ctx-real", age_days=60)

    housekeeping.sweep(keep_days=1)

    assert dev.exists()
