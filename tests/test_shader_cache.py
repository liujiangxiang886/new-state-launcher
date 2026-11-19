"""着色器缓存清理 + 界面启动路径的存档备份。

为什么必须测
------------

「角色立绘全白」的根因是 ``user://shader_cache`` 被写坏（进程被强杀时
GLSL 二进制只写一半，下次启动 ``ShaderGLES3::initialize()`` 失败，所有
材质回退白色）。修复分两层：

1. 启动前清缓存 —— 但**玩家走的是界面**（``QProcess``），不经过
   ``runner.launch``。当初只改 ``launch()`` 时 CLI 能清、界面不清，
   症状照旧复现。所以必须断言 ``GameProcess.start`` 真的调了清理。
2. 强杀后兜底清 —— ``_on_finished`` 里 ``_killed`` 为真时再清一次。

同一个坑还有第二处：存档备份原本也只在 ``runner.launch`` 里，
界面路径从来没备份过。这里一并锁住。

跑法：``python tests/test_shader_cache.py`` 或 ``pytest tests/test_shader_cache.py``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import runner  # noqa: E402
from app.core import runtime_lock, userdir  # noqa: E402


@pytest.fixture
def fake_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 launcher_dir 指到临时目录，并在里面造一份假缓存。"""
    target = tmp_path / "app_userdata" / "shader_cache" / "CanvasShaderGLES3"
    target.mkdir(parents=True)
    (target / "shader.cache").write_bytes(b"x" * 4096)
    monkeypatch.setattr(userdir, "launcher_dir", lambda *a, **k: tmp_path / "app_userdata")
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda *a, **k: False)
    return tmp_path / "app_userdata" / "shader_cache"


def _cfg(**runtime) -> dict:
    cfg = {"runtime": {"clear_shader_cache": True}}
    cfg["runtime"].update(runtime)
    return cfg


def test_purge_removes_cache_and_reports_bytes(fake_cache: Path) -> None:
    freed = runner.purge_shader_cache(_cfg(), "4.7", "default")
    assert freed >= 4096, "应该按字节数回报释放量"
    assert not fake_cache.exists(), "缓存目录必须被删掉"


def test_purge_respects_switch(fake_cache: Path) -> None:
    freed = runner.purge_shader_cache(_cfg(clear_shader_cache=False), "4.7", "default")
    assert freed == 0
    assert fake_cache.exists(), "关闭开关时不能动缓存（排障要留现场）"


def test_purge_skips_while_game_running(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda *a, **k: True)
    freed = runner.purge_shader_cache(_cfg(), "4.7", "default")
    assert freed == 0
    assert fake_cache.exists(), "游戏在跑时缓存被占用，删不干净也不该动"


def test_purge_noop_when_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userdir, "launcher_dir", lambda *a, **k: tmp_path / "nope")
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda *a, **k: False)
    assert runner.purge_shader_cache(_cfg(), "4.7", "default") == 0


def test_purge_reports_reason(fake_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """返回值要能区分「没缓存 / 游戏在跑 / 删失败」，否则现场没法查。"""
    freed, why = runner.purge_shader_cache_ex(_cfg(), "4.7", "default")
    assert freed >= 4096 and "已清理" in why

    freed, why = runner.purge_shader_cache_ex(_cfg(clear_shader_cache=False), "4.7", "default")
    assert freed == 0 and "禁用" in why

    monkeypatch.setattr(runtime_lock, "is_game_running", lambda *a, **k: True)
    freed, why = runner.purge_shader_cache_ex(_cfg(), "4.7", "default")
    assert freed == 0 and "运行" in why


def test_purge_renames_before_delete(fake_cache: Path) -> None:
    """改名优先：即使目录删不掉，也不能让引擎下次看到旧名字。"""
    runner.purge_shader_cache(_cfg(), "4.7", "default")
    assert not fake_cache.exists()
    leftovers = list(fake_cache.parent.glob("shader_cache.trash-*"))
    assert len(leftovers) <= 1, "残渣留档可以有，但不能堆一堆同名目录"


def _long_cache(root: Path, depth: int = 12) -> tuple[Path, Path]:
    """造一份路径超过 260 字符的缓存目录（模拟真实档位）。"""
    cache = root / "app_userdata" / "shader_cache"
    cur = cache
    for _ in range(depth):
        cur = cur / ("d" * 40)
    os.makedirs(runner._win_long(cur), exist_ok=True)
    payload = cur / ("f" * 30 + ".cache")
    with open(runner._win_long(payload), "wb") as fh:
        fh.write(b"y" * 5000)
    return cache, payload


def test_purge_handles_path_over_max_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实缓存路径可达 330+ 字符，超过 Windows 260 上限。

    2026-11-11 现场：``shader_cache`` 里的文件路径长 338 字符，
    ``Path.is_file()`` 全返回 False、``shutil.rmtree`` 静默失败，于是
    「已清理」的日志下面其实一个字节都没动，第二次启动照样读坏缓存。
    这里锁死：超长路径也必须统计出字节并能删干净。
    """
    cache, payload = _long_cache(tmp_path)
    if os.name == "nt":
        assert len(str(payload)) > 260, "夹具没造出超长路径，测试就失去意义"
    monkeypatch.setattr(userdir, "launcher_dir", lambda *a, **k: tmp_path / "app_userdata")
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda *a, **k: False)

    freed, why = runner.purge_shader_cache_ex(_cfg(), "4.7", "default")
    assert freed == 5000, f"超长路径下字节统计必须准确，实际 {freed}"
    assert "已清理" in why
    assert not cache.exists(), "超长路径的缓存目录必须真正删掉"


def test_purge_sweeps_old_trash_without_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 shader_cache 时也要顺手回收旧残渣。

    之前把清理放在「没有缓存目录就直接返回」之后，于是玩家每次启动都走这条
    最常见的分支，残渣永远扫不到 —— 连测 4 次就攒了 4 份 trash 目录。
    """
    base = tmp_path / "app_userdata"
    for stamp in ("111", "222"):
        d = base / f"shader_cache.trash-{stamp}" / "CanvasShaderGLES3"
        os.makedirs(runner._win_long(d), exist_ok=True)
        with open(runner._win_long(d / "x.cache"), "wb") as fh:
            fh.write(b"z" * 100)
    monkeypatch.setattr(userdir, "launcher_dir", lambda *a, **k: base)
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda *a, **k: False)

    freed, why = runner.purge_shader_cache_ex(_cfg(), "4.7", "default")
    assert freed == 0 and "没有缓存目录" in why
    assert not list(base.glob("shader_cache.trash-*")), "旧残渣应被回收"


