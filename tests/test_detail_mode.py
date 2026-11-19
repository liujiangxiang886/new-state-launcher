"""展示模式：简洁 vs 详细（诊断页折叠）。

要解决的矛盾：**同一份界面要给两类人用。**

| 谁 | 想知道 | 看到 11 行技术明细的反应 |
|---|---|---|
| 玩家 | 这两个版本能不能玩 | 「这软件好复杂，我是不是搞坏了什么」 |
| 开发者 | 索引在不在、指纹对不对 | 「怎么只有两行，什么都看不到」 |

做法：设置项 ``ui.show_detail``，默认关闭（简洁）。
玩家默认看到的就是「每个版本一行」；要细节的人去设置里自己开。

两条关键行为：
1. 默认简洁，且**出问题的那条线自动展开** —— 出问题时还折叠，
   玩家得先猜该点哪儿。
2. 「没检测过」不算异常 —— 否则整个页面永远是「N 项需注意」，
   黄点会失去意义。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402


def _cfg(show_detail: bool | None = None) -> dict:
    cfg = cfg_mod.load()
    cfg = copy.deepcopy(cfg)
    if show_detail is None:
        (cfg.get("ui") or {}).pop("show_detail", None)
    else:
        cfg.setdefault("ui", {})["show_detail"] = show_detail
    return cfg


@pytest.fixture(autouse=True)
def _isolate_build_reports(tmp_path, monkeypatch):
    """把 ``builds/`` 指到空目录：诊断页会读最近一次启动的 effect-report.json。

    不隔离会踩到这个坑（2026-11-13 实测）：本机 ``builds/`` 里留着几天前
    4.7 线跑测产生的报告，里面 3 处旧补丁没命中 → 诊断页多出一行
    「扩展补丁 4.7 异常」→ 4.7 那条线被判为异常 → **自动展开** →
    「简洁模式两行」的断言失败。

    那是**环境状态**，不是被测行为：这几条用例说的是折叠逻辑。
    真正的「扩展补丁行」由下面 ``test_failed_patch_row_marks_line_and_expands``
    显式构造报告来覆盖。
    """
    from app.core import paths as paths_mod

    monkeypatch.setattr(paths_mod, "BUILDS", tmp_path / "builds", raising=False)


# ---------------------------------------------------------------- 配置层


def test_show_detail_defaults_to_false() -> None:
    """默认简洁 —— 玩家和开发者都先看简洁版。

    需要细节的是少数时刻，主动去开比每次都被淹没要好。
    """
    from app.ui.pyside import features

    assert features.show_detail(_cfg(None)) is False


def test_show_detail_respects_config() -> None:
    from app.ui.pyside import features

    assert features.show_detail(_cfg(True)) is True
    assert features.show_detail(_cfg(False)) is False


# ---------------------------------------------------------------- 诊断页


def test_diagnostics_collapses_by_default(qapp) -> None:
    """简洁模式：每个游戏版本一行。"""
    from app.ui.pyside.pages.diagnostics import DiagnosticsPage

    page = DiagnosticsPage(_cfg(None), lambda *_a, **_k: None)
    page.refresh()
    t = page.table
    names = [t.item(i, 0).text() for i in range(t.rowCount())]

    # 两行汇总，标题是玩家认得的版本名
    assert len(names) == 2, f"简洁模式应只有两行，实际 {names}"
    assert any("1.45" in n for n in names)
    assert any("4.7" in n for n in names)
    # 不该出现技术明细行
    assert not any("引擎" in n for n in names), "简洁模式不该显示「引擎」这类技术行"


def test_diagnostics_shows_all_when_detail_on(qapp) -> None:
    from app.ui.pyside.pages.diagnostics import DiagnosticsPage

    page = DiagnosticsPage(_cfg(True), lambda *_a, **_k: None)
    page.refresh()
    t = page.table
    names = [t.item(i, 0).text() for i in range(t.rowCount())]
    assert len(names) > 6, f"详细模式应展开全部明细，实际只有 {names}"
    assert any("引擎" in n for n in names)


def test_problem_line_auto_expands(qapp) -> None:
    """**出问题的那条线自动展开**，正常的那条保持折叠。

    这条是关键：出问题时还折叠，玩家得先猜该点哪儿才能看到原因。
    """
    from app.ui.pyside.pages.diagnostics import DiagnosticsPage

    cfg = _cfg(None)
    # 让 4.7 的基线指向不存在的文件
    cfg["baselines"]["jjxf.v4.7"]["path"] = "game/does-not-exist.pck"

    page = DiagnosticsPage(cfg, lambda *_a, **_k: None)
    page.refresh()
    t = page.table
    rows = [(t.item(i, 0).text(), t.item(i, 1).text()) for i in range(t.rowCount())]

    # 4.7 的明细行出现了
    assert any("基线 jjxf.v4.7" in n for n, _s in rows), "4.7 出问题时应自动展开它的明细"
    # 4.6 的明细行没出现（它是正常的，保持折叠）
    assert not any("基线 jjxf.v1.45" in n for n, _s in rows), "正常的线不该被展开"


def test_undetected_drift_is_not_a_problem(qapp) -> None:
    """「源码漂移未检测」不算异常。

    原来标 warn，导致两条线永远「N 项需注意」——
    玩家什么也没做错，只是没点那个按钮。黄点会因此失去意义。
    """
    from app.ui.pyside.pages.diagnostics import DiagnosticsPage

    page = DiagnosticsPage(_cfg(True), lambda *_a, **_k: None)
    page.refresh()
    t = page.table
    for i in range(t.rowCount()):
        name = t.item(i, 0).text()
        if name.startswith("源码漂移"):
            detail = t.item(i, 2).text()
            if "未检测" in detail:
                assert t.item(i, 1).text() == "正常", (
                    f"{name} 只是没检测过，不该标成需注意"
                )


def test_toggle_button_flips_without_touching_config(qapp) -> None:
    """页头那个按钮只是临时切换显示，不改设置。"""
    from app.ui.pyside.pages.diagnostics import DiagnosticsPage

    cfg = _cfg(None)
    before = cfg.get("ui", {}).get("show_detail")
    page = DiagnosticsPage(cfg, lambda *_a, **_k: None)
    assert page.show_details is False
    page._toggle_details()
    assert page.show_details is True
    assert cfg.get("ui", {}).get("show_detail") == before, "按钮不该改配置"


# ---------------------------------------------------------------- 设置变更后要说变就变


def test_refresh_rereads_setting(qapp) -> None:
    """**改了设置再 refresh，必须立刻反映出来。**

    这是实测踩过的坑：``show_details`` 原来只在 ``__init__`` 里读一次，
    玩家在设置页勾「详细展示」→ 保存 → 主窗口调 ``diagnostics.refresh()``
    → 页面仍按旧的折叠状态渲染，表现就是「勾了没反应」。
    """
    from app.ui.pyside.pages.diagnostics import DiagnosticsPage

    cfg = _cfg(False)
    page = DiagnosticsPage(cfg, lambda *_a, **_k: None)
    page.refresh()
    assert page.table.rowCount() == 2

    # 模拟：玩家在设置页开启并保存（同一个 cfg 对象被更新）
    cfg.setdefault("ui", {})["show_detail"] = True
    page.refresh()
    assert page.show_details is True
    assert page.table.rowCount() > 6, "设置改成详细后，refresh 必须重读并展开"


def test_detail_toggle_writes_config_immediately(qapp, tmp_path, monkeypatch) -> None:
    """在设置页勾选，**立刻**落盘，不用点「保存设置」。

    这是个开关型显示选项，用户勾上就想看到效果。跟着保存按钮走
    会制造「我勾了怎么没变」的困惑（实测遇到过）。
    """
    from app.ui.pyside.pages.settings import SettingsPage

    target = tmp_path / "launcher.json"
    target.write_text(json.dumps({"ui": {"show_detail": False}}), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", target)

    cfg = cfg_mod.load()
    page = SettingsPage(cfg, lambda *_a, **_k: None)
    assert page.detail_check.isChecked() is False

    page.detail_check.setChecked(True)

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["ui"]["show_detail"] is True, "勾选后必须立刻落盘"


# ---------------------------------------------------------------- 扩展补丁未生效


def test_failed_patch_row_marks_line_and_expands(qapp, tmp_path) -> None:
    """「已启用但没生效」必须在体检表里露出来，并把那条线摊开。

    玩家的真实遭遇（2026-11-08 反馈）：扩展列表显示「已启用」，
    启动日志也显示覆盖层挂载成功，但进游戏毫无变化 ——
    text_patch 的锚点在这条线的源码里命中 0 处，改动被 compose 层丢掉。

    这条信息原来只写进 ``builds/<run>/compose.json``，玩家不会去看。
    现在读 ``effect-report.json`` 直接报在体检表里；而且**只有真有问题
    才占一行** —— 全部生效时沉默，否则两线变四行，又回到「太复杂」。
    """
    from app.ui.pyside.pages.diagnostics import DiagnosticsPage

    run = tmp_path / "builds" / "run-20261113"
    run.mkdir(parents=True)
    (run / "effect-report.json").write_text(
        json.dumps(
            {
                "line": "4.7",
                "failed_patches": [
                    {
                        "ext_id": "mch.treasure_dup",
                        "target": "scripts/pet_state.gd",
                        "reason": "字面量命中 0 处，期望 1 处（基线可能已变）",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    page = DiagnosticsPage(_cfg(None), lambda *_a, **_k: None)
    page.refresh()
    t = page.table
    rows = [
        (t.item(i, 0).text(), t.item(i, 1).text(), t.item(i, 2).text())
        for i in range(t.rowCount())
    ]
    names = [n for n, _s, _d in rows]

    assert "扩展补丁 4.7" in names, f"未生效的补丁必须单独占一行，实际 {names}"
    tone = next(s for n, s, _d in rows if n == "扩展补丁 4.7")
    assert tone == "异常", f"这一行必须标异常，实际 {tone}"
    detail = next(d for n, _s, d in rows if n == "扩展补丁 4.7")
    assert "mch.treasure_dup" in detail and "未生效" in detail

    # 4.7 因为这一项被判异常 → 自动展开它的明细
    assert any(n.startswith("基线 jjxf.v4.7") for n in names), "出问题的线应自动展开"
    # 4.6 是全正常的，保持折叠，不该出现明细行
    assert not any(n.startswith("基线 jjxf.v1.45") for n in names), "正常的线不该被展开"
    # 没问题的线不该凭空空多一行
    assert "扩展补丁 4.6" not in names
