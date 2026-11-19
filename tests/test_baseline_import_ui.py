"""自定义基线的界面入口 + 产品中文名回归。

之前导入只走 CLI，设置页与自定义线对话框里都没有入口；这一组测试钉住
「两个入口都在」以及「改名只动 edition.app_name() 一处」。
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

from app import edition  # noqa: E402
from app.core import custom_baseline as cb  # noqa: E402
from app.core import custom_lines as cl  # noqa: E402
from app.core import identity_gate  # noqa: E402
from app.ui.pyside.pages.baseline_import_dialog import BaselineImportDialog  # noqa: E402
from app.ui.pyside.pages.custom_lines_dialog import CustomLinesDialog  # noqa: E402

HASH = "a" * 64
SCRIPT = "extends Node\n\nfunc combat_power():\n\treturn 1\n"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def cfg() -> dict:
    # SettingsPage 会硬索引 engines/baselines/profiles，用真实默认配置补齐，
    # 再覆盖成测试用的单条自定义线（与 tests/test_detail_mode.py 同款做法）。
    from app.core import config as cfg_mod

    data = cfg_mod.load()
    data["custom_lines"] = [{
        "id": "custom:demo1", "name": "旋风改版", "kind": "pck",
        "path": "D:/games/body.pck", "engine_line": "4.7",
        "identity_hash": HASH, "identity_engine_line": "4.7",
        "identity_baseline": "jjxf.v1.45", "access_tier": "full",
    }]
    return data


def _baseline_dir(tmp: Path) -> Path:
    root = tmp / "baseline"
    src = root / "sources" / "scripts"
    src.mkdir(parents=True)
    (src / "game.gd").write_text(SCRIPT, encoding="utf-8")
    (root / "index.json").write_text(json.dumps({
        "files": ["scripts/game.gd"],
        "symbols": {"scripts/game.gd": ["combat_power"]},
        "anchors": {"scripts/game.gd": ["func combat_power("]},
    }), encoding="utf-8")
    (root / "baseline.json").write_text(json.dumps({
        "schema": 1, "identity_hash": HASH, "engine_line": "4.7",
        "source_dir": "sources", "index": "index.json",
    }), encoding="utf-8")
    return root


# ---------------------------------------------------------------- 中文名

def test_main_window_constructs_with_chinese_title(qapp) -> None:
    # 2026-11-11 的真实事故：改名时 _build_sidebar 用了 _edition 却没导入，
    # 界面测试全绿但 MainWindow 一构造就 NameError、GUI 起不来。
    # 钉住「真实类名构造主窗口」这一步，防再次半改。
    from PySide6.QtCore import QCoreApplication, QEvent

    from app.ui.pyside.main_window import MainWindow

    cfg_data = __import__("app.core.config", fromlist=["load"]).load()
    w = MainWindow(cfg_data)
    assert w.windowTitle() == "新起点启动器"

    # 必须真的销毁，不能只写 deleteLater()：没有事件循环时 DeferredDelete
    # 永远不会被派发，窗口会一直活到解释器退出。之后 shiboken 去析构对应的
    # C++ 对象就是 access violation ／ ``Fatal Python error: Aborted``，
    # 表现为「报告写着全绿，但进程退出码是 3/127/139」。
    w.close()
    w.setParent(None)
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_app_name_is_chinese() -> None:
    assert edition.app_name() == "新起点启动器"


def test_no_hardcoded_english_title_left() -> None:
    for rel in ("app/ui/pyside/main_window.py", "app/ui/pyside/mecha_shell.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert 'New state launcher"' not in text, f"{rel} 还有硬编码英文标题"


def test_notice_popup_title_uses_product_name() -> None:
    # 不构造弹窗：它带 15 秒 QTimer，在 pytest 里会让进程不干净退出。
    # 直接钉住「弹窗标题走 app_name()」这一行源码即可。
    text = (ROOT / "app/ui/pyside/notice_dialog.py").read_text(encoding="utf-8")
    assert "edition.app_name()" in text
    assert "使用前须知" in text


# ---------------------------------------------------------------- 界面入口

def _hosted_settings_page(qapp, cfg):
    """把 SettingsPage 放进一个可见宿主里再返回。

    不 show 的 widget 即使 setVisible(True) 也返回 isVisible()==False，
    没法测「卡片显示/隐藏」这种可见性逻辑 —— 所以包一层 QDialog 并 show。
    """
    from PySide6.QtWidgets import QDialog, QVBoxLayout

    from app.ui.pyside.pages.settings import SettingsPage

    page = SettingsPage(cfg, lambda *a, **k: None)
    host = QDialog()
    host.setLayout(QVBoxLayout())
    host.layout().addWidget(page)
    host.show()
    qapp.processEvents()
    return page, host


def test_settings_page_has_baseline_card(qapp, cfg) -> None:
    from PySide6.QtWidgets import QPushButton

    from app.ui.pyside.pages.settings import SettingsPage

    page = SettingsPage(cfg, lambda *a, **k: None)
    assert hasattr(page, "baseline_state")
    # 开发者模式默认关闭 → 自定义基线卡片**不显示**（面向改版创作者，
    # 普通玩家看不到这个入口）。控件仍在树里（隐藏而非删除）。
    assert page.baseline_card.isVisible() is False
    texts = [b.text() for b in page.baseline_card.findChildren(QPushButton)]
    assert any("导入自定义基线" in t for t in texts)
    assert any("查看导入准则" in t for t in texts)
    assert "已导入且校验有效" in page.baseline_state.text()


def test_baseline_card_visibility_follows_dev_mode(qapp, cfg) -> None:
    from PySide6.QtWidgets import QPushButton

    from app.ui.pyside.pages.settings import SettingsPage

    # 开启开发者模式 → 卡片显示
    cfg.setdefault("ui", {})["developer_mode"] = True
    page, host = _hosted_settings_page(qapp, cfg)
    assert page.baseline_card.isVisible() is True
    texts = [b.text() for b in page.baseline_card.findChildren(QPushButton)]
    assert any("导入自定义基线" in t for t in texts)
    assert "已导入且校验有效" in page.baseline_state.text()

    # 关闭开发者模式 → 卡片立即隐藏（勾选即生效，不用点保存）
    page.dev_check.setChecked(False)
    qapp.processEvents()
    assert page.baseline_card.isVisible() is False
    host.close()


def test_custom_lines_dialog_has_baseline_button(qapp, cfg) -> None:
    dlg = CustomLinesDialog(cfg)
    assert hasattr(dlg, "baseline_btn")
    assert not dlg.baseline_btn.isEnabled()  # 未选线时禁用
    assert "导入基线" in dlg.baseline_btn.text()


def test_import_dialog_preselects_line(qapp, cfg) -> None:
    dlg = BaselineImportDialog(cfg, line_id="custom:demo1")
    assert dlg.line_box.currentData() == "custom:demo1"
    assert dlg.line_box.count() == 1
    assert dlg.verify_btn.isEnabled() and dlg.import_btn.isEnabled()


def test_import_dialog_verify_and_import(qapp, cfg, tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    root = _baseline_dir(tmp_path)
    monkeypatch.setattr(
        identity_gate, "probe",
        lambda p: SimpleNamespace(accepted=True, identity_hash=HASH,
                                  engine_line="4.7", reason="ok"),
    )
    monkeypatch.setattr(cb, "_body_paths",
                        lambda p: ({"scripts/game.gd", "scripts/game.gdc"}, ""))
    monkeypatch.setattr(cb.paths, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    dlg = BaselineImportDialog(cfg, line_id="custom:demo1")
    dlg.dir_edit.setText(str(root))

    dlg._run(do_import=False)
    assert "校验通过" in dlg.detail.text()
    assert "已导入" not in dlg.status.text()

    dlg._run(do_import=True)
    assert "已导入并登记" in dlg.detail.text()
    assert "已导入并有效" in dlg.status.text()
    assert cb.status("custom:demo1", HASH)["ready"]


def test_import_dialog_reports_bad_baseline(qapp, cfg, tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    root = _baseline_dir(tmp_path)
    meta = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
    meta["identity_hash"] = "f" * 64
    (root / "baseline.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(
        identity_gate, "probe",
        lambda p: SimpleNamespace(accepted=True, identity_hash=HASH,
                                  engine_line="4.7", reason="ok"),
    )
    monkeypatch.setattr(cb, "_body_paths",
                        lambda p: ({"scripts/game.gd"}, ""))
    monkeypatch.setattr(cb.paths, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    dlg = BaselineImportDialog(cfg, line_id="custom:demo1")
    dlg.dir_edit.setText(str(root))
    dlg._run(do_import=False)
    assert "未通过" in dlg.detail.text()
    assert "identity_hash" in dlg.detail.text()
