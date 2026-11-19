"""界面层集成验证：分类筛选、分类对话框、游戏线编辑器。

为什么必测：``categories.py`` 的单元测试全绿，不代表界面接对了。
这一层过去完全没有测试（全项目 0 个 UI 测试），只能靠 ``--selftest``
截图肉眼判断 —— 而截图看不出「点了筛选没反应」「改了分类没落盘」。

**必须重定向 ``cfg_mod.CONFIG_PATH``**：``CategoryDialog`` / ``CustomLinesDialog``
保存时调的是 ``cfg_mod.save(cfg)``，不带路径就写真实的
``config/launcher.json``。测试污染玩家配置比测试失败严重得多。
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

from app.core import categories as cat  # noqa: E402
from app.core import config as cfg_mod  # noqa: E402
from qt_cleanup import dispose  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _dispose_widgets(qapp):
    """每个用例结束后销毁它新开的窗口。

    本文件里的页面/对话框都是**临时对象**，测试只读它们的控件状态。
    留着不删会活到解释器退出 —— 报告全绿、退出码却是 3221225477
    （access violation），详见 ``qt_cleanup`` 模块注释。
    """
    from PySide6.QtWidgets import QApplication

    before = {id(w) for w in QApplication.topLevelWidgets()}
    yield
    dispose(*[w for w in QApplication.topLevelWidgets() if id(w) not in before])


def _cfg(tmp_path: Path) -> dict:
    exe = tmp_path / "mod.exe"
    exe.write_bytes(b"MZ")
    pck = tmp_path / "pack.pck"
    pck.write_bytes(b"GDPC")
    return {
        "engines": {"4.6": {"kind": "editor"}},
        "baselines": {
            "jjxf.v1.45": {"engine": "4.6", "path": str(exe)},
        },
        "custom_lines": [
            {
                "id": "custom:aaa",
                "name": "某改版A",
                "kind": "exe",
                "path": str(exe),
                "category": cat.MOD_ID,
            },
            {
                "id": "custom:bbb",
                "name": "某资源包B",
                "kind": "pck",
                "path": str(pck),
                "engine_line": "4.6",
                "category": cat.STORY_ID,
            },
        ],
        "ui": {"last_version": "4.6"},
    }


# ---------------------------------------------------------------- 库页筛选


def test_library_grouped_selector(qapp, tmp_path: Path) -> None:
    from app.ui.pyside.pages.library import LibraryPage

    page = LibraryPage(_cfg(tmp_path))
    texts = [page.selector.itemText(i) for i in range(page.selector.count())]

    # 三类分组标题都在（默认分类下 原版/剧情/改版 各有条目）
    assert any("原版" in t and "—" in t for t in texts), texts
    assert any("改版" in t and "—" in t for t in texts), texts

    # 标题行必须不可选，否则用户能「选中一个分类标题然后点启动」
    model = page.selector.model()
    for i in range(page.selector.count()):
        if page.selector.itemData(i) is None:
            assert not model.item(i).isEnabled(), f"分组标题可被选中：{page.selector.itemText(i)}"


def test_library_filter_really_filters(qapp, tmp_path: Path) -> None:
    from app.ui.pyside.pages.library import LibraryPage

    page = LibraryPage(_cfg(tmp_path))
    datas = [page.selector.itemData(i) for i in range(page.selector.count())]
    assert "4.6" in datas and "4.6" in datas and "custom:aaa" in datas

    # 切到「改版」筛选
    idx = page.filter_combo.findData(cat.MOD_ID)
    assert idx >= 0, [page.filter_combo.itemText(i) for i in range(page.filter_combo.count())]
    page.filter_combo.setCurrentIndex(idx)

    datas = [page.selector.itemData(i) for i in range(page.selector.count())]
    picked = [d for d in datas if d]
    assert picked == ["custom:aaa"], f"筛选后应只剩改版条目，实际 {picked}"
    assert page.current_version() == "custom:aaa"


def test_library_filter_back_to_all(qapp, tmp_path: Path) -> None:
    from app.ui.pyside.pages.library import LibraryPage

    page = LibraryPage(_cfg(tmp_path))
    page.filter_combo.setCurrentIndex(page.filter_combo.findData(cat.MOD_ID))
    page.filter_combo.setCurrentIndex(0)  # 全部分类
    datas = [d for d in (page.selector.itemData(i) for i in range(page.selector.count())) if d]
    assert set(datas) == {"4.6", "4.6", "custom:aaa", "custom:bbb"}


# ---------------------------------------------------------------- 分类对话框


def test_category_dialog_add_assign_delete(qapp, tmp_path: Path, monkeypatch) -> None:
    from app.ui.pyside.pages.category_dialog import CategoryDialog

    saved_to = tmp_path / "launcher.json"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", saved_to)

    cfg = _cfg(tmp_path)
    dlg = CategoryDialog(cfg)

    # 表格行数 = 4 个内置分类
    assert dlg.cat_table.rowCount() == 4

    # 新增
    made = cat.add(cfg, "联机版")
    dlg.reload()
    assert dlg.cat_table.rowCount() == 5

    # 指派：把改版 A 挪进新分类
    from app.core import catalog

    entry = next(e for e in catalog.games(cfg) if e.id == "custom:aaa")
    dlg._assign(entry, made.id)
    got = next(
        item for item in cfg["custom_lines"] if item["id"] == "custom:aaa"
    )
    assert got["category"] == made.id
    assert saved_to.is_file(), "改动必须落盘（Dialog 的 _save 没被调用？）"
    on_disk = json.loads(saved_to.read_text(encoding="utf-8"))
    assert any(c["id"] == made.id for c in on_disk["categories"])

    # 删除 → 条目回落到「其他」
    assert cat.remove(cfg, made.id)
    dlg.reload()
    got = next(item for item in cfg["custom_lines"] if item["id"] == "custom:aaa")
    assert got["category"] == cat.OTHER_ID
    assert dlg.cat_table.rowCount() == 4


def test_category_dialog_protected_delete(qapp, tmp_path: Path, monkeypatch) -> None:
    from app.ui.pyside.pages.category_dialog import CategoryDialog

    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "launcher.json")
    dlg = CategoryDialog(_cfg(tmp_path))

    # 选中「其他」→ 删除按钮必须禁用
    ids = dlg._cat_ids
    dlg.cat_table.selectRow(ids.index(cat.OTHER_ID))
    dlg._on_cat_selection()
    assert not dlg.del_btn.isEnabled(), "「其他」不能删，按钮应禁用"

    dlg.cat_table.selectRow(ids.index(cat.MOD_ID))
    dlg._on_cat_selection()
    assert dlg.del_btn.isEnabled()


def test_category_dialog_game_combo_reflects_category(qapp, tmp_path: Path, monkeypatch) -> None:
    """右侧「游戏归属」的下拉要显示当前分类，不能全都停在第一项。"""
    from PySide6.QtWidgets import QComboBox

    from app.ui.pyside.pages.category_dialog import CategoryDialog

    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "launcher.json")
    dlg = CategoryDialog(_cfg(tmp_path))

    seen: dict[str, str] = {}
    for row in range(dlg.game_table.rowCount()):
        combo = dlg.game_table.cellWidget(row, 2)
        assert isinstance(combo, QComboBox)
        seen[dlg._game_table_ids[row]] = str(combo.currentData())

    assert seen["jjxf.v1.45"] == cat.OFFICIAL_ID
    assert seen["custom:aaa"] == cat.MOD_ID
    # 自定义线自带 category 时以它为准（这里故意填「剧情」验证不会被覆盖）
    assert seen["custom:bbb"] == cat.STORY_ID


# ---------------------------------------------------------------- 线编辑器


def test_line_editor_values_for_pck(qapp, tmp_path: Path) -> None:
    from app.ui.pyside.pages.custom_lines_dialog import LineEditor

    pck = tmp_path / "g.pck"
    pck.write_bytes(b"GDPC")
    cfg = _cfg(tmp_path)

    editor = LineEditor(cfg, kind="pck", path=str(pck), name="测试包")
    # pck 形态：引擎线与主场景必须可用
    assert editor.engine_box.isEnabled()
    assert editor.scene_edit.isEnabled()

    editor.scene_edit.setText("res://boot.tscn")
    editor.args_edit.setText("--fullscreen")
    editor.cat_box.setCurrentIndex(editor.cat_box.findData(cat.MOD_ID))

    values = editor.values()
    assert values["name"] == "测试包"
    assert values["path"] == str(pck)
    assert values["extra"]["kind"] == "pck"
    assert values["extra"]["main_scene"] == "res://boot.tscn"
    assert values["extra"]["args"] == "--fullscreen"
    assert values["extra"]["category"] == cat.MOD_ID
    assert values["extra"]["engine_line"] == "4.6"


def test_line_editor_disables_pck_fields_for_exe(qapp, tmp_path: Path) -> None:
    from app.ui.pyside.pages.custom_lines_dialog import LineEditor

    exe = tmp_path / "g.exe"
    exe.write_bytes(b"MZ")
    editor = LineEditor(_cfg(tmp_path), kind="exe", path=str(exe), name="x")
    assert not editor.engine_box.isEnabled(), "exe 形态不需要引擎线"
    assert not editor.scene_edit.isEnabled(), "exe 形态不需要主场景"


def test_line_editor_defaults_match_core(qapp, tmp_path: Path) -> None:
    """界面默认值必须和核心逻辑算出的一致。

    这类「界面默认 ≠ 核心默认」的漂移踩过两次：
    - 引擎线停在 4.6，而核心取了别的版本 → v4 的 pck 挂到 4.6 直接报错
    - 分类停在「原版」，而核心对空分类回落「改版」→ 新建的改版被归到原版
    """
    from app.core import custom_lines as cl
    from app.ui.pyside.pages.custom_lines_dialog import LineEditor

    cfg = _cfg(tmp_path)
    pck = tmp_path / "n.pck"
    pck.write_bytes(b"GDPC")

    editor = LineEditor(cfg, kind="pck", path=str(pck), name="新建")
    assert editor.engine_box.currentData() == cl.normalized_engine_line(cfg, "")
    assert editor.cat_box.currentData() == cat.MOD_ID


def test_custom_lines_tip_has_no_raw_markdown(qapp, tmp_path: Path, monkeypatch) -> None:
    """QLabel 不解析 markdown —— 文案里留 ``**`` 会原样显示成星号。"""
    from PySide6.QtWidgets import QLabel

    from app.ui.pyside.pages.custom_lines_dialog import CustomLinesDialog

    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "launcher.json")
    dlg = CustomLinesDialog(_cfg(tmp_path))
    joined = "\n".join(w.text() for w in dlg.findChildren(QLabel) if w.text())
    assert "**" not in joined, "文案里有未渲染的 markdown 星号"


# ---------------------------------------------------------------- 扩展列表按线过滤


def test_installed_list_only_shows_current_line(qapp, tmp_path: Path, monkeypatch) -> None:
    """扩展列表只显示**当前线**装了的包。

    为什么：4.6 与 4.7 的包互不通用（本体、代码层、锚点全不同），
    一条线的包在另一条线上不参与挂载。混在一张表里，玩家会以为
    「可以跨线装」，去点启用只得到「不在此线」这种不知该干什么的提示。

    判据必须用 ``lines`` 真值来源，不能只看顶层 ``engine_line`` ——
    后者只是「最近一次安装」的镜像，一条包同时装在两条线上时它只记
    最后一次，拿它过滤会把另一条线的记录错判成「不在本线」。
    """
    from app.ui.pyside.pages.extensions import ExtensionsPage

    reg = tmp_path / "ext.json"
    reg.write_text(
        json.dumps(
            {
                "schema": 1,
                "items": [
                    {"id": "only46", "name": "只在4.6", "enabled": True,
                     "engine_line": "4.6", "lines": {"4.6": {"line": "4.6", "status": "ready"}}},
                    {"id": "only47", "name": "只在4.7", "enabled": True,
                     "engine_line": "4.7", "lines": {"4.7": {"line": "4.7", "status": "ready"}}},
                    # 顶层 engine_line 是 4.7，但两条线都装了 —— 必须两边都出现
                    {"id": "both", "name": "两条都有", "enabled": True, "engine_line": "4.7",
                     "lines": {"4.6": {"line": "4.6", "status": "ready"},
                               "4.7": {"line": "4.7", "status": "ready"}}},
                ],
            }
        ),
        encoding="utf-8",
    )
    import app.core.ext.service as service_mod

    monkeypatch.setattr(service_mod, "REGISTRY", reg)

    page = ExtensionsPage(_cfg(tmp_path), lambda *_a, **_k: None)

    def names(line: str) -> list[str]:
        page.set_current_line(line)
        page._last_reload_stamp = None
        page.reload_installed(force=True)
        t = page.installed_table
        return [t.item(i, 0).text() for i in range(t.rowCount())]

    assert names("4.6") == ["只在4.6", "两条都有"]
    assert names("4.7") == ["只在4.7", "两条都有"]
