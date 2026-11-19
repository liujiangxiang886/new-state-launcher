"""「管理分类」对话框：自定义分类 + 把游戏归到分类。

为什么两件事放一个对话框
------------------------

分类的价值在于「能归类」。只让玩家增删分类、却不能把游戏挪进去，
那分类就是个摆设。所以左边管理分类、右边直接改每条游戏的归属，
关掉即生效，不用来回切页面。

改动立即写配置（与「管理游戏线」一致），关闭后由调用方刷新界面。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ....core import catalog as catalog_mod
from ....core import categories as cat_mod
from ....core import config as cfg_mod
from ..widgets import section_title


class CategoryDialog(QDialog):
    """分类管理。改动即时写盘。"""

    def __init__(self, cfg: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("管理分类")
        self.setMinimumSize(860, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        tip = QLabel(
            "分类只影响「游戏库」里的分组与筛选，不影响启动。"
            "删除分类时，归在它下面的游戏会自动落到「其他」，不会被删掉。"
        )
        tip.setObjectName("Faint")
        tip.setWordWrap(True)
        root.addWidget(tip)

        cols = QHBoxLayout()
        cols.setSpacing(12)
        cols.addWidget(self._build_category_panel(), 1)
        cols.addWidget(self._build_assign_panel(), 1)
        root.addLayout(cols, 1)

        ops = QHBoxLayout()
        ops.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setProperty("variant", "primary")
        close_btn.clicked.connect(self.accept)
        ops.addWidget(close_btn)
        root.addLayout(ops)

        self.reload()

    # ------------------------------------------------------------ 左：分类

    def _build_category_panel(self) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addWidget(section_title("分类", "可改名、排序；内置分类也可改名"))

        self.cat_table = QTableWidget(0, 2)
        self.cat_table.setHorizontalHeaderLabels(["分类名", "游戏数"])
        self.cat_table.verticalHeader().setVisible(False)
        self.cat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cat_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cat_table.setAlternatingRowColors(True)
        head = self.cat_table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.cat_table.itemSelectionChanged.connect(self._on_cat_selection)
        col.addWidget(self.cat_table, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.add_btn = QPushButton("新增…")
        self.add_btn.setProperty("variant", "primary")
        self.add_btn.clicked.connect(self._add)
        row.addWidget(self.add_btn)
        self.rename_btn = QPushButton("改名…")
        self.rename_btn.setProperty("variant", "ghost")
        self.rename_btn.clicked.connect(self._rename)
        row.addWidget(self.rename_btn)
        self.up_btn = QPushButton("上移")
        self.up_btn.setProperty("variant", "ghost")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        row.addWidget(self.up_btn)
        self.down_btn = QPushButton("下移")
        self.down_btn.setProperty("variant", "ghost")
        self.down_btn.clicked.connect(lambda: self._move(1))
        row.addWidget(self.down_btn)
        self.del_btn = QPushButton("删除")
        self.del_btn.setProperty("variant", "danger")
        self.del_btn.clicked.connect(self._remove)
        row.addWidget(self.del_btn)
        row.addStretch(1)
        col.addLayout(row)
        return box

    # ------------------------------------------------------------ 右：归类

    def _build_assign_panel(self) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addWidget(section_title("游戏归属", "下拉直接改，改完即保存"))

        self.game_table = QTableWidget(0, 3)
        self.game_table.setHorizontalHeaderLabels(["游戏", "类型", "分类"])
        self.game_table.verticalHeader().setVisible(False)
        self.game_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.game_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.game_table.setAlternatingRowColors(True)
        head = self.game_table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        col.addWidget(self.game_table, 1)
        return box

    # ------------------------------------------------------------ 刷新

    def reload(self) -> None:
        self._reload_categories()
        self._reload_games()

    def _game_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in catalog_mod.games(self.cfg):
            counts[entry.category] = counts.get(entry.category, 0) + 1
        return counts

    def _reload_categories(self) -> None:
        cats = cat_mod.load(self.cfg)
        counts = self._game_counts()
        self._cat_ids = [c.id for c in cats]
        self.cat_table.setRowCount(0)
        for cat in cats:
            row = self.cat_table.rowCount()
            self.cat_table.insertRow(row)
            name_item = QTableWidgetItem(cat.name)
            if cat.id in cat_mod.PROTECTED:
                name_item.setToolTip("这个分类是删除其它分类后的回落目标，不能删")
            self.cat_table.setItem(row, 0, name_item)
            count_item = QTableWidgetItem(str(counts.get(cat.id, 0)))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.cat_table.setItem(row, 1, count_item)
        self._on_cat_selection()

    def _reload_games(self) -> None:
        games = catalog_mod.games(self.cfg)
        cats = cat_mod.load(self.cfg)
        self._game_table_ids: list[str] = []
        self.game_table.setRowCount(0)
        for entry in games:
            row = self.game_table.rowCount()
            self.game_table.insertRow(row)
            self._game_table_ids.append(entry.id)
            name_item = QTableWidgetItem(entry.name)
            if entry.status and entry.status != "就绪":
                name_item.setToolTip(entry.status)
                name_item.setText(f"{entry.name}（{entry.status}）")
            self.game_table.setItem(row, 0, name_item)
            self.game_table.setItem(
                row, 1, QTableWidgetItem("自定义" if entry.kind == "custom" else "内置")
            )

            combo = QComboBox()
            for cat in cats:
                combo.addItem(cat.name, cat.id)
            idx = combo.findData(entry.category)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            # 用默认参数固定 entry，避免闭包晚绑定拿到最后一个
            combo.currentIndexChanged.connect(
                lambda _i, e=entry, c=combo: self._assign(e, str(c.currentData() or ""))
            )
            self.game_table.setCellWidget(row, 2, combo)

    # ------------------------------------------------------------ 操作

    def _selected_cat_id(self) -> str:
        rows = {i.row() for i in self.cat_table.selectedItems()}
        if not rows:
            return ""
        row = sorted(rows)[0]
        return self._cat_ids[row] if 0 <= row < len(self._cat_ids) else ""

    def _on_cat_selection(self) -> None:
        cid = self._selected_cat_id()
        has = bool(cid)
        for btn in (self.rename_btn, self.up_btn, self.down_btn):
            btn.setEnabled(has)
        # 「其他」是删除其它分类后的回落目标，按下去必然报错 —— 直接禁掉
        self.del_btn.setEnabled(has and cid not in cat_mod.PROTECTED)

    def _add(self) -> None:
        from .custom_lines_dialog import _ask_text

        name, ok = _ask_text(self, "新增分类", "分类名（最多 16 字）", "")
        if not ok:
            return
        try:
            cat_mod.add(self.cfg, name)
        except ValueError as exc:
            QMessageBox.warning(self, "无法新增", str(exc))
            return
        self._save()
        self.reload()

    def _rename(self) -> None:
        from .custom_lines_dialog import _ask_text

        cid = self._selected_cat_id()
        if not cid:
            return
        current = cat_mod.name_of(self.cfg, cid)
        name, ok = _ask_text(self, "改名", "新的分类名", current)
        if not ok:
            return
        try:
            cat_mod.rename(self.cfg, cid, name)
        except ValueError as exc:
            QMessageBox.warning(self, "无法改名", str(exc))
            return
        self._save()
        self.reload()

    def _move(self, delta: int) -> None:
        cid = self._selected_cat_id()
        if not cid:
            return
        if cat_mod.move(self.cfg, cid, delta):
            self._save()
            self.reload()
            # 保持选中，方便连续点上移/下移
            if cid in self._cat_ids:
                self.cat_table.selectRow(self._cat_ids.index(cid))

    def _remove(self) -> None:
        cid = self._selected_cat_id()
        if not cid:
            return
        name = cat_mod.name_of(self.cfg, cid)
        count = self._game_counts().get(cid, 0)
        detail = f"归在它下面的 {count} 个游戏会落到「其他」。" if count else "当前没有游戏归在它下面。"
        answer = QMessageBox.question(
            self,
            "删除分类",
            f"删除分类「{name}」？\n\n{detail}\n（游戏本身不会被删除）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            cat_mod.remove(self.cfg, cid)
        except ValueError as exc:
            QMessageBox.warning(self, "无法删除", str(exc))
            return
        self._save()
        self.reload()

    def _assign(self, entry, category_id: str) -> None:
        """把一条游戏改到某个分类。"""
        if entry.kind == "baseline":
            cat_mod.set_baseline_category(self.cfg, entry.id, category_id)
        else:
            from ....core import custom_lines as cl

            cl.update_line(self.cfg, entry.id, category=category_id)
        self._save()
        self._reload_categories()

    def _save(self) -> None:
        try:
            cfg_mod.save(self.cfg)
        except OSError as exc:
            QMessageBox.warning(self, "配置未保存", f"写入配置失败：{exc}")
