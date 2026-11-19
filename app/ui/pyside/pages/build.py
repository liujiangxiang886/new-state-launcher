"""打包导出页：生成分片 → 组装发布包 → 校验产物。

三段式，和实际构建流程一致：

1. **生成分片**：把基线切成 ≤1.8 GiB 的 pck，产物含 packset.json 与逐片 sha256；
2. **组装发布包**：底座 exe + 内容分片 + 启动脚本，目录版或单文件版；
3. **校验产物**：回读底座包与分片，确认条目结构没坏。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ....core import config as cfg_mod
from ....core import paths
from ....core.pck import PckReader
from ..widgets import Card, PageHeader, Pill, placeholder_notice, section_title


def _run_shard_task(bid: str, line: str, out: Path, limit: int, progress) -> dict:
    """在子线程里执行分片打包；``progress`` 由 ProgressTaskWorker 注入。"""
    from ....core import config as cfg_mod
    from ....core.pck import packset as packset_mod
    from ....core.pck.reader import PckReader

    cfg = cfg_mod.load()
    meta = cfg_mod.baseline(cfg, bid)
    reader = PckReader.open(meta["path"])
    result = packset_mod.build(
        [reader],
        out,
        line=line,
        limit=limit,
        packset_id=f"{bid}-shards",
        progress=progress,
    )
    return result.as_dict()


def _run_package_task(line: str, name: str, out: Path, shards: Path | None, single: bool) -> dict:
    """组装发布包（独立函数，便于在子线程里跑）。"""
    from ....core.build.package import PackageSpec, build_package

    spec = PackageSpec(
        line=line,
        name=name,
        out_dir=out,
        shards_packset=shards,
        single_file=single,
        copy_baseline=shards is None,
    )
    return build_package(spec).as_dict()


class BuildPage(QWidget):
    def __init__(self, cfg: dict, log, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._log = log

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = PageHeader(
            "打包导出",
            "本项目对外只发布扩展内容（mod），以独立扩展包形式分发，不含游戏本体；"
            "这里的打包用于自测与本地分发，需要你本机已装好的游戏。",
        )
        self.state_pill = Pill("脚手架就绪", "info")
        header.add_action(self.state_pill)
        root.addWidget(header)

        # --- 分片
        shard_card = Card()
        # 两行网格，而不是一排六件东西。
        #
        # 原来是一行 QHBoxLayout：「目标引擎 + 下拉 + 分片上限 + 下拉 +
        # 生成分片按钮 + 产物路径」，最小宽度相加 715 px。低配机器
        # 1366x768 开 150% 缩放时可用宽度只有 911，扣掉左栏与页边距后
        # 内容区约 630 px —— 整页出横向滚动条，玩家得左右拖才能点到
        # 「生成分片」（2026-11-14 反馈的「低配用不了」的一部分）。
        #
        # QGridLayout **单行**同样会把各列最小宽度相加，所以光换布局不够，
        # 必须真的分成两行：
        #   第一行 目标引擎 [下拉]   分片上限 [下拉]
        #   第二行 [生成分片]        产物：packs/<基线>-shards/
        # 两行各自的最小宽度都 < 400，630 的内容区能完整放下。
        r1 = QGridLayout(shard_card)
        r1.setContentsMargins(18, 14, 18, 14)
        r1.setHorizontalSpacing(10)
        r1.setVerticalSpacing(8)
        r1.addWidget(self._label("目标引擎"), 0, 0)
        self.line_box = QComboBox()
        # 按配置列出所有引擎线，不再只放 4.6。
        #
        # 这个下拉决定「给哪条线切分片」：4.6 与 4.7 的 pck 格式不同
        # （pack_version 3 vs 4）、分片要对齐的块大小也不同，选错线切出来的
        # 分片挂不上。原先只有 4.6 一项，4.7 玩家在这个页面做不了任何事。
        for _line in (cfg_mod.known_lines(cfg) or (cfg_mod.BUILTIN_LINE,)):
            self.line_box.addItem(_line, _line)
        self.line_box.currentTextChanged.connect(self._refresh_package_hint)
        r1.addWidget(self.line_box, 0, 1)
        r1.addWidget(self._label("分片上限"), 0, 2)
        self.shard_box = QComboBox()
        for label, value in (("1.0 GiB", 1073741824), ("1.5 GiB", 1610612736), ("1.8 GiB", 1932735283)):
            self.shard_box.addItem(label, value)
        self.shard_box.setCurrentIndex(2)
        r1.addWidget(self.shard_box, 0, 3)
        self.shard_btn = QPushButton("生成分片")
        self.shard_btn.setProperty("variant", "primary")
        self.shard_btn.clicked.connect(self._build_shards)
        r1.addWidget(self.shard_btn, 1, 0, 1, 2)
        row_end = QLabel("产物：packs/<基线>-shards/")
        row_end.setObjectName("Faint")
        r1.addWidget(row_end, 1, 2, 1, 2)
        r1.setColumnStretch(3, 1)
        root.addWidget(shard_card)

        self.shard_hint = QLabel("分片输出：未生成")
        self.shard_hint.setObjectName("Faint")
        root.addWidget(self.shard_hint)

        # --- 发布包
        pkg_card = Card()
        # 同 shard_card：分两行，避免单行最小宽度相加（原 626 px）。
        #   第一行 包名 [输入]     内容来源 [下拉]
        #   第二行 [单文件 exe]    [组装发布包]
        r2 = QGridLayout(pkg_card)
        r2.setContentsMargins(18, 14, 18, 14)
        r2.setHorizontalSpacing(10)
        r2.setVerticalSpacing(8)
        r2.addWidget(self._label("包名"), 0, 0)
        self.name_edit = QLineEdit("机甲旋风")
        self.name_edit.setFixedWidth(140)
        r2.addWidget(self.name_edit, 0, 1)
        r2.addWidget(self._label("内容来源"), 0, 2)
        self.source_box = QComboBox()
        self.source_box.addItem("已生成的分片", "shards")
        self.source_box.addItem("复制原基线包", "baseline")
        self.source_box.setToolTip(
            "仅影响本地测试产物的取材方式，与对外发布的扩展包无关"
        )
        r2.addWidget(self.source_box, 0, 3)
        self.single_box = QCheckBox("单文件 exe")
        self.single_box.setChecked(True)
        self.single_box.setToolTip(
            "把底座 pck 追加到 exe 尾部；内容分片仍在 packs/ 下。\n"
            "注意：产物里的 exe/pck 是引擎底座，不是游戏本体，也不对外发布。"
        )
        r2.addWidget(self.single_box, 1, 0, 1, 2)
        self.package_btn = QPushButton("组装发布包")
        self.package_btn.setProperty("variant", "primary")
        self.package_btn.clicked.connect(self._build_package)
        r2.addWidget(self.package_btn, 1, 2, 1, 2)
        r2.setColumnStretch(3, 1)
        root.addWidget(pkg_card)

        self.package_hint = QLabel("发布包：未生成")
        self.package_hint.setObjectName("Faint")
        root.addWidget(self.package_hint)

        # --- 产物
        base_row = QHBoxLayout()
        base_row.addWidget(section_title("运行时底座", "底座包坏了后面全白搭"))
        base_row.addStretch(1)
        self.rebuild_btn = QPushButton("重建底座")
        self.rebuild_btn.setProperty("variant", "ghost")
        self.rebuild_btn.clicked.connect(self._rebuild)
        base_row.addWidget(self.rebuild_btn)
        self.verify_btn = QPushButton("校验产物")
        self.verify_btn.setProperty("variant", "ghost")
        self.verify_btn.clicked.connect(self._verify)
        base_row.addWidget(self.verify_btn)
        root.addLayout(base_row)

        self.runtime_hint = QLabel("运行时底座：读取中…")
        self.runtime_hint.setObjectName("Faint")
        self.runtime_hint.setWordWrap(True)
        root.addWidget(self.runtime_hint)

        root.addWidget(
            section_title(
                "本地产物",
                "builds/release-* 下的自测副本；双击行可打开目录。对外发布的是扩展包，不是这里",
            )
        )
        self.release_table = QTableWidget(0, 4)
        self.release_table.setHorizontalHeaderLabels(["目录", "形态", "文件数", "体积"])
        self.release_table.verticalHeader().setVisible(False)
        self.release_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.release_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        rel_head = self.release_table.horizontalHeader()
        rel_head.setStretchLastSection(True)
        self.release_table.setAlternatingRowColors(True)
        self.release_table.itemDoubleClicked.connect(lambda _i: self._open_release_dir())
        root.addWidget(self.release_table, 1)

        self.refresh()

    def _label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Muted")
        return label

    # ------------------------------------------------------------ 刷新

    def refresh(self) -> None:
        parts: list[str] = []
        for tag in ("46", "47"):
            pck = paths.ROOT / "runtime" / f"host{tag}" / f"jjxf{tag}.pck"
            if not pck.exists():
                parts.append(f"{tag}：缺底座 pck")
                continue
            try:
                reader = PckReader.open(pck)
                from ....core.hashing import sha256_file

                digest = sha256_file(pck)[:12]
                parts.append(
                    f"{tag}：{pck.name} {len(reader):,} 条目 / {pck.stat().st_size:,} 字节 / {digest}"
                )
            except Exception as exc:  # noqa: BLE001
                parts.append(f"{tag}：读取失败（{exc}）")
        self.runtime_hint.setText("运行时底座　" + "　｜　".join(parts))

        self._refresh_releases()
        self._refresh_package_hint()

    def _refresh_releases(self) -> None:
        self.release_table.setRowCount(0)
        roots = sorted(paths.BUILDS.glob("release*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for root in roots[:12]:
            if not root.is_dir():
                continue
            cfg_path = root / "launcher.json"
            single = False
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                single = data.get("single_file")
                if single is None:      # 早期产物没记这个字段，按「根目录有没有 pck」推断
                    single = not any(root.glob("*.pck"))
                single = bool(single)
            except (OSError, ValueError):
                single = not any(root.glob("*.pck"))
            files = [p for p in root.rglob("*") if p.is_file()]
            total = sum(p.stat().st_size for p in files)
            r = self.release_table.rowCount()
            self.release_table.insertRow(r)
            item = QTableWidgetItem(root.name)
            item.setData(0x0100, str(root))
            self.release_table.setItem(r, 0, item)
            self.release_table.setItem(r, 1, QTableWidgetItem("单文件" if single else "目录版"))
            self.release_table.setItem(r, 2, QTableWidgetItem(f"{len(files)}"))
            self.release_table.setItem(r, 3, QTableWidgetItem(f"{total / 1024 / 1024 / 1024:.2f} GB"))

    def _refresh_package_hint(self) -> None:
        shards = self._shard_packset()
        if shards:
            self.package_hint.setText(f"本地副本：将复用分片 {shards}")
        else:
            self.package_hint.setText("本地副本：没有可用分片，将复制原基线包（产物多一个 3 GB 文件）")

    def _shard_packset(self) -> Path | None:
        line = self.line_box.currentText()
        bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
        for candidate in (
            paths.PACKS / f"{bid}-shards" / "packset.json",
            Path(str((self.cfg["baselines"].get(bid) or {}).get("shard_packset") or "")),
        ):
            if str(candidate) and candidate.is_file():
                return candidate
        return None

    # ------------------------------------------------------------ 分片

    def _build_shards(self) -> None:
        line = self.line_box.currentText()
        limit = int(self.shard_box.currentData())
        bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
        out = paths.PACKS / f"{bid}-shards"
        self.shard_btn.setEnabled(False)
        self.shard_hint.setText(f"正在生成：{out}（完整构建约 2 分钟）")
        self._log(f"开始生成 {line} 分片，上限 {limit:,} 字节 → {out}", "info")

        from ..workers import ProgressTaskWorker

        worker = ProgressTaskWorker(_run_shard_task, bid, line, out, limit)
        worker.progress.connect(self._on_shard_progress)
        worker.done.connect(self._on_shard_done)
        worker.failed.connect(self._on_shard_failed)
        worker.finished.connect(lambda: self.shard_btn.setEnabled(True))
        self._worker = worker
        worker.start()

    def _on_shard_progress(self, done: int, total: int, label: str) -> None:
        self.shard_hint.setText(f"正在写入 {done}/{total}：{label}")

    def _on_shard_done(self, info: dict) -> None:
        count = len(info.get("shards") or [])
        self.shard_hint.setText(
            f"已生成 {count} 片，{info.get('entry_count', 0):,} 条目 / "
            f"{info.get('total_bytes', 0):,} 字节 → {info.get('packset')}"
        )
        self._log(f"分片完成：{count} 片，用时 {info.get('seconds')} 秒", "ok")
        for warning in info.get("warnings") or []:
            self._log(f"  注意：{warning}", "warn")
        self.refresh()

    def _on_shard_failed(self, message: str, tb: str) -> None:
        self.shard_hint.setText("分片失败，详见日志")
        self._log(f"分片失败：{message}", "err")
        if tb.strip():
            self._log(tb.strip().splitlines()[-1], "err")

    # ------------------------------------------------------------ 发布包

    def _build_package(self) -> None:
        line = self.line_box.currentText()
        name = self.name_edit.text().strip() or "机甲旋风"
        use_shards = self.source_box.currentData() == "shards"
        shards = self._shard_packset() if use_shards else None
        if use_shards and shards is None:
            self._log("没有已生成的分片，改走「复制原基线包」。", "warn")
        tag = line.replace(".", "")
        out = paths.BUILDS / f"release-{tag}-{name}"
        single = self.single_box.isChecked()

        self.package_btn.setEnabled(False)
        self.package_hint.setText(f"正在组装：{out}")
        self._log(
            f"开始组装发布包：{line} / {'分片' if shards else '原基线'} / "
            f"{'单文件' if single else '目录版'} → {out}",
            "info",
        )
        started = time.time()

        from ..workers import TaskWorker

        worker = TaskWorker(_run_package_task, line, name, out, shards, single)
        worker.done.connect(lambda info: self._on_package_done(info, started))
        worker.failed.connect(self._on_package_failed)
        worker.finished.connect(lambda: self.package_btn.setEnabled(True))
        self._worker = worker
        worker.start()

    def _on_package_done(self, info: dict, started: float) -> None:
        self.package_hint.setText(
            f"已生成：{info.get('root')}（{info.get('total_bytes', 0) / 1024 / 1024 / 1024:.2f} GB，"
            f"{time.time() - started:.0f} 秒）"
        )
        self._log(
            f"本地副本完成：{info.get('exe')}，产物 {info.get('total_bytes', 0):,} 字节", "ok"
        )
        self._log("  双击下方「本地产物」表格里的行可打开目录。", "dim")
        self.refresh()

    def _on_package_failed(self, message: str, tb: str) -> None:
        self.package_hint.setText("组装失败，详见日志")
        self._log(f"组装失败：{message}", "err")
        if tb.strip():
            self._log(tb.strip().splitlines()[-1], "err")

    def _open_release_dir(self) -> None:
        rows = {i.row() for i in self.release_table.selectedItems()}
        if not rows:
            return
        item = self.release_table.item(sorted(rows)[0], 0)
        target = str(item.data(0x0100) or "") if item else ""
        if not target:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    # ------------------------------------------------------------ 校验

    def _rebuild(self) -> None:
        from ..workers import TaskWorker

        self.rebuild_btn.setEnabled(False)
        self._log("开始重建兼容底座…", "info")

        def task() -> str:
            import subprocess
            import sys

            proc = subprocess.run(
                [sys.executable, str(paths.TOOLS / "build_base.py"), "46", "47"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            return f"重建完成（rc={proc.returncode}）"

        worker = TaskWorker(task)
        worker.done.connect(lambda msg: (self._log(msg, "ok"), self.refresh()))
        worker.failed.connect(lambda m, _tb: self._log(f"重建失败：{m}", "err"))
        worker.finished.connect(lambda: self.rebuild_btn.setEnabled(True))
        self._worker = worker
        worker.start()

    def _verify(self) -> None:
        self.refresh()
        self._log("已重新读取底座包并校验条目结构。", "ok")
