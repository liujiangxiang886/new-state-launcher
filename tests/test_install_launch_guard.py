"""导入与启动的互斥契约（2026-11-17 用户报的缺陷）。

用户原话：「导入 mod 的时候也可以启动游戏」。这不该允许：导入要重写
``packs/<id>/overlay-*.pck``、改注册表，这段窗口里启动只会读到半装状态。

这里钉住四件事：
1. 导入前必须弹提示，且**原句**在（用户明确要求的文案）；
2. 用户取消 → 不排队、不动状态；
3. 导入进行中：启动按钮禁用，且 ``_launch`` 直接拒绝（绕过禁用态的入口）；
4. 游戏在跑时：导入被拒（反方向同样会读到半装状态）。
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
from qt_cleanup import dispose  # noqa: E402

REQUIRED = "这可能需要一些时间进行导入，在此期间无法启动游戏。"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def window(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent

    from app.ui.pyside.main_window import MainWindow

    data = cfg_mod.load()
    data.setdefault("ui", {})["last_version"] = "4.6"
    w = MainWindow(data)
    yield w
    w.close()
    w.setParent(None)
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    dispose(w)


class _FakeBox:
    """替掉 QMessageBox：只记录调用，不真的等用户点。"""

    from PySide6.QtWidgets import QMessageBox as _MB

    Icon = _MB.Icon
    ButtonRole = _MB.ButtonRole

    instances: list["_FakeBox"] = []
    #: 下一次 exec() 之后 clickedButton() 返回哪个按钮：True=接受，False=取消
    accept_next = True

    def __init__(self, parent=None) -> None:
        self.text = ""
        self.info = ""
        self.title = ""
        self.buttons: list[tuple[str, object, object]] = []
        _FakeBox.instances.append(self)

    def setIcon(self, *a) -> None: ...
    def setWindowTitle(self, t) -> None:
        self.title = t

    def setText(self, t) -> None:
        self.text = t

    def setInformativeText(self, t) -> None:
        self.info = t

    def addButton(self, text, role):
        btn = object()
        self.buttons.append((text, btn, role))
        return btn

    def setDefaultButton(self, *a) -> None: ...
    def setEscapeButton(self, *a) -> None: ...
    def exec(self) -> int:
        return 1

    def clickedButton(self):
        # 约定：最后一个按钮是「取消」，第一个是接受
        return self.buttons[0][1] if _FakeBox.accept_next else self.buttons[-1][1]

    @staticmethod
    def information(parent=None, title="", text=""):
        box = _FakeBox(parent)
        box.title = title
        box.text = text
        return None


@pytest.fixture()
def fake_box(monkeypatch):
    import app.ui.pyside.main_window as mw

    _FakeBox.instances = []
    _FakeBox.accept_next = True
    monkeypatch.setattr(mw, "QMessageBox", _FakeBox)
    return _FakeBox


def test_confirm_import_shows_required_sentence(window, fake_box) -> None:
    assert window._confirm_import(3, "4.6") is True
    box = fake_box.instances[-1]
    assert box.text == REQUIRED
    assert "4.6" in box.info and "3" in box.info


def test_cancel_import_keeps_queue_untouched(window, fake_box) -> None:
    fake_box.accept_next = False
    before = list(window._install_queue)

    window.install_extension([Path("D:/tmp/demo.zip")])

    assert window._install_queue == before
    assert window._install_active is False


def test_launch_button_disabled_while_importing(window, fake_box) -> None:
    window._install_active = True
    window._sync_launch_button()

    assert window.side_launch.isEnabled() is False
    assert "导入" in window.side_launch.text()
    # 游戏库页那个启动按钮必须同步受控，两处结论不能分叉
    assert window.library.launch_btn.isEnabled() is False


def test_launch_is_refused_while_importing(window, fake_box) -> None:
    called: list[str] = []

    def boom(*_a, **_k):
        called.append("launch")
        raise AssertionError("导入期间不该继续走启动流程")

    window._install_active = True
    window._ensure_instance = boom            # type: ignore[assignment]
    window._launch("4.6")

    assert called == []                        # 压根没进启动准备
    assert REQUIRED in fake_box.instances[-1].text


def test_import_refused_while_game_running(window, fake_box) -> None:
    class _Running:
        running = True

    window.instances[1] = _Running()           # type: ignore[assignment]
    try:
        assert window._confirm_import(1, "4.7") is False
        assert "退出游戏" in fake_box.instances[-1].text
    finally:
        window.instances.pop(1, None)


def test_install_state_recovers_when_worker_start_fails(window, fake_box) -> None:
    """队列推进抛异常时必须复位，否则启动按钮永远停在「导入中…」。"""

    def boom() -> None:
        raise RuntimeError("磁盘满了")

    window._install_active = True
    window._start_next_install = boom          # type: ignore[assignment]
    window._start_next_install_guarded()

    assert window._install_active is False
    assert "导入中" not in window.side_launch.text()
