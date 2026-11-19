"""导入角色存档向导。

为什么做成「三步向导」而不是一个文件选择框
------------------------------------------

玩家的真实困惑不是「怎么点按钮」，而是**不知道存档在哪**：
原版游戏把角色存档写在 ``%APPDATA%\\Godot\\app_userdata\\机甲旋风 · 单机``，
而启动器的底座工程名不同，写的是 ``机甲旋风 · 兼容底座 4.6``。两个目录名
不一样，所以「换了启动器，角色就没了」。光给一个「选择文件」按钮，玩家
依然找不到东西。

因此这里按「① 原版存档在哪 → ② 选哪一份 → ③ 导到哪里」串成三步，
每步都把**完整绝对路径**摆在界面上（可选中、可复制、可一键打开），
并在第 3 步明确写出「导入前会先备份当前存档」。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ....core import userdir
from ....core.errors import LauncherError
from .. import colors
from ..widgets import Card


def _open_path(path) -> None:
    from pathlib import Path

    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


def _path_label(text: str) -> QLabel:
    """等宽、可选、可换行的路径块——长路径不许把对话框撑宽。"""
    label = QLabel(text)
    label.setObjectName("PathText")
    label.setWordWrap(True)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setMinimumWidth(0)
    return label


def _step(index: str, title: str, hint: str = "") -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    badge = QLabel(index)
    badge.setFixedSize(20, 20)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    badge.setStyleSheet(
        f"background:{colors.tint(colors.accent(), 0.20)}; color:{colors.accent_hover()};"
        f"border:1px solid {colors.tint(colors.accent(), 0.55)}; border-radius:10px;"
        "font-size:11px; font-weight:700;"
    )
    row.addWidget(badge)
    head = QLabel(title)
    head.setObjectName("SectionTitle")
    row.addWidget(head)
    if hint:
        extra = QLabel(hint)
        extra.setObjectName("Faint")
        row.addWidget(extra)
    row.addStretch(1)
    return w


class SaveImportDialog(QDialog):
    """玩家原有角色存档 → 启动器档位。"""

    #: 导入成功后带出结果字典（主窗口用来刷日志 / 刷新界面）
    imported = Signal(dict)

    def __init__(self, cfg: dict, profile: str, line: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.profile = profile
        self.line = line
        #: 下拉框每一项对应的来源（索引与 combo 一一对应）
        self._items: list[userdir.Source] = []
        self._done = False

        self.setWindowTitle("导入角色存档")
        self.setModal(True)
        self.setMinimumWidth(760)
        self.setMinimumHeight(560)
        self.resize(820, 700)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        title = QLabel("导入角色存档")
        title.setObjectName("CardTitle")
        root.addWidget(title)
        sub = QLabel(
            "把原版游戏（或旧启动器）里的角色存档复制进启动器的当前存档档位。\n"
            "原文件只读不改：复制过去，不会删除也不会覆盖你原来那份。"
        )
        sub.setObjectName("Faint")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # 三步卡片放进滚动区：窗口只有 660 高时（1366×768 笔记本），
        # 直接堆在对话框里会把文件表格压成一条缝。滚动区保证任何高度下
        # 表格都能显示完整几行，按钮也永远够得着。
        body = QWidget()
        body_col = QVBoxLayout(body)
        body_col.setContentsMargins(0, 0, 0, 0)
        body_col.setSpacing(10)
        scroll = QScrollArea()
        scroll.setObjectName("ImportScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # ---------------- 第 1 步：原版存档在哪
        step1 = Card()
        s1 = QVBoxLayout(step1)
        s1.setContentsMargins(14, 12, 14, 12)
        s1.setSpacing(6)
        s1.addWidget(_step("1", "游戏默认存档位置", "直接双击原版游戏时，角色存档就存在这里"))
        self.original_label = _path_label(userdir.original_dir().as_posix())
        s1.addWidget(self.original_label)
        self.original_state = QLabel("")
        self.original_state.setObjectName("Faint")
        self.original_state.setWordWrap(True)
        s1.addWidget(self.original_state)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        open_btn = QPushButton("打开这个目录")
        open_btn.setProperty("variant", "ghost")
        open_btn.clicked.connect(lambda: _open_path(userdir.original_dir()))
        copy_btn = QPushButton("复制路径")
        copy_btn.setProperty("variant", "ghost")
        copy_btn.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(userdir.original_dir().as_posix())
        )
        row1.addWidget(open_btn)
        row1.addWidget(copy_btn)
        row1.addStretch(1)
        s1.addLayout(row1)
        body_col.addWidget(step1)

        # ---------------- 第 2 步：选来源
        step2 = Card()
        s2 = QVBoxLayout(step2)
        s2.setContentsMargins(14, 12, 14, 12)
        s2.setSpacing(6)
        s2.addWidget(_step("2", "选择要导入的存档", "自动列出本机所有可能的存档目录"))
        pick_row = QHBoxLayout()
        pick_row.setSpacing(8)
        self.source_box = QComboBox()
        self.source_box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.source_box.currentIndexChanged.connect(lambda _i: self._refresh_files())
        pick_row.addWidget(self.source_box, 1)
        browse = QPushButton("手动选择…")
        browse.setProperty("variant", "ghost")
        browse.clicked.connect(self._browse)
        pick_row.addWidget(browse)
        s2.addLayout(pick_row)

        self.files = QTableWidget(0, 3)
        self.files.setHorizontalHeaderLabels(["文件", "大小", "修改时间"])
        self.files.verticalHeader().setVisible(False)
        self.files.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.files.setMinimumHeight(118)
        self.files.setMaximumHeight(150)
        self.files.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.files.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        s2.addWidget(self.files)
        self.files_hint = QLabel("")
        self.files_hint.setObjectName("Faint")
        self.files_hint.setWordWrap(True)
        s2.addWidget(self.files_hint)
        body_col.addWidget(step2)

        # ---------------- 第 3 步：导到哪里
        step3 = Card()
        s3 = QVBoxLayout(step3)
        s3.setContentsMargins(14, 12, 14, 12)
        s3.setSpacing(6)
        s3.addWidget(_step("3", "确认导入位置", "启动器里这条版本线的真实存档目录"))
        self.target_label = _path_label("")
        s3.addWidget(self.target_label)
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        open_target = QPushButton("打开目标目录")
        open_target.setProperty("variant", "ghost")
        open_target.clicked.connect(lambda: _open_path(userdir.launcher_dir(self.profile, self.line, self.cfg)))
        copy_target = QPushButton("复制路径")
        copy_target.setProperty("variant", "ghost")
        copy_target.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(
                userdir.launcher_dir(self.profile, self.line, self.cfg).as_posix()
            )
        )
        row3.addWidget(open_target)
        row3.addWidget(copy_target)
        row3.addStretch(1)
        s3.addLayout(row3)

        self.backup_box = QCheckBox("导入前先备份当前存档（推荐，备份放在 .history/ 里可还原）")
        self.backup_box.setChecked(True)
        s3.addWidget(self.backup_box)
        body_col.addWidget(step3)

        # ---------------- 底部
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("Faint")
        root.addWidget(self.status)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setProperty("variant", "ghost")
        self.cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self.cancel_btn)
        self.import_btn = QPushButton("开始导入")
        self.import_btn.setProperty("variant", "primary")
        self.import_btn.clicked.connect(self._do_import)
        footer.addWidget(self.import_btn)
        root.addLayout(footer)

        self._load_sources()

    # ------------------------------------------------------------ 数据

    def _load_sources(self) -> None:
        self._items = userdir.candidates(self.cfg, self.profile, self.line)
        self.source_box.clear()
        for src in self._items:
            state = "可导入" if src.usable else "无文件"
            self.source_box.addItem(f"{src.label}　·　{state}　·　{src.path}", src.key)
        self._refresh_files()

    def _current_source(self) -> userdir.Source | None:
        idx = self.source_box.currentIndex()
        if 0 <= idx < len(self._items):
            return self._items[idx]
        return None

    def _refresh_files(self) -> None:
        src = self._current_source()
        self.files.setRowCount(0)
        if src is None:
            self.files_hint.setText("")
            self.original_state.setText("")
            self.target_label.setText(userdir.launcher_dir(self.profile, self.line, self.cfg).as_posix())
            self.import_btn.setEnabled(False)
            return

        for item in src.files:
            row = self.files.rowCount()
            self.files.insertRow(row)
            name = QTableWidgetItem(item.rel)
            if item.rel.lower().startswith("offline_roles"):
                # 角色数据本体：给个强调色，让玩家一眼确认选中的是正确的文件
                name.setForeground(QBrush(QColor(colors.accent_hover())))
                name.setToolTip("角色存档本体：导入这个才算把角色带过来")
            self.files.setItem(row, 0, name)
            size = QTableWidgetItem(userdir.human_size(item.size))
            size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.files.setItem(row, 1, size)
            when = QTableWidgetItem(time.strftime("%Y-%m-%d %H:%M", time.localtime(item.mtime)))
            self.files.setItem(row, 2, when)

        self.files_hint.setText(f"这一份：{src.describe()}　·　{src.path}")
        # 下拉框里放不下完整路径（会被省略号截断），tooltip 补全
        self.source_box.setToolTip(src.path.as_posix())
        self.import_btn.setEnabled(src.usable and not self._done)
        if not src.usable:
            self.status.setText(
                "这个来源里没有可导入的存档文件。原版存档应包含 offline_roles.json"
                "（角色数据），如果找不到，请检查是否玩的是别的客户端版本。"
            )

        original = userdir.original_dir()
        if original.is_dir():
            count = len(userdir.list_save_files(original))
            self.original_state.setText(f"已找到这个目录，其中有 {count} 个存档文件。")
            self.original_state.setStyleSheet(f"color:{colors.ok()}; font-size:11px;")
        else:
            self.original_state.setText(
                "这台机器上没有这个目录——说明原版游戏没在这台电脑上跑过，"
                "或者存档在别的盘/别的电脑上，请用第 2 步的「手动选择」导入。"
            )
            self.original_state.setStyleSheet(f"color:{colors.warn()}; font-size:11px;")
        self.target_label.setText(userdir.launcher_dir(self.profile, self.line, self.cfg).as_posix())

    def _browse(self) -> None:
        picked = QFileDialog.getExistingDirectory(
            self, "选择存档目录（里面应有 offline_roles.json）", str(userdir.original_dir().parent)
        )
        if not picked:
            return
        from pathlib import Path

        src = userdir.Source(
            key="manual",
            label=f"手动选择：{picked}",
            path=Path(picked),
            files=userdir.list_save_files(Path(picked)),
        )
        self._items.append(src)
        self.source_box.addItem(f"手动选择　·　{src.describe()}　·　{picked}", "manual")
        self.source_box.setCurrentIndex(self.source_box.count() - 1)
        self._refresh_files()

    # ------------------------------------------------------------ 执行

    def _do_import(self) -> None:
        src = self._current_source()
        if src is None:
            return
        extra = ()
        self.import_btn.setEnabled(False)
        self.status.setText("正在导入…")
        try:
            result = userdir.import_saves(
                self.profile,
                self.line,
                src.path,
                cfg=self.cfg,
                backup=self.backup_box.isChecked(),
                extra_lines=extra,
            )
        except LauncherError as exc:
            self.import_btn.setEnabled(True)
            self.status.setText("")
            QMessageBox.warning(self, "导入失败", str(exc.message))
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，避免异常把对话框卡住
            self.import_btn.setEnabled(True)
            self.status.setText("")
            QMessageBox.critical(self, "导入失败", f"未预期的错误：{exc}")
            return

        self._done = True
        lines = "、".join(f"{t['line']} 线（{t['files']} 个文件）" for t in result["targets"])
        detail = [
            f"已从：{result['source']}",
            f"导入到：{lines}",
            f"合计 {len(result.get('files') or [])} 个文件 · "
            f"{userdir.human_size(result['bytes'])}（写入 {len(result['targets'])} 条版本线）",
        ]
        if result.get("backup"):
            detail.append(f"导入前已备份原存档：{result['backup']}")
        if result.get("skipped"):
            detail.append("跳过：" + "；".join(result["skipped"][:3]))
        self.status.setText("\n".join(detail))
        self.status.setStyleSheet(f"color:{colors.ok()};")
        self.import_btn.setText("完成")
        try:
            self.import_btn.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.import_btn.clicked.connect(self.accept)
        self.cancel_btn.setText("关闭")
        self.imported.emit(result)
