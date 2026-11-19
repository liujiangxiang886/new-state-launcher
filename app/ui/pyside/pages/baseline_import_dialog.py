"""自定义基线导入对话框（改版创作者的界面入口）。

设置页与「管理自定义版本」对话框共用这一份实现，避免两处各写一套校验。
底层规则全在 :mod:`app.core.custom_baseline`（B0–B5），这里只负责选线、
选目录、展示逐项结果、登记，以及打开《自定义基线导入准则》。

注意：``identity_gate.probe`` 要扫 GB 级本体的路径索引，几秒到几十秒不等。
这里用等待光标 + 先刷一次事件循环让按钮看起来是响应过的；不引入线程，
是因为登记与状态刷新都要回到主线程，而且导入是一次性、低频操作。
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

#: 准则文档随发布包放在 exe 同级
DOC_REL = Path("扩展开发文档") / "公共文档" / "自定义基线导入准则.md"


class BaselineImportDialog(QDialog):
    """校验 / 导入某条自定义线的反解源码基线。"""

    def __init__(self, cfg: dict, line_id: str = "", parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("自定义基线 · 校验与导入")
        self.resize(620, 340)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        tip = QLabel(
            "需要让改版支持方法钩子类扩展时，由改版创作者按《自定义基线导入准则》"
            "准备 baseline.json、sources/ 明文 .gd 源码与 index.json，再在这里导入。\n"
            "纯资源 / 数据改版不需要导入。启动器不反解、不逆向本体。"
        )
        tip.setObjectName("Faint")
        tip.setWordWrap(True)
        root.addWidget(tip)

        row = QHBoxLayout()
        row.addWidget(QLabel("自定义线"))
        self.line_box = QComboBox()
        self.line_box.currentIndexChanged.connect(self._on_line_changed)
        row.addWidget(self.line_box, 1)
        root.addLayout(row)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("基线目录"))
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("包含 baseline.json / sources/ / index.json 的目录")
        path_row.addWidget(self.dir_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        root.addLayout(path_row)

        btns = QHBoxLayout()
        self.verify_btn = QPushButton("只校验")
        self.verify_btn.clicked.connect(lambda: self._run(do_import=False))
        btns.addWidget(self.verify_btn)
        self.import_btn = QPushButton("校验并导入")
        self.import_btn.setProperty("variant", "primary")
        self.import_btn.clicked.connect(lambda: self._run(do_import=True))
        btns.addWidget(self.import_btn)
        doc_btn = QPushButton("查看导入准则")
        doc_btn.setProperty("variant", "ghost")
        doc_btn.clicked.connect(self._open_doc)
        btns.addWidget(doc_btn)
        btns.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        root.addLayout(btns)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self.detail)
        root.addStretch(1)

        self._reload_lines(prefer=line_id)
        self._refresh_status()

    # ------------------------------------------------------------ 线与目录

    def _lines(self) -> list:
        from ....core import custom_lines as cl

        return cl.load_all(self.cfg)

    def _reload_lines(self, prefer: str = "") -> None:
        self.line_box.blockSignals(True)
        self.line_box.clear()
        for line in self._lines():
            label = f"{line.name}（{line.id}）"
            self.line_box.addItem(label, line.id)
        self.line_box.blockSignals(False)
        if prefer:
            idx = self.line_box.findData(prefer)
            if idx >= 0:
                self.line_box.setCurrentIndex(idx)
        empty = self.line_box.count() == 0
        self.verify_btn.setEnabled(not empty)
        self.import_btn.setEnabled(not empty)

    def _selected(self):
        line_id = str(self.line_box.currentData() or "")
        if not line_id:
            return None
        from ....core import custom_lines as cl

        return cl.get_line(self.cfg, line_id)

    def _browse(self) -> None:
        picked = QFileDialog.getExistingDirectory(
            self, "选择自定义基线目录", self.dir_edit.text() or str(Path.home())
        )
        if picked:
            self.dir_edit.setText(picked)

    def _doc_path(self) -> Path:
        from ....core import paths

        return paths.project_root() / DOC_REL

    def _open_doc(self) -> None:
        p = self._doc_path()
        if not p.is_file():
            QMessageBox.information(
                self, "找不到文档",
                f"准则文档不在发布包里：{p}\n\n也可以查看启动器同目录的「扩展开发文档」。",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    # ------------------------------------------------------------ 状态与校验

    def _on_line_changed(self, _idx: int = 0) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        from ....core import custom_baseline as cb

        line = self._selected()
        if line is None:
            self.status.setText("还没有登记任何自定义线。先到「游戏库 → 管理自定义版本」添加。")
            return
        st = cb.status(line.id, str(getattr(line, "identity_hash", "") or ""))
        if st.get("ready"):
            self.status.setText(
                f"✔ {line.name}：已导入并有效（{st.get('scripts')} 个源码脚本，"
                f"校验于 {st.get('verified_at')}）。\n"
                "提示：插桩层生成器接入前，方法钩子类扩展仍会明确跳过，不会静默挂载。"
            )
        else:
            self.status.setText(f"○ {line.name}：{st.get('reason')}")

    def _run(self, do_import: bool = False) -> None:
        from ....core import custom_baseline as cb
        from ....core import identity_gate, line_desc

        line = self._selected()
        if line is None:
            QMessageBox.information(self, "先选线", "请先选择要导入基线的自定义版本。")
            return
        base = Path(self.dir_edit.text().strip())
        if not base.is_dir():
            QMessageBox.information(self, "先选目录", "请选择包含 baseline.json 的基线目录。")
            return

        self.detail.setText("正在校验…（大本体需要扫路径索引，请稍候）")
        QApplication.processEvents()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            verdict = identity_gate.probe(line.path)
            if not verdict.accepted:
                QMessageBox.warning(self, "本体未通过身份闸门", verdict.reason)
                self.detail.setText(f"<b>拒绝</b>：{escape(verdict.reason)}")
                return
            required: set[str] = set()
            desc_path = line_desc.desc_path_for(line.path)
            if desc_path.is_file():
                try:
                    for cap in line_desc.load_desc(desc_path).get("capabilities") or []:
                        if isinstance(cap, dict) and str(cap.get("kind") or "") in ("method", "hook"):
                            if cap.get("target"):
                                required.add(str(cap["target"]))
                except (line_desc.LineDescError, OSError, ValueError):
                    pass
            report = cb.verify(base, verdict, line.path, required_scripts=required)
        finally:
            QApplication.restoreOverrideCursor()

        if not report.ok:
            items = "".join(f"<li>{escape(e)}</li>" for e in report.errors[:10])
            self.detail.setText(
                f"<b>未通过</b>（{len(report.errors)} 条错误）<ul>{items}</ul>"
                "修法见《自定义基线导入准则》第 7 节。"
            )
            QMessageBox.warning(
                self, "基线校验未通过",
                report.summary() + "\n\n" + "\n".join(report.errors[:6]),
            )
            return

        if do_import:
            cb.register(line.id, base, report)
        head = "已导入并登记" if do_import else "校验通过（未登记）"
        self.detail.setText(
            f"<b>{head}</b>：{len(report.scripts)} 个源码脚本，"
            f"本体对应 {report.body_match:.0%}，0 错误。"
        )
        self._refresh_status()
        if do_import:
            QMessageBox.information(
                self, "导入完成",
                f"{line.name} 的自定义基线已导入。\n\n"
                "注意：插桩层生成器尚未接入自定义线，方法钩子类扩展仍会明确跳过；"
                "纯资源/数据扩展不受影响。",
            )
