"""右栏「已接入扩展」的显隐契约。

1.4 之前这一栏不管有没有内容，都把「上移 / 下移 / 移出列表 / 删除所有扩展
并清理缓存」四行控件摆在那里：空列表时全靠禁用态顶着，把卡片顶到溢出——
实测底部那行计数被日志面板削掉半截。现在改成按内容显隐，下面几条断言
就是这个契约的守卫。

不需要真实游戏文件：基线健康度、配置落盘都打桩，只验证布局状态。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core.ext import service as service_mod  # noqa: E402
from qt_cleanup import dispose  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _write(module, items: list[dict]) -> None:
    module.REGISTRY.write_text(
        json.dumps({"schema": 1, "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )


def _ext(ext_id: str, line: str = "4.6") -> dict:
    return {
        "id": ext_id,
        "name": ext_id.upper(),
        "version": "1.0",
        "entry_count": 3,
        "enabled": True,
        "engine_line": line,
        "lines": {line: {"enabled": True}},
    }


def _base_cfg(line: str = "4.6") -> dict:
    return {
        "ui": {"last_version": line},
        "extensions": {"extension_mode": "normal"},
        "engines": {"4.6": {"kind": "editor"}},
        "baselines": {
            "jjxf.v1.45": {"engine": "4.6", "path": "mchGame.exe"},
        },
    }


@pytest.fixture
def make_page(qapp, monkeypatch):
    """造一个只关心右栏的库页。"""
    monkeypatch.setattr(cfg_mod, "baseline_health", lambda *_a, **_k: ("ok", ""))
    monkeypatch.setattr(cfg_mod, "baseline_exists", lambda *_a, **_k: True)
    # 页面切换版本时会顺手保存「上次用的线」，测试里不能让写到真实配置上
    monkeypatch.setattr(cfg_mod, "save", lambda *_a, **_k: True)

    from app.ui.pyside.pages.library import LibraryPage

    made: list = []

    def _make(cfg: dict):
        page = LibraryPage(cfg)
        page.resize(380, 560)
        page.show()
        qapp.processEvents()
        made.append(page)
        return page

    yield _make
    # 只 close() 不够：控件仍活到解释器退出，shiboken 析构时 access violation，
    # 退出码变 3221225477，看起来像测试失败。必须真删（见 qt_cleanup 说明）。
    dispose(*made)


def _vis(page) -> dict[str, bool]:
    return {
        "empty": page.mods_empty.isVisible(),
        "area": page.mods_area.isVisible(),
        "sort": page.sort_ops.isVisible(),
        "wipe": page.wipe_ops.isVisible(),
        "count": page.mods_count.isVisible(),
        "import_btn": page.empty_import_btn.isVisible(),
    }


def test_empty_line_shows_only_the_drop_zone(make_page) -> None:
    """空列表：只留空状态块，四个用不上的控件全部收起。"""
    _write(service_mod, [])
    page = make_page(_base_cfg())

    state = _vis(page)
    assert state["empty"] is True
    assert state["import_btn"] is True
    assert state["area"] is False
    assert state["sort"] is False, "没有行可以排序，按钮不该占位"
    assert state["wipe"] is False, "没东西可删，危险按钮不该出现"
    assert state["count"] is False, "「共 0 项」没有信息量"


def test_listed_line_shows_ops(make_page) -> None:
    """有扩展：列表、排序、整批清理、计数都要回来。"""
    _write(service_mod, [_ext("mod.a"), _ext("mod.b")])
    page = make_page(_base_cfg())

    state = _vis(page)
    assert state["area"] is True
    assert state["empty"] is False
    assert state["sort"] is True
    assert state["wipe"] is True
    assert state["count"] is True
    assert [r for r in page._row_ids] == ["mod.a", "mod.b"]


def test_custom_line_hides_import_entry(make_page) -> None:
    """自定义改版本来就不加载扩展：空状态要留，但不能引导「导入扩展包」。"""
    _write(service_mod, [])
    cfg = _base_cfg()
    cfg["custom_lines"] = [
        {"id": "custom:demo", "name": "某改版", "kind": "exe", "path": "x.exe"}
    ]
    page = make_page(cfg)
    page.set_version("custom:demo", emit=False)

    state = _vis(page)
    assert state["empty"] is True
    assert state["import_btn"] is False
    assert state["sort"] is False and state["wipe"] is False and state["count"] is False
    assert "不加载扩展" in page.empty_title.text()


# ---------------------------------------------------------------- 基线变更信号

def test_baseline_change_emits_signal(make_page, monkeypatch) -> None:
    """导入/回滚游戏本体必须发 ``baselineChanged``。

    这个 bug 的表现是「首次导入 exe 后侧栏仍显示无法启动，得关掉重启」：
    库页 ``refresh()`` 只重绘本页，左侧启动按钮由主窗口
    ``_sync_launch_button()`` 裁决，没有这条信号它就永远停在导入前的结论上。
    """
    import app.core.baseline_ops as baseline_ops

    page = make_page(_base_cfg())

    fired: list[int] = []
    page.baselineChanged.connect(lambda: fired.append(1))

    class _Probe:
        path = Path("mchGame.exe")
        kind = "pck"
        pack_version = 3
        entry_count = 2
        size = 1234
        sha256 = "x" * 64

    # adopt 会写配置、动缓存；这里只关心信号有没有发出来
    monkeypatch.setattr(baseline_ops, "adopt", lambda *_a, **_k: {})
    monkeypatch.setattr(baseline_ops, "invalidate_caches", lambda: None)
    # 导入完成会弹模态 QMessageBox：不桩掉就会卡住事件循环（headless 下必挂）。
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *_a, **_k: None)

    page._adopt_baseline(
        _Probe(), {"key": "4.6", "baseline": "jjxf.v1.45", "title": "原版"},
        smoke=False, rebuild=False,
    )

    assert fired, "导入完成没有发 baselineChanged，侧栏不会刷新"
