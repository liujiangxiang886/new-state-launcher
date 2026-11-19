"""小游戏页：注册本机 exe、独立启动、存档隔离。

小游戏是独立进程，与游戏本体互不影响。每个条目可以有自己的参数与工作目录，
默认把 ``APPDATA/LOCALAPPDATA`` 重定向到 ``profiles/<存档>/minigames/<id>``，
这样存档、配置都关在自己的目录里。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileInfo, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileIconProvider,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ....core import minigames as mini
from ....core import paths
from .. import colors
from ..widgets import Card, PageHeader, Pill, placeholder_notice, section_title


class MinigameDialog(QDialog):
    """编辑一个小游戏条目（名称 / 参数 / 工作目录 / 存档隔离）。"""

    def __init__(self, mg: mini.Minigame, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mg = mg
        self.setWindowTitle(f"编辑：{mg.name}")
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self.name_edit = QLineEdit(mg.name)
        form.addRow("名称", self.name_edit)

        exe_row = QHBoxLayout()
        self.exe_edit = QLineEdit(mg.exe)
        exe_row.addWidget(self.exe_edit, 1)
        browse_exe = QPushButton("选择…")
        browse_exe.setProperty("variant", "ghost")
        browse_exe.clicked.connect(self._pick_exe)
        exe_row.addWidget(browse_exe)
        form.addRow("程序", exe_row)

        self.args_edit = QPlainTextEdit("\n".join(mg.args))
        self.args_edit.setPlaceholderText("每行一个参数；不需要参数就留空")
        self.args_edit.setFixedHeight(84)
        form.addRow("启动参数", self.args_edit)

        cwd_row = QHBoxLayout()
        self.cwd_edit = QLineEdit(mg.workdir)
        cwd_row.addWidget(self.cwd_edit, 1)
        browse_cwd = QPushButton("选择…")
        browse_cwd.setProperty("variant", "ghost")
        browse_cwd.clicked.connect(self._pick_cwd)
        cwd_row.addWidget(browse_cwd)
        form.addRow("工作目录", cwd_row)

        layout.addLayout(form)

        self.isolate = QCheckBox("隔离存档（把 APPDATA / LOCALAPPDATA 指到 profiles 下的独立目录）")
        self.isolate.setChecked(mg.isolate_save)
        layout.addWidget(self.isolate)

        extra = QFormLayout()
        extra.setSpacing(8)
        self.env_edit = QPlainTextEdit(
            "\n".join(f"{k}={v}" for k, v in (mg.env or {}).items())
        )
        self.env_edit.setPlaceholderText("每行一个 KEY=VALUE，例如 JJXF_DEBUG=1")
        self.env_edit.setFixedHeight(66)
        extra.addRow("环境变量", self.env_edit)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 24 * 60 * 60)
        self.timeout_spin.setSingleStep(60)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setSpecialValueText("不限时")
        self.timeout_spin.setValue(int(mg.timeout_sec or 0))
        self.timeout_spin.setFixedWidth(140)
        extra.addRow("超时自动结束", self.timeout_spin)

        self.tags_edit = QLineEdit(", ".join(mg.tags or []))
        self.tags_edit.setPlaceholderText("逗号分隔，仅用于自己归类")
        extra.addRow("标签", self.tags_edit)
        layout.addLayout(extra)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择程序", str(Path.home()), "程序 (*.exe)")
        if path:
            self.exe_edit.setText(path)
            if not self.cwd_edit.text().strip():
                self.cwd_edit.setText(str(Path(path).parent))

    def _pick_cwd(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择工作目录", self.cwd_edit.text() or str(Path.home()))
        if path:
            self.cwd_edit.setText(path)

    def apply_to(self) -> mini.Minigame:
        mg = self.mg
        mg.name = self.name_edit.text().strip() or mg.name
        mg.exe = self.exe_edit.text().strip() or mg.exe
        mg.args = [ln.strip() for ln in self.args_edit.toPlainText().splitlines() if ln.strip()]
        mg.workdir = self.cwd_edit.text().strip()
        mg.isolate_save = self.isolate.isChecked()
        env: dict[str, str] = {}
        for line in self.env_edit.toPlainText().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key:
                env[key] = value.strip()
        mg.env = env
        mg.timeout_sec = int(self.timeout_spin.value())
        mg.tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        return mg


class MinigamesPage(QWidget):
    def __init__(self, cfg: dict, log, proc, current_profile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._log = log
        self.proc = proc
        self._profile = current_profile
        self._icons = QFileIconProvider()
        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = PageHeader("小游戏", "登记本机的独立 exe，启动时按需隔离存档；也可以把 exe 直接拖进来。")
        self.state_pill = Pill("空闲", "muted")
        header.add_action(self.state_pill)
        self.add_btn = QPushButton("添加程序…")
        self.add_btn.setProperty("variant", "primary")
        self.add_btn.clicked.connect(self._pick_exe)
        header.add_action(self.add_btn)
        root.addWidget(header)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "程序", "参数", "存档隔离", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setIconSize(QSize(16, 16))
        head = self.table.horizontalHeader()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemDoubleClicked.connect(lambda _item: self._edit_selected())
        root.addWidget(self.table, 1)

        actions = Card()
        row = QHBoxLayout(actions)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(8)
        self.launch_btn = QPushButton("启动选中")
        self.launch_btn.setProperty("variant", "primary")
        self.launch_btn.setEnabled(False)
        self.launch_btn.clicked.connect(self._launch_selected)
        row.addWidget(self.launch_btn)
        self.stop_btn = QPushButton("结束")
        self.stop_btn.setProperty("variant", "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.proc.stop)
        row.addWidget(self.stop_btn)
        self.edit_btn = QPushButton("编辑…")
        self.edit_btn.setProperty("variant", "ghost")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self._edit_selected)
        row.addWidget(self.edit_btn)
        self.remove_btn = QPushButton("移除")
        self.remove_btn.setProperty("variant", "ghost")
        self.remove_btn.setEnabled(False)
        self.remove_btn.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_btn)
        self.open_btn = QPushButton("打开存档目录")
        self.open_btn.setProperty("variant", "ghost")
        self.open_btn.clicked.connect(self._open_save_dir)
        row.addWidget(self.open_btn)
        row.addStretch(1)
        root.addWidget(actions)

        self.placeholder = placeholder_notice(
            "还没有登记小游戏。\n点右上角「添加程序…」选一个 exe，或直接把 exe 拖进窗口。"
        )
        root.addWidget(self.placeholder, 1)

        self.proc.stateChanged.connect(self._on_state)
        self.proc.finished.connect(self._on_finished)

        self.reload()

    # ------------------------------------------------------------ 数据

    def icon_for(self, exe: str) -> QIcon:
        try:
            return self._icons.icon(QFileInfo(exe))
        except Exception:  # noqa: BLE001 - 取不到图标不算错误
            return QIcon()

    def reload(self) -> None:
        items = mini.load()
        self.table.setRowCount(0)
        for mg in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            name_item = QTableWidgetItem(mg.name)
            icon = self.icon_for(mg.exe)
            if not icon.isNull():
                name_item.setIcon(icon)
            name_item.setData(Qt.ItemDataRole.UserRole, mg.id)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(mg.exe))
            args_text = " ".join(mg.args)
            self.table.setItem(row, 2, QTableWidgetItem(args_text or "—"))
            iso = QTableWidgetItem("是" if mg.isolate_save else "否")
            iso.setForeground(QBrush(QColor(colors.ok() if mg.isolate_save else colors.faint())))
            self.table.setItem(row, 3, iso)
            state = QTableWidgetItem("就绪" if mg.exists else "文件缺失")
            state.setForeground(QBrush(QColor(colors.ok() if mg.exists else colors.err())))
            self.table.setItem(row, 4, state)

        self.placeholder.setVisible(not items)
        self.table.setVisible(bool(items))
        self.state_pill.setText(f"{len(items)} 个程序" if items else "空闲")
        self.state_pill.set_tone("ok" if items else "muted")

    def _selected(self) -> mini.Minigame | None:
        rows = {i.row() for i in self.table.selectedItems()}
        if not rows:
            return None
        item = self.table.item(sorted(rows)[0], 0)
        mg_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        return mini.get(mg_id) if mg_id else None

    def _on_selection(self) -> None:
        has = self._selected() is not None
        self.launch_btn.setEnabled(has and not self.proc.running)
        self.edit_btn.setEnabled(has)
        self.remove_btn.setEnabled(has)

    # ------------------------------------------------------------ 操作

    def _pick_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择小游戏程序", str(Path.home()), "程序 (*.exe)")
        if path:
            self.add_exe(Path(path))

    def add_exe(self, path: Path) -> None:
        try:
            mg = mini.add(path)
        except (FileNotFoundError, OSError) as exc:
            self._log(f"添加失败：{exc}", "err")
            return
        self._log(f"已登记小游戏：{mg.name} → {mg.exe}", "ok")
        self.reload()

    def _edit_selected(self) -> None:
        mg = self._selected()
        if not mg:
            return
        before = mg.as_dict()
        dialog = MinigameDialog(mg, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.apply_to()
        if not Path(updated.exe).exists():
            self._log(f"程序不存在，未保存：{updated.exe}", "err")
            return
        mini.update(updated)
        changed = [k for k in ("name", "exe", "args", "workdir", "isolate_save") if before.get(k) != updated.as_dict().get(k)]
        self._log(f"已保存 {updated.name}（改动：{'、'.join(changed) or '无'}）", "ok")
        self.reload()

    def _remove_selected(self) -> None:
        mg = self._selected()
        if not mg:
            return
        mini.remove(mg.id)
        self._log(f"已移除：{mg.name}", "dim")
        self.reload()

    def _launch_selected(self) -> None:
        mg = self._selected()
        if not mg:
            return
        if self.proc.running:
            self._log("已有进程在运行，先结束它。", "warn")
            return
        program, args, cwd, env = mini.build_invocation(mg, self.cfg, self._profile())
        self._log(f"启动小游戏：{mg.name}（存档隔离={'开' if mg.isolate_save else '关'}）", "info")
        self.state_pill.setText("运行中")
        self.state_pill.set_tone("ok")
        self.launch_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.proc.start_external(
            program, args, cwd, env, label=mg.name, timeout_sec=int(mg.timeout_sec or 0)
        )

    def _open_save_dir(self) -> None:
        mg = self._selected() or (mini.load() or [None])[0]
        target = mini.save_dir(mg, self._profile()) if mg else paths.PROFILES
        target.mkdir(parents=True, exist_ok=True)
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # ------------------------------------------------------------ 进程状态

    def _on_state(self, state: str) -> None:
        running = state in ("starting", "running")
        self.stop_btn.setEnabled(running)
        self.launch_btn.setEnabled(not running and self._selected() is not None)
        if state == "starting":
            self.state_pill.setText("启动中")
            self.state_pill.set_tone("warn")
        elif state == "exited":
            self.state_pill.setText("空闲")
            self.state_pill.set_tone("muted")

    def _on_finished(self, summary: dict) -> None:
        rc = summary.get("returncode")
        secs = summary.get("seconds")
        if summary.get("ok"):
            self._log(f"小游戏正常退出（rc={rc}，{secs} 秒）", "ok")
        elif summary.get("killed"):
            self._log(f"小游戏已被结束（{secs} 秒）", "warn")
        else:
            self._log(f"小游戏异常退出，rc={rc}", "err")

    # ------------------------------------------------------------ 拖拽

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._dropped(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._dropped(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._dropped(event)
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.add_exe(path)

    def _dropped(self, event) -> Path | None:
        if not event.mimeData().hasUrls():
            return None
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if p.suffix.lower() == ".exe":
                return p
        return None
