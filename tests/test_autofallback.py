"""启动失败自动兜底（S9）。

要解决的问题不是「告诉玩家哪错了」，而是**让玩家看不到「起不来」**——
因为玩家不会去看日志，他会拍照发群说「启动器是不是坏了」。

这组测试守住四件事：

1. **判据**：只有「秒退 + 非手动 + 返回码非 0」才算启动失败。
   玩了一会儿才崩的不算（那多半是游戏内 bug，停 mod 也解决不了，
   反而让玩家莫名其妙少东西）。
2. **降级顺序**：先停「玩家自己加的」，不行再全停，全停还崩就转人工。
3. **不死循环**：同一组合只自动重试一次。
4. **临时停用不改玩家的启用状态**：那是玩家自己的选择。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import paths  # noqa: E402
from app.core.ext import autofallback as af  # noqa: E402
from app.core.ext import enable as enable_mod  # noqa: E402


# ---------------------------------------------------------------- 判据


def test_fast_crash_is_detected() -> None:
    assert af.crashed_early({"seconds": 0.8, "returncode": 1}) is True


def test_zero_returncode_is_benign() -> None:
    """返回码 0 的秒退不算失败。

    那多半是玩家立刻点了关闭，或游戏自己优雅退出。误判的代价是
    「我明明能玩，启动器却把我的 mod 停了」—— 比不处理更让人困惑。
    """
    assert af.crashed_early({"seconds": 0.5, "returncode": 0}) is False


def test_killed_is_not_crash() -> None:
    assert af.crashed_early({"seconds": 1.0, "returncode": 1, "killed": True}) is False


def test_late_exit_is_not_startup_failure() -> None:
    """跑了一会儿才退出的不算启动失败。"""
    assert af.crashed_early({"seconds": 45.0, "returncode": 1}) is False


def test_missing_fields_are_safe() -> None:
    assert af.crashed_early({}) is False
    assert af.crashed_early({"seconds": "abc", "returncode": 1}) is False


# ---------------------------------------------------------------- 降级


def test_l1_disables_only_unverified() -> None:
    """第一次秒退：只停「玩家自己加的」（无验证记录的）。"""
    plan = af.decide(crashes=1, unverified=["user.ext", "other.ext"])
    assert plan.level == af.LEVEL_UNOFFICIAL
    assert set(plan.disable) == {"user.ext", "other.ext"}
    assert not plan.manual


def test_l1_prefers_misplaced() -> None:
    """装错线的包优先停（它比「未验证」更确定是问题源）。"""
    plan = af.decide(crashes=1, unverified=["a"], misplaced=["wrong.line"])
    assert set(plan.disable) == {"a", "wrong.line"}


def test_l2_when_no_suspects() -> None:
    """没有可疑包（全是官方验证过的）→ 只能全停。"""
    plan = af.decide(crashes=1, unverified=[])
    assert plan.level == af.LEVEL_ALL


def test_l2_after_first_retry() -> None:
    plan = af.decide(crashes=1, unverified=["a"], already_retried=True)
    assert plan.level == af.LEVEL_ALL


def test_l3_when_all_disabled_still_crashes() -> None:
    """全停之后还崩 → 不是扩展的问题，交人工。"""
    plan = af.decide(crashes=2, unverified=[], already_retried=True)
    assert plan.level == af.LEVEL_MANUAL
    assert plan.manual is True
    assert "本体" in plan.message


def test_no_infinite_restart() -> None:
    """L3 不再重启 —— 重启也解决不了本体问题。"""
    plan = af.decide(crashes=3, unverified=[], already_retried=True)
    assert af.should_retry(plan) is False


def test_only_one_retry() -> None:
    """正常路径（L1/L2）会重启，但状态里带 already_retried 后就不再给 L1。"""
    first = af.decide(crashes=1, unverified=["a"])
    assert af.should_retry(first) is True
    second = af.decide(crashes=1, unverified=["a"], already_retried=True)
    assert second.level == af.LEVEL_ALL, "第二次不该再走 L1"


# ---------------------------------------------------------------- 临时停用


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # conftest 的 autouse 已经建过 tmp_path/cache，这里只确保存在
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "CACHE", cache)
    return cache


def test_temp_disabled_roundtrip(cache_dir: Path) -> None:
    enable_mod.set_temp_disabled("4.7", ["a.ext", "b.ext"], "启动失败")
    assert enable_mod.temp_disabled("4.7") == ["a.ext", "b.ext"]
    # 别的线互不影响
    assert enable_mod.temp_disabled("4.6") == []


def test_temp_disabled_is_per_line(cache_dir: Path) -> None:
    enable_mod.set_temp_disabled("4.7", ["x"], "")
    enable_mod.set_temp_disabled("4.6", ["y"], "")
    assert enable_mod.temp_disabled("4.7") == ["x"]
    assert enable_mod.temp_disabled("4.6") == ["y"]


def test_clear_temp_disabled(cache_dir: Path) -> None:
    enable_mod.set_temp_disabled("4.7", ["a"], "")
    enable_mod.clear_temp_disabled("4.7")
    assert enable_mod.temp_disabled("4.7") == []


def test_disabled_mods_are_filtered_out(cache_dir: Path, monkeypatch) -> None:
    """**临时停用只影响生效集合，不改玩家的启用状态。**

    这是关键约束：玩家回头看扩展页应该还是「已启用」，另有一行说明
    「上次因启动失败被跳过」。如果直接改注册表，玩家会以为是自己关的，
    而且在界面上重新打开时会和兜底逻辑打架。
    """
    from app.core.ext import service as service_mod

    item = {
        "id": "suspect.ext",
        "name": "suspect.ext",
        "enabled": True,          # 玩家是启用的
        "engine_line": "4.7",
        "lines": {"4.7": {"line": "4.7", "baseline": "jjxf.v4.7", "status": "ready"}},
    }
    monkeypatch.setattr(service_mod.ExtensionService, "installed", lambda self: [item])
    monkeypatch.setattr(enable_mod, "_fits_launcher", lambda item, line: (True, ""))

    assert [m["id"] for m in enable_mod.enabled_mods({}, "4.7")] == ["suspect.ext"]

    enable_mod.set_temp_disabled("4.7", ["suspect.ext"], "测试")
    assert enable_mod.enabled_mods({}, "4.7") == [], "临时停用期间不该进生效集合"

    # 玩家的启用状态没变
    assert item["enabled"] is True

    enable_mod.clear_temp_disabled("4.7")
    assert [m["id"] for m in enable_mod.enabled_mods({}, "4.7")] == ["suspect.ext"]