def test_ui_path_purges_and_backs_up_saves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """界面启动必须同时做：清缓存 + 存档快照。

    这两件事原先都只写在 ``runner.launch`` 里，而界面用 ``QProcess`` 另起
    一条路，结果玩家路径上两件事都没发生。
    """
    pytest.importorskip("PySide6")
    from unittest import mock

    from PySide6.QtCore import QCoreApplication

    from app.ui.pyside import workers

    QCoreApplication.instance() or QCoreApplication([])

    calls = {"purge": 0, "snapshot": 0}
    monkeypatch.setattr(
        workers.runner, "purge_shader_cache_ex",
        lambda cfg, line, profile=None: (calls.__setitem__("purge", calls["purge"] + 1) or 1024, "已清理"),
    )
    from app.core import profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod, "snapshot",
        lambda name=None, **kw: calls.__setitem__("snapshot", calls["snapshot"] + 1)
        or {"profile": name, "path": str(tmp_path), "backup": "", "files": {}},
    )

    gp = workers.GameProcess()
    with mock.patch.object(workers.QProcess, "start", lambda self: None):
        gp.start(
            cfg_mod.load(), "4.7",
            packset=str(tmp_path / "x.pck"),
            main_scene="res://scenes/start_page.tscn",
            profile="default",
            log_path=tmp_path / "mount_report.json",
        )
    assert calls["purge"] == 1, "界面启动前必须清着色器缓存"
    assert calls["snapshot"] == 1, "界面启动前必须备份存档"
    assert gp._snap, "快照要留着，退出后用它比对存档变化"


def test_ui_writes_saves_diff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """退出后要把存档变化写成 saves-diff.json，界面靠它提示玩家。"""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from app.core import profiles as profiles_mod
    from app.ui.pyside import workers

    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(
        profiles_mod, "finish",
        lambda snap: {"delta": {"total": 1, "added": ["a.json"]}, "summary": "存档 +1 -0 ~0"},
    )
    gp = workers.GameProcess()
    gp._run_dir = tmp_path
    gp._snap = {"profile": "default", "path": str(tmp_path), "backup": "", "files": {}}
    out = gp._finish_saves()
    assert out and out["summary"] == "存档 +1 -0 ~0"
    assert (tmp_path / "saves-diff.json").exists()


def test_ui_purges_after_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """退出后必须清缓存 —— 不只强杀，正常退出也一样。

    2026-11-11：正常关窗口（退出码 0）后下一次启动仍全白，说明「刚写完」的
    缓存本身就坏。只防强杀不够。
    """
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from app.ui.pyside import workers

    QCoreApplication.instance() or QCoreApplication([])
    calls = {"purge": 0}
    monkeypatch.setattr(
        workers.runner, "purge_shader_cache_ex",
        lambda cfg, line, profile=None: (calls.__setitem__("purge", calls["purge"] + 1) or 2048, "已清理"),
    )
    gp = workers.GameProcess()
    gp._cfg = {"runtime": {"clear_shader_cache": True}}
    gp._line_key = "4.7"
    gp._profile = "default"
    gp._killed = False          # 正常退出也清
    gp._started_at = 0
    gp._run_dir = tmp_path
    gp._snap = None
    gp._on_finished(0, None)    # 只验证清理分支，其余字段为空也不该抛
    assert calls["purge"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
