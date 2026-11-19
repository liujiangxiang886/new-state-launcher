"""「管理游戏线」对话框：登记多条自定义改版游戏。

只做三件事：添加、编辑、移除登记。**不删游戏文件**，也不碰扩展。

两种形态的区别（界面上必须讲清楚，否则玩家会选错文件）
------------------------------------------------------

- **exe 形态**：改版自带完整运行时，启动器直接执行它。
- **pck 形态**：作者只发了资源包（或它的 exe 是未改动的 Godot 导出模板），
  必须借启动器自带的底座引擎挂载。这时还要选**引擎线**和**主场景**。

添加流程刻意做成「先选文件、再填参数」：文件后缀决定了类型，类型决定了
后面哪些字段有意义。让玩家从空表单开始勾类型，很容易配出一个
「类型是 pck 但选了个 exe」的坏记录。

为什么单独一个文件而不是塞进 library.py
--------------------------------------
游戏库页已经 1300 多行，且这个对话框的职责与「选版本启动」无关——它管理的是
配置项。拆开之后 library 只负责展示与触发，保持单一职责。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ....core import categories as cat_mod
from ....core import custom_lines as cl
from .. import colors


class CustomLinesDialog(QDialog):
    """自定义游戏线管理器。改动立即写配置，关闭后由调用方刷新界面。"""

    def __init__(self, cfg: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("管理游戏线")
        self.setMinimumSize(880, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        #: 留着引用：隐藏表格时要动态把 stretch 让给空提示，
        #: 否则隐藏控件的 stretch 仍占位，界面会出现一大片空白。
        self._root = root

        title = QLabel("自定义游戏线")
        title.setObjectName("CardTitle")
        root.addWidget(title)
        tip = QLabel(
            "可以添加多个改版游戏，启动时在游戏库的版本下拉里选择。"
            "登记时会按<b>文件内容</b>校验是否为 Godot 机甲旋风改版，冒名或其它"
            "游戏的包不予接入。默认只启动游戏；改版作者随包提供 <b>line.json</b> "
            "描述与反解工程索引、并通过一致性校验后，才升级为「可协商扩展」。"
            "启动器不会改动游戏文件。"
        )
        tip.setObjectName("Faint")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "游戏文件", "分类", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_selection)
        root.addWidget(self.table, 1)

        self.empty_hint = QLabel(
            "还没有自定义游戏线。点下方「添加…」选择 .exe（改版自带运行时）"
            "或 .pck（只发了资源包，借底座引擎挂载）。"
        )
        self.empty_hint.setObjectName("Faint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty_hint)

        ops = QHBoxLayout()
        ops.setSpacing(8)
        self.add_btn = QPushButton("添加…")
        self.add_btn.setProperty("variant", "primary")
        self.add_btn.clicked.connect(self._add)
        ops.addWidget(self.add_btn)
        self.edit_btn = QPushButton("编辑…")
        self.edit_btn.setProperty("variant", "ghost")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self._edit)
        ops.addWidget(self.edit_btn)
        self.remove_btn = QPushButton("移除登记")
        self.remove_btn.setProperty("variant", "danger")
        self.remove_btn.setEnabled(False)
        self.remove_btn.clicked.connect(self._remove)
        ops.addWidget(self.remove_btn)
        self.baseline_btn = QPushButton("导入基线…")
        self.baseline_btn.setProperty("variant", "ghost")
        self.baseline_btn.setEnabled(False)
        self.baseline_btn.setToolTip(
            "为选中的改版导入反解源码基线（baseline.json / sources/ / index.json）。"
            "只有需要方法钩子类扩展时才用。"
        )
        self.baseline_btn.clicked.connect(self._import_baseline)
        ops.addWidget(self.baseline_btn)
        ops.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        ops.addWidget(close_btn)
        root.addLayout(ops)

        self._row_ids: list[str] = []
        self.reload()

    # ------------------------------------------------------------ 列表

    def reload(self) -> None:
        lines = cl.load_all(self.cfg)
        self._row_ids = [line.id for line in lines]
        self.table.setRowCount(0)
        for line in lines:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(line.name))
            kind_item = QTableWidgetItem("资源包" if line.is_pck else "改版 exe")
            if line.is_pck:
                kind_item.setToolTip(f"用 {line.engine_line or '?'} 底座引擎挂载")
            self.table.setItem(row, 1, kind_item)
            item = QTableWidgetItem(line.path)
            item.setToolTip(line.path)
            self.table.setItem(row, 2, item)
            self.table.setItem(row, 3, QTableWidgetItem(cat_mod.name_of(self.cfg, line.category) if line.category else cat_mod.name_of(self.cfg, cat_mod.MOD_ID)))
            state = "就绪" if line.exists else "文件缺失"
            state_item = QTableWidgetItem(state)
            if not line.exists:
                from PySide6.QtGui import QBrush, QColor

                state_item.setForeground(QBrush(QColor(colors.warn())))
            self.table.setItem(row, 4, state_item)
        self.table.setVisible(bool(lines))
        self.empty_hint.setVisible(not lines)
        # 表格隐藏后它的 stretch(1) 仍在参与分配，会把空白撑成一大片。
        # 谁可见就把 stretch 给谁。
        lay = self._root
        lay.setStretch(lay.indexOf(self.table), 1 if lines else 0)
        lay.setStretch(lay.indexOf(self.empty_hint), 0 if lines else 1)
        self._on_selection()

    def _selected_id(self) -> str:
        rows = {i.row() for i in self.table.selectedItems()}
        if not rows:
            return ""
        row = sorted(rows)[0]
        return self._row_ids[row] if 0 <= row < len(self._row_ids) else ""

    def _on_selection(self) -> None:
        has = bool(self._selected_id())
        self.edit_btn.setEnabled(has)
        self.remove_btn.setEnabled(has)
        self.baseline_btn.setEnabled(has)

    def _import_baseline(self) -> None:
        from .baseline_import_dialog import BaselineImportDialog

        line_id = self._selected_id()
        if not line_id:
            return
        dlg = BaselineImportDialog(self.cfg, line_id=line_id, parent=self)
        dlg.exec()
        self.reload()

    # ------------------------------------------------------------ 增删改

    def _add(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择游戏文件（.exe 或 .pck）",
            "",
            "游戏文件 (*.exe *.pck);;可执行文件 (*.exe);;资源包 (*.pck);;所有文件 (*)",
        )
        if not path:
            return
        picked = Path(path)
        kind = cl.KIND_PCK if picked.suffix.lower() == ".pck" else cl.KIND_EXE
        editor = LineEditor(self.cfg, kind=kind, path=str(picked), name=picked.stem, parent=self)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        values = editor.values()
        try:
            line = cl.add_line(self.cfg, values["name"], values["path"], **values["extra"])
        except ValueError as exc:
            QMessageBox.warning(self, "无法添加", str(exc))
            return
        self._save_cfg()
        self.reload()
        note = (
            f"\n\n启动时会用 {line.engine_line} 底座引擎挂载它，主场景 {line.main_scene_or_default()}。"
            if line.is_pck
            else ""
        )
        QMessageBox.information(
            self,
            "已添加",
            f"「{line.name}」已登记。\n\n存档隔离目录：{line.profile_name()}{note}\n"
            "这条线启动时不会加载任何扩展。",
        )

    def _edit(self) -> None:
        line_id = self._selected_id()
        if not line_id:
            return
        line = cl.get_line(self.cfg, line_id)
        if line is None:
            self.reload()
            return
        editor = LineEditor(
            self.cfg,
            kind=line.kind,
            path=line.path,
            name=line.name,
            engine_line=line.engine_line,
            main_scene=line.main_scene,
            args=line.args,
            profile=line.profile,
            category=line.category,
            note=line.note,
            parent=self,
        )
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        values = editor.values()
        try:
            cl.update_line(
                self.cfg,
                line_id,
                name=values["name"],
                path=values["path"],
                **values["extra"],
            )
        except ValueError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._save_cfg()
        self.reload()

    def _remove(self) -> None:
        line_id = self._selected_id()
        if not line_id:
            return
        line = cl.get_line(self.cfg, line_id)
        if line is None:
            self.reload()
            return
        answer = QMessageBox.question(
            self,
            "移除登记",
            f"从启动器里移除「{line.name}」？\n\n"
            "只删除这条登记，**不会删除游戏文件**。\n"
            f"游戏文件仍在：{line.path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        cl.remove_line(self.cfg, line_id)
        self._save_cfg()
        self.reload()

    def _save_cfg(self) -> None:
        from ....core import config as cfg_mod

        try:
            cfg_mod.save(self.cfg)
        except OSError as exc:
            QMessageBox.warning(self, "配置未保存", f"写入配置失败：{exc}")


class LineEditor(QDialog):
    """一条游戏线的完整参数表单。"""

    def __init__(
        self,
        cfg: dict,
        *,
        kind: str = cl.KIND_EXE,
        path: str = "",
        name: str = "",
        engine_line: str = "",
        main_scene: str = "",
        args: str = "",
        profile: str = "",
        category: str = "",
        note: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("游戏线参数")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_edit = QLineEdit(name)
        form.addRow("名称", self.name_edit)

        file_row = QHBoxLayout()
        file_row.setSpacing(6)
        self.path_edit = QLineEdit(path)
        file_row.addWidget(self.path_edit, 1)
        browse = QPushButton("浏览…")
        browse.setProperty("variant", "ghost")
        browse.clicked.connect(self._browse)
        file_row.addWidget(browse)
        file_wrap = QWidget()
        file_wrap.setLayout(file_row)
        form.addRow("游戏文件", file_wrap)

        self.kind_box = QComboBox()
        self.kind_box.addItem("改版 exe（自带运行时，直接执行）", cl.KIND_EXE)
        self.kind_box.addItem("资源包 pck（借底座引擎挂载）", cl.KIND_PCK)
        idx = self.kind_box.findData(kind)
        self.kind_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.kind_box.currentIndexChanged.connect(self._on_kind_changed)
        form.addRow("类型", self.kind_box)

        self.engine_box = QComboBox()
        for line in sorted((self.cfg.get("engines") or {})):
            self.engine_box.addItem(f"{line} 底座", line)
        # 默认值必须跟核心逻辑一致：``custom_lines.normalized_engine_line("")``
        # 取的是**最高版本**引擎。界面上要是停在第一项（4.6），玩家新建一个
        # pck 线就会把 v4 的包挂到 4.6 引擎上，直接报 "Pack version unsupported: 4"。
        # 早先这里就是 findData 返回 -1 → 留在 index 0，两边默认不一致。
        try:
            default_line = cl.normalized_engine_line(self.cfg, engine_line)
        except ValueError:
            default_line = engine_line
        idx = self.engine_box.findData(default_line)
        if idx >= 0:
            self.engine_box.setCurrentIndex(idx)
        form.addRow("引擎线", self.engine_box)

        self.scene_edit = QLineEdit(main_scene)
        self.scene_edit.setPlaceholderText(cl.DEFAULT_MAIN_SCENE)
        form.addRow("主场景", self.scene_edit)

        self.args_edit = QLineEdit(args)
        self.args_edit.setPlaceholderText("空格分隔，例如 --fullscreen")
        form.addRow("启动参数", self.args_edit)

        self.profile_edit = QLineEdit(profile)
        self.profile_edit.setPlaceholderText("留空自动生成；每个名字对应一份独立存档")
        form.addRow("存档目录", self.profile_edit)

        self.cat_box = QComboBox()
        for cat in cat_mod.load(self.cfg):
            self.cat_box.addItem(cat.name, cat.id)
        # 默认分类也必须跟核心逻辑一致：自定义线不填分类时，目录投影按
        # ``categories.MOD_ID``（改版）算。这里要是停在 index 0（原版），
        # 玩家新建的改版会被归到「原版」里，和目录页显示的对不上。
        target_cat = category or cat_mod.MOD_ID
        idx = self.cat_box.findData(target_cat)
        if idx < 0:
            idx = self.cat_box.findData(cat_mod.resolve(self.cfg, target_cat))
        if idx >= 0:
            self.cat_box.setCurrentIndex(idx)
        form.addRow("分类", self.cat_box)

        self.note_edit = QLineEdit(note)
        self.note_edit.setPlaceholderText("给自己看的备注")
        form.addRow("备注", self.note_edit)

        root.addLayout(form)

        self.hint = QLabel("")
        self.hint.setObjectName("Faint")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        ops = QHBoxLayout()
        ops.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setProperty("variant", "ghost")
        cancel.clicked.connect(self.reject)
        ops.addWidget(cancel)
        ok = QPushButton("确定")
        ok.setProperty("variant", "primary")
        ok.clicked.connect(self._accept)
        ok.setDefault(True)
        ops.addWidget(ok)
        root.addLayout(ops)

        self._on_kind_changed()

    # ------------------------------------------------------------ 联动

    def _on_kind_changed(self, _idx: int = 0) -> None:
        is_pck = self.kind_box.currentData() == cl.KIND_PCK
        for widget in (self.engine_box, self.scene_edit):
            widget.setEnabled(is_pck)
        if is_pck:
            self.hint.setText(
                "pck 形态会用底座引擎挂载这个资源包：底座提供 project.godot 与兼容层，"
                "包提供游戏数据。若该包依赖自己的工程设置，可能需要手动指定主场景"
                f"（默认 {cl.DEFAULT_MAIN_SCENE}）。"
            )
        else:
            self.hint.setText(
                "exe 形态会直接执行这个文件，启动器只做存档隔离，不介入它的资源加载。"
            )

    def _browse(self) -> None:
        is_pck = self.kind_box.currentData() == cl.KIND_PCK
        pattern = "资源包 (*.pck);;所有文件 (*)" if is_pck else "可执行文件 (*.exe);;所有文件 (*)"
        title = "选择游戏资源包" if is_pck else "选择游戏可执行文件"
        path, _ = QFileDialog.getOpenFileName(self, title, "", pattern)
        if path:
            self.path_edit.setText(path)
            if not self.name_edit.text().strip():
                self.name_edit.setText(Path(path).stem)

    # ------------------------------------------------------------ 结果

    def _accept(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "缺少文件", "请先选择游戏文件。")
            return
        suffix = Path(path).suffix.lower()
        kind = self.kind_box.currentData()
        expected = ".pck" if kind == cl.KIND_PCK else ".exe"
        if suffix != expected:
            answer = QMessageBox.question(
                self,
                "类型与文件不匹配",
                f"选的是{expected}类型，但文件后缀是 {suffix or '（无）'}。\n\n"
                "要继续吗？（继续的话请在「类型」里改成正确的那个）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def values(self) -> dict:
        kind = str(self.kind_box.currentData())
        return {
            "name": self.name_edit.text().strip(),
            "path": self.path_edit.text().strip(),
            "extra": {
                "kind": kind,
                "engine_line": str(self.engine_box.currentData() or ""),
                "main_scene": self.scene_edit.text().strip(),
                "args": self.args_edit.text().strip(),
                "profile": self.profile_edit.text().strip(),
                "category": str(self.cat_box.currentData() or ""),
                "note": self.note_edit.text().strip(),
            },
        }


def _ask_text(
    parent: QWidget, title: str, label: str, initial: str = ""
) -> tuple[str, bool]:
    """单行输入对话框（Qt 没有现成的，自己拼一个最小的）。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(460)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    lay.addWidget(QLabel(label))
    edit = QLineEdit(initial)
    edit.selectAll()
    lay.addWidget(edit)
    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton("取消")
    cancel.setProperty("variant", "ghost")
    cancel.clicked.connect(dlg.reject)
    row.addWidget(cancel)
    ok = QPushButton("确定")
    ok.setProperty("variant", "primary")
    ok.clicked.connect(dlg.accept)
    ok.setDefault(True)
    row.addWidget(ok)
    lay.addLayout(row)
    okk = dlg.exec() == QDialog.DialogCode.Accepted
    return edit.text().strip(), okk
