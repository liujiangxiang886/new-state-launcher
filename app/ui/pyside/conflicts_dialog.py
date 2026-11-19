"""冲突处置面板：一行一个位置，四选一。

只在「宽松校验 / 开发者模式」出现。普通模式不弹这个窗——玩家只需要知道
能不能启动，技术判断不该丢给他。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ....core.ext import decisions as decisions_mod
from ....core.ext import units as units_mod


class ConflictDialog(QDialog):
    """逐项选择停用 / 兼容 / 依赖，结果写入 ``decisions.json``。"""

    def __init__(
        self,
        units: list[dict],
        *,
        line: str,
        meta: dict[str, dict],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("处理扩展冲突")
        self.resize(860, 460)
        self.line = line
        self.meta = meta
        self.rows: list[dict[str, Any]] = []

        root = QVBoxLayout(self)
        tip = QLabel(
            "这些扩展改了同一处内容。逐项选择处理方式；只影响这一处，"
            "其它功能照常生效。"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["位置", "涉及的扩展", "处理方式"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        for unit in units:
            self._add_row(unit)

        buttons = QDialogButtonBox(self)
        save = buttons.addButton("保存处理", QDialogButtonBox.ButtonRole.AcceptRole)
        skip = buttons.addButton("这次不处理", QDialogButtonBox.ButtonRole.RejectRole)
        save.clicked.connect(self._save)
        skip.clicked.connect(self.reject)
        root.addWidget(buttons)

    def _impact(self, party: str, unit: dict) -> int:
        return sum(
            1
            for other in self.rows
            if party in (other["unit"].get("parties") or []) and other["unit"] is not unit
        )

    def _add_row(self, unit: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        detail = units_mod.describe(
            units_mod.Unit(
                target=str(unit.get("target") or ""),
                locator=str(unit.get("locator") or ""),
                kind=str(unit.get("kind") or "data"),
                parties=list(unit.get("parties") or []),
                detail=str(unit.get("detail") or ""),
                payload=dict(unit.get("payload") or {}),
            )
        )
        cell = QTableWidgetItem(detail)
        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        cell.setToolTip(str(unit.get("detail") or ""))
        self.table.setItem(row, 0, cell)

        parties = [str(x) for x in (unit.get("parties") or [])]
        party_item = QTableWidgetItem("、".join(parties))
        party_item.setFlags(party_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 1, party_item)

        holder = QHBoxLayout()
        holder.setContentsMargins(4, 2, 4, 2)
        combo = QComboBox()
        combo.addItem("按默认处理", ("auto", ""))
        for party in parties:
            n = self._impact(party, unit)
            suffix = f"（会同时关掉它另外 {n} 处改动）" if n else ""
            combo.addItem(f"停用 {party}{suffix}", ("disable", party))
        if unit.get("kind") != "files":
            combo.addItem("兼容：两边都保留，填一个最终值", ("compatible", ""))
        combo.addItem(f"依赖：{parties[0]} 在前，{parties[-1]} 在后", ("dependent", ""))
        value = QLineEdit()
        value.setPlaceholderText("兼容值（数字或 JSON）")
        value.setVisible(False)

        def on_change(_index: int) -> None:
            action = combo.currentData()[0]
            value.setVisible(action == "compatible")

        combo.currentIndexChanged.connect(on_change)
        holder.addWidget(combo, 1)
        holder.addWidget(value, 1)
        # 用容器是为了让 combo + 输入框在同一格里
        from PySide6.QtWidgets import QWidget

        box = QWidget()
        box.setLayout(holder)
        self.table.setCellWidget(row, 2, box)
        self.table.setRowHeight(row, 38)

        self.rows.append(
            {"unit": unit, "combo": combo, "value": value, "parties": parties}
        )

    def _save(self) -> None:
        out: list[decisions_mod.Decision] = []
        for row in self.rows:
            action, party = row["combo"].currentData()
            if action == "auto":
                continue
            unit = row["unit"]
            decision = decisions_mod.Decision(
                line=self.line,
                target=str(unit.get("target") or ""),
                locator=str(unit.get("locator") or ""),
                parties=tuple(row["parties"]),
                action=action,
                party=party,
                fingerprint=decisions_mod.fingerprint(
                    self.line,
                    str(unit.get("target") or ""),
                    str(unit.get("locator") or ""),
                    row["parties"],
                    self.meta,
                ),
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
            if action == "compatible":
                raw = row["value"].text().strip()
                if unit.get("kind") == "text":
                    decision.value = raw
                else:
                    try:
                        decision.value = json.loads(raw)
                    except json.JSONDecodeError:
                        QMessageBox.warning(
                            self,
                            "值的格式不对",
                            f"{unit.get('target')} {unit.get('locator')}\n\n"
                            "请输入数字、字符串或合法的 JSON。",
                        )
                        return
            if action == "dependent":
                decision.before = row["parties"][0]
                decision.after = row["parties"][-1]
            out.append(decision)

        self.decisions = out
        self.accept()
