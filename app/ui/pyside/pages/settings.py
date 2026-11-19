"""设置页：路径、存档配置、外观（自定义背景）、日志级别。写回 config/launcher.json。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ....core import config as cfg_mod
from ....core import paths
from ....core import runtime_lock
from ....core import uninstall as uninstall_mod
from ....core import userdir
from .. import features
from ..widgets import Card, PageHeader, section_title

#: 垂直同步档位（Godot 4 的 vsync_mode 枚举）。
#:
#: 只暴露「关 / 开」两档：Mailbox 与自适应在 GL Compatibility 渲染器下
#: 实测与普通开启无明显差别，多出来的选项只会让玩家选错。
VSYNC_ITEMS = (
    (0, "关闭（推荐）"),
    (1, "开启"),
)


def _display_path(value: str) -> str:
    """显示用：把包内相对路径展开成绝对路径。

    配置里允许存 ``runtime/host46/jjxf46.exe`` 这类相对路径（整个目录搬走后
    仍然有效），但输入框里显示相对路径会让人看不出指向哪，所以统一展开。
    """
    p = cfg_mod.resolve_path(value or "")
    return str(p) if p else ""


def _store_path(value: str) -> str:
    """保存用：落在包内的存相对路径，包外保持绝对。"""
    return cfg_mod.stored_path(value)



def _mean_luminance(img: QImage) -> int:
    """缩到 16x16 取平均亮度（0-255）。只用来判断「这张图是不是偏暗」。"""
    small = img.scaled(
        16, 16, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation
    )
    total = 0
    for y in range(small.height()):
        for x in range(small.width()):
            c = small.pixelColor(x, y)
            total += (c.red() * 299 + c.green() * 587 + c.blue() * 114) // 1000
    return total // max(1, small.width() * small.height())


class PathRow(QWidget):
    """一行路径输入 + 浏览按钮。"""

    def __init__(self, value: str = "", mode: str = "file", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.edit = QLineEdit(value)
        self.edit.setPlaceholderText("绝对路径")
        row.addWidget(self.edit, 1)
        btn = QPushButton("浏览…")
        btn.setProperty("variant", "ghost")
        btn.setFixedWidth(66)
        btn.clicked.connect(self._browse)
        row.addWidget(btn)

    def _browse(self) -> None:
        current = self.edit.text().strip()
        start = str(Path(current).parent) if current and Path(current).parent.exists() else str(Path.home())
        if self.mode == "dir":
            picked = QFileDialog.getExistingDirectory(self, "选择目录", start)
        else:
            picked, _ = QFileDialog.getOpenFileName(self, "选择文件", start)
        if picked:
            self.edit.setText(picked)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, text: str) -> None:
        self.edit.setText(text)


class SettingsPage(QWidget):
    def __init__(
        self,
        cfg: dict,
        log,
        on_saved=None,
        on_background_changed=None,
        on_data_changed=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._log = log
        self._on_saved = on_saved
        self._on_bg = on_background_changed
        # 卸载游戏本体会连带删掉该线的扩展与剧情包，删完要让库页/启动按钮重算
        self._on_data_changed = on_data_changed

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        root.addWidget(PageHeader("设置", "路径改动保存后立即生效，下次启动使用新值。"))

        card = Card()
        form = QFormLayout(card)
        form.setContentsMargins(20, 16, 20, 16)
        form.setSpacing(10)

        self.sources = PathRow(_display_path((cfg.get("sources") or {}).get("reversed_project", "")), "dir")

        # 引擎与基线的路径行**按配置动态生成**。
        #
        # 原先是两个写死的控件 ``engine46`` / ``base145``，只覆盖
        # 4.6 引擎 + 1.45 基线。双线之后 4.7 的路径在这个页面**完全不可见、
        # 不可改** —— 玩家换了本体位置只能去手改 launcher.json。
        #
        # 用字典存控件，key 是 "engine:4.6" / "baseline:jjxf.v4.7" 这种。
        # ``engine46`` / ``base145`` 保留为别名，避免外部引用（含测试）断掉。
        self._path_rows: dict[str, PathRow] = {}
        pf = form
        self._path_form = pf
        for _line in (cfg_mod.known_lines(cfg) or (cfg_mod.BUILTIN_LINE,)):
            _meta = (cfg.get("engines") or {}).get(_line) or {}
            row = PathRow(_display_path(_meta.get("path", "")))
            self._path_rows[f"engine:{_line}"] = row
            pf.addRow(self._label(f"{_line} 引擎"), row)

        for _bid, _meta in (cfg.get("baselines") or {}).items():
            if not str((_meta or {}).get("path", "")).strip() and not _meta.get("engine"):
                # 完全没登记的基线不占位置（避免列表里出现一堆空行）
                continue
            row = PathRow(_display_path((_meta or {}).get("path", "")))
            self._path_rows[f"baseline:{_bid}"] = row
            short = str(_bid).split(".v")[-1]
            pf.addRow(self._label(f"{short} 基线"), row)

        # 兼容别名：老代码/测试按这两个名字取控件
        self.engine46 = self._path_rows.get("engine:4.6") or PathRow(
            _display_path(cfg["engines"]["4.6"].get("path", ""))
        )
        self.base145 = self._path_rows.get("baseline:jjxf.v1.45") or PathRow(
            _display_path(cfg["baselines"]["jjxf.v1.45"].get("path", ""))
        )

        form.addRow(self._label("反解工程"), self.sources)
        # 反解工程是开发者工具（取 .gd 原文用），玩家版不露这一行。
        # 用 setRowVisible 而不是删控件：_reload/_save 都按控件取值，删了要到处判空。
        self._show_sources = features.show_source_project(cfg)
        if not self._show_sources:
            form.setRowVisible(self.sources, False)

        self.profile_combo = QComboBox()
        self.profile_combo.setEditable(True)
        self.profile_combo.addItems(["default"])
        self.profile_combo.setCurrentText(cfg["profiles"].get("default", "default"))
        self.profile_combo.currentTextChanged.connect(lambda _t: self._refresh_save_paths())
        form.addRow(self._label("默认存档"), self.profile_combo)

        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level.setCurrentText(str(cfg.get("log_level", "INFO")).upper())
        form.addRow(self._label("日志级别"), self.log_level)

        # 勾选框的文本**不能自动换行**（Qt 不支持），所以它会把整页的
        # 最小宽度撑到整句长度。原句「把 APPDATA 重定向到 profiles/<名字>
        # （推荐，避免动真实存档）」实测 504 px，加上左侧标签与边距把
        # settings 页的 minimumSizeHint 顶到 678 px —— 940 宽窗口装不下，
        # 出现横向滚动条；低配机器（1366x768 + 150% 缩放，可用 911）更挤。
        #
        # 处理：勾选框只留短标题，括号里的理由移到下一行可换行的说明标签。
        # 信息一点没少，但最小宽度从 504 降到 180 左右。
        self.redirect = QCheckBox("把 APPDATA 重定向到 profiles/<名字>")
        self.redirect.setChecked(bool(cfg["profiles"].get("redirect_appdata", True)))
        form.addRow(self._label("存档隔离"), self.redirect)

        redirect_hint = QLabel("推荐开启：避免动到真实存档。关闭后会直接读写游戏的原目录。")
        redirect_hint.setObjectName("Faint")
        redirect_hint.setWordWrap(True)
        redirect_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        form.addRow("", redirect_hint)

        # 存档位置：把「原版在哪 / 启动器在哪」写清楚。
        # 玩家反馈的「角色存档丢失」有九成是这两个目录名不同造成的，
        # 只写 profiles/<名字> 会让人以为存档就在那，进去却只有 appdata。
        self.save_paths = QLabel("")
        self.save_paths.setObjectName("PathText")
        self.save_paths.setWordWrap(True)
        self.save_paths.setMinimumWidth(0)
        self.save_paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.save_paths.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        form.addRow(self._label("存档位置"), self.save_paths)
        self._refresh_save_paths()

        self.shard = QComboBox()
        for label, value in (("1.0 GiB", 1073741824), ("1.5 GiB", 1610612736), ("1.8 GiB（默认）", 1932735283)):
            self.shard.addItem(label, value)
        current_shard = int((cfg.get("packs") or {}).get("shard_limit_bytes", 1932735283))
        idx = self.shard.findData(current_shard)
        self.shard.setCurrentIndex(idx if idx >= 0 else self.shard.count() - 1)
        form.addRow(self._label("分片上限"), self.shard)

        root.addWidget(card)

        # ---------------- 外观
        root.addWidget(section_title("外观", "自定义背景图会压暗处理，保证文字可读"))
        look = Card()
        look_row = QHBoxLayout(look)
        look_row.setContentsMargins(20, 14, 20, 14)
        look_row.setSpacing(12)

        self.bg_preview = QLabel()
        self.bg_preview.setFixedSize(132, 56)
        self.bg_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bg_preview.setStyleSheet(
            "background:#14161a; border:1px solid #2b3037; border-radius:8px; color:#6b747e; font-size:11px;"
        )
        look_row.addWidget(self.bg_preview)

        bg_col = QVBoxLayout()
        bg_col.setSpacing(6)
        bg_btns = QHBoxLayout()
        bg_btns.setSpacing(8)
        self.bg_pick = QPushButton("选择背景图…")
        self.bg_pick.clicked.connect(self._pick_background)
        self.bg_clear = QPushButton("清除背景")
        self.bg_clear.setProperty("variant", "ghost")
        self.bg_clear.clicked.connect(self._clear_background)
        bg_btns.addWidget(self.bg_pick)
        bg_btns.addWidget(self.bg_clear)
        bg_btns.addStretch(1)
        bg_col.addLayout(bg_btns)

        dim_row = QHBoxLayout()
        dim_row.setSpacing(8)
        dim_label = QLabel("压暗")
        dim_label.setObjectName("Muted")
        self.dim_slider = QSlider(Qt.Orientation.Horizontal)
        self.dim_slider.setRange(0, 90)
        self.dim_slider.setValue(int((cfg.get("ui") or {}).get("background_dim", 40)))
        self.dim_slider.setFixedWidth(180)
        self.dim_value = QLabel(f"{self.dim_slider.value()}%")
        self.dim_value.setObjectName("Faint")
        self.dim_slider.valueChanged.connect(self._on_dim_changed)
        dim_row.addWidget(dim_label)
        dim_row.addWidget(self.dim_slider)
        dim_row.addWidget(self.dim_value)
        dim_row.addStretch(1)
        bg_col.addLayout(dim_row)

        self.bg_hint = QLabel("纯色背景")
        self.bg_hint.setObjectName("Faint")
        bg_col.addWidget(self.bg_hint)

        look_row.addLayout(bg_col, 1)
        root.addWidget(look)

        # ----------------------------------------------------------
        # 展示模式：简洁 / 详细
        #
        # 这一项解决的是「同一份界面要给两类人用」的矛盾：
        #   · 玩家想知道的是「这两个版本能不能玩」→ 简洁
        #   · 开发者/报问题时要知道「索引在不在、指纹对不对」→ 详细
        #
        # 做成设置项而不是按版本写死，是因为**开发者版也要能当玩家用**
        # （自测玩家视角），玩家版偶尔也要看细节（截图报问题）。
        # 默认简洁：绝大多数时间是在玩，需要细节的是少数时刻。
        # ----------------------------------------------------------
        mode_card = Card()
        mode_col = QVBoxLayout(mode_card)
        mode_col.setContentsMargins(20, 14, 20, 14)
        mode_col.setSpacing(8)
        head = QLabel("展示模式")
        head.setStyleSheet("font-weight: 600;")
        mode_col.addWidget(head)

        self.detail_check = QCheckBox("详细展示（适用于开发 / 排查问题）")
        self.detail_check.setChecked(features.show_detail(cfg))
        # **勾选即生效**，不用等「保存设置」。
        #
        # 实测反馈：玩家勾上之后以为已经生效，切到诊断页发现没变 ——
        # 因为他还没点保存。展示模式是个「立刻想看效果」的开关，
        # 让它跟着保存按钮走只会制造困惑。
        #
        # 立即写配置 + 通知主窗口刷新（主窗口会把新 cfg 同步给各页）。
        self.detail_check.toggled.connect(self._on_detail_toggled)
        self.detail_check.setToolTip(
            "开启后，诊断页会展开全部技术检查项（引擎路径、基线文件、\n"
            "索引大小、代码层指纹、源码漂移等）。\n\n"
            "关闭时每个游戏版本只占一行，出问题的那一行会自动展开。\n"
            "玩游戏的日常使用建议关闭。"
        )
        mode_col.addWidget(self.detail_check)

        mode_hint = QLabel(
            "简洁：每个游戏版本一行。出了问题的那一行会自动展开。\n"
            "详细：始终列出全部技术检查项（引擎路径、基线文件、索引、代码指纹等）。"
        )
        mode_hint.setObjectName("Faint")
        mode_hint.setWordWrap(True)
        mode_col.addWidget(mode_hint)

        # ----------------------------------------------------------
        # 开发者模式（2026-11-08 单包化后新增）
        #
        # 单包发布之后不再按 edition 分「玩家版 / 开发者版」，所以
        # 「要不要看到反解工程这类工具」只能由设置项决定。
        #
        # 默认关闭 —— 玩家不需要反解工程路径、也不需要知道「源码层」
        # 是什么概念。需要的人（作者、排查问题）自己开。
        #
        # 与「详细展示」的区别：那个控制诊断页铺几行；这个控制**是否有
        # 开发者工具入口**（反解工程路径设置、扩展体检明细等）。
        # ----------------------------------------------------------
        self.dev_check = QCheckBox("开发者模式（显示反解工程等工具）")
        self.dev_check.setChecked(features.developer_only(cfg))
        self.dev_check.setToolTip(
            "开启后，「设置 → 路径」里会出现「反解工程」一行，\n"
            "扩展管理页会显示完整的隔离扫描明细。\n\n"
            "普通游玩不需要开启。"
        )
        self.dev_check.toggled.connect(self._on_dev_mode_toggled)
        mode_col.addWidget(self.dev_check)

        # ----------------------------------------------------------
        # 状态面板默认隐藏（机甲风外壳，2026-11-09）
        #
        # 默认开启：启动时停在 HUB 视图，机甲立绘完整；点底部任意导航
        # 或右下角状态胶囊才展开功能页。关掉则功能页常驻。
        #
        # 和上面两个开关一样「勾选即生效」：立即落盘 + 调 on_saved
        # 让主窗口重载配置并同步可见性。
        # ----------------------------------------------------------
        self.panel_check = QCheckBox("状态面板默认隐藏（保留机甲立绘）")
        raw = (cfg.get("ui") or {}).get("status_panel_hidden")
        self.panel_check.setChecked(True if raw is None else bool(raw))
        self.panel_check.setToolTip(
            "开启：启动后只显示 HUB（背景立绘 + 圆形启动按钮 + 底部导航），\n"
            "点任意导航按钮或右下角状态胶囊才展开功能页。\n\n"
            "关闭：功能页始终显示。两种状态功能都完整，只是观感不同。"
        )
        self.panel_check.toggled.connect(self._on_panel_toggled)
        mode_col.addWidget(self.panel_check)

        # ----------------------------------------------------------
        # 纯净模式（2026-11-09 用户要求）
        #
        # 勾选后底部导航只留「游戏库 / 扩展管理 / 小游戏 / 设置」，
        # 把「打包导出」「诊断」这两个作者向入口藏起来。
        # 默认开启 —— 普通玩家不该看到打包和诊断。
        # ----------------------------------------------------------
        self.pure_check = QCheckBox("纯净模式（只保留常用导航）")
        raw_pure = (cfg.get("ui") or {}).get("pure_mode")
        self.pure_check.setChecked(True if raw_pure is None else bool(raw_pure))
        self.pure_check.setToolTip(
            "开启：底部只显示「游戏库 / 扩展管理 / 小游戏 / 设置」。\n"
            "关闭：额外显示「打包导出 / 诊断」两个作者向入口。\n\n"
            "两页本身不会被删除，随时可以关掉纯净模式找回。"
        )
        self.pure_check.toggled.connect(self._on_pure_toggled)
        mode_col.addWidget(self.pure_check)

        # ----------------------------------------------------------
        # HUB 三个区域各自独立可隐藏（2026-11-09 用户圈出红框要求）
        #
        # 「不影响整体观感」是核心诉求：这三块都在立绘上，全隐藏后
        # HUB 只剩左侧圆形启动按钮。三者**互不影响**，都默认隐藏。
        #
        # 键名统一加 hub_ 前缀，和 status_panel_hidden 区分开：
        # 后者控制「HUB ⇄ 功能页」整体切换，这里控制 HUB 内部元素。
        # ----------------------------------------------------------
        self.hub_region_checks: dict[str, QCheckBox] = {}
        for key, label, tip in (
            (
                "hub_hide_stats",
                "HUB 隐藏「扩展与状态」面板",
                "那块带科技蓝边框的统计面板（已装扩展 / 冲突 / 剧情线）。\n"
                "它是 HUB 上面积最大的一块，隐藏后立绘露出最多。",
            ),
            (
                "hub_hide_config",
                "HUB 隐藏「当前配置」条",
                "游戏线 / 存档档位那一条细信息。\n"
                "隐藏后 HUB 右侧不再有任何文字。",
            ),
            (
                "hub_hide_statusbar",
                "HUB 隐藏底部扩展状态条",
                "导航条上方那条「扩展 空闲 · 待命 / 就绪」信息。\n"
                "隐藏后底部只剩橙色导航按钮。",
            ),
        ):
            chk = QCheckBox(label)
            raw_region = (cfg.get("ui") or {}).get(key)
            chk.setChecked(True if raw_region is None else bool(raw_region))
            chk.setToolTip(tip)
            chk.toggled.connect(
                lambda checked, k=key: self._on_hub_region_toggled(k, checked)
            )
            self.hub_region_checks[key] = chk
            mode_col.addWidget(chk)

        dev_hint = QLabel(
            "反解工程是给修改游戏脚本的人用的：启动器从那里读取 .gd 原文，"
            "用来给你的扩展打补丁。不填也能装扩展，只是没法改原版脚本。"
        )
        dev_hint.setObjectName("Faint")
        dev_hint.setWordWrap(True)
        mode_col.addWidget(dev_hint)

        root.addWidget(mode_card)

        # ----------------------------------------------------------
        # 启动优化（2026-11-11）
        #
        # 起因：玩家普遍反映 4.7 剧情版「内存占用太高」。PCL 百宝箱里有个
        # 「内存优化」按钮，原理是遍历全系统进程调 EmptyWorkingSet，把各
        # 进程工作集里的冷页换出到页面文件 —— 物理占用能降约 1/3，但任何
        # 进程的提交内存一个字节都没少。
        #
        # 这里做同样的事，但**只针对别的进程**，绝不碰游戏：4.7 的 7GB 是
        # 每帧都在读的热数据，腾走等于每帧硬缺页，只会更卡。所以它的定位
        # 是「让游戏启动时有更多空闲物理页」，不是「让游戏的占用变小」。
        #
        # 默认关闭，三条理由都写在 config.py 的注释里：收益波动大、
        # 机械硬盘上会顿一下、承诺不了具体数字。
        # ----------------------------------------------------------
        trim_card = Card()
        trim_col = QVBoxLayout(trim_card)
        trim_col.setContentsMargins(20, 14, 20, 14)
        trim_col.setSpacing(8)

        trim_head = QLabel("启动优化")
        trim_head.setStyleSheet("font-weight: 600;")
        trim_col.addWidget(trim_head)

        self.trim_check = QCheckBox("启动前释放系统内存（把其它程序的冷页换出到页面文件）")
        self.trim_check.setChecked(
            bool((cfg.get("runtime") or {}).get("trim_working_set"))
        )
        self.trim_check.setToolTip(
            "原理和 PCL 的「百宝箱 → 内存优化」一样：对其它进程调用\n"
            "EmptyWorkingSet，把它们工作集里「暂时不用的页」换出物理内存。\n\n"
            "注意：这**不会**让游戏的占用变小，只是给游戏多腾出一些空闲\n"
            "物理内存。代价是被换出的页下次访问要重新读回来 —— 机械硬盘上\n"
            "会觉得「切回浏览器卡一下」，固态硬盘几乎无感。\n\n"
            "默认关闭。游戏进程永远不会被处理。"
        )
        self.trim_check.toggled.connect(self._on_trim_toggled)
        trim_col.addWidget(self.trim_check)

        trim_hint = QLabel(
            "只处理其它程序，不碰游戏本体。实测同机可腾出 0.3~1.1 GB，"
            "具体取决于当时后台开了多少东西。"
        )
        trim_hint.setObjectName("Faint")
        trim_hint.setWordWrap(True)
        trim_col.addWidget(trim_hint)

        root.addWidget(trim_card)

        # ----------------------------------------------------------
        # 画面与帧率（2026-11-14）
        #
        # 起因：底座原版没有限帧也没有 vsync，高刷屏上引擎会跑满刷新率。
        # 这游戏是 2D、动画只有 60 帧，超出部分全是空转，白吃内存带宽与
        # 页面文件配额 —— 8 GB 机器上这就是「选完角色进图卡住几十秒」的
        # 放大器之一（另一个是贴图缓存上限，已降到 64 MiB）。
        #
        # 为什么做成可调而不是写死 60：
        # - 高配玩家想要更高帧减少输入延迟，放开到 90/120 或「不限制」；
        # - 更弱的机器（集显 / 单通道内存）需要 30 来换稳定性。
        # 默认 60 是「改了也看不出差别」与「弱机能跑」之间的平衡点。
        # ----------------------------------------------------------
        fps_card = Card()
        fps_col = QVBoxLayout(fps_card)
        fps_col.setContentsMargins(20, 14, 20, 14)
        fps_col.setSpacing(8)

        fps_head = QLabel("画面与帧率")
        fps_head.setStyleSheet("font-weight: 600;")
        fps_col.addWidget(fps_head)

        fps_form = QFormLayout()
        fps_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        fps_form.setContentsMargins(0, 0, 0, 0)
        fps_form.setHorizontalSpacing(12)
        fps_form.setVerticalSpacing(8)

        self.fps_combo = QComboBox()
        for value in cfg_mod.FPS_CHOICES:
            self.fps_combo.addItem(cfg_mod.fps_label(value), value)
        current_fps = cfg_mod.fps_limit(cfg)
        # 配置里存了列表外的合法值（例如手改的 75）时补进下拉，否则
        # setCurrentIndex 找不到会静默落回第一项 —— 玩家会看到
        # 「我设的 75 变成了不限制」。
        if self.fps_combo.findData(current_fps) < 0:
            self.fps_combo.addItem(cfg_mod.fps_label(current_fps), current_fps)
        self.fps_combo.setCurrentIndex(self.fps_combo.findData(current_fps))
        self.fps_combo.setToolTip(
            "限制游戏每秒最多渲染多少帧。\n\n"
            "默认 60 帧：与游戏动画帧率一致，再高只是空转。\n"
            "机器较弱（集成显卡 / 内存不足 8 GB）时选 30 更稳；\n"
            "高刷显示器可以选 90 / 120，或「不限制」。\n\n"
            "改完下次启动生效。"
        )
        self.fps_combo.currentIndexChanged.connect(self._on_fps_changed)
        fps_form.addRow("帧率上限", self.fps_combo)

        self.vsync_combo = QComboBox()
        for value, text in VSYNC_ITEMS:
            self.vsync_combo.addItem(text, value)
        current_vsync = cfg_mod.vsync_mode(cfg)
        self.vsync_combo.setCurrentIndex(max(0, self.vsync_combo.findData(current_vsync)))
        self.vsync_combo.setToolTip(
            "垂直同步。默认关闭 —— 限帧已经能压住画面撕裂，\n"
            "再开 vsync 会额外占一帧缓冲。\n\n"
            "如果画面仍有明显撕裂，可以试「开启」。"
        )
        self.vsync_combo.currentIndexChanged.connect(self._on_vsync_changed)
        fps_form.addRow("垂直同步", self.vsync_combo)

        fps_col.addLayout(fps_form)

        fps_hint = QLabel(
            "改小可以明显降低发热与内存带宽占用；改大只在机器有余力时有意义。"
        )
        fps_hint.setObjectName("Faint")
        fps_hint.setWordWrap(True)
        fps_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        fps_col.addWidget(fps_hint)

        root.addWidget(fps_card)

        # ---------------- 自定义基线（改版创作者入口）
        #
        # 之前导入只走 CLI，界面上完全没有入口 —— 开发者在设置里看不到
        # 「我该把反解源码放哪、怎么验」。这里给一个卡片：状态 + 打开
        # 共用导入对话框 + 直接打开准则文档。
        #
        # **只在「开发者模式」开启时显示**（和「反解工程」路径行同一个开关管）。
        # 普通玩家看不到这个入口 —— 它面向改版创作者，需要准备 baseline.json、
        # 明文脚本与索引才能用，对玩家没有可执行价值。隐藏用 setVisible 而非
        # 删控件：_refresh_baseline_state 还要读注册表刷新状态数，删了要到处判空。
        self.baseline_card = Card()
        bc = QVBoxLayout(self.baseline_card)
        bc.setContentsMargins(20, 14, 20, 14)
        bc.setSpacing(8)

        baseline_head = QLabel("自定义基线（改版创作者）")
        baseline_head.setStyleSheet("font-weight: 600;")
        bc.addWidget(baseline_head)

        baseline_hint = QLabel(
            "需要让改版支持方法钩子类扩展时，由改版创作者准备 baseline.json、"
            "sources/ 明文脚本与 index.json，再在这里校验并导入。"
            "纯资源 / 数据改版无需导入；启动器不反解、不逆向游戏本体。"
        )
        baseline_hint.setObjectName("Faint")
        baseline_hint.setWordWrap(True)
        bc.addWidget(baseline_hint)

        self.baseline_state = QLabel("")
        self.baseline_state.setObjectName("Faint")
        self.baseline_state.setWordWrap(True)
        bc.addWidget(self.baseline_state)

        baseline_ops = QHBoxLayout()
        baseline_ops.setSpacing(8)
        open_btn = QPushButton("校验 / 导入自定义基线…")
        open_btn.setProperty("variant", "ghost")
        open_btn.clicked.connect(self._open_baseline_dialog)
        baseline_ops.addWidget(open_btn)
        doc_btn = QPushButton("查看导入准则")
        doc_btn.setProperty("variant", "ghost")
        doc_btn.clicked.connect(self._open_baseline_doc)
        baseline_ops.addWidget(doc_btn)
        baseline_ops.addStretch(1)
        bc.addLayout(baseline_ops)
        root.addWidget(self.baseline_card)
        self._refresh_baseline_state()
        # 自定义基线是开发者工具：仅在「开发者模式」开启时显示。
        self.baseline_card.setVisible(features.developer_only(self.cfg))

        actions = QHBoxLayout()
        actions.setSpacing(8)
        save = QPushButton("保存设置")
        save.setProperty("variant", "primary")
        save.clicked.connect(self._save)
        reload_btn = QPushButton("放弃修改")
        reload_btn.setProperty("variant", "ghost")
        reload_btn.clicked.connect(self._reload)
        actions.addWidget(save)
        actions.addWidget(reload_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        # ---------------- 卸载（危险操作，单独一区，避免误触）
        root.addWidget(
            section_title(
                "卸载启动器",
                "只卸载本启动器本身；游戏本体不在这里，卸载不会动它",
            )
        )
        danger = Card()
        dv = QVBoxLayout(danger)
        dv.setContentsMargins(18, 14, 18, 14)
        dv.setSpacing(8)

        tip = QLabel(
            "卸载会删除启动器程序与配置。扩展包与游戏存档属于你自己的数据，"
            "默认保留——勾选后才会一并删除，且不可恢复。"
        )
        tip.setObjectName("Faint")
        tip.setWordWrap(True)
        dv.addWidget(tip)

        self.uninstall_boxes: dict[str, QCheckBox] = {}
        # 5 个复选框每行 3 个。
        #
        # 但**光分网格不够**：QGridLayout 同一行各列的最小宽度仍然相加，
        # 而 QCheckBox 的 minimumSizeHint 无法压缩（设 Ignored 策略也没用，
        # 实测仍是整句宽度）。三项合计 228+204+168=600，加上卡片左右各 18
        # 与两处 14 的间距就顶到 660 以上 —— 630 的内容区放不下，整页仍会
        # 出横向滚动条（2026-11-14 实测 settings 页 678 px）。
        #
        # 所以勾选框只显示短名，**带体积**；全称与说明进 tooltip。
        # 信息没有减少，但三项合计从 600 降到约 500。
        short = {
            "program": "程序文件",
            "config": "配置",
            "residue": "缓存残留",
            "packs": "扩展包",
            "profiles": "游戏存档",
        }
        row = QGridLayout()
        row.setHorizontalSpacing(14)
        row.setVerticalSpacing(6)
        col = 0
        line = 0
        for stat in uninstall_mod.scan():
            name = short.get(stat.key, stat.label)
            box = QCheckBox(f"{name}（{uninstall_mod.human_size(stat.bytes)}）")
            # 全称放 tooltip，勾选框本身只留短名
            box_tip = f"{stat.label}：{uninstall_mod.human_size(stat.bytes)}"
            box.setChecked(stat.key in uninstall_mod.DEFAULT_SCOPES)
            if stat.key == "program":
                # 程序文件是卸载的本体，不允许取消
                box.setEnabled(False)
                box.setToolTip(f"{box_tip}\n程序文件是卸载的主体，必须删除")
            elif stat.key in ("packs", "profiles"):
                box.setToolTip(f"{box_tip}\n这是你自己的数据，删除后不可恢复；默认不删")
            else:
                box.setToolTip(box_tip)
            if not stat.exists:
                box.setText(f"{name}（无）")
                box.setEnabled(False)
            row.addWidget(box, line, col)
            self.uninstall_boxes[stat.key] = box
            col += 1
            if col >= 2:
                col = 0
                line += 1
        dv.addLayout(row)

        self.uninstall_hint = QLabel("")
        self.uninstall_hint.setObjectName("Faint")
        self.uninstall_hint.setWordWrap(True)
        dv.addWidget(self.uninstall_hint)
        self._update_uninstall_hint()

        un_row = QHBoxLayout()
        un_row.setSpacing(8)
        self.uninstall_btn = QPushButton("卸载启动器…")
        self.uninstall_btn.setProperty("variant", "danger")
        self.uninstall_btn.clicked.connect(self._uninstall)
        un_row.addWidget(self.uninstall_btn)
        un_row.addStretch(1)
        dv.addLayout(un_row)
        root.addWidget(danger)

        # ---------------- 卸载游戏本体（不随启动器分发）
        root.addWidget(
            section_title(
                "卸载游戏本体",
                "本体不随启动器分发；这里只删你登记过的那几个游戏文件",
            )
        )
        body_card = Card()
        bv = QVBoxLayout(body_card)
        bv.setContentsMargins(18, 14, 18, 14)
        bv.setSpacing(8)

        body_tip = QLabel(
            "启动器只记录游戏本体的路径（不复制、不改动它）。下面列的是你本机真实存在的"
            "游戏文件，删除后要重新下载/解压游戏才能再玩；扩展包与角色存档不在删除范围内。"
        )
        body_tip.setObjectName("Faint")
        body_tip.setWordWrap(True)
        bv.addWidget(body_tip)

        self.body_boxes: dict[str, QCheckBox] = {}
        body_grid = QGridLayout()
        body_grid.setHorizontalSpacing(14)
        body_grid.setVerticalSpacing(6)
        # 短名 + 体积；全称与路径进 tooltip。
        #
        # 同卸载范围那一处：QCheckBox 的最小宽度压不下去，两项
        # 「游戏本体 · 1.45 原版（3.1 GB）」各 288 px，合计 576 加间距
        # 就超过 630 的内容区。改为只显示「本体 1.45（3.1 GB）」约 200 px。
        body_short = {
            "jjxf.v1.45": "本体 1.45",
            "jjxf.v4.7": "本体 4.7",
        }
        for index, stat in enumerate(uninstall_mod.scan_game_body(self.cfg)):
            name = body_short.get(stat.key, stat.label)
            box = QCheckBox(f"{name}（{uninstall_mod.human_size(stat.bytes)}）")
            # 默认一定不勾：这是几个 GB、且不可恢复的删除
            box.setChecked(False)
            if not stat.exists:
                box.setText(f"{name}（未登记或不存在）")
                box.setEnabled(False)
                box.setToolTip(f"{stat.label}\n文件不存在或没有登记")
            else:
                box.setToolTip(
                    f"{stat.label}：{uninstall_mod.human_size(stat.bytes)}\n"
                    + "\n".join(p.as_posix() for p in stat.files())
                )
            body_grid.addWidget(box, index // 2, index % 2)
            self.body_boxes[stat.key] = box
        bv.addLayout(body_grid)

        self.body_hint = QLabel("")
        self.body_hint.setObjectName("Faint")
        self.body_hint.setWordWrap(True)
        bv.addWidget(self.body_hint)

        body_row = QHBoxLayout()
        body_row.setSpacing(8)
        self.body_btn = QPushButton("卸载所选游戏本体…")
        self.body_btn.setProperty("variant", "danger")
        self.body_btn.clicked.connect(self._uninstall_game_body)
        body_row.addWidget(self.body_btn)
        body_row.addStretch(1)
        bv.addLayout(body_row)
        root.addWidget(body_card)
        root.addStretch(1)

        for box in self.uninstall_boxes.values():
            box.toggled.connect(lambda _c: self._update_uninstall_hint())
        for box in self.body_boxes.values():
            box.toggled.connect(lambda _c: self._update_body_hint())
        self._update_body_hint()

        self._refresh_preview()

    # ------------------------------------------------------------ 背景

    def _refresh_preview(self) -> None:
        path = str((self.cfg.get("ui") or {}).get("background_image") or "")
        dim = int((self.cfg.get("ui") or {}).get("background_dim", 55))
        if not path or not Path(path).exists():
            self.bg_preview.setPixmap(QPixmap())
            self.bg_preview.setText("纯色背景")
            self.bg_hint.setText("未设置背景图（纯色）")
            return
        pix = QPixmap(path)
        if pix.isNull():
            self.bg_preview.setText("读取失败")
            self.bg_hint.setText(f"图片无法解码：{Path(path).name}")
            return
        self.bg_preview.setText("")
        # 预览按实际压暗值画，避免「设置里很亮、进界面很暗」的错觉
        scaled = pix.scaled(
            self.bg_preview.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        canvas = QPixmap(scaled.size())
        canvas.fill(QColor("#14161a"))
        painter = QPainter(canvas)
        painter.drawPixmap(0, 0, scaled)
        if dim > 0:
            painter.fillRect(canvas.rect(), QColor(0, 0, 0, int(255 * dim / 100)))
        painter.end()
        self.bg_preview.setPixmap(canvas)
        self.bg_hint.setText(f"已应用：{Path(path).name}　·　压暗 {dim}%（数值越大背景越暗）")

    def _pick_background(self) -> None:
        start = str(Path.home())
        picked, _ = QFileDialog.getOpenFileName(
            self, "选择背景图片", start, "图片 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if picked:
            self.set_background(picked)

    def set_background(self, picked: str) -> bool:
        """设置背景并立即生效（对话框与回归脚本共用这一个入口）。"""
        probe = QImage(picked)
        if probe.isNull():
            self._log(f"这张图无法读取（支持 png/jpg/webp/bmp）：{picked}", "err")
            return False
        self.cfg.setdefault("ui", {})["background_image"] = picked

        # 暗图在深色界面上叠 45% 压暗几乎看不见，这里按平均亮度自动下调
        note = ""
        lum = _mean_luminance(probe)
        dim = int((self.cfg["ui"] or {}).get("background_dim", 55))
        if lum < 70 and dim > 15:
            dim = 15
            self.cfg["ui"]["background_dim"] = dim
            self.dim_slider.blockSignals(True)
            self.dim_slider.setValue(dim)
            self.dim_slider.blockSignals(False)
            self.dim_value.setText(f"{dim}%")
            note = f"；图片偏暗（平均亮度 {lum}），压暗已下调到 {dim}%"

        cfg_mod.save(self.cfg)
        self._refresh_preview()
        if self._on_bg:
            self._on_bg()
        self._log(f"已设置背景图：{Path(picked).name}{note}", "ok")
        return True

    def _clear_background(self) -> None:
        self.cfg.setdefault("ui", {})["background_image"] = ""
        cfg_mod.save(self.cfg)
        self._refresh_preview()
        if self._on_bg:
            self._on_bg()
        self._log("已恢复纯色背景。", "dim")

    def _on_dim_changed(self, value: int) -> None:
        self.dim_value.setText(f"{value}%")
        self.cfg.setdefault("ui", {})["background_dim"] = int(value)
        cfg_mod.save(self.cfg)
        if self._on_bg:
            self._on_bg()

    # ------------------------------------------------------------ 其他

    def _label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Muted")
        label.setFixedWidth(96)
        return label

    def _refresh_save_paths(self) -> None:
        """把两处存档路径写出来：原版 + 启动器（每条线各一份）。

        这里不判断「路径存不存在」——玩家可能刚装启动器还没跑过游戏，
        写「不存在」只会增加困惑；路径本身才是他需要的信息。

        **按线遍历**：原实现只列 4.6，4.7 玩家在设置页看不到自己的
        存档落在哪，想手动备份也无从下手。
        """
        if not hasattr(self, "save_paths"):
            return
        profile = self.profile_combo.currentText().strip() or "default"
        lines = [f"原版（直接开游戏时）：{userdir.original_dir().as_posix()}"]
        for line in (cfg_mod.known_lines(self.cfg) or (cfg_mod.BUILTIN_LINE,)):
            path = userdir.launcher_dir(profile, line, self.cfg)
            note = "" if userdir.redirect_enabled(self.cfg) else "　（未启用存档隔离）"
            lines.append(f"启动器 {line} 线：{path.as_posix()}{note}")
        self.save_paths.setText("\n".join(lines))

    def _on_detail_toggled(self, checked: bool) -> None:
        """展示模式开关：立即写配置并刷新界面。

        为什么不等「保存设置」：这是个开关型的显示选项，用户勾上就想
        立刻看到效果。跟着保存按钮走会产生「我勾了怎么没变」的困惑
        （实测遇到过）。

        只写这一个键，不碰其它未保存的编辑 —— 玩家可能在同一个页面里
        改了路径但还没保存，不能因为勾了一下展示模式就把那些草稿一起落盘。
        """
        self.cfg.setdefault("ui", {})["show_detail"] = bool(checked)
        try:
            disk = cfg_mod.load()
        except Exception:  # noqa: BLE001
            disk = None
        if isinstance(disk, dict):
            # 用磁盘上的最新内容为底，只覆盖 show_detail —— 避免把
            # self.cfg 里其它未保存的改动带进去。
            disk.setdefault("ui", {})["show_detail"] = bool(checked)
            try:
                cfg_mod.save(disk)
            except OSError as exc:
                self._log(f"保存展示模式失败：{exc}", "warn")
                return
        self._log(
            "已切换到详细展示" if checked else "已切换到简洁展示", "dim"
        )
        if self._on_saved:
            self._on_saved()

    def _on_dev_mode_toggled(self, checked: bool) -> None:
        """开发者模式开关：立即写配置并刷新界面。

        与 :meth:`_on_detail_toggled` 同一个理由（勾上就想看效果），
        只是键不同：``ui.developer_mode``。

        打开后「设置 → 路径」会多出「反解工程」一行，扩展管理页也会
        显示完整的隔离扫描明细 —— 那些东西对玩家没有可执行价值。
        """
        self.cfg.setdefault("ui", {})["developer_mode"] = bool(checked)
        try:
            disk = cfg_mod.load()
        except Exception:  # noqa: BLE001
            disk = None
        if isinstance(disk, dict):
            # 以磁盘内容为底，只覆盖这一个键 —— 不把本页其它未保存的
            # 草稿（比如改了路径还没保存）一起落盘。
            disk.setdefault("ui", {})["developer_mode"] = bool(checked)
            try:
                cfg_mod.save(disk)
            except OSError as exc:
                self._log(f"保存开发者模式失败：{exc}", "warn")
                return
        self._log("已开启开发者模式" if checked else "已关闭开发者模式", "dim")
        # 同步「自定义基线」卡片可见性（与反解工程行同一开关）。
        if hasattr(self, "baseline_card"):
            self.baseline_card.setVisible(bool(checked))
        if self._on_saved:
            self._on_saved()

    def _on_pure_toggled(self, checked: bool) -> None:
        """纯净模式：立即写 ``ui.pure_mode`` 并刷新导航可见性。

        落盘范式与 ``_on_panel_toggled`` 一致：以磁盘内容为底只覆盖这一个键。
        """
        self.cfg.setdefault("ui", {})["pure_mode"] = bool(checked)
        try:
            disk = cfg_mod.load()
        except Exception:  # noqa: BLE001
            disk = None
        if isinstance(disk, dict):
            disk.setdefault("ui", {})["pure_mode"] = bool(checked)
            try:
                cfg_mod.save(disk)
            except OSError as exc:
                self._log(f"保存纯净模式偏好失败：{exc}", "warn")
                return
        self._log(
            "已开启纯净模式（只留常用导航）"
            if checked else "已关闭纯净模式（显示全部导航）",
            "dim",
        )
        if self._on_saved:
            self._on_saved()

    def _on_panel_toggled(self, checked: bool) -> None:
        """状态面板开关：立即写 ``ui.status_panel_hidden`` 并刷新界面。

        落盘范式与 ``_on_dev_mode_toggled`` 完全一致：以磁盘内容为底，
        只覆盖这一个键，不把本页其它未保存草稿一起写回去。
        然后走 ``on_saved`` → ``MainWindow._on_cfg_saved`` 重载配置，
        主窗口会按新值同步右侧功能页的可见性。
        """
        self.cfg.setdefault("ui", {})["status_panel_hidden"] = bool(checked)
        try:
            disk = cfg_mod.load()
        except Exception:  # noqa: BLE001
            disk = None
        if isinstance(disk, dict):
            disk.setdefault("ui", {})["status_panel_hidden"] = bool(checked)
            try:
                cfg_mod.save(disk)
            except OSError as exc:
                self._log(f"保存状态面板偏好失败：{exc}", "warn")
                return
        self._log(
            "已设为 HUB 视图（默认隐藏功能页）"
            if checked else "已设为常驻显示功能页",
            "dim",
        )
        if self._on_saved:
            self._on_saved()

    def _on_hub_region_toggled(self, key: str, checked: bool) -> None:
        """HUB 某个区域开关：立即写 ``ui.<key>`` 并刷新。

        与 ``_on_panel_toggled`` 同一范式：以磁盘内容为底只覆盖这一个键，
        不把本页其它未保存草稿写回去。
        """
        self.cfg.setdefault("ui", {})[key] = bool(checked)
        try:
            disk = cfg_mod.load()
        except Exception:  # noqa: BLE001
            disk = None
        if isinstance(disk, dict):
            disk.setdefault("ui", {})[key] = bool(checked)
            try:
                cfg_mod.save(disk)
            except OSError as exc:
                self._log(f"保存 HUB 区段偏好失败：{exc}", "warn")
                return
        label = {
            "hub_hide_stats": "扩展与状态面板",
            "hub_hide_config": "当前配置条",
            "hub_hide_statusbar": "底部扩展状态条",
        }.get(key, key)
        self._log(
            f"HUB 已隐藏{label}" if checked else f"HUB 已显示{label}", "dim"
        )
        if self._on_saved:
            self._on_saved()

    def _refresh_baseline_state(self) -> None:
        """统计「已导入且仍有效」的自定义基线数。"""
        from ....core import custom_baseline as cb
        from ....core import custom_lines as cl
        from ....core.fsutil import read_json

        reg = read_json(cb.registry_path(), default={}) or {}
        ready = 0
        for line_id in reg:
            line = cl.get_line(self.cfg, str(line_id))
            if line is not None and cb.status(line.id, line.identity_hash).get("ready"):
                ready += 1
        self.baseline_state.setText(
            f"当前已导入且校验有效的基线：{ready} 条"
            + ("（源码或本体变动会自动失效，需重新导入）" if ready else "")
        )

    def _open_baseline_dialog(self) -> None:
        from .baseline_import_dialog import BaselineImportDialog

        dlg = BaselineImportDialog(self.cfg, parent=self)
        dlg.exec()
        self._refresh_baseline_state()

    def _open_baseline_doc(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from .baseline_import_dialog import DOC_REL

        p = paths.project_root() / DOC_REL
        if not p.is_file():
            QMessageBox.information(
                self, "找不到文档", f"准则文档不在发布包里：{p}"
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _on_trim_toggled(self, checked: bool) -> None:
        """启动优化开关：立即写 ``runtime.trim_working_set`` 并刷新。

        与 ``_on_panel_toggled`` 同一范式：以磁盘内容为底只覆盖这一个键，
        不把本页其它未保存草稿一起写回去。

        勾上时顺手跑一次 dry_run（只统计、不执行），把「能腾多少」当场
        告诉玩家 —— 否则他勾完不知道有没有用，只能等下次启动看日志。
        """
        self.cfg.setdefault("runtime", {})["trim_working_set"] = bool(checked)
        try:
            disk = cfg_mod.load()
        except Exception:  # noqa: BLE001
            disk = None
        if isinstance(disk, dict):
            disk.setdefault("runtime", {})["trim_working_set"] = bool(checked)
            try:
                cfg_mod.save(disk)
            except OSError as exc:
                self._log(f"保存启动优化设置失败：{exc}", "warn")
                return

        if not checked:
            self._log("已关闭启动前释放系统内存", "dim")
            if self._on_saved:
                self._on_saved()
            return

        # 勾上时给个预估。dry_run 只读工作集、不调用 EmptyWorkingSet，
        # 对正在跑的程序零影响，所以可以直接在 UI 线程里跑（约 0.2 秒）。
        try:
            from ....core import memtrim

            report = memtrim.trim_working_sets(dry_run=True)
            if report.ok and report.processes:
                # 上限而不是承诺：EmptyWorkingSet 只腾「冷页」，一个程序的
                # 工作集不会全被换出（实测约 1/3~1/2）。写死一个预期数字
                # 只会让玩家下次发现「没那么多」时觉得被骗。
                self._log(
                    f"已开启；当前最多可腾出约 {memtrim.human(report.estimated)}"
                    f"（{report.processes} 个程序，实际通常是其中一半上下）",
                    "dim",
                )
            else:
                self._log("已开启；当前没有值得腾出的后台程序", "dim")
        except Exception as exc:  # noqa: BLE001 - 预估失败不影响开关
            self._log(f"已开启（预估失败：{exc}）", "dim")

        if self._on_saved:
            self._on_saved()

    def _reload(self) -> None:
        cfg = cfg_mod.load()
        self.cfg.clear()
        self.cfg.update(cfg)
        # 按字典回填所有路径行（含 4.7）。只刷存在的行，不在配置里的不碰。
        for key, row in getattr(self, "_path_rows", {}).items():
            kind, _, ident = key.partition(":")
            if kind == "engine":
                meta = (cfg.get("engines") or {}).get(ident) or {}
            else:
                meta = (cfg.get("baselines") or {}).get(ident) or {}
            row.set_value(_display_path(meta.get("path", "")))
        if getattr(self, "_show_sources", True):
            self.sources.set_value(
                _display_path((cfg.get("sources") or {}).get("reversed_project", ""))
            )
        self.profile_combo.setCurrentText(cfg["profiles"].get("default", "default"))
        self._refresh_save_paths()
        self.log_level.setCurrentText(str(cfg.get("log_level", "INFO")).upper())
        self.redirect.setChecked(bool(cfg["profiles"].get("redirect_appdata", True)))
        if hasattr(self, "trim_check"):
            self.trim_check.setChecked(
                bool((cfg.get("runtime") or {}).get("trim_working_set"))
            )
        if hasattr(self, "detail_check"):
            self.detail_check.setChecked(features.show_detail(cfg))
        if hasattr(self, "dev_check"):
            self.dev_check.setChecked(features.developer_only(cfg))
        self.dim_slider.setValue(int((cfg.get("ui") or {}).get("background_dim", 40)))
        self._refresh_preview()
        self._log("已重新载入配置。", "dim")

    # ------------------------------------------------------------ 画面与帧率

    def _on_fps_changed(self, _index: int) -> None:
        """帧率档位改了就立刻落盘 —— 这是玩家最可能「改完就去启动」的一项。

        不做「必须点保存才生效」：设置页的保存按钮在底部，玩家改完下拉
        常常直接切到游戏库点启动，结果改了个寂寞。``trim_check`` 也是
        即时生效的，两处行为保持一致。
        """
        value = self.fps_combo.currentData()
        if value is None:
            return
        value = int(value)
        self.cfg.setdefault("runtime", {})["max_fps"] = value
        self._persist_runtime()

    def _on_vsync_changed(self, _index: int) -> None:
        value = self.vsync_combo.currentData()
        if value is None:
            return
        self.cfg.setdefault("runtime", {})["vsync"] = int(value)
        self._persist_runtime()

    def _persist_runtime(self) -> None:
        """把 runtime 段写回磁盘，并同步内存里的 cfg。

        ``save`` 之后必须 ``_reload``：其它页面持有的是 ``cfg_mod.load()``
        读到的新对象，这里不刷会留下两份不一致的配置。
        """
        try:
            cfg_mod.save(self.cfg)
        except Exception as exc:  # noqa: BLE001 - 保存失败要提示，不该崩
            self._log(f"保存失败：{exc}", "bad")
            return
        fps = cfg_mod.fps_limit(self.cfg)
        vs = cfg_mod.vsync_mode(self.cfg)
        self._log(
            f"画面设置已保存：帧率上限 {cfg_mod.fps_label(fps)}、"
            f"垂直同步 {'开启' if vs else '关闭'}（下次启动生效）",
            "ok",
        )
        if self._on_saved:
            self._on_saved()

    def _save(self) -> None:
        # 按字典收集所有路径行。只写回**已存在**的条目 ——
        # 配置里没有这条线/基线时不该凭空创建（那会让界面出现幽灵条目）。
        for key, row in getattr(self, "_path_rows", {}).items():
            kind, _, ident = key.partition(":")
            if kind == "engine":
                slot = (self.cfg.get("engines") or {}).get(ident)
            else:
                slot = (self.cfg.get("baselines") or {}).get(ident)
            if isinstance(slot, dict):
                slot["path"] = _store_path(row.value())
        # 看不到这一行就别写它：玩家版保存设置不该把开发者留下的路径抹成空。
        if getattr(self, "_show_sources", True):
            self.cfg.setdefault("sources", {})["reversed_project"] = _store_path(self.sources.value())
        self.cfg["profiles"]["default"] = self.profile_combo.currentText().strip() or "default"
        self.cfg["profiles"]["redirect_appdata"] = self.redirect.isChecked()
        self.cfg["log_level"] = self.log_level.currentText()
        self.cfg.setdefault("packs", {})["shard_limit_bytes"] = int(self.shard.currentData())
        self.cfg.setdefault("ui", {})["background_dim"] = int(self.dim_slider.value())
        if hasattr(self, "detail_check"):
            self.cfg.setdefault("ui", {})["show_detail"] = bool(self.detail_check.isChecked())
        cfg_mod.save(self.cfg)
        self._refresh_save_paths()
        self._log(f"设置已保存：{cfg_mod.CONFIG_PATH}", "ok")
        if self._on_saved:
            self._on_saved()

    # ------------------------------------------------------------ 卸载

    def _selected_scopes(self) -> list[str]:
        """已勾选的范围。

        **不能用 ``isEnabled()`` 过滤**：``program`` 是「必删但禁用勾选框」，
        用 isEnabled 过滤会让它从列表里消失，提示里就会荒唐地写「保留：程序文件」。
        「有没有东西可删」用 ``exists`` 判断，与「能不能取消」分开。
        """
        return [key for key, box in self.uninstall_boxes.items() if box.isChecked()]

    def _update_uninstall_hint(self) -> None:
        """实时算「将要删除什么、保留什么」，让用户在点按钮前就知道后果。"""
        scopes = self._selected_scopes()
        stats = {s.key: s for s in uninstall_mod.scan()}
        will = [stats[k] for k in scopes if k in stats]
        keep = [s for k, s in stats.items() if k not in scopes and s.exists]
        total = sum(s.bytes for s in will)
        parts = [f"将删除：{'、'.join(s.label for s in will) or '（未选择）'}"
                 f"，共 {uninstall_mod.human_size(total)}"]
        if keep:
            parts.append("保留：" + "、".join(s.label for s in keep))
        if "profiles" in scopes:
            parts.append("⚠ 已勾选删除游戏存档，此操作不可恢复")
        self.uninstall_hint.setText("　·　".join(parts))

    def _uninstall(self) -> None:
        scopes = self._selected_scopes()
        if "program" not in scopes:
            scopes = ["program", *scopes]
        targets = uninstall_mod.plan(None, scopes)
        stats = {s.key: s for s in uninstall_mod.scan()}
        total = sum(stats[k].bytes for k in scopes if k in stats)
        labels = "、".join(uninstall_mod.SCOPES[k] for k in scopes)

        # 二次确认：把「删什么、留什么、不可恢复」逐条写清
        body = [f"将删除：{labels}", f"合计约 {uninstall_mod.human_size(total)}", ""]
        keep = [s.label for k, s in stats.items() if k not in scopes and s.exists]
        if keep:
            body.append("保留：" + "、".join(keep))
        if "profiles" in scopes:
            body.append("")
            body.append("⚠ 游戏存档将被删除，无法恢复。")
        body.extend(["", f"安装目录：{paths.ROOT}", "", "确认卸载？"])
        answer = QMessageBox.warning(
            self,
            "确认卸载启动器",
            "\n".join(body),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._log("已取消卸载", "dim")
            return

        # 先立即删能删的（配置/缓存/扩展/存档），再交给 bat 删程序本体
        #
        # 「程序本体」的判断要用 edition.exe_names()：改名之后若还在这里
        # 硬写旧名字，新 exe 会被当成普通文件直接删掉 —— 卸载脚本来不及
        # 接手，玩家下次双击就找不到程序了。别名一起认，是因为老版本
        # 升级上来的目录里可能还留着旧 exe。
        from .... import edition as _edition

        program_names = set(_edition.exe_names()) | {
            "_internal", "runtime", "engines", "baseline", "schemas",
        }
        soft = [t for t in targets if t.name not in program_names]
        ok_count, failures = uninstall_mod.remove_now(soft)
        self._log(f"已删除 {ok_count} 项（配置/缓存等），{len(failures)} 项失败", "info" if not failures else "warn")
        for item in failures:
            self._log(f"  未删除：{item}", "warn")

        hard = [t for t in targets if t not in soft]
        try:
            script = uninstall_mod.write_uninstall_script(None, hard)
            uninstall_mod.launch_uninstall_script(script)
        except Exception as exc:  # noqa: BLE001 - 自删除失败要如实说
            QMessageBox.critical(
                self,
                "卸载未完成",
                f"程序文件无法自动删除：{exc}\n\n"
                f"配置与缓存已清理。请手动删除目录：\n{paths.ROOT}",
            )
            return

        QMessageBox.information(
            self,
            "卸载中",
            "已开始卸载。\n\n"
            "启动器会自动关闭，随后删除程序文件（约 2 秒）。\n"
            "如果目录仍然存在，说明有文件被占用，请关闭后手动删除。",
        )
        self._log("已启动卸载流程，启动器即将退出", "ok")
        # 关闭主窗口：随后 bat 等 2 秒再删，保证句柄已释放
        window = self.window()
        if window is not None:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(300, window.close)

    # ------------------------------------------------------------ 卸载游戏本体

    def _selected_body_keys(self) -> list[str]:
        return [key for key, box in self.body_boxes.items() if box.isChecked()]

    def _update_body_hint(self) -> None:
        """点按钮前就把「删多大、删完什么后果」写清楚。"""
        keys = self._selected_body_keys()
        if not keys:
            self.body_hint.setText("未选择：不会删除任何游戏文件。")
            return
        stats = {s.key: s for s in uninstall_mod.scan_game_body(self.cfg)}
        total = sum(stats[key].bytes for key in keys if key in stats)
        cascade = []
        # 用 line_of_body(cfg) 现算，不读模块级快照 —— 配置运行期会变
        # （导入本体后会写 engine 字段），快照会过时。
        body_lines = uninstall_mod.line_of_body(self.cfg)
        for key in keys:
            line = body_lines.get(key)
            if not line:
                continue
            assets = uninstall_mod.line_assets(self.cfg, line)
            if assets.extensions or assets.stories:
                cascade.append(f"{line} 的 {assets.summary()}")
        text = (
            f"将删除 {len(keys)} 条版本线的本体，合计 {uninstall_mod.human_size(total)}；"
            "删除后需要重新下载游戏才能再玩。"
        )
        if cascade:
            text += "\n同时会卸载该线专属的：" + "；".join(cascade) + "。"
        self.body_hint.setText(text)

    def _uninstall_game_body(self) -> None:
        """删游戏本体：三道闸门（勾选 → 清单确认 → 输入确认词）。

        为什么这么重：这是几个 GB、不可恢复、且**在启动器安装目录之外**的删除。
        用户资产一旦删掉没法靠重装启动器恢复，宁可多点两下。

        1.4 起带级联：本体没了，挂在上面的扩展与剧情包只会变成一堆报错来源，
        所以一并卸掉——但顺序是「扩展 → 剧情 → 本体文件 → 工作目录」，
        先摘挂载再删底座，中途失败也不会留下「本体还在、扩展已删」之外的怪状态。
        """
        keys = self._selected_body_keys()
        if not keys:
            QMessageBox.information(self, "未选择", "请先勾选要卸载的版本线。")
            return
        if runtime_lock.is_game_running():
            QMessageBox.information(
                self, "游戏正在运行", "请先在「游戏库」里结束游戏，再卸载游戏本体。"
            )
            return

        stats = {s.key: s for s in uninstall_mod.scan_game_body(self.cfg)}
        targets = [(key, path) for key in keys for path in stats[key].files()]
        if not targets:
            QMessageBox.information(
                self, "没有可删除的文件", "勾选的版本线在本机找不到对应的游戏文件。"
            )
            return

        total = 0
        for _key, path in targets:
            try:
                total += path.stat().st_size
            except OSError:
                continue

        body_lines = uninstall_mod.line_of_body(self.cfg)
        assets = {
            key: uninstall_mod.line_assets(self.cfg, body_lines.get(key, ""))
            for key in keys
        }
        extra_bytes = sum(a.extra_bytes for a in assets.values())

        body = ["将永久删除以下游戏文件：", ""]
        for key, path in targets:
            body.append(f"· {stats[key].label}")
            body.append(f"  {path.as_posix()}")
        body.extend(["", f"本体合计 {uninstall_mod.human_size(total)}。", ""])

        for key in keys:
            item = assets[key]
            if not item.extensions and not item.stories:
                continue
            body.append(f"同时卸载 {item.line} 线的关联内容（本体删了它们也跑不起来）：")
            if item.extensions:
                names = "、".join(a.label() for a in item.extensions)
                body.append(f"· {len(item.extensions)} 个扩展：{names}")
                body.append("  注册表记录与覆盖层文件一并删除，不保留副本。")
            if item.stories:
                names = "、".join(a.label() for a in item.stories)
                body.append(f"· {len(item.stories)} 个剧情包：{names}")
            body.append("")

        purge_note = (
            "并清理该线的插桩缓存与安装中间目录（可重建，不含你打出来的发布包）。"
            if any(a.stale_workdirs for a in assets.values())
            else ""
        )
        body.extend(
            [
                purge_note,
                "",
                f"合计释放约 {uninstall_mod.human_size(total + extra_bytes)}，删除后无法恢复。",
                "角色存档（profiles/）与另一条版本线不受影响。",
                "",
                "确认继续？",
            ]
        )
        answer = QMessageBox.warning(
            self,
            "确认卸载游戏本体",
            "\n".join(body),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._log("已取消卸载游戏本体", "dim")
            return

        typed, ok = QInputDialog.getText(
            self,
            "最后确认",
            f"将删除 {len(targets)} 个游戏文件与关联扩展/剧情包，"
            f"共 {uninstall_mod.human_size(total + extra_bytes)}。\n"
            "此操作不可恢复。请输入「删除」两个字继续：",
        )
        if not ok or typed.strip() != "删除":
            self._log("已取消卸载游戏本体（确认词不匹配）", "dim")
            return

        failures: list[str] = []
        from ....core.ext import service as ext_service_mod

        svc = ext_service_mod.service(self.cfg)

        # ① 扩展：只卸该线那一份，另一条线的同名扩展保持不动
        body_lines = uninstall_mod.line_of_body(self.cfg)
        for key in keys:
            line = body_lines.get(key, "")
            ext_ids = [a.id for a in assets[key].extensions]
            if not ext_ids:
                continue
            try:
                report = svc.uninstall_line(ext_ids, line)
            except Exception as exc:  # noqa: BLE001 - 单线失败不该挡住后面
                failures.append(f"{line} 扩展：{exc}")
                self._log(f"{line} 扩展卸载失败：{exc}", "warn")
                continue
            self._log(
                f"已卸载 {line} 线 {len(report.removed)} 个扩展"
                + (f"（{len(report.missing)} 个本就不在）" if report.missing else ""),
                "ok" if not report.warnings else "warn",
            )
            for item in report.warnings:
                self._log(f"  警告：{item}", "warn")

        # ② 本体文件
        removed, freed, body_failures = uninstall_mod.remove_game_body(self.cfg, keys)
        failures.extend(body_failures)
        self._log(
            f"已删除 {removed} 个游戏文件，释放 {uninstall_mod.human_size(freed)}",
            "ok" if not body_failures else "warn",
        )
        for item in body_failures:
            self._log(f"  未删除：{item}", "warn")

        # ③ 工作期缓存（插桩缓存 + 安装中间目录，不含用户打出来的发布包）
        body_lines = uninstall_mod.line_of_body(self.cfg)
        for key in keys:
            line = body_lines.get(key, "")
            if not line:
                continue
            count, purged, purge_failures = uninstall_mod.purge_line_workdirs(line)
            failures.extend(f"{line} 缓存：{x}" for x in purge_failures)
            if count or purged:
                self._log(
                    f"已清理 {line} 线工作期缓存 {count} 项，释放 {uninstall_mod.human_size(purged)}",
                    "ok" if not purge_failures else "warn",
                )

        if failures:
            detail = "失败：\n" + "\n".join(failures)
        else:
            detail = (
                "配置里的路径记录保留着：重新下载游戏后，"
                "可在「游戏库 → 导入游戏本体」重新登记。"
            )
        QMessageBox.information(
            self,
            "卸载完成",
            f"已删除 {removed} 个文件，释放 {uninstall_mod.human_size(freed)}；"
            f"关联扩展/剧情包与缓存已一并清理。\n\n{detail}",
        )
        if self._on_data_changed is not None:
            self._on_data_changed()
        # 删完体积变了，重算一遍勾选框文案
        for key, box in self.body_boxes.items():
            stat = next((s for s in uninstall_mod.scan_game_body(self.cfg) if s.key == key), None)
            if stat is None:
                continue
            box.blockSignals(True)
            box.setChecked(False)
            box.blockSignals(False)
            if stat.exists:
                box.setText(f"{stat.label}（{uninstall_mod.human_size(stat.bytes)}）")
                box.setEnabled(True)
            else:
                box.setText(f"{stat.label}（已卸载）")
                box.setEnabled(False)
        self._update_body_hint()

