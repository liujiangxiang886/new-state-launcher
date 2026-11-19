"""双开测试面板：同时跑多个游戏实例，各自独立存档目录与运行目录。

为什么必须隔离：两个实例若共用同一个 ``user://``，会互相覆盖存档、配置与
日志，测试出来的结果不可信。所以每个实例启动时用不同的 profile
（``runner.build_env`` 会把 APPDATA 指到 ``profiles/<名字>``），
运行目录也带实例号后缀，避免同一秒启动撞名。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..widgets import Pill, section_title

STATE_TEXT = {
    "idle": "空闲",
    "starting": "启动中",
    "running": "运行中",
    "stopping": "结束中",
    "exited": "已退出",
}
STATE_TONE = {
    "idle": "muted",
    "starting": "warn",
    "running": "ok",
    "stopping": "warn",
    "exited": "muted",
}
#: 这些状态下按钮显示为「结束」
ACTIVE_STATES = ("starting", "running", "stopping")


class _InstanceRow(QFrame):
    """单个实例一行：编号 + 状态 + 说明 + 启动/结束。"""

    startRequested = Signal(int)
    stopRequested = Signal(int)

    def __init__(self, slot: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.slot = slot
        self._active = False
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(46)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)

        name = QLabel(f"实例 {slot}")
        name.setFixedWidth(56)
        name.setStyleSheet("font-size:13px; font-weight:600;")
        row.addWidget(name)

        self.state = Pill(STATE_TEXT["idle"], "muted")
        row.addWidget(self.state)

        self.info = QLabel("未启动")
        self.info.setObjectName("Faint")
        self.info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.info, 1)

        self.btn = QPushButton("启动")
        self.btn.setProperty("variant", "primary")
        self.btn.setFixedSize(76, 30)
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self._on_click)
        row.addWidget(self.btn)

    def _on_click(self) -> None:
        if self._active:
            self.stopRequested.emit(self.slot)
        else:
            self.startRequested.emit(self.slot)

    def set_state(self, state: str, info: str = "") -> None:
        self._active = state in ACTIVE_STATES
        self.state.setText(STATE_TEXT.get(state, state))
        self.state.set_tone(STATE_TONE.get(state, "muted"))
        self.info.setText(info or "")
        if self._active:
            self.btn.setText("结束" if state == "running" else "…")
            self.btn.setProperty("variant", "danger")
        else:
            self.btn.setText("启动")
            self.btn.setProperty("variant", "primary")
        # variant 变了要重新套样式表，否则按钮颜色不跟着变
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        # 启动中/结束中禁止重复点击，避免连点出两个进程
        self.btn.setEnabled(state not in ("starting", "stopping"))


class InstancePanel(QWidget):
    """实例管理入口：列出所有槽位，可分别启动/结束。"""

    startRequested = Signal(int)
    stopRequested = Signal(int)

    def __init__(self, slots: int = 2, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.slots = max(1, int(slots))
        self._rows: dict[int, _InstanceRow] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.addWidget(
            section_title("双开测试", "两个实例用各自的存档目录，互不干扰；用于联机/对照测试")
        )
        for index in range(1, self.slots + 1):
            row = _InstanceRow(index, self)
            row.startRequested.connect(self.startRequested)
            row.stopRequested.connect(self.stopRequested)
            self._rows[index] = row
            root.addWidget(row)

    def set_state(self, slot: int, state: str, info: str = "") -> None:
        row = self._rows.get(int(slot))
        if row is not None:
            row.set_state(state, info)
