"""扩展管理页：已安装列表 + 导入安装 + 仅校验预览。

安装走 :class:`app.core.ext.service.ExtensionService`（主窗口统一调度，
因为进度要显示在主窗口的扩展状态栏里）；本页只负责展示与触发。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
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

from .... import edition
from ....core import baseline as baseline_mod
from ....core import config as cfg_mod
from ....core import paths
from ....core.ext import decisions as decisions_mod
from ....core.ext import manifest as ext_manifest
from ....core.ext.service import ExtensionService, REGISTRY
from .. import colors
from ..dnd import drop_paths
from ..workers import InstallWorker
from ..widgets import Card, PageHeader, Pill, placeholder_notice, section_title

#: 行内操作列宽。实测：「启用/停用」这类两个字的按钮，在 QSS 的
#: ``padding: 7px 16px`` 下 sizeHint 是 60px；两个按钮 120 + 间距 5
#: + cellWidget 左右内边距 8 = 133。给 146 留富余，避免字体缩放后再被省略。
ROW_ACTION_COL_W = 146
ROW_BTN_MIN_W = 60

STATUS_TEXT = {
    "ready": ("已就绪", "ok"),
    "needs_hooks": ("待插桩", "warn"),
}

DISABLED_TEXT = ("已停用", "muted")

#: 扩展隔离模式的档位 → 人话。日志与提示统一用这个，别各写一份。
_MODE_LABELS = cfg_mod.MODE_LABELS


class ExtensionsPage(QWidget):
    """扩展列表。增删改（安装/卸载/启停/升级）都会发 :attr:`modsChanged`。

    主窗口收到后重算启动可用性——否则会出现「刚卸载了冲突包，按钮却还是禁用的」。
    """

    modsChanged = Signal()

    def set_game_running(self, running: bool, detail: str = "") -> None:
        """游戏运行期间锁定所有会改变「启用集合」的按钮。

        锁的不只是启停：卸载会删覆盖层文件（运行中会失败或留半删状态），
        批量升级会重建覆盖层，都要一起锁。安装不受限——装完不会自动生效，
        要下次启动才组装。

        行内按钮的重绘**不立刻做**：``reload_installed()`` 会清空表格重建
        全部行控件（19 行 × 8 列 + 38 个按钮），而这个回调在游戏退出瞬间
        会被触发——哪怕玩家正停在「设置」页，重建的是完全看不见的表格。
        这里改成标脏，等页面真的被切到时（``showEvent``）再重建。
        """
        self._game_running = bool(running)
        self._lock_detail = detail
        for btn in (
            getattr(self, "toggle_btn", None),
            getattr(self, "uninstall_btn", None),
            getattr(self, "verify_all_btn", None),
            # 清理会删缓存与孤儿目录：运行中的实例已把这些 pck 挂进内存，
            # 此时删文件会让「当前实例」和「下次启动」不一致（踩过）。
            getattr(self, "cleanup_btn", None),
        ):
            if btn is None:
                continue
            btn.setEnabled(not running and getattr(btn, "_was_enabled", True))
            btn.setToolTip(
                f"游戏正在运行（{detail}），退出后才能操作" if running else ""
            )
        # 行内按钮逐行重绘，让每行的启停/卸载一起变灰。
        # 页面可见就立刻刷（玩家正看着它），看不见就标脏，等 showEvent。
        if hasattr(self, "installed_table"):
            if self.isVisible():
                self.reload_installed()
            else:
                self._rows_dirty = True

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """页面被切到时补上欠下的重建。"""
        super().showEvent(event)
        if getattr(self, "_rows_dirty", False):
            self._rows_dirty = False
            self.reload_installed()

    def game_running(self) -> bool:
        return bool(getattr(self, "_game_running", False))

    def __init__(
        self,
        cfg: dict,
        log,
        install_handler: Callable[[list[Path]], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._log = log
        self._install = install_handler
        self._manifest: ext_manifest.Manifest | None = None
        self._package: Path | None = None
        self._paths_set: frozenset[str] | None = None
        #: 主窗口当前选中的引擎线，由 :meth:`set_current_line` 同步。
        #: 空串表示还没同步过，此时回落到配置里的 last_version。
        self._line_hint = ""
        #: 上一次 :meth:`reload_installed` 时的注册表指纹（含线与模式）。
        self._last_reload_stamp: tuple | None = None
        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = PageHeader(
            "扩展管理",
            "扩展包只含 mod 内容，不含游戏本体；把它拖进窗口任意位置即可安装，也可点右侧按钮选择文件。",
        )
        self.verify_btn = QPushButton("仅校验")
        self.verify_btn.setProperty("variant", "ghost")
        self.verify_btn.clicked.connect(self._pick_verify)
        header.add_action(self.verify_btn)
        self.import_btn = QPushButton("导入并安装…")
        self.import_btn.setProperty("variant", "primary")
        self.import_btn.clicked.connect(self.pick_install)
        header.add_action(self.import_btn)
        root.addWidget(header)

        # ---------------- 隔离模式
        #
        # 放在扩展页而不是设置页：它是**扩展域的行为策略**（扩展之间能不能
        # 互相读写数据），不是全局偏好。设置页装的是路径、存档、外观。
        # 而且扩展页已经有现成的红/黄条机制，隔离违规天然是第三条横幅。
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(8)
        mode_label = QLabel("扩展隔离模式")
        mode_label.setProperty("variant", "muted")
        self.mode_combo = QComboBox()
        self._rebuild_mode_combo()
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.mode_combo)
        # 联机协议闸门：**只有发布者版**才有这个能力。
        # 玩家版 / 开发者版连控件都不创建——不是隐藏，是压根没有这个入口；
        # 配置层（config.netplay_open）对非发布者版也恒返回 False，
        # 手改 launcher.json 同样无效。
        if edition.is_publisher():
            self.netplay_check = QCheckBox("允许联机协议")
            self.netplay_check.setToolTip(
                "开启后，携带 ENet / UDP / TCP / WebSocket 等 P2P/直连代码的扩展不再被拒装；\n"
                "阻断项会降级为安装提示。仅发布者版提供此项。"
            )
            self.netplay_check.setChecked(cfg_mod.netplay_open(cfg))
            self.netplay_check.toggled.connect(self._on_netplay_toggled)
            mode_row.addWidget(self.netplay_check)
        else:
            self.netplay_check = None
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        # ---------------- 已安装
        installed_head = section_title("已安装", "带方法钩子的包会在启动时自动插桩，无需手工处理")
        root.addWidget(installed_head)

        self.conflict_label = QLabel()
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setStyleSheet(
            f"background:{colors.tint(colors.err(), 0.12)};"
            f"border:1px solid {colors.err()}; border-radius:8px;"
            f"padding:8px 12px; color:{colors.err()}; font-size:12px;"
        )
        self.conflict_label.setVisible(False)
        root.addWidget(self.conflict_label)

        # 覆盖提醒单独一条黄条：数据补丁撞车**不阻断启动**（判 error 会让
        # 现网 10 个启用包全部起不来），但必须让人看见，否则「后挂覆盖先挂、
        # 前一个扩展的修改静默消失」永远查不出原因。
        self.warn_label = QLabel()
        self.warn_label.setWordWrap(True)
        self.warn_label.setStyleSheet(
            f"background:{colors.tint(colors.warn(), 0.12)};"
            f"border:1px solid {colors.warn()}; border-radius:8px;"
            f"padding:8px 12px; color:{colors.warn()}; font-size:12px;"
        )
        self.warn_label.setVisible(False)
        root.addWidget(self.warn_label)

        self.resolve_btn = QPushButton("处理冲突…")
        self.resolve_btn.setProperty("variant", "ghost")
        self.resolve_btn.setVisible(False)
        self.resolve_btn.clicked.connect(self._open_conflicts)
        root.addWidget(self.resolve_btn)

        # 开发者模式的剔除预览：必须在启动前就让人看见「会少哪几个」，
        # 否则一次剔掉一半扩展会被当成故障。
        self.dev_drop_label = QLabel()
        self.dev_drop_label.setWordWrap(True)
        self.dev_drop_label.setStyleSheet(
            f"background:{colors.tint(colors.warn(), 0.12)};"
            f"border:1px solid {colors.warn()}; border-radius:8px;"
            f"padding:8px 12px; color:{colors.warn()}; font-size:12px;"
        )
        self.dev_drop_label.setVisible(False)
        root.addWidget(self.dev_drop_label)

        # 隔离违规单独一条红条：与「冲突」（阻断）和「覆盖提醒」（不阻断）
        # 都不是一回事 —— 它说的是「扩展之间越界访问了对方的数据」。
        self.isolation_label = QLabel()
        self.isolation_label.setWordWrap(True)
        self.isolation_label.setStyleSheet(
            f"background:{colors.tint(colors.err(), 0.12)};"
            f"border:1px solid {colors.err()}; border-radius:8px;"
            f"padding:8px 12px; color:{colors.err()}; font-size:12px;"
        )
        self.isolation_label.setVisible(False)
        root.addWidget(self.isolation_label)

        self.installed_table = QTableWidget(0, 8)
        self.installed_table.setHorizontalHeaderLabels(
            ["名称", "作者", "版本", "引擎线", "状态", "条目", "安装时间", "操作"]
        )
        self.installed_table.verticalHeader().setVisible(False)
        self.installed_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.installed_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 批量卸载的前提：不改成多选，Shift/Ctrl 点第二行只会替换选择。
        # 默认是 SingleSelection，这是「只能一个个卸」的直接原因之一。
        self.installed_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.installed_table.setAlternatingRowColors(True)
        # 时间列只显示到分钟（完整值在 tooltip），字号比正文小一档，进一步压宽度
        self.installed_table.setStyleSheet(
            "QTableWidget { font-size: 12px; }"
            "QTableWidget::item { padding: 0 4px; }"
        )
        header = self.installed_table.horizontalHeader()
        # 名称吃掉剩余空间（长名字不再被截断），其余按内容收紧，整体更紧凑。
        # 踩过的坑：安装时间列曾被误设成 Stretch，吃掉 250px，把「操作」列挤到
        # 131px——两个按钮放不下，文字被 Qt 省略成「…」。
        # 现在：名称 Stretch 独占富余；时间列按内容收紧即可（"2026-10-28 23:35:20"
        # 约 130px 就够）；操作列固定宽度，保证两个按钮永远完整。
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4, 5, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        self.installed_table.setColumnWidth(7, ROW_ACTION_COL_W)
        self.installed_table.verticalHeader().setDefaultSectionSize(30)
        # 黄条会占掉 2-3 行高度，最小高度给到 150 才能保证至少 4 行完整可见；
        # 110 时会出现「最后一行只露半截」的割裂感。
        self.installed_table.setMinimumHeight(150)
        root.addWidget(self.installed_table, 1)

        installed_actions = QHBoxLayout()
        installed_actions.setSpacing(8)
        self.uninstall_btn = QPushButton("卸载选中")
        self.uninstall_btn.setProperty("variant", "danger")
        self.uninstall_btn.setEnabled(False)
        self.uninstall_btn.clicked.connect(self._uninstall_selected)
        installed_actions.addWidget(self.uninstall_btn)
        self.toggle_btn = QPushButton("启用 / 停用")
        self.toggle_btn.setProperty("variant", "ghost")
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.clicked.connect(self._toggle_selected)
        installed_actions.addWidget(self.toggle_btn)
        self.verify_all_btn = QPushButton("整体校验")
        self.verify_all_btn.setProperty("variant", "ghost")
        self.verify_all_btn.setToolTip(
            "把这条线上所有已启用扩展一起挂起来真跑一次（几十秒到几分钟）。\n"
            "批量安装为了速度跳过了逐个冒烟，装完用它兜底。"
        )
        self.verify_all_btn.clicked.connect(self._verify_all)
        installed_actions.addWidget(self.verify_all_btn)
        self.open_dir_btn = QPushButton("打开扩展目录")
        self.open_dir_btn.setProperty("variant", "ghost")
        self.open_dir_btn.clicked.connect(self._open_pack_dir)
        installed_actions.addWidget(self.open_dir_btn)
        self.cleanup_btn = QPushButton("清理残留")
        self.cleanup_btn.setProperty("variant", "ghost")
        self.cleanup_btn.setToolTip(
            "清理「卸载后仍被检测到」产生的派生残留：\n"
            "· 注册表里指向已不存在的扩展文件的过期条目\n"
            "· packs/ 下不在注册表里的孤儿目录\n"
            "· 来源扩展文件已删除的插桩缓存\n"
            "· builds/ 下的安装中间产物\n"
            "不会碰已安装扩展、剧情包与游戏存档。"
        )
        self.cleanup_btn.clicked.connect(self._cleanup_residue)
        installed_actions.addWidget(self.cleanup_btn)
        installed_actions.addStretch(1)
        root.addLayout(installed_actions)
        self.installed_table.itemSelectionChanged.connect(self._on_selection)

        self.installed_empty = QLabel(
            "还没有安装扩展。\n把 .zip / .jjm 拖到窗口任意位置即可安装，或点右上角「导入并安装…」。"
        )
        self.installed_empty.setObjectName("Faint")
        self.installed_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.installed_empty.setVisible(False)
        root.addWidget(self.installed_empty)

        # ---------------- 校验预览
        root.addWidget(section_title("校验预览", "只看不装，用来确认文件与钩子是否配套"))
        self.placeholder = placeholder_notice(
            "尚未选择扩展包。\n点右上角「仅校验」选一个包先看一眼，或直接把包拖进窗口安装。"
        )
        root.addWidget(self.placeholder, 1)

        self.summary_card = Card()
        s_layout = QVBoxLayout(self.summary_card)
        s_layout.setContentsMargins(18, 14, 18, 14)
        s_layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.title_label = QLabel("—")
        self.title_label.setObjectName("CardTitle")
        top.addWidget(self.title_label)
        self.id_label = QLabel("")
        self.id_label.setObjectName("Faint")
        top.addWidget(self.id_label)
        top.addStretch(1)
        self.engine_pill = Pill("—", "info")
        self.baseline_pill = Pill("—", "muted")
        self.verdict_pill = Pill("—", "muted")
        top.addWidget(self.engine_pill)
        top.addWidget(self.baseline_pill)
        top.addWidget(self.verdict_pill)
        s_layout.addLayout(top)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("Muted")
        self.meta_label.setWordWrap(True)
        s_layout.addWidget(self.meta_label)
        root.addWidget(self.summary_card)

        self.files_title = section_title("文件清单", "“替换”会额外写自指 remap")
        root.addWidget(self.files_title)
        self.files_table = self._make_table(["类型", "包内来源", "游戏内目标"])
        root.addWidget(self.files_table, 1)

        self.hooks_title = section_title("方法钩子", "已逐条对到原始脚本；缺方法会标红")
        root.addWidget(self.hooks_title)
        self.hooks_table = self._make_table(["脚本", "方法", "阶段", "核对"])
        root.addWidget(self.hooks_table, 1)

        self._show_detail(False)
        self.reload_installed()

    # ------------------------------------------------------------ 已安装

    def _registry_stamp(self) -> tuple:
        """能区分「列表会不会变」的最小指纹：注册表内容 + 当前线 + 隔离模式。

        安装 / 卸载 / 启停都会改写注册表文件（mtime 与大小至少动一个）；
        切线与换模式会改变冲突结论，也计入。注册表还不存在（一个包都没装）
        时用 ``None`` 占位，同样参与比较。
        """
        line = self._current_line()
        mode = cfg_mod.extension_mode(self.cfg)
        try:
            st = REGISTRY.stat()
            return (line, mode, st.st_mtime_ns, st.st_size)
        except OSError:
            return (line, mode, None, None)

    def _item_on_line(self, item: dict, line: str) -> bool:
        """这个扩展是否装在 ``line`` 上。

        与 ``extensions.py`` 里 ``_on_line`` / ``enable.enabled_mods`` 同一套
        判据：``lines`` 字典是真值来源；只有老格式（没有 ``lines``）才看
        顶层 ``engine_line``。

        为什么不能只看顶层 ``engine_line``：那只是「最近一次安装」的镜像，
        一个包同时装在两条线上时它只会记住最后一次，拿它过滤会把另一条线的
        记录错判成「不在本线」。
        """
        lines = item.get("lines")
        if isinstance(lines, dict) and lines:
            return line in lines
        return str(item.get("engine_line") or "") == line

    def reload_installed(self, *, force: bool = False) -> None:
        """重扫已装列表并重算冲突。``force=True`` 跳过缓存（安装/启停后用）。"""
        # 切页会反复触发这里。注册表和模式都没变时，重扫的结论一定一样，
        # 直接复用上一次的渲染——扫描 + 冲突解析实测约 200ms，每次切页都
        # 白跑一遍就是肉眼可见的卡顿。
        stamp = self._registry_stamp()
        if not force and stamp is not None and stamp == self._last_reload_stamp:
            return
        self._last_reload_stamp = stamp
        items = ExtensionService(self.cfg).installed()
        # 开发者模式下 gate 会整组剔除一部分包。表格若不标出来，就会出现
        # 「日志说剔 13 个、这里 18 行全绿」——同一件事两种说法。
        from ....core.ext import effective as effective_mod

        line_hint = self._current_line()
        try:
            view = effective_mod.of(self.cfg, line_hint)
        except Exception:  # noqa: BLE001 - 判定失败不该让列表渲染不出来
            view = None
        self.installed_table.setRowCount(0)

        # **只显示当前线装了的扩展。**
        #
        # 为什么：4.6 和 4.7 的包互不通用（本体、代码层、锚点全都不同），
        # 一条线的包在另一条线上根本不参与挂载。把它们混在一张表里，
        # 玩家看到「4.6 / 4.7」两串行并列，第一反应是「我可以跨线装」，
        # 于是去点启用、去装「别的线」的包，得到的只有「不在此线」这种
        # 看了也不知该干什么的提示。
        #
        # 隐藏比标灰更干净：当前线就是「我现在要玩的这条」，列表里出现
        # 一条永远用不上的记录，只是噪音。要管理另一条线，切到那条线即可
        # —— 顶栏的版本下拉就是干这个的，切过去列表自然换成那条线的包。
        #
        # 判据用 ``lines`` 真值来源（与 ``_on_line`` / ``enabled_mods`` 一致）：
        # 老格式（只有 ``engine_line``）也兜住，避免升级后老包凭空消失。
        items = [it for it in items if self._item_on_line(it, line_hint)]

        for item in items:
            row = self.installed_table.rowCount()
            self.installed_table.insertRow(row)
            ext_id = str(item.get("id", ""))
            self.installed_table.setItem(row, 0, QTableWidgetItem(str(item.get("name") or ext_id)))
            # 作者来自 manifest.v2 的 author 字段；包里没写就标「未标注」，
            # 不留空白（空着会被当成「没有作者」），也不猜测来源。
            author = str(item.get("author") or "").strip()
            author_item = QTableWidgetItem(author or "未标注")
            if not author:
                author_item.setForeground(QBrush(QColor(colors.faint())))
                author_item.setToolTip(
                    "该扩展包 manifest.v2.json 的 author 为空。\n"
                    "作者名由扩展包自带，启动器不会代填或从包名推测。"
                )
            self.installed_table.setItem(row, 1, author_item)
            self.installed_table.setItem(row, 2, QTableWidgetItem(str(item.get("version", ""))))
            # 「引擎」列显示这个扩展装在哪几条线上
            lines = item.get("lines")
            if isinstance(lines, dict) and lines:
                line_text = " / ".join(sorted(lines))
            else:
                line_text = str(item.get("engine_line", ""))
            self.installed_table.setItem(row, 3, QTableWidgetItem(line_text))
            # **按线取状态**：同一个包在 4.6 / 4.7 各有独立的启用标记与
            # 插桩状态。顶层字段只是「最近一次安装」的镜像，直接读它会让
            # 「4.6 关掉了、4.7 还开着」显示成同一行状态。
            cur_line = self._current_line()
            line_entry = lines.get(cur_line) if isinstance(lines, dict) else None
            enabled = bool(
                (line_entry or {}).get("enabled", item.get("enabled", True))
            )
            on_line = isinstance(lines, dict) and cur_line in lines
            drop_tip = ""
            status_item_tip = ""
            if not enabled:
                text, tone = DISABLED_TEXT
            elif view is not None and ext_id in view.dropped_set:
                text, tone = ("已剔除", "err")
                drop_tip = view.text_of(ext_id)
            elif not on_line:
                # 装的是别的线（老数据里可能残留）：在当前线上不会被挂载
                text, tone = ("不在此线", "warn")
            else:
                raw_status = str(
                    (line_entry or {}).get("status") or item.get("status", "")
                )
                text, tone = STATUS_TEXT.get(raw_status, ("已启用", "ok"))
                if raw_status == "needs_hooks":
                    # 待插桩不是故障，是「还没启动过」。插桩发生在启动组装时，
                    # 首次导入必然是待插桩；启动一次游戏后注册表会翻成已就绪。
                    status_item_tip = (
                        "待插桩：这个扩展要用方法钩子，插桩在启动游戏时进行。\n"
                        "首次导入显示「待插桩」属正常现象；启动一次游戏后会变成"
                        "「已就绪」，即表示钩子已成功注册、扩展已生效。\n"
                        "如果启动之后仍然是待插桩，再看启动日志里的插桩告警。"
                    )
                else:
                    status_item_tip = ""
            status_item = QTableWidgetItem(text)
            status_item.setForeground(
                QBrush(
                    QColor(
                        {
                            "ok": colors.ok(),
                            "warn": colors.warn(),
                            "err": colors.err(),
                        }.get(tone, colors.faint())
                    )
                )
            )
            if not on_line:
                status_item.setToolTip("这个扩展没有装到当前版本线上，不会被挂载")
            if drop_tip:
                status_item.setToolTip(drop_tip)
            if status_item_tip:
                status_item.setToolTip(status_item_tip)
            self.installed_table.setItem(row, 4, status_item)
            self.installed_table.setItem(row, 5, QTableWidgetItem(f"{int(item.get('entry_count') or 0):,}"))
            # 时间只显示到分钟：完整串 "2026-10-28 18:37:57" 实测要 228px，
            # 列一多就会超过视口宽度，把名称列挤扁。
            # 截到 "10-28 18:37" 约 90px，完整时间放进 tooltip。
            raw_time = str(item.get("installed_at", "") or "")
            short_time = raw_time[5:16] if len(raw_time) >= 16 else raw_time
            time_item = QTableWidgetItem(short_time)
            if raw_time:
                time_item.setToolTip(raw_time)
            self.installed_table.setItem(row, 6, time_item)
            self.installed_table.setItem(row, 7, QTableWidgetItem(""))
            self.installed_table.item(row, 0).setData(0x0100, ext_id)
            # 行内操作：不用先选中再点底部按钮，逐行独立启停/卸载
            self.installed_table.setCellWidget(row, 7, self._row_actions(ext_id, enabled))

            # 补丁/钩子情况写进 tooltip，主表格保持 6 列不拥挤
            bits: list[str] = []
            if isinstance(lines, dict) and len(lines) > 1:
                bits.append(f"已安装到 {len(lines)} 条线：{', '.join(sorted(lines))}")
            for lname, ldata in sorted((lines or {}).items()):
                lp = ldata.get("patches") or []
                if lp:
                    applied = sum(int(p.get("applied") or 0) for p in lp)
                    bits.append(f"[{lname}] 补丁 {len(lp)} 条 / 命中 {applied} 处")
                    for record in lp:
                        if record.get("issues"):
                            bits.append(f"  · {record.get('target')}：{record['issues'][0]}")
            patches = item.get("patches") or []
            if patches:
                applied = sum(int(p.get("applied") or 0) for p in patches)
                bits.append(f"补丁 {len(patches)} 条 / 命中 {applied} 处")
                for record in patches[:4]:
                    detail = "、".join(record.get("details") or []) or "—"
                    bits.append(f"  · {record.get('target')}（{record.get('kind')}）{detail}")
            hooks = item.get("runtime_hooks") or []
            if hooks:
                bits.append(f"插桩点 {len(hooks)} 个")
            if bits:
                self.installed_table.item(row, 0).setToolTip("\n".join(bits))
        self.installed_empty.setVisible(not items)
        self.installed_table.setVisible(bool(items))
        self._refresh_conflicts(items)
        self._apply_degraded_status()
        # 重建表格会清空选择，但不会自动触发 itemSelectionChanged。
        # 不补这一下，按钮可用态会停留在上一次的选择结果上（例如「卸载选中
        # 没有选中项却还是亮的」）。
        self._on_selection()

    def _on_mode_changed(self, _index: int) -> None:
        """隔离模式切换 —— 立即保存并重跑检查。

        扩展页是「按需操作」语义（装一个扩展立刻就写注册表），不是设置页
        那种「整页一次性保存」—— 所以这里不等「保存」按钮。
        """
        value = str(self.mode_combo.currentData() or cfg_mod.DEFAULT_EXTENSION_MODE)
        self.cfg.setdefault("extensions", {})["extension_mode"] = value
        try:
            cfg_mod.save(self.cfg)
        except OSError as exc:
            self._log(f"隔离模式保存失败：{exc}", "err")
            return
        self._log(
            f"扩展隔离模式：{_MODE_LABELS.get(value, value)}",
            "info" if not cfg_mod.is_strict(value) else "warn",
        )
        # 立刻重跑冲突/隔离检查，红黄条即时更新
        self.reload_installed()

    def _on_netplay_toggled(self, checked: bool) -> None:
        """联机协议闸门开关 —— 立即保存并重跑检查。"""
        self.cfg.setdefault("extensions", {})["netplay_open"] = bool(checked)
        try:
            cfg_mod.save(self.cfg)
        except OSError as exc:
            self._log(f"联机协议设置保存失败：{exc}", "err")
            return
        self._log(
            "联机协议：已开放（带 P2P/直连代码的扩展不再被拒装）"
            if checked
            else "联机协议：已关闭（恢复 1.5.5 的 R2 闸门）",
            "warn" if checked else "info",
        )
        self.reload_installed()

    def _refresh_conflicts(self, items: list[dict]) -> None:
        """按引擎线各解析一次启用集合：红条报阻断，黄条报覆盖。

        过去这里有两个会**静默失效**的问题，必须按新口径走：

        1. 硬编码读 ``manifest.v2.json``，而安装写的是
           ``manifest-46.v2.json``。只有旧包才同时留两份，新装的包读不到
           就直接被 ``isinstance(data, dict)`` 跳过，红条永远不出现
           （``packs/test.member`` 就是只有带线清单的）。
        2. 按顶层 ``engine_line`` 分组，而顶层只是「最近一次安装的线」的
           镜像；双线包的完整信息在 ``lines`` 里，另一条线的冲突会漏检。
        """
        from ....core.ext import effective as effective_mod
        from ....core.ext import enable as enable_mod
        from ....core.ext import resolver as resolver_mod

        lines = sorted(
            {
                str(line)
                for item in items
                if item.get("enabled", True)
                for line in (
                    (item.get("lines") or {}).keys()
                    if isinstance(item.get("lines"), dict) and item.get("lines")
                    else [str(item.get("engine_line") or "")]
                )
            }
        )
        # 同一条线上的问题可能被多个入口各命中一次，按位置聚合。
        mode = cfg_mod.extension_mode(self.cfg)
        unit_rows: list[dict] = []
        blockers: list[dict] = []
        for line in lines:
            pairs = enable_mod.enabled_manifests(self.cfg, line)
            if len(pairs) < 2:
                continue
            view = effective_mod.of(self.cfg, line)
            if view.dropped:
                pairs = [(pid, data) for pid, data in pairs if pid in view.keep_set]
                if len(pairs) < 2:
                    continue
            resolution = resolver_mod.resolve(
                pairs,
                compose=cfg_mod.extension_compose(self.cfg),
                mode=mode,
                line=line,
                store=decisions_mod.Store.load(cfg_mod.paths.CONFIG_DIR / "decisions.json"),
            )
            plan = getattr(resolution, "plan", None)
            for state in getattr(plan, "states", []) or []:
                row = state.as_dict()
                row["line"] = line
                unit_rows.append(row)
            for problem in resolution.problems:
                # 位置级冲突已经由处置面板接管，这里只留其它真阻断
                if problem.level == "error" and problem.code not in (
                    "conflict.location",
                    "conflict.files",
                ):
                    blockers.append(problem.as_dict())

        self._conflict_units = unit_rows
        pending = [u for u in unit_rows if u.get("action") == "pending"]
        degraded = [u for u in unit_rows if u.get("action") == "degraded"]

        if blockers or (cfg_mod.is_strict(mode) and pending):
            body = "\n".join(
                f"· {u.get('message')}" for u in (pending or blockers)[:5]
            )
            self.conflict_label.setText(
                f"有 {len(pending) + len(blockers)} 处需要处理才能启动。\n{body}"
                if body
                else "扩展组合有冲突，处理好才能启动。"
            )
            self.conflict_label.setVisible(True)
        else:
            self.conflict_label.setVisible(False)

        if not unit_rows:
            self.warn_label.setVisible(False)
            self.resolve_btn.setVisible(False)
        elif not cfg_mod.is_strict(mode):
            # 宽松档只看到一句结论，细节在 tooltip / 日志里
            self.warn_label.setText(
                f"有 {len(degraded)} 处扩展互相影响。游戏可以启动，"
                "但相关功能可能不完整。"
            )
            self.warn_label.setToolTip(
                "\n".join(f"· {u.get('message')}" for u in degraded[:12])
            )
            self.warn_label.setVisible(bool(degraded))
            self.resolve_btn.setVisible(False)
        elif not cfg_mod.is_strict(mode):
            # 玩家版的宽松校验：只给「能不能玩」的结论，不给处置入口。
            # 「保留前者 / 使用后者」这种选择是内容层面的取舍，玩家无从判断，
            # 点进去只会更困惑（2026-11-05 按此口径拆分两版）。
            parts = []
            if degraded:
                parts.append(f"{len(degraded)} 处扩展改动重叠，已自动取舍")
            if pending:
                parts.append(f"{len(pending)} 处改动重叠，按安装顺序处理")
            self.warn_label.setText(
                "；".join(parts) + "。\n游戏可以正常启动，个别功能可能表现不同。"
            )
            self.warn_label.setToolTip(
                "\n".join(f"· {u.get('message')}" for u in (pending + degraded)[:12])
            )
            self.warn_label.setVisible(bool(pending or degraded))
            self.resolve_btn.setVisible(False)
        else:
            body = "\n".join(f"· {u.get('message')}" for u in (pending + degraded)[:5])
            self.warn_label.setText(
                f"有 {len(pending)} 处待处理、{len(degraded)} 处已自动取舍。\n{body}"
            )
            self.warn_label.setToolTip("")
            self.warn_label.setVisible(bool(pending or degraded))
            self.resolve_btn.setVisible(bool(pending or degraded))

        self._refresh_isolation(items)
        self._refresh_dev_drop()


    def _apply_degraded_status(self) -> None:
        """普通模式下把「被自动取舍」的扩展标出来，不藏在日志里。"""
        rows = getattr(self, "_conflict_units", []) or []
        hit = {
            pid
            for row in rows
            if row.get("action") == "degraded"
            for pid in (row.get("parties") or [])
        }
        if not hit:
            return
        for row in range(self.installed_table.rowCount()):
            item = self.installed_table.item(row, 0)
            if item is None:
                continue
            if str(item.data(0x0100) or "") not in hit:
                continue
            status = self.installed_table.item(row, 4)
            if status is None or status.text() != "已就绪":
                continue
            status.setText("部分功能")
            status.setForeground(QBrush(QColor(colors.warn())))
            status.setToolTip("这个扩展与别的扩展改了同一处，启动器已自动取舍")

    def _open_conflicts(self) -> None:
        from ....core import config as cfg_mod
        from ....core.ext import decisions as decisions_mod
        from ....core.ext import enable as enable_mod
        from ..conflicts_dialog import ConflictDialog

        rows = getattr(self, "_conflict_units", []) or []
        if not rows:
            return
        line = str(rows[0].get("line") or self._current_line())
        pairs = enable_mod.enabled_manifests(self.cfg, line)
        meta = {pid: data for pid, data in pairs}
        dlg = ConflictDialog(rows, line=line, meta=meta, parent=self)
        if dlg.exec() != ConflictDialog.DialogCode.Accepted:
            return
        store = decisions_mod.Store.load(cfg_mod.paths.CONFIG_DIR / "decisions.json")
        for decision in getattr(dlg, "decisions", []):
            store.put(decision)
        store.save()
        self._log(f"已保存 {len(getattr(dlg, 'decisions', []))} 条冲突处置", "info")
        # 处置写在 decisions.json，不在注册表指纹里，必须强制重算
        self.reload_installed(force=True)

    def _refresh_dev_drop(self) -> None:
        """开发者模式：提前展示「将剔除 N 个、剩 M 个可启动」。

        正文只列包名（一行排开），每个包撞在哪个文件放进 tooltip。
        13 个包逐行铺开会把下面的表格整个挤出可视区（1080x700 实测
        连表头都看不到），而「哪些包会被剔」才是这里必须一眼看见的。
        """
        from ....core.ext import effective as effective_mod

        if not cfg_mod.is_strict(cfg_mod.extension_mode(self.cfg)):
            self.dev_drop_label.setVisible(False)
            self.dev_drop_label.setToolTip("")
            return
        parts: list[str] = ["开发者校验（导入即准入；冲突拒装，合集整包不通过就不装）："]
        tips: list[str] = []
        # 按配置遍历所有线，不再只算 4.6。
        # 原来写死 `(BUILTIN_LINE,)`，导致 4.7 上的剔除情况在预览里完全不出现 ——
        # 玩家在 4.7 装了一堆包，看到的却是「4.6 线：剔除 0 个」，等于没提示。
        for line in (cfg_mod.known_lines(self.cfg) or (cfg_mod.BUILTIN_LINE,)):
            try:
                view = effective_mod.of(self.cfg, line)
                decision = view.plan
            except Exception as exc:  # noqa: BLE001 - 预览失败不该让页面崩
                parts.append(f"{line} 线：判定失败（{exc}）")
                continue
            if view.dropped:
                parts.append(
                    f"{line} 线：剔除 {len(view.dropped)} 个，生效 {len(view.keep)} 个 —— "
                    + "、".join(view.dropped)
                )
                tips.append(f"=== {line} 线：每个包撞在哪些文件 ===")
                if decision is not None:
                    tips.extend(decision.drop_lines())
                    tips.append("")
                    tips.extend(decision.drop_groups())
            elif view.keep:
                parts.append(f"{line} 线：{len(view.keep)} 个扩展全部通过，无需剔除")
            else:
                parts.append(f"{line} 线：没有已启用的扩展")
        parts.append(
            "（同一处内容被多个包改动时整组剔除，先装后装一个都不留；"
            "悬停看每个包撞的文件）"
        )
        self.dev_drop_label.setText("\n".join(parts))
        self.dev_drop_label.setToolTip("\n".join(tips))
        self.dev_drop_label.setVisible(True)

    def _runtime_violations(self) -> list[dict]:
        """读**最近一次启动**留下的 mod_report.json 里的越界记录。

        这是「运行期真的被拦下了」的唯一证据：源码扫描只能看出「它写了
        越界代码」，只有报告能证明「它真的越界过、而且被拦住了」。
        """
        from ....core import smoke as smoke_mod

        latest = None
        try:
            for p in paths.BUILDS.glob("run*/mod_report.json"):
                if latest is None or p.stat().st_mtime > latest.stat().st_mtime:
                    latest = p
        except OSError:
            return []
        if latest is None:
            return []
        try:
            return smoke_mod.read_mod_report_full(latest).violations
        except Exception:  # noqa: BLE001 - 报告坏了不该让页面崩
            return []

    def _refresh_isolation(self, items: list[dict]) -> None:
        """体检条：扩展之间的越界访问、钩子点共享。

        **只在开发者版显示**（原先是 ``is_strict``，把宽松校验也算进来了）。

        为什么玩家不该看：这类提示说的是「两个包驻留在同一个钩子点」
        「某个包越界读了别人的数据」。玩家对这些的可行动作是零——既不能改
        代码也不能改包，而红色会让他误判成「机器坏了」。玩家只需要知道
        「能不能启动」，那由上面那条真冲突红条负责。

        为什么开发者必须看：它揭示的是文件层面看不出来的运行期冲突。构建期
        的锚点/位置检查只看「改没改同一段文本」，看不到「两个回调改同一个
        内存对象」。开发者要靠这条知道该去哪补兼容。

        为了让开发者拿到**结论**而不是风险猜测，这里接入了键级判定
        （``ext.hook_keys``）：能静态算出「改的键不重叠」就直接标「可安全
        共存」，真重叠（或 skip_original 吞掉后者）才标冲突。
        """
        from ....core.ext import hook_keys as keys_mod
        from ....core.ext import hook_scan as scan_mod
        from ....core.ext import isolation as iso
        from ....core.ext.service import ExtensionService

        mode = cfg_mod.extension_mode(self.cfg)
        # 玩家版一律不显示：它的出厂档是宽松校验，这一类对它没有可执行价值。
        if not cfg_mod.is_strict(mode):
            self.isolation_label.setVisible(False)
            return

        line = self._current_line()
        tag = line.replace(".", "")
        per_ext: dict[str, list[iso.PeerFinding]] = {}
        src_roots: dict[str, object] = {}
        runtime_hooks: dict[str, list[dict]] = {}
        for item in items:
            ext_id = str(item.get("id") or "")
            if not ext_id or not item.get("enabled", True):
                continue
            src = paths.BUILDS / f"_install_{ext_id}_{tag}" / "src"
            files = iso.load_src_files(src)
            if files:
                per_ext[ext_id] = iso.scan_tree(files, ext_id)
            if src.is_dir():
                src_roots[ext_id] = src
            entry = (item.get("lines") or {}).get(line)
            if isinstance(entry, dict):
                runtime_hooks[ext_id] = entry.get("runtime_hooks") or []

        # 钩子点共享：**用真实注册的相位**归组，不用插桩表。
        #
        # 插桩层为了「运行期注册一定命中」对缺 runtime_hooks 的老记录做
        # before+after 双插（见 enable._hooks_of_item）。若拿插桩表判撞车，
        # 一个只注册 after 的包会凭空多出一个 before，和别人的 before 假撞车
        # —— 实测 17 个包里 15 个缺 runtime_hooks，能虚报 2 条。
        #
        # 所以这里走 hook_scan：优先 runtime_hooks，缺了就从安装源码反查
        # add_method_hook 的第三参数。两件事用两份数据，绝不混用。
        by_point: dict[tuple[str, str, str], set[str]] = {}
        for ext_id in src_roots.keys() | runtime_hooks.keys():
            scanned = None
            for h in runtime_hooks.get(ext_id) or []:
                key = (
                    str(h.get("script", "")),
                    str(h.get("method", "")),
                    str(h.get("phase", "")).strip().lower(),
                )
                if all(key):
                    by_point.setdefault(key, set()).add(ext_id)
            if runtime_hooks.get(ext_id):
                continue
            # 没有 runtime_hooks：源码反查真实相位
            try:
                if scanned is None:
                    scanned = scan_mod.scan_package(src_roots[ext_id])  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - 反查失败就跳过该包的撞车判定
                continue
            for site in scanned.sites:
                by_point.setdefault(site.key, set()).add(ext_id)

        hook_rows: list[str] = []
        safe_rows: list[str] = []
        clashes = 0
        unknowns = 0
        for (script, method, phase), owners in sorted(by_point.items()):
            if len(owners) < 2:
                continue
            try:
                analysis = keys_mod.analyze_point(
                    src_roots, script, method, phase, sorted(owners)
                )
            except Exception:  # noqa: BLE001 - 判定失败不影响主流程
                continue
            names = "、".join(sorted(owners))
            if analysis.level == "safe":
                # **判定安全的不进主体**。
                #
                # 原来 safe 也往 hook_rows 里塞一行「无需处理」，结果就是：
                # 只要有两个包共用同一个钩子点，面板永远挂在那儿 ——
                # 哪怕键完全不重叠、实际毫无影响。
                #
                # 用户实测反馈：「安装是成功的，但是提示还在」。
                # 提示在，玩家就以为还有问题，实际什么也不用做。
                # 现在 safe 收进单独列表，只在「展开详情」里给要查的人看。
                safe_rows.append(
                    f"{method}（{phase}）：{names} —— {analysis.message}"
                )
                continue
            if analysis.level == "unknown":
                # unknown 是「解析不出写入」，不等于冲突。
                # 原来一并计入 clashes 并把面板染红，红条因此失去意义。
                unknowns += 1
                hook_rows.append(
                    f"{method}（{phase}）：{names} 共用此钩子点，"
                    f"{analysis.message}"
                )
                continue
            clashes += 1
            hook_rows.append(
                f"{method}（{phase}）：{names} 共用此钩子点，{analysis.message}"
            )

        all_findings = [f for fs in per_ext.values() for f in fs]

        # **只把 block 放进主面板。**
        #
        # 2026-11-08 分级之后，L0-1/2/3/4/7/11 全是 warn —— 它们代表
        # 「这个包读了宿主 / 读了别人的资源」，是本项目扩展的**正常写法**
        # （定位就是补全原版缺失内容）。把它们铺在面板上，等于每次装完包
        # 都留一大片黄字，玩家以为出了问题，实际什么也不用做。
        #
        # warn 收进 notice_rows，只在 tooltip 里给排查的人看。
        rows: list[str] = list(hook_rows)
        notice_rows: list[str] = []
        for f in all_findings:
            text = f"{f.ext_id}：{f.detail}" + (
                f"（{f.path}:{f.line}）" if f.path else ""
            )
            if f.severity == "block":
                rows.append(text)
            else:
                notice_rows.append(text)
        block_count = sum(1 for f in all_findings if f.severity == "block")

        # 源码已清理的扩展：只剩注册表里存的扫描结果。
        #
        # 判据是「有没有扫到源码」而不是「rows 空不空」—— 分级之后
        # warn 不进 rows，rows 为空但源码扫过是很常见的正常情况，
        # 用 rows 判空会让这条后备逻辑误触发、把老记录重复报一遍。
        if not per_ext:
            svc = ExtensionService(self.cfg)
            for item in items:
                if not item.get("enabled", True):
                    continue
                for f in svc._stored_findings(item) or []:
                    if f.get("severity") != "block":
                        continue
                    block_count += 1
                    rows.append(f"{item.get('id')}：{f.get('detail')}（安装时扫描结果）")

        # 运行期真的越界了 —— 这条最硬，无论扫描结果如何都要显示
        violations = self._runtime_violations()
        for v in violations[:8]:
            who = str(v.get("from") or "?")
            target = str(v.get("target") or "?")
            detail = str(v.get("detail") or "")
            rows.append(f"运行期：{who} → {target}：{detail}" if detail else f"运行期：{who} → {target}")
        if len(violations) > 8:
            rows.append(f"… 另有 {len(violations) - 8} 条运行期越界记录")

        # 到这里 rows 里只剩「需要人看一眼」的项了（safe 已收进 safe_rows）。
        # 如果连一条都没有，整个面板隐藏 —— 这正是用户实测反馈的那条：
        # 「安装是成功的，但是提示还在」。没东西要处理就不该占地方。
        if not rows:
            self.isolation_label.setVisible(False)
            return

        # 有**真冲突**才用红；全是「需人工确认」用琥珀。
        # 否则开发者会对红免疫，真问题反而被淹掉。
        severe = clashes > 0 or block_count > 0 or bool(violations)
        color = colors.err() if severe else colors.warn()
        self.isolation_label.setStyleSheet(
            f"background:{colors.tint(color, 0.12)};"
            f"border:1px solid {color}; border-radius:8px;"
            f"padding:8px 12px; color:{color}; font-size:12px;"
        )

        head = f"扩展体检（{_MODE_LABELS.get(mode, mode)}）："
        parts: list[str] = []
        if clashes:
            parts.append(f"{clashes} 处钩子真冲突")
        if block_count:
            parts.append(f"{block_count} 处越界阻断")
        if unknowns:
            parts.append(f"{unknowns} 处需人工确认")
        if violations:
            parts.append(f"最近一次启动有 {len(violations)} 次越界")
        head += "，".join(parts) if parts else "有待确认项"
        if not severe:
            # 明确告诉玩家「这些不影响玩」，否则他看到琥珀条还是会去点
            head += "（不影响本次使用）"

        body = "\n".join(rows[:3])
        if len(rows) > 3:
            body += f"\n… 还有 {len(rows) - 3} 项"
        from .... import edition as _edition

        body += f"\n详情：{_edition.exe_name()} cli ext-audit"

        tip = list(rows)
        if notice_rows:
            tip.append("")
            tip.append(f"— 以下 {len(notice_rows)} 条为提示级（不影响使用）—")
            tip.extend(notice_rows[:40])
            if len(notice_rows) > 40:
                tip.append(f"… 另有 {len(notice_rows) - 40} 条，见 cli ext-audit")
        if safe_rows:
            # 安全项不占主面板，但开发者排查时想核对还是得能看到
            tip.append("")
            tip.append(f"— 以下 {len(safe_rows)} 处已判定可安全共存 —")
            tip.extend(safe_rows)
        self.isolation_label.setText(head + "\n" + body)
        # 面板只铺 3 项，完整清单放 tooltip（否则又要把表格挤出可视区）
        self.isolation_label.setToolTip("\n".join(tip))
        self.isolation_label.setVisible(True)

    def _on_selection(self) -> None:
        ids = self._selected_ids()
        # 启停与跨线安装都是**单包**语义：多选时显式禁用，比让用户点了
        # 之后只对第一行生效更不意外。
        single = len(ids) == 1
        # 运行中必须一起压住：reload_installed 末尾会调本方法，
        # 只看「有没有选中」会把 set_game_running 刚压下去的按钮又点亮。
        running = self.game_running()
        self.uninstall_btn.setEnabled(bool(ids) and not running)
        self.uninstall_btn.setText(f"卸载选中 ({len(ids)})" if len(ids) > 1 else "卸载选中")
        self.toggle_btn.setEnabled(single and not running)
        self.toggle_btn.setToolTip("多选时请先单选一个再启停" if len(ids) > 1 else "")
        # set_game_running 解锁时靠这个属性恢复可用态。过去没有任何地方写它，
        # 解锁后总把「卸载选中」重新点成可用（哪怕一行没选），只能靠下一次
        # 选择事件兜底——顺手补上。
        self.uninstall_btn._was_enabled = bool(ids)
        self.toggle_btn._was_enabled = single

    def _toggle_selected(self) -> None:
        ext_id = self._selected_id()
        if ext_id:
            self._toggle_id(ext_id)

    def _uninstall_selected(self) -> None:
        """底部按钮：批量卸载所有选中项。"""
        ids = self._selected_ids()
        if ids:
            self._uninstall_ids(ids)

    def _ask_uninstall_line(
        self, ext_id: str, installed_lines: dict, cur_line: str
    ) -> str | None:
        """多线包卸载时问一句「只卸这条线，还是全卸」。

        返回值：线 id = 只卸该线；``""`` = 全部卸载；``None`` = 取消。

        为什么必须问：4.6 与 4.7 的覆盖层是两份独立文件，只卸一条线不会
        影响另一条线还能不能玩。反过来，直接删整个 ``packs/<id>`` 会让
        另一条线静默失效——玩家切过去只会看到「扩展没了」，还以为是启动器
        自己弄丢的。
        """
        lines = sorted(str(k) for k in installed_lines if str(k))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("卸载范围")
        box.setText(f"「{ext_id}」同时装在 {len(lines)} 条线上：{'、'.join(lines)}")
        box.setInformativeText(
            "两条线的覆盖层、补丁结果与启用状态互相独立。\n"
            "只卸载一条线时，另一条线仍可正常启动。"
        )
        only_btn = None
        if cur_line in lines:
            only_btn = box.addButton(
                f"只卸载 {cur_line} 线", QMessageBox.ButtonRole.AcceptRole
            )
        all_btn = box.addButton("全部卸载", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is only_btn and only_btn is not None:
            return cur_line
        if clicked is all_btn:
            return ""
        return None

    def _uninstall_ids(self, ids: list[str]) -> None:
        """批量卸载：依赖预检 → 一次确认 → 一次写盘 → 汇总。

        确认框必须列名字：批量删除不可撤销，只写「3 个」用户根本不知道删的
        是哪三个。超过 5 个折叠成「等 N 个」，避免弹窗被撑出屏幕。
        """
        if not self._guard_running("卸载扩展"):
            return
        svc = ExtensionService(self.cfg)
        entries = {str(i.get("id")): i for i in svc.installed()}
        names = [str((entries.get(i) or {}).get("name") or i) for i in ids]
        shown = "、".join(names[:5]) + (f" 等 {len(names)} 个" if len(names) > 5 else "")

        dependents = svc.dependents_of(ids)
        extra = ""
        if dependents:
            lines = [
                f"{d.get('name')}（{d.get('id')}）依赖 {'、'.join(d.get('requires') or [])}"
                for d in dependents[:5]
            ]
            extra = (
                "\n\n注意：以下扩展依赖它们，卸载后启动会被拒绝：\n  "
                + "\n  ".join(lines)
                + "\n可以稍后一并卸载，或重新装回被依赖的包。"
            )

        cur_line = self._current_line()
        target_line = ""
        if len(ids) == 1:
            item = entries.get(ids[0]) or {}
            installed_lines = item.get("lines") if isinstance(item.get("lines"), dict) else {}
            if len(installed_lines) > 1:
                target_line = self._ask_uninstall_line(ids[0], installed_lines, cur_line)
                if target_line is None:
                    self._log("已取消卸载", "dim")
                    return

        scope_text = f"{target_line} 线" if target_line else ""
        answer = QMessageBox.question(
            self,
            f"确认卸载{scope_text}扩展" if len(ids) == 1 else f"确认卸载 {len(ids)} 个扩展",
            f"将卸载{'（仅 ' + target_line + ' 线）' if target_line else ''}：{shown}\n\n"
            f"会删除{'该线的覆盖层与注册记录' if target_line else '它们的扩展文件与注册记录'}，"
            f"且不可撤销。{extra}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._log("已取消卸载", "dim")
            return

        from ....core import runtime_lock

        try:
            if target_line:
                report = svc.uninstall_line(ids, target_line)
            else:
                report = svc.uninstall_many(ids)
        except runtime_lock.ModLockedError as exc:
            # core 层也会拦（CLI/脚本同样受保护），这里必须接住，否则异常
            # 会冒到 Qt 事件循环里，界面既不提示也不复位。
            QMessageBox.warning(self, "游戏正在运行", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "卸载失败", str(exc))
            self._log(f"卸载失败：{exc}", "err")
            return

        self._log(report.message, "ok" if report.ok else "warn")
        for warning in report.warnings:
            self._log(f"  {warning}", "warn")
        if dependents:
            self._log(
                "卸载后这些扩展缺少依赖，启动会被拒绝："
                + "、".join(str(d.get("id")) for d in dependents),
                "warn",
            )
        self.reload_installed(force=True)  # 卸载刚改了注册表，别吃缓存
        self.modsChanged.emit()

    # ------------------------------------------------------------ 残留清理

    def _cleanup_residue(self) -> None:
        """清理「卸载后仍能检测到」的派生残留。

        三处残留各自独立：注册表里指向已删覆盖层的过期条目、``packs/`` 下
        不在注册表的孤儿目录、来源已消失的插桩缓存，以及 ``builds/`` 里的
        安装中间产物。先算计划给用户看，再执行——直接删会让人不知道删了什么。
        """
        from ....core.ext import janitor
        from ....core.ext.janitor import human_size

        if not self._guard_running("清理扩展残留"):
            return
        findings = janitor.plan()
        if not findings:
            self._log("没有发现需要清理的残留", "ok")
            QMessageBox.information(
                self,
                "清理残留",
                "没有发现需要清理的残留。\n\n"
                "已安装扩展、剧情包、游戏存档都不会被列入清理范围。",
            )
            return

        total = sum(f.bytes for f in findings)
        lines = [f"· {f.detail}" for f in findings[:12]]
        if len(findings) > 12:
            lines.append(f"· ……等共 {len(findings)} 项")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("清理扩展残留")
        box.setText(f"发现 {len(findings)} 项残留，共 {human_size(total)}：")
        box.setInformativeText("\n".join(lines))
        box.setDetailedText(
            "清理范围说明：\n"
            "· 注册表过期条目：登记的覆盖层文件已不存在，列出来也启动不了\n"
            "· packs/ 孤儿目录：不在注册表里，不参与启动组装\n"
            "· 插桩缓存：来源覆盖层已删除，不可能再被正确命中\n"
            "· 安装中间产物：builds/_install_* 与 _bundle_*，纯临时文件\n\n"
            "不会删除：仍被注册表引用的扩展、story-* 剧情包、"
            "*-shards 分片包、profiles/ 存档、config/launcher.json。"
        )
        ok_btn = box.addButton("清理", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is not ok_btn:
            return

        report = janitor.apply(findings)
        self._log(report.message(), "ok" if report.ok else "warn")
        for rec in report.removed[:20]:
            self._log(f"  已清理：{rec.detail}", "dim")
        for failure in report.failed:
            self._log(f"  未能删除：{failure.get('path')} — {failure.get('error')}", "warn")

        self.reload_installed(force=True)  # 清理删了文件，别吃缓存
        self.modsChanged.emit()

    def _selected_ids(self) -> list[str]:
        """所有选中行的扩展 id（按行号升序、去重）。

        ext_id 存在第 0 列的 ``Qt.UserRole``（0x0100），和 ``_selected_id``
        用同一个位置，避免出现两套取值口径。
        """
        rows = sorted({i.row() for i in self.installed_table.selectedItems()})
        out: list[str] = []
        for row in rows:
            item = self.installed_table.item(row, 0)
            ext_id = str(item.data(0x0100) or "") if item else ""
            if ext_id and ext_id not in out:
                out.append(ext_id)
        return out

    def _selected_id(self) -> str:
        ids = self._selected_ids()
        return ids[0] if ids else ""

    # ------------------------------------------------------------ 行内操作

    def _row_actions(self, ext_id: str, enabled: bool) -> QWidget:
        """每行右侧的「启用/停用 + 卸载」，紧凑两枚小按钮。

        宽度必须自己算准：``QTableWidget.setCellWidget`` 里的控件不会参与
        列宽的 ResizeToContents 计算，列宽给不够时 Qt 会把按钮文字省略成「…」。
        """
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(4, 1, 4, 1)
        lay.setSpacing(5)

        toggle = QPushButton("停用" if enabled else "启用")
        toggle.setProperty("variant", "ghost")
        toggle.setFixedHeight(22)
        toggle.setMinimumWidth(ROW_BTN_MIN_W)
        # 行内按钮不做横向 padding：QSS 全局的 16px 左右内边距在这里太奢侈，
        # 扣掉后只剩 53px 反而把文字挤成「…」。固定宽度 + 紧凑内边距最稳。
        toggle.setStyleSheet("padding: 0 6px;")
        if self.game_running():
            toggle.setEnabled(False)
            toggle.setToolTip(f"游戏正在运行（{self._lock_detail}），退出后才能操作")
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setToolTip("停用后启动时不挂载它（文件保留，可随时启用）")
        toggle.clicked.connect(lambda _=False, i=ext_id: self._toggle_id(i))
        lay.addWidget(toggle)

        drop = QPushButton("卸载")
        drop.setProperty("variant", "danger")
        drop.setFixedHeight(22)
        drop.setMinimumWidth(ROW_BTN_MIN_W)
        drop.setStyleSheet("padding: 0 6px;")
        if self.game_running():
            drop.setEnabled(False)
            drop.setToolTip(f"游戏正在运行（{self._lock_detail}），退出后才能操作")
        drop.setCursor(Qt.CursorShape.PointingHandCursor)
        drop.setToolTip("删除该扩展的扩展文件与注册记录")
        drop.clicked.connect(lambda _=False, i=ext_id: self._uninstall_id(i))
        lay.addWidget(drop)
        lay.addStretch(0)
        return box

    def _guard_running(self, action: str) -> bool:
        """动作前置检查：游戏在跑就提示并返回 False（拒绝执行）。

        界面上按钮已经禁用了，但这里再拦一道——防止通过快捷键、右键菜单或
        残留的信号连接绕过禁用态。
        """
        if not self.game_running():
            return True
        QMessageBox.information(
            self,
            "游戏正在运行",
            f"游戏正在运行（{self._lock_detail}），此时不能{action}。\n\n"
            "请先退出游戏，等进程完全结束后再操作。",
        )
        return False

    def _log_hint(self, stage: str) -> None:
        """失败时补一段「去哪儿拿日志」的指引。

        用户反馈过：出问题时界面只报一句错，人只能拿手机拍屏幕。
        安装/启动/校验任何一处出错，都必须能立刻拿到完整日志——
        所以这里统一把「当前日志文件路径」和「运行目录」打进日志区，
        两个路径都能直接复制。
        """
        from ....core.logging import current_log_file

        self._log("———— 排障指引 ————", "warn")
        self._log("请点日志区右侧「复制」把日志贴给 AI，或用「打开目录」找到日志文件；"
                  "不要用手机拍屏幕（拍图会丢堆栈与路径细节）。", "warn")
        log_path = current_log_file()
        if log_path is not None:
            self._log(f"当前日志文件：{log_path}", "dim")
        if stage:
            self._log(f"出错环节：{stage}", "dim")

    def _toggle_id(self, ext_id: str) -> None:
        """启用/停用**当前线**。只改注册状态，不碰文件，随时可逆。

        4.6 与 4.7 的启用状态互相独立：在 4.6 停用一个包，切到 4.7 它仍然
        是启用的。所以这里必须把当前线传下去，不能改「整包」的全局开关。
        """
        if not self._guard_running("启用/停用扩展"):
            return
        line = self._current_line()
        svc = ExtensionService(self.cfg)
        current = svc.get(ext_id) or {}
        lines = current.get("lines") if isinstance(current.get("lines"), dict) else {}
        entry = lines.get(line) if isinstance(lines, dict) else None
        target = not bool((entry or {}).get("enabled", current.get("enabled", True)))
        try:
            ok = svc.set_enabled(ext_id, target, line=line)
        except Exception as exc:  # noqa: BLE001 - 含 ModLockedError
            QMessageBox.warning(self, "无法修改启用状态", str(exc))
            return
        if not ok:
            QMessageBox.information(
                self,
                "无法修改启用状态",
                f"「{ext_id}」没有装到 {line} 线，请切到它所在的那条线再操作。",
            )
            return
        self._log(
            f"扩展 {ext_id} 在 {line} 线已{'启用' if target else '停用'}", "ok"
        )
        self.reload_installed(force=True)  # 启停改了注册表，别吃缓存
        self.modsChanged.emit()

    def _uninstall_id(self, ext_id: str) -> None:
        """行内单包卸载：复用批量路径。

        保留这个方法名是因为行内按钮按它连接；
        实现只有一行，行为与底部「卸载选中」完全一致（同一个确认框、
        同一套依赖预检），不会出现「行内能删、底部删不掉」的分叉。
        """
        self._uninstall_ids([ext_id])

    # ------------------------------------------------------------ 整体校验

    def _rebuild_mode_combo(self) -> None:
        """按**当前线**重建档位下拉。

        为什么必须是「重建」而不是「初始化时建一次」：

        可选档位是**随线变化**的（4.6 两档，4.7 只有完全校验）。原来这段
        只在 ``__init__`` 里跑一次，于是玩家在游戏库从 4.6 切到 4.7 之后，
        扩展页仍然显示 4.6 的两档 —— 下拉里那个「宽松校验」在 4.7 上
        根本不该存在，玩家选了它也只会被 `available_modes` 静默纠正。

        这条不只是显示问题：档位决定禁用名单（``self._disabled_modes``），
        显示与实际不一致会让人以为「我明明选了完全校验，怎么还按宽松跑」。
        """
        from ..features import available_modes

        modes = available_modes(self._current_line())
        labels = {
            "loose": "宽松校验（装得上，但可能黑屏）",
            "strict": "完全校验（装不上就不装，稳定）",
        }
        prev = str(self.mode_combo.currentData() or "")

        blocked = self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        for value in modes:
            self.mode_combo.addItem(labels[value], value)

        # 选中项：优先保留玩家原来的选择（切回 4.6 时不该被重置），
        # 那条线上没有该档时才回落到该线的默认档。
        cur_mode = prev if prev in modes else cfg_mod.normalize_mode(
            cfg_mod.extension_mode(self.cfg)
        )
        if cur_mode not in modes:
            # 例如 4.7 上配置里残留 loose：显示上按 strict 走，但**不改配置** ——
            # 玩家切回 4.6 时不该发现自己选的档被悄悄改了。
            cur_mode = modes[0]
            self.mode_combo.setEnabled(False)
            self.mode_combo.setToolTip(f"{self._current_line()} 固定使用完全校验")
        else:
            self.mode_combo.setEnabled(True)
            self.mode_combo.setToolTip(
                "\n\n".join(
                    f"{labels[m].split('（')[0]}：{cfg_mod.MODE_HELP[m]}" for m in modes
                )
            )
        idx = self.mode_combo.findData(cur_mode)
        self.mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.mode_combo.blockSignals(blocked)

    def set_current_line(self, line: str) -> None:
        """主窗口切版本时同步过来，整体校验就按界面上选的线跑。

        过去这里读 ``cfg["ui"]["last_version"]``，那是「上次点了启动」的记忆，
        与界面上当前选中的版本不一定一致——实测配置里是 4.6、界面在另一条线，
        于是「整体校验」会拿两条线的启用集合去验另一条线，结论对不上。

        **切版本时必须重建档位下拉**：可选档位随线变化（4.6 两档、4.7 只有
        完全校验）。原来下拉只在构造时建一次，切到 4.7 后仍显示 4.6 的两档。
        """
        text = str(line or "").strip()
        if not text:
            return
        changed = text != self._line_hint
        self._line_hint = text
        if changed and getattr(self, "mode_combo", None) is not None:
            self._rebuild_mode_combo()

    def _current_line(self) -> str:
        """当前正在管理的引擎线。

        优先用主窗口同步来的当前线（``_line_hint``），没有才回落到配置记忆。

        **回落到配置记忆时不再强制归到 4.6。** 原实现是：

            return line if line == BUILTIN_LINE else BUILTIN_LINE

        那行在只有 4.6 一条线时看不出问题，加了 4.7 之后就出事了：
        玩家在游戏库切到 4.7，扩展页仍按 4.6 算 —— 档位显示两档、
        检查按 4.6 做、剔除按 4.6 判，全都是错的线。

        现在的回落规则：配置记忆里的线**只要真的存在**就用它，否则才归到
        内置线。判据用 ``known_lines``（配置里登记了哪些引擎线），
        而不是线名白名单 —— 后者每加一条线就得改一次代码。
        """
        hint = str(getattr(self, "_line_hint", "") or "")
        if hint:
            return hint
        line = str(((self.cfg.get("ui") or {}).get("last_version")) or cfg_mod.BUILTIN_LINE)
        known = set(cfg_mod.known_lines(self.cfg))
        return line if line in known else cfg_mod.BUILTIN_LINE

    def _verify_all(self) -> None:
        """整线组合校验：真跑一次引擎，核对每个启用的包是否真的装载。

        单个安装的冒烟只证明「这一个包能装起来」；批量为了速度还跳过了它。
        这里是最终兜底：所有已启用扩展一起挂起来，看 ``mods_loaded`` 与
        声明集合是否一致。几十秒到几分钟，所以只在用户主动点时执行。

        逻辑在 :mod:`app.core.ext.verify`，**不再** 按路径加载
        ``tools/verify_mods.py``——打包后 ``tools/`` 不在发布包里，那样点必崩。
        """
        if not self._guard_running("整体校验扩展"):
            return
        line = self._current_line()
        self.verify_all_btn.setEnabled(False)
        self._log(f"开始整体校验 {line}（会真跑一次引擎，可能要几分钟）…", "info")

        from ....core.ext.verify import verify_line
        from ..workers import TaskWorker

        worker = TaskWorker(verify_line, self.cfg, line, 240.0)
        worker.done.connect(self._on_verify_all_done)
        worker.failed.connect(self._on_verify_all_failed)
        worker.finished.connect(lambda: self.verify_all_btn.setEnabled(True))
        # 必须留住引用：QThread 运行中被回收会直接崩。
        self._verify_worker = worker
        worker.start()

    def _on_verify_all_done(self, out: dict) -> None:
        if not isinstance(out, dict):
            self._log("整体校验返回了异常结果，已忽略。", "err")
            return
        ok = bool(out.get("ok"))
        loaded = out.get("loaded") or []
        self._log(
            f"整体校验{'通过' if ok else '未通过'}：实际装载 {len(loaded)} 个"
            f"，耗时 {out.get('seconds', '-')} 秒",
            "ok" if ok else "err",
        )
        for ext_id in out.get("missing") or []:
            self._log(f"  未装载：{ext_id}", "err")
        for ext_id in out.get("unexpected") or []:
            self._log(f"  未声明却被装载：{ext_id}", "warn")
        for item in out.get("failed") or []:
            self._log(f"  装载失败：{item.get('id')} — {item.get('error')}", "err")
        for problem in out.get("problems") or []:
            self._log(f"  {problem}", "err")
        events = out.get("game_events") or []
        if events:
            # 事件订阅过去是静默失效的重灾区（订阅被拒、扩展却以为订上了），
            # 现在把「真的挂上了哪些」直接打到日志里。
            self._log(f"  已订阅游戏事件：{'、'.join(events)}", "dim")
        if out.get("run_dir"):
            self._log(f"  运行目录：{out['run_dir']}", "dim")
        if not ok:
            # 校验失败必须给可复制的排障材料：日志文件路径 + 运行目录，
            # 让用户「复制日志给 AI 看」，而不是拿手机拍屏幕。
            self._log_hint("整体校验未通过")

    def _on_verify_all_failed(self, message: str, tb: str) -> None:
        self._log(f"整体校验失败：{message}", "err")
        # traceback 全文比最后一行有用得多：最后一行常常是 Qt 的包装帧。
        for line in (tb or "").strip().splitlines()[-6:]:
            self._log(line, "dim")
        self._log_hint("整体校验失败")

    def _open_pack_dir(self) -> None:
        paths.PACKS.mkdir(parents=True, exist_ok=True)
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.PACKS)))

    # ------------------------------------------------------------ 拖拽

    def _drop_paths(self, event) -> list[Path]:
        """一次拖入的所有包（保序去重、过滤后缀）。"""
        return drop_paths(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        found = self._drop_paths(event)
        if found:
            event.acceptProposedAction()
            label = f"{len(found)} 个包" if len(found) > 1 else found[0].name
            self._log(f"松手即安装：{label}", "dim")

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._drop_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        found = self._drop_paths(event)
        if not found:
            event.ignore()
            return
        event.acceptProposedAction()
        if not self._install:
            self._log("未连接安装器，请从主窗口重试。", "err")
            return
        self._install(found)

    # ------------------------------------------------------------ 导入

    def pick_install(self) -> None:
        """弹文件选择框导入扩展包。公开方法：库页空状态也走同一个入口。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择扩展包（可多选）", str(Path.home()), "扩展包 (*.zip *.jjm *.pack)"
        )
        if not paths:
            return
        if self._install:
            self._install([Path(p) for p in paths])
        else:
            self._log("未连接安装器，请从主窗口重试。", "err")

    def _pick_verify(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要校验的扩展包", str(Path.home()), "扩展包 (*.zip *.jjm *.pack)"
        )
        if path:
            self.verify_package(Path(path))

    def verify_package(self, package: Path) -> None:
        self._package = package
        self._log(f"正在校验：{package.name}", "info")
        self.verify_btn.setEnabled(False)
        from ..workers import TaskWorker

        worker = TaskWorker(self._load_manifest, package)
        worker.done.connect(self._on_loaded)
        worker.failed.connect(self._on_load_failed)
        worker.finished.connect(lambda: self.verify_btn.setEnabled(True))
        self._worker = worker
        worker.start()

    def _load_manifest(self, package: Path):
        probe = ext_manifest.load(package)
        line = probe.engine_line or "4.6"
        src_meta = (self.cfg.get("runtime_sources") or {}).get(line)
        if src_meta and src_meta.get("project_godot"):
            src_root = Path(src_meta["project_godot"]).parent
        else:
            # 与 service.install 一致：只填了「反解工程」也要能核对钩子
            reversed_root = (self.cfg.get("sources") or {}).get("reversed_project") or ""
            src_root = Path(reversed_root) if reversed_root and Path(reversed_root).is_dir() else None
        try:
            paths_set = baseline_mod.path_set(line)
        except FileNotFoundError:
            paths_set = None
        m = ext_manifest.load(package, src_root, paths_set)
        return m, line, paths_set

    def _on_load_failed(self, message: str, tb: str) -> None:
        self._log(f"校验失败：{message}", "err")
        if tb.strip():
            self._log(tb.strip().splitlines()[-1], "err")

    def _on_loaded(self, payload) -> None:
        m, line, paths_set = payload
        self._manifest = m
        self._paths_set = paths_set
        summary = m.summary()
        self._log(
            f"{summary['name']} {summary['version']}：{summary['files']} 文件 / "
            f"{summary['method_hooks']} 钩子，校验{'通过' if summary['ok'] else '未通过'}",
            "ok" if summary["ok"] else "err",
        )

        self._show_detail(True)
        self.title_label.setText(summary["name"])
        self.id_label.setText(f"{summary['id']} · v{summary['version']}")
        self.engine_pill.setText(f"Godot {line}")
        self.engine_pill.set_tone("info")
        self.baseline_pill.setText(summary["baseline"] or "未声明基线")
        self.baseline_pill.set_tone("muted")
        self.verdict_pill.setText("校验通过" if summary["ok"] else "存在问题")
        self.verdict_pill.set_tone("ok" if summary["ok"] else "err")
        self.meta_label.setText(
            f"入口 {summary['entry'] or '—'}　|　数据补丁 {summary['data_patches']}　|　"
            f"文本补丁 {summary['text_patches']}　|　框架要求 {m.data.get('framework') or '无'}"
        )

        self.files_table.setRowCount(0)
        for rec in m.files:
            row = self.files_table.rowCount()
            self.files_table.insertRow(row)
            kind = "替换" if (paths_set and self._covers(rec["target"], paths_set)) else "新增"
            self.files_table.setItem(row, 0, QTableWidgetItem(kind))
            self.files_table.setItem(row, 1, QTableWidgetItem(rec["source"]))
            self.files_table.setItem(row, 2, QTableWidgetItem(rec["target"]))
            if kind == "替换":
                self.files_table.item(row, 0).setForeground(QBrush(QColor(colors.warn())))

        # 补丁也列进「文件清单」，用来源列写明改了什么，
        # 否则导入一个只有补丁的包会看到空表格，以为没生效。
        data_patches = m.data.get("data_patches") or []
        text_patches = m.data.get("text_patches") or []
        for patch in data_patches:
            row = self.files_table.rowCount()
            self.files_table.insertRow(row)
            ops = len(ext_manifest.ops_of(patch))
            item = QTableWidgetItem("数据补丁")
            item.setForeground(QBrush(QColor(colors.accent())))
            self.files_table.setItem(row, 0, item)
            self.files_table.setItem(row, 1, QTableWidgetItem(f"{ops} 个 JSON 操作"))
            self.files_table.setItem(row, 2, QTableWidgetItem(str(patch.get("target") or "")))
        for patch in text_patches:
            row = self.files_table.rowCount()
            self.files_table.insertRow(row)
            mode = str(patch.get("mode") or "literal")
            find = str(patch.get("find") or "")
            item = QTableWidgetItem("文本补丁")
            item.setForeground(QBrush(QColor(colors.accent())))
            self.files_table.setItem(row, 0, item)
            self.files_table.setItem(
                row, 1, QTableWidgetItem(f"{mode} · 期望 {patch.get('expect_count', 1)} 处 · {find[:36]}")
            )
            self.files_table.setItem(row, 2, QTableWidgetItem(str(patch.get("target") or "")))

        self.hooks_table.setRowCount(0)
        for h in m.method_hooks:
            row = self.hooks_table.rowCount()
            self.hooks_table.insertRow(row)
            self.hooks_table.setItem(row, 0, QTableWidgetItem(h["target"]))
            self.hooks_table.setItem(row, 1, QTableWidgetItem(h["method"]))
            self.hooks_table.setItem(row, 2, QTableWidgetItem(h["phase"]))
            ok = any(
                i.code == "hook.ok" and f"{h['target']}::{h['method']}" in i.message for i in m.issues
            )
            item = QTableWidgetItem("✓ 存在" if ok else "未核对")
            item.setForeground(QBrush(QColor(colors.ok() if ok else colors.faint())))
            self.hooks_table.setItem(row, 3, item)

    # ------------------------------------------------------------ 工具

    @staticmethod
    def _covers(target: str, paths_set: frozenset[str]) -> bool:
        """是否覆盖了基线既有资源。

        导出包里脚本只有 ``x.gdc`` + ``x.gd.remap``，没有 ``x.gd``，
        所以不能只判断 ``target in paths_set``。
        """
        if target in paths_set:
            return True
        return target.endswith((".gd", ".tscn", ".tres", ".res")) and f"{target}.remap" in paths_set

    def _show_detail(self, visible: bool) -> None:
        for widget in (
            self.placeholder,
            self.summary_card,
            self.files_title,
            self.files_table,
            self.hooks_title,
            self.hooks_table,
        ):
            widget.setVisible(not visible if widget is self.placeholder else visible)

    def _make_table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)
        table.setMinimumHeight(96)
        return table
