"""导入游戏本体时的「先探测、再登记」对话框。

以前导入只弹一句「把下面这个文件登记为基线？」——玩家选错了 exe/pck
也无从判断，登记完还会因为 ``size`` 对不上被判「文件缺失」。这里把
探测结果摆出来，让用户在写配置之前就能看见：类型、格式、条目数、结论。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ....core import baseline_ops
from .. import colors


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class ProbeDialog(QDialog):
    """展示 :class:`baseline_ops.BaselineProbe` 的识别结果与校验结论。"""

    def __init__(
        self,
        probe: baseline_ops.BaselineProbe,
        issues: list[baseline_ops.LineIssue],
        line: str,
        title: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._probe = probe
        self._issues = issues
        self.setWindowTitle(f"导入游戏本体 — {title}")
        self.setMinimumWidth(600)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        intro = QLabel(
            f"已识别这个文件。确认无误后再登记为 {line} 线的基线；"
            "登记只记录路径，不复制、不改动文件本身。"
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.addRow("文件", self._path_label(probe.path.as_posix()))
        kind_text = (
            "exe（内嵌 pck，尾部带 GDPC 标记）"
            if probe.kind == "exe"
            else "pck（独立数据包）"
        )
        form.addRow("类型", QLabel(kind_text))
        form.addRow("大小", QLabel(f"{probe.size:,} 字节（{human_size(probe.size)}）"))
        if probe.kind == "exe":
            form.addRow(
                "数据包",
                QLabel(f"内嵌 {probe.pck_size:,} 字节（{human_size(probe.pck_size)}）"),
            )
        form.addRow(
            "格式",
            QLabel(
                f"pack_version = {probe.pack_version} → 引擎线 "
                f"{probe.engine_line or '未知'}（{probe.engine_text}）"
            ),
        )
        form.addRow(
            "条目",
            QLabel(f"{probe.entry_count:,} 条" if probe.entry_count else "未读取"),
        )
        root.addLayout(form)

        verdict = QWidget()
        vbox = QVBoxLayout(verdict)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(6)
        for issue in issues:
            label = QLabel(f"{'✖' if issue.level == 'error' else '⚠' if issue.level == 'warn' else 'ℹ'} {issue.message}")
            label.setWordWrap(True)
            color = (
                colors.err()
                if issue.level == "error"
                else colors.warn()
                if issue.level == "warn"
                else colors.dim()
            )
            label.setStyleSheet(
                f"background:{colors.tint(color, 0.10)}; border:1px solid {color};"
                f"border-radius:8px; padding:8px 10px; color:{color}; font-size:12px;"
            )
            vbox.addWidget(label)
        if not issues:
            ok_label = QLabel("✔ 可以登记为这条线的基线")
            ok_label.setStyleSheet(f"color:{colors.ok()}; font-size:12px;")
            vbox.addWidget(ok_label)
        root.addWidget(verdict)

        self.smoke_box = QCheckBox("导入后立即跑一次挂载冒烟（约 30–180 秒；失败会自动撤销本次导入）")
        self.smoke_box.setChecked(True)
        root.addWidget(self.smoke_box)

        self.rebuild_box = QCheckBox("重建基线索引（缺失时自动建；已存在会重新生成）")
        self.rebuild_box.setChecked(True)
        root.addWidget(self.rebuild_box)

        buttons = QDialogButtonBox()
        self.adopt_btn = buttons.addButton("登记这个文件", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.adopt_btn.setEnabled(baseline_ops.can_adopt(probe, line))
        if not self.adopt_btn.isEnabled():
            self.adopt_btn.setToolTip("校验未通过，不能登记；请按上面的提示换一个文件。")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(buttons)
        root.addLayout(row)

    @staticmethod
    def _path_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet("font-size:12px;")
        return label

    def smoke_enabled(self) -> bool:
        return self.smoke_box.isChecked()

    def rebuild_enabled(self) -> bool:
        return self.rebuild_box.isChecked()

    def accept(self) -> None:  # noqa: D102 - Qt 命名
        super().accept()
