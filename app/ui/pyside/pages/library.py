"""游戏库页：左「当前游戏配置」+ 右「已接入扩展」双栏。

布局对齐设计图（图4）：左侧放当前版本的配置卡片（圆形徽标、名称、
切换引擎线 / 本地存档、存档档位下拉），右侧是已接入扩展列表（编号徽章、
名称、作者与版本、条目数、启用开关，外加排序与缓存按钮）。

对外接口保持不变——主窗口仍通过 ``launchRequested`` / ``stopRequested`` /
``versionChanged`` / ``instanceStartRequested`` / ``instanceStopRequested``
与 ``current_version()`` / ``current_profile()`` / ``set_state()`` /
``set_launch_readiness()`` / ``refresh()`` 交互，避免重建布局时把主窗口一起改坏。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ....core import baseline_ops
from ....core import config as cfg_mod
from ....core import paths, runner, userdir
from .. import features
from ..widgets import Card, PageHeader, Pill, section_title

#: 内置版本的展示信息。
#:
#: 1.4 起只有一条内置线（4.6 = 原版 1.45）；第二条「自定义版」由玩家自己
#: 登记，从 ``config.custom_lines`` 动态读，不在这个表里。
VERSIONS = [
    {
        "key": cfg_mod.BUILTIN_LINE,
        "baseline": cfg_mod.BUILTIN_BASELINE,
        "title": "机甲旋风 1.45 · 原版",
        "badge": "1.45",
        "engine": "Godot 4.6.0",
    },
    {
        "key": "4.7",
        "baseline": "jjxf.v4.7",
        "title": "机甲旋风 4.7 · 剧情版",
        "badge": "4.7",
        "engine": "Godot 4.7.2",
        # 4.7 的本体是独立 pck（模板 exe 不内嵌数据），导入时只让选 pck
        "kind": "pck",
    },
]

def _versions(cfg: dict | None = None) -> list[dict]:
    """当前可见的内置线。

    **1.7 起不再按 edition 过滤 4.7。**

    1.6 时 4.7 只是「发布者版外挂的一条剧情线」——它的基线、引擎、剧情包
    都不在玩家版里，所以界面上按 edition 砍掉，免得玩家点了报「基线缺失」。

    1.7 把 4.7 恢复为**正式引擎线**（双线启动器），三件东西都随包发：

    - `runtime/host47/`（模板 exe + 底座 pck）
    - `baseline/identity-4.7.json`（代码层指纹，验证标志要它）
    - 出厂配置里的 `engines["4.7"]` 与 `baselines["jjxf.v4.7"]` 占位

    所以现在**两条线都该出现**。没导入本体时 4.7 显示为「未关联」，
    点启动由 `readiness` 给出「请先导入游戏本体」的明确提示 ——
    这比「列表里根本没这一项」清楚得多：玩家知道有这条线，也知道缺什么。

    唯一的过滤依据是**配置里到底有没有这条线**（``known_lines``）。
    理由：一条线能不能用，配置说了算 —— 引擎登记、基线占位都在配置里。
    运行时文件缺失是「待导入」，不是「没有这条线」，两者必须区分：
    前者该展示并提示导入，后者才该隐藏。

    ``cfg`` 传了就按它判断（测试与自定义页面用），不传读真实配置。
    """
    from ....core import config as cfg_mod

    known = set(cfg_mod.known_lines(cfg))
    return [v for v in VERSIONS if v["key"] in known]


#: 排序后的顺序存在配置里，键名固定，避免和别的 ui 子项撞车
ORDER_KEY = "extension_order"


def _on_line(item: dict, line: str) -> bool:
    """这个扩展是否装在 ``line`` 上。

    ``lines`` 是真值来源；只有老格式（没有 ``lines``）才看顶层 ``engine_line``
    ——顶层只是「最近一次安装」的镜像，拿它判断会把别的线的包算到当前线上。
    """
    lines = item.get("lines")
    if isinstance(lines, dict) and lines:
        return line in lines
    return str(item.get("engine_line") or "") == line


class LibraryPage(QWidget):
    launchRequested = Signal(str)
    stopRequested = Signal()
    versionChanged = Signal(str)
    #: 双开：按槽位启动 / 结束某个实例
    instanceStartRequested = Signal(int)
    instanceStopRequested = Signal(int)
    #: 需要写进主窗口「运行日志」的一行（本页没有日志控件，交给主窗口显示）
    logMessage = Signal(str, str)
    #: 请求打开「管理游戏线」对话框（由主窗口弹，避免页面自己持有对话框状态）
    manageLinesRequested = Signal()
    #: 请求打开扩展包导入（空扩展时右栏那个「导入扩展包」按钮发出来的）
    importModsRequested = Signal()
    #: 请求打开「管理分类」对话框
    manageCategoriesRequested = Signal()
    #: 扩展包的安装结果被本页改动（如「删除所有扩展并清理缓存」），
    #: 需要主窗口重算就绪度与启动按钮
    modsChanged = Signal()
    #: 基线被登记或回滚（导入游戏本体 / 冒烟失败回退）。
    #:
    #: 必须单独发信号：``refresh()`` 只重绘本页控件，而左侧「无法启动」
    #: 与启动按钮的可用性由主窗口 ``_sync_launch_button()`` 裁决。
    #: 以前导入完只刷新了本页，侧栏还停留在导入前的结论（仍显示「基线文件
    #: 不存在」），必须关掉重启才活过来——就是这个信号缺了。
    baselineChanged = Signal()

    def _custom_lines(self) -> list:
        from ....core import custom_lines as cl

        return cl.load_all(self.cfg)

    def _category_filter(self) -> str:
        """当前选的分类筛选（空串 = 全部）。"""
        if not hasattr(self, "filter_combo"):
            return ""
        return str(self.filter_combo.currentData() or "")

    def _fill_filters(self) -> None:
        """填充分类筛选下拉。只在有游戏归属的分类出现，避免一排 0 项的空分类。"""
        from ....core import categories as cat_mod

        blocked = self.filter_combo.blockSignals(True)
        current = self._category_filter()
        self.filter_combo.clear()
        self.filter_combo.addItem("全部分类", "")

        counts: dict[str, int] = {}
        for meta in _versions(self.cfg):
            cid = cat_mod.category_of_baseline(self.cfg, meta["baseline"])
            counts[cid] = counts.get(cid, 0) + 1
        for line in self._custom_lines():
            cid = cat_mod.resolve(self.cfg, line.category or cat_mod.MOD_ID)
            counts[cid] = counts.get(cid, 0) + 1
        for cat in cat_mod.load(self.cfg):
            if cat.id not in counts:
                continue
            self.filter_combo.addItem(f"{cat.name}（{counts[cat.id]}）", cat.id)

        idx = self.filter_combo.findData(current)
        self.filter_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.filter_combo.blockSignals(blocked)

    def _first_selectable(self) -> int:
        """第一个可选中的行（跳过分组标题那种禁用项）。"""
        for i in range(self.selector.count()):
            if self.selector.itemData(i):
                return i
        return -1

    def _add_group_header(self, text: str) -> None:
        """插一行不可选的分组标题。"""
        index = self.selector.count()
        self.selector.addItem(f"— {text} —", None)
        model = self.selector.model()
        item = model.item(index) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(False)

    def _fill_selector(self, *, initial: bool = False) -> None:
        """填充版本下拉：按分类分组，内置两条与全部自定义线都在里面。

        分组标题是**禁用项**（不可选中），这样「分类清晰」但不会多出
        一组假的选项。选中项仍靠 userData 认 id，分支逻辑不变。
        """
        from ....core import categories as cat_mod

        saved = self.current_version() if not initial else self._initial_version()
        blocked = self.selector.blockSignals(True)
        self.selector.clear()

        groups: dict[str, list[tuple[str, str]]] = {}
        for meta in _versions(self.cfg):
            cid = cat_mod.category_of_baseline(self.cfg, meta["baseline"])
            groups.setdefault(cid, []).append((meta["title"], meta["key"]))
        for line in self._custom_lines():
            cid = cat_mod.resolve(self.cfg, line.category or cat_mod.MOD_ID)
            mark = "" if line.exists else "（文件缺失）"
            kind_text = "自定义资源包" if line.is_pck else "自定义改版"
            groups.setdefault(cid, []).append((f"{line.name}　·　{kind_text}{mark}", line.id))

        want = self._category_filter()
        order = [c.id for c in cat_mod.load(self.cfg) if c.id in groups]
        # 兜底：分类表里没有的 id（配置被手改坏）也要能显示出来
        order += [cid for cid in groups if cid not in order]
        if want:
            order = [cid for cid in order if cid == want]

        for cid in order:
            self._add_group_header(cat_mod.name_of(self.cfg, cid))
            for label, key in groups[cid]:
                self.selector.addItem(label, key)

        idx = self.selector.findData(saved)
        if idx < 0:
            idx = self._first_selectable()
        self.selector.setCurrentIndex(idx if idx >= 0 else 0)
        self.selector.blockSignals(blocked)

    def _on_filter_changed(self, _idx: int = 0) -> None:
        before = self.current_version()
        self._fill_selector()
        key = self.current_version()
        self._apply_detail(key)
        self.refresh_mods()
        if key != before:
            self.versionChanged.emit(key)

    def reload_categories(self) -> None:
        """分类被增删改后刷新筛选与分组。"""
        self._fill_filters()
        self._fill_selector()
        self._apply_detail(self.current_version())

    def reload_lines(self) -> None:
        """自定义线被增删改后刷新下拉与详情。"""
        self._fill_filters()
        self._fill_selector()
        self._apply_detail(self.current_version())
        self.refresh_mods()

    def __init__(self, cfg: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        #: 行号 → 扩展 id，供排序按钮反查
        self._row_ids: list[str] = []
        #: 导入本体的后台探测/校验线程。必须保住引用，否则线程对象会被 GC，
        #: 回调直接不触发（表现为「导入点了没反应」）。
        self._probe_worker = None
        self._import_task_worker = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = PageHeader(
            "游戏库",
            "机甲旋风 · 兼容启动器",
        )
        # 双开测试已移除（2026-11-08 用户决策）。
        # 入口按钮与底部面板都不再创建 —— 单包发布后所有玩家都会看到它，
        # 而「槽位 / 两套存档」这类概念对玩游戏的玩家没有意义。
        self.instances_btn = None
        self.import_btn = QPushButton("导入游戏本体")
        self.import_btn.setProperty("variant", "ghost")
        self.import_btn.setToolTip(
            "把本机的游戏本体登记为基线；启动器本身不含游戏本体。\n"
            "4.6 线（1.45）选 exe（数据内嵌在 exe 尾部）或 pack_version=3 的 pck；"
            "4.7 剧情线选 pack_version=4 的 pck。"
        )
        self.import_btn.clicked.connect(self._import_game_exe)
        header.add_action(self.import_btn)
        # 「校验本体」：导入时没跑校验（或校验失败）的人从这里补。
        # 平时不显示 —— 只在 verify_state 是 pending/failed 时出现，
        # 避免让「已经能正常的玩家」看到一个不知道干什么的按钮。
        self.verify_btn = QPushButton("校验本体")
        self.verify_btn.setProperty("variant", "ghost")
        self.verify_btn.setToolTip(
            "对当前线已登记的游戏本体跑一次挂载冒烟。\n"
            "导入后必须先通过这一步才能启动 —— 防止在包还没校验完时\n"
            "就启动游戏、读到半截的 pck 导致进程崩溃。"
        )
        self.verify_btn.clicked.connect(self._verify_baseline)
        self.verify_btn.setVisible(False)
        header.add_action(self.verify_btn)
        self.help_btn = QPushButton("使用说明")
        self.help_btn.setProperty("variant", "ghost")
        self.help_btn.clicked.connect(self._show_help)
        header.add_action(self.help_btn)
        root.addWidget(header)

        # ---------------- 双栏主体
        cols = QHBoxLayout()
        cols.setSpacing(12)
        # 左右比重 8:17（原 9:13）。
        #
        # 2026-11-09 用户要求「当前游戏配置模块缩小一些，扩展列表每项大一些、
        # 字体适中」：左栏只是静态信息（版本号 + 存档档位 + 路径），
        # 内容不会变多，压窄不影响可读性；右栏是扩展列表，
        # 条目需要横向空间放「名称 / 版本 / 状态 / 按钮」四段，
        # 之前 9:13 时状态标签被挤到边缘（截图里「已启用」被裁）。
        # 左栏先建出来，才有 filter_combo / selector / profile_box。
        #
        # **比例要生效，必须先解开子控件的宽度约束。**
        # 实测左栏 Card 的 sizeHint 是 398（右栏只有 294），
        # 元凶是三个 QComboBox 的 Qt 默认最小宽度 256 ——
        # stretch 只能分配「剩余」空间，尺寸被 hint 顶住后
        # 8:17 完全不生效，看起来反而左宽右窄（截图确认）。
        config_card = self._build_config_card()
        for box in (
            getattr(self, "filter_combo", None),
            getattr(self, "selector", None),
            getattr(self, "profile_box", None),
        ):
            if box is not None:
                box.setMinimumWidth(110)
                box.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
                )

        # **必须解开卡片的宽度约束，否则 8:17 比例形同虚设。**
        #
        # 实测：左栏 Card 的 sizeHint 是 367，Qt 会把它当成「理想宽度」，
        # 于是 stretch 因子只能分配剩下的空间 —— 结果 8:17 算出来
        # 左 367 / 右 325，看起来反而是左宽右窄（截图确认过）。
        #
        # 两处一起改才有效：
        #   1. Card 自身 sizePolicy 的横向设为 Ignored（忽略 sizeHint）
        #   2. 用 setMinimumWidth(0) 解掉内部控件传递上来的最小宽度
        mods_card = self._build_mods_card()
        for card in (config_card, mods_card):
            card.setMinimumWidth(0)
            card.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )

        # 比例 11:14（≈0.44 : 0.56），原 9:13。
        #
        # 试过 8:17 —— 右栏确实宽敞了，但左栏被压到 221px，
        # 「管理分类 / 管理游戏线」两个按钮并排需要约 320px，
        # 于是文字被挤重叠、按钮被压扁（截图确认）。
        # 11:14 在 704 可用宽下给左栏约 310、右栏约 394，
        # 既不像原来那么挤，也放得下所有按钮。
        cols.addWidget(config_card, 11)
        cols.addWidget(mods_card, 14)
        root.addLayout(cols)

        # ---------------- 启动条
        launch_bar = Card()
        launch_row = QHBoxLayout(launch_bar)
        launch_row.setContentsMargins(16, 12, 16, 12)
        launch_row.setSpacing(10)
        self.launch_btn = QPushButton("启动游戏")
        self.launch_btn.setProperty("variant", "primary")
        self.launch_btn.setMinimumWidth(132)
        self.launch_btn.setFixedHeight(40)
        self.launch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_btn.clicked.connect(lambda: self.launchRequested.emit(self.current_version()))
        launch_row.addWidget(self.launch_btn)
        #: 主窗口给出的启动裁决（就绪度 + 导入中 + 空闲槽位）。
        #: ``set_state`` 在进程退出时也会动这个按钮，必须尊重同一份裁决——
        #: 否则「导入中游戏刚好退出」会把按钮重新点亮，绕过互斥。
        self._readiness_ok = True
        self._readiness_reason = ""
        self.detail_btn = QPushButton("版本详情")
        self.detail_btn.setProperty("variant", "ghost")
        self.detail_btn.setFixedHeight(40)
        self.detail_btn.clicked.connect(self._show_details)
        launch_row.addWidget(self.detail_btn)
        launch_row.addStretch(1)
        self.state_pill = Pill("空闲", "muted")
        launch_row.addWidget(self.state_pill)
        self.state_text = QLabel("尚未启动")
        self.state_text.setObjectName("Muted")
        launch_row.addWidget(self.state_text)
        self.stop_btn = QPushButton("结束游戏")
        self.stop_btn.setProperty("variant", "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stopRequested.emit)
        launch_row.addWidget(self.stop_btn)
        root.addWidget(launch_bar)

        # ---------------- 双开测试面板已移除（2026-11-08 用户决策）
        #
        # 原来的 InstancePanel 让玩家管理两个「槽位」，各自独立存档目录。
        # 那是作者做本机联机/对照测试用的，属于开发辅助而非玩家功能。
        #
        # 保留 self.instance_panel = None：外部（main_window 的
        # instanceStartRequested 等信号连接）会访问这个属性，
        # 删掉会 AttributeError。
        self.instance_panel = None

        # ---------------- 快捷入口
        root.addWidget(section_title("快捷入口", "存档、日志、项目目录"))
        quick = Card()
        quick_row = QHBoxLayout(quick)
        quick_row.setContentsMargins(16, 10, 16, 10)
        quick_row.setSpacing(8)
        self.path_hint = QLabel("")
        self.path_hint.setObjectName("Faint")
        self.path_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # 这一行原本是「当前存档：profiles\default　（user:// 重定向到该目录）」，
        # 单行最小宽度实测 480px，直接把这个卡片撑到 776px、整页撑到 1106px——
        # 右侧「已接入扩展」因此被挤出可视区（截图里右侧一列按钮全被切掉）。
        # 允许换行 + 允许被压窄，是解决横向溢出的第一步。
        self.path_hint.setWordWrap(True)
        self.path_hint.setMinimumWidth(0)
        self.path_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        quick_row.addWidget(self.path_hint, 1)
        for text, action in (
            ("存档目录", self._open_profile_dir),
            ("日志目录", self._open_log_dir),
            ("项目目录", self._open_root_dir),
        ):
            btn = QPushButton(text)
            btn.setProperty("variant", "ghost")
            btn.clicked.connect(action)
            quick_row.addWidget(btn)
        root.addWidget(quick)

        root.addStretch(1)

        self._fill_profiles()
        self.profile_box.currentTextChanged.connect(self._on_profile_changed)
        self._update_path_hint()
        self._apply_detail(self.current_version())
        self.refresh_mods()

    # ------------------------------------------------------------ 左栏：当前游戏配置

    def _build_config_card(self) -> QWidget:
        card = Card()
        col = QVBoxLayout(card)
        col.setContentsMargins(16, 14, 16, 14)
        col.setSpacing(10)

        col.addWidget(section_title("当前游戏配置", "本机已关联的游戏执行体与存档隔离方案"))

        self.ver_title = QLabel("")
        self.ver_title.setObjectName("CardTitle")
        col.addWidget(self.ver_title)

        # ---------------- 选择器放在徽标**之前**
        # 原因：日志抽屉展开时页面可视高只有约 300px，而整张卡片要 400px+。
        # 徽标、说明这些「装饰」放前面，会把「选哪个游戏 / 哪个分类」挤到
        # 折叠线以下 —— 用户打开页面找不到唯一重要的两个控件。
        # 控件优先于装饰，滚动交给页面本身。
        #
        # 分类筛选：游戏多了以后，一个扁平下拉会很难找；先缩小范围再选具体游戏。
        cat_row = QHBoxLayout()
        cat_row.setSpacing(8)
        cat_label = QLabel("分类")
        cat_label.setObjectName("Muted")
        cat_row.addWidget(cat_label)
        self.filter_combo = QComboBox()
        self.filter_combo.setToolTip("按分类筛选游戏；分类可在「管理分类」里自己增删改名")
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        cat_row.addWidget(self.filter_combo, 1)
        self.manage_cats_btn = QPushButton("管理分类")
        self.manage_cats_btn.setProperty("variant", "ghost")
        # 按钮文字**不允许被压缩**：左栏压窄时 Qt 会按 Minimum 策略
        # 把它挤到比文字还窄，出现「管理分类」被截成「管理分类」这种
        # 半个字的问题（实测）。用 sizeHint 当最小宽度锁死。
        # **不能用 sizeHint()**：构建期 QSS 还没应用，sizeHint 只有 80，
        # 而实际文字需要 99 —— 结果按钮还是被截成「理游戏线」。
        # 用字体度量自己算：文字宽 + 左右内边距（QSS 里 padding 10px 各边）。
        _fm = self.manage_cats_btn.fontMetrics()
        self.manage_cats_btn.setMinimumWidth(
            _fm.horizontalAdvance("管理分类") + 26
        )
        self.manage_cats_btn.setToolTip("新增/改名/排序/删除游戏分类")
        self.manage_cats_btn.clicked.connect(self.manageCategoriesRequested.emit)
        cat_row.addWidget(self.manage_cats_btn)
        col.addLayout(cat_row)

        # 版本选择（对应设计图的「切换引擎线」）
        pick = QHBoxLayout()
        pick.setSpacing(8)
        self.selector = QComboBox()
        self._fill_filters()
        self._fill_selector(initial=True)
        self.selector.setToolTip("切换要启动的版本线；自定义改版只启动游戏本身，不加载任何扩展")
        self.selector.currentIndexChanged.connect(self._on_selector_changed)
        pick.addWidget(self.selector, 1)
        self.manage_lines_btn = QPushButton("管理游戏线")
        self.manage_lines_btn.setProperty("variant", "ghost")
        _fm2 = self.manage_lines_btn.fontMetrics()
        self.manage_lines_btn.setMinimumWidth(
            _fm2.horizontalAdvance("管理游戏线") + 26
        )
        self.manage_lines_btn.setToolTip("添加/编辑/移除自定义改版游戏")
        self.manage_lines_btn.clicked.connect(self.manageLinesRequested.emit)
        pick.addWidget(self.manage_lines_btn)
        # 「本地存档」原在这行（下拉框 + 管理游戏线 + 本地存档 三样挤在
        # 304px 里），导致「管理游戏线」被截成「理游戏线」。
        # 现在它挪到下面的「存档档位」行，与存档下拉框同行 ——
        # 语义也更贴近（都是存档相关），每行只放两样东西。
        col.addLayout(pick)

        self.profile_open_btn = QPushButton("本地存档")
        self.profile_open_btn.setProperty("variant", "ghost")
        self.profile_open_btn.setToolTip(
            "打开当前档位的**真实**存档目录\n"
            "（profiles/<档位>/appdata/Godot/app_userdata/机甲旋风 · 兼容底座 <线>）"
        )
        self.profile_open_btn.clicked.connect(self._open_profile_dir)

        # 圆形徽标：用版本号代替头像，避免伪造游戏角色图
        self.detail_badge = QLabel("")
        self.detail_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_badge.setFixedSize(76, 76)
        self.detail_badge.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #12345c, stop:1 #1c5794);"
            "color:#bfe4ff; border:1px solid #2f74b8; border-radius:38px;"
            "font-size:22px; font-weight:700;"
        )
        badge_row = QHBoxLayout()
        badge_row.addStretch(1)
        badge_row.addWidget(self.detail_badge)
        badge_row.addStretch(1)
        col.addLayout(badge_row)

        self.detail_title = QLabel("")
        self.detail_title.setObjectName("CardTitle")
        self.detail_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.detail_title)

        self.detail_sub = QLabel("")
        self.detail_sub.setObjectName("CardSub")
        self.detail_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_sub.setWordWrap(True)
        col.addWidget(self.detail_sub)

        # ---------------- 4.7 剧情覆盖层
        #
        # 剧情包不是「扩展」：它是 4.7 基线的源码层/内容层，登记在
        # ``baselines.jjxf.v4.7.story_overlay``，挂载顺序是
        # ``baseline → story → mod``。这里只做「可见 + 可替换」，**不**把它
        # 塞进扩展列表——那会重新引入「story 波次排在 extension 之后」
        # 的覆盖问题（mod 会被剧情包整片盖掉）。
        self.story_box = QWidget()
        story_col = QVBoxLayout(self.story_box)
        story_col.setContentsMargins(0, 6, 0, 0)
        story_col.setSpacing(4)

        story_head = QHBoxLayout()
        story_head.setSpacing(6)
        story_cap = QLabel("4.7 剧情覆盖层")
        story_cap.setObjectName("Muted")
        story_head.addWidget(story_cap)
        story_head.addStretch(1)
        self.story_pill = Pill("已启用", "ok")
        story_head.addWidget(self.story_pill)
        story_col.addLayout(story_head)

        self.story_path = self._path_text()
        story_col.addWidget(self.story_path)

        self.story_info = QLabel("")
        self.story_info.setObjectName("Faint")
        self.story_info.setWordWrap(True)
        story_col.addWidget(self.story_info)

        story_btns = QHBoxLayout()
        story_btns.setSpacing(6)
        self.story_pick_btn = QPushButton("替换剧情包…")
        self.story_pick_btn.setProperty("variant", "ghost")
        self.story_pick_btn.setToolTip(
            "选择一个新的 .pck 覆盖当前剧情层；旧文件会备份为 story.pck.bak"
        )
        self.story_pick_btn.clicked.connect(self._pick_story)
        story_btns.addWidget(self.story_pick_btn)
        story_open_btn = QPushButton("打开目录")
        story_open_btn.setProperty("variant", "ghost")
        story_open_btn.clicked.connect(self._open_story_dir)
        story_btns.addWidget(story_open_btn)
        story_btns.addStretch(1)
        story_col.addLayout(story_btns)

        self.story_box.setVisible(False)
        col.addWidget(self.story_box)

        # 存档档位
        slot_row = QHBoxLayout()
        slot_row.setSpacing(8)
        slot_label = QLabel("存档档位")
        slot_label.setObjectName("Muted")
        slot_row.addWidget(slot_label)
        self.profile_box = QComboBox()
        self.profile_box.setToolTip("user:// 会被重定向到 profiles/<名字>，与真实存档隔离")
        slot_row.addWidget(self.profile_box, 1)
        # 「本地存档」按钮跟着存档档位走（原在「游戏线」那行，
        # 三样挤一起会把「管理游戏线」文字截断，见上面 pick 处的说明）
        slot_row.addWidget(self.profile_open_btn)
        col.addLayout(slot_row)

        # ---------------- 存档位置（原版 vs 启动器）
        # 「角色存档丢失」的真实原因不是文件没了，而是两边 user:// 目录名不同：
        # 原版工程名是「机甲旋风 · 单机」，启动器底座是「机甲旋风 · 兼容底座 4.6」。
        # 玩家不可能自己猜到这件事，所以把两条绝对路径直接摆在卡片里，
        # 并提供「导入存档」把原版那份复制进来。
        col.addWidget(self._build_save_paths())

        self.tag_row = QWidget()
        tags = QHBoxLayout(self.tag_row)
        tags.setContentsMargins(0, 0, 0, 0)
        tags.setSpacing(6)
        self.tags: list[Pill] = []
        tag_wrap = QHBoxLayout()
        tag_wrap.addStretch(1)
        tag_wrap.addWidget(self.tag_row)
        tag_wrap.addStretch(1)
        col.addLayout(tag_wrap)
        # 不在这里 addStretch：左栏内容本来就少于右栏，再加 stretch 会在
        # 底部留一大片空白，视觉上像「没加载完」。让它靠上，高度由右栏撑。
        return card

    # ------------------------------------------------------------ 左栏：存档位置

    def _path_text(self, text: str = "") -> QLabel:
        """等宽、可选中、可换行的路径块。

        路径必须能选中复制（玩家要拿它去资源管理器里找），但绝不能因为它
        太长就把整张卡撑宽——所以横向策略用 Ignored + 换行。
        """
        label = QLabel(text)
        label.setObjectName("PathText")
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setMinimumHeight(22)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def _build_save_paths(self) -> QWidget:
        """把「原版存档在哪 / 启动器存档在哪」写在卡片里。"""
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(4)

        cap1 = QLabel("游戏默认存档位置（直接双击原版游戏时）")
        cap1.setObjectName("Faint")
        cap1.setWordWrap(True)
        col.addWidget(cap1)
        self.original_path = self._path_text()
        col.addWidget(self.original_path)

        cap2 = QLabel("启动器存档位置（当前档位的真实目录）")
        cap2.setObjectName("Faint")
        cap2.setWordWrap(True)
        col.addWidget(cap2)
        self.active_path = self._path_text()
        col.addWidget(self.active_path)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.import_save_btn = QPushButton("角色存档")
        # 这一行三个按钮挤在 ~300px 里，Text 会被 QSS 的
        # ``padding: 7px 16px``（左右各 16）+ 1px 边框吃掉宽度，
        # 而 Qt 的 sizeHint 只按默认 padding 算 —— 实测给 86，
        # 实际需要 86 + 32 才够，于是「角色存档」被截成「角色存」。
        # 直接按「文字宽 + 左右各 17」算死最小宽度。
        _fw = self.import_save_btn.fontMetrics()
        self.import_save_btn.setMinimumWidth(
            _fw.horizontalAdvance("角色存档") + 40
        )
        self.import_save_btn.setProperty("variant", "primary")
        self.import_save_btn.setToolTip("把原版游戏（或旧启动器）里的角色存档复制进当前档位")
        self.import_save_btn.clicked.connect(self._import_saves)
        btns.addWidget(self.import_save_btn)
        open_btn = QPushButton("打开目录")
        open_btn.setProperty("variant", "ghost")
        open_btn.clicked.connect(self._open_profile_dir)
        btns.addWidget(open_btn)
        copy_btn = QPushButton("复制路径")
        copy_btn.setProperty("variant", "ghost")
        copy_btn.setToolTip("复制启动器存档目录的完整路径")
        copy_btn.clicked.connect(self._copy_active_path)
        btns.addWidget(copy_btn)
        btns.addStretch(1)
        col.addLayout(btns)
        return box

    def _refresh_save_paths(self) -> None:
        from ....core import custom_lines as cl

        key = self.current_version()
        profile = self.current_profile()
        if cl.is_custom_id(key):
            self.original_path.setText(userdir.original_dir().as_posix())
            self.active_path.setText("自定义改版：存档位置由该改版自己的工程名决定，启动器不接管")
            self.import_save_btn.setEnabled(False)
            return

        self.import_save_btn.setEnabled(True)
        info = userdir.describe(self.cfg, profile, key)
        self.original_path.setText(info["original"])
        text = info["active"]
        if not info["redirect"]:
            # 关掉「存档隔离」时不会重定向 APPDATA，目录与档位无关，必须说清楚
            text += "　（已关闭存档隔离，所有档位共用这里）"
        elif not info["active_exists"]:
            text += "　（还没生成，首次启动后出现）"
        self.active_path.setText(text)
        self.active_path.setToolTip(info["active"])
        self.original_path.setToolTip(info["original"])

    def _copy_active_path(self) -> None:
        from PySide6.QtGui import QGuiApplication

        from ....core import custom_lines as cl

        key = self.current_version()
        if cl.is_custom_id(key):
            QGuiApplication.clipboard().setText(userdir.original_dir().as_posix())
            return
        QGuiApplication.clipboard().setText(
            userdir.launcher_dir(self.current_profile(), key, self.cfg).as_posix()
        )
        if hasattr(self, "logMessage"):
            self.logMessage.emit("已复制存档目录路径", "dim")

    def _import_saves(self) -> None:
        from ....core import custom_lines as cl
        from .save_import import SaveImportDialog

        key = self.current_version()
        if cl.is_custom_id(key):
            QMessageBox.information(
                self,
                "自定义改版",
                "当前选中的是自定义改版游戏线，它的存档目录由它自己的工程名决定，\n"
                "启动器不接管，因此不需要在这里导入。\n\n"
                "如需把原版存档迁到自定义改版里，请直接复制到那个改版自己的 user:// 目录。",
            )
            return
        if self.game_running():
            QMessageBox.information(
                self,
                "游戏正在运行",
                "请先点「结束游戏」再导入存档。\n\n"
                "运行中的游戏退出时会用内存里的数据覆盖刚导入的文件，导了也等于没导。",
            )
            return
        dialog = SaveImportDialog(self.cfg, self.current_profile(), key, self)
        dialog.imported.connect(self._on_saves_imported)
        dialog.exec()

    def _on_saves_imported(self, result: dict) -> None:
        lines = "、".join(f"{t['line']} 线" for t in result.get("targets") or [])
        self.logMessage.emit(
            f"已导入角色存档：{result.get('copied', 0)} 个文件 → {lines or '（无）'}", "ok"
        )
        self._refresh_save_paths()
        self.refresh_mods()

    # ------------------------------------------------------------ 右栏：已接入扩展

    def _build_mods_card(self) -> QWidget:
        card = Card()
        col = QVBoxLayout(card)
        col.setContentsMargins(16, 14, 16, 14)
        col.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.mods_title = section_title("已接入扩展", "")
        head.addWidget(self.mods_title)
        head.addStretch(1)
        self.rebuild_cache_btn = QPushButton("重建当前缓存")
        self.rebuild_cache_btn.setProperty("variant", "ghost")
        self.rebuild_cache_btn.setToolTip("清掉本版本的插桩缓存，下次启动按当前启用列表重建")
        self.rebuild_cache_btn.clicked.connect(self._rebuild_cache)
        head.addWidget(self.rebuild_cache_btn)
        self.refresh_mods_btn = QPushButton("刷新")
        self.refresh_mods_btn.setProperty("variant", "ghost")
        self.refresh_mods_btn.clicked.connect(self.refresh_mods)
        head.addWidget(self.refresh_mods_btn)
        col.addLayout(head)

        # 扩展列表容器（用滚动区包住，行数多时不挤坏布局）
        from PySide6.QtWidgets import QScrollArea

        self.mods_area = QScrollArea()
        self.mods_area.setObjectName("ModScroll")
        self.mods_area.setWidgetResizable(True)
        self.mods_area.setFrameShape(QScrollArea.Shape.NoFrame)
        # 扩展列表只可能纵向变长，横向永远不该出现滚动条：
        # 一旦出现就说明某一行内容把容器撑宽了，那是 bug 不是特性，
        # 应该让文字换行而不是让用户横向拖。这里强制关闭横向滚动。
        self.mods_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 4.6 线实测有 13 个扩展：希望一屏尽量多露几行。
        # 但不能把最小高度顶太高——原来写死 300，加上上面的配置卡后整页
        # 需要 765px 高，而 760 窗口扣掉日志区只剩 421px，于是整页只能滚动，
        # 底部操作按钮永远看不到。改成 180：仍能露 4~5 行，
        # 且给日志区留出空间（日志可拖动放大，页面高度不该被它长期挤压）。
        self.mods_area.setMinimumHeight(180)
        holder = QWidget()
        holder.setObjectName("ModList")
        self.mods_holder_layout = QVBoxLayout(holder)
        # 底部留 4px：否则滚到尽头时最后一行紧贴裁切边，看着像被切掉半截
        self.mods_holder_layout.setContentsMargins(0, 0, 0, 4)
        self.mods_holder_layout.setSpacing(6)
        self.mods_holder_layout.addStretch(1)
        self.mods_area.setWidget(holder)
        col.addWidget(self.mods_area, 1)

        # 空状态：整块替换列表区。
        # 1.3 的做法是在列表下方挂一行浅色小字，而下面四行按钮/提示/计数
        # 一个都用不了仍然占位，空列表反而是最挤的状态（截图里底部计数
        # 被日志面板削掉半截）。现在空状态单独成块、独占剩余空间。
        self.mods_empty = self._build_mods_empty()
        col.addWidget(self.mods_empty, 1)

        # ---------------- 底部操作区
        # 拆成三个能独立显隐的块：排序 / 整批清理 / 计数。
        # 空列表时全收起来，卡片只剩「拖进来装」这一个信息；
        # 有扩展时也只占三行（1.3 是四行，且提示和按钮各占一行）。
        self.sort_ops = QWidget()
        sort_row = QHBoxLayout(self.sort_ops)
        sort_row.setContentsMargins(0, 0, 0, 0)
        sort_row.setSpacing(6)
        self.mod_up_btn = QPushButton("上移")
        self.mod_down_btn = QPushButton("下移")
        self.mod_out_btn = QPushButton("移出列表")
        for b in (self.mod_up_btn, self.mod_down_btn, self.mod_out_btn):
            b.setProperty("variant", "ghost")
            b.setEnabled(False)
            sort_row.addWidget(b)
        self.mod_up_btn.setToolTip("与上一行交换加载顺序（靠前的先挂载）")
        self.mod_down_btn.setToolTip("与下一行交换加载顺序")
        self.mod_out_btn.setToolTip("从当前列表隐藏，不删文件")
        self.mod_up_btn.clicked.connect(lambda: self._move_selected(-1))
        self.mod_down_btn.clicked.connect(lambda: self._move_selected(1))
        self.mod_out_btn.clicked.connect(lambda: self._remove_selected_from_list())
        sort_row.addStretch(1)

        self.wipe_ops = QWidget()
        wipe_row = QHBoxLayout(self.wipe_ops)
        wipe_row.setContentsMargins(0, 0, 0, 0)
        wipe_row.setSpacing(8)
        # 这一格原来是「清除缓存」，和右上角的「重建当前缓存」功能重复
        # （两者都是先清插桩缓存）。1.4 起改成整批清理：大规模更新时
        # 一条线几十个扩展挨个卸载太费事，这里一次清空。
        self.mod_clear_btn = QPushButton("删除所有扩展并清理缓存")
        self.mod_clear_btn.setProperty("variant", "danger")
        self.mod_clear_btn.setEnabled(False)
        self.mod_clear_btn.clicked.connect(self._wipe_line_mods)
        wipe_row.addWidget(self.mod_clear_btn)
        # 提示跟着按钮走，而不是另起一行：它只在有东西可清时才有意义，
        # 单独占一行会把卡片顶高、把下面的计数挤出去。
        # 文案取短版：最小窗口（1020）时这一行只剩 87px，写全
        # 「适用于大规模更新的情况下」会被裁掉后三个字，完整说法放 tooltip。
        self.wipe_hint = QLabel("大规模更新时用")
        self.wipe_hint.setObjectName("Faint")
        self.wipe_hint.setWordWrap(True)
        self.wipe_hint.setMinimumWidth(0)
        self.wipe_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        wipe_row.addWidget(self.wipe_hint, 1)

        self.mods_count = QLabel("")
        self.mods_count.setObjectName("Faint")
        # 只让横向可被压缩（避免长计数撑宽卡片），纵向按内容取高度。
        # 注意：Ignored 会让 minimumSizeHint 变得极小（实测 33x11），
        # 布局会把行高压到 11px 使文字像被削平，所以显式给一个纵向保底。
        self.mods_count.setWordWrap(True)
        self.mods_count.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.mods_count.setMinimumHeight(20)

        col.addWidget(self.sort_ops)
        col.addWidget(self.wipe_ops)
        col.addWidget(self.mods_count)
        return card

    def _build_mods_empty(self) -> QWidget:
        """空扩展时的引导块：一块虚线拖放区，不是一段孤零零的灰字。"""
        from PySide6.QtWidgets import QFrame

        box = QFrame()
        box.setObjectName("ModEmpty")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(box)
        col.setContentsMargins(24, 20, 24, 20)
        col.setSpacing(6)
        col.addStretch(1)

        self.empty_icon = QLabel()
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_icon.setFixedHeight(44)
        col.addWidget(self.empty_icon)

        self.empty_title = QLabel("还没有接入扩展")
        self.empty_title.setObjectName("ModEmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setWordWrap(True)
        col.addWidget(self.empty_title)

        self.empty_text = QLabel("把扩展包拖进这个窗口，或点下面的按钮选择文件")
        self.empty_text.setObjectName("ModEmptyText")
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_text.setWordWrap(True)
        col.addWidget(self.empty_text)

        col.addSpacing(10)
        # 空状态直接给入口，别让人为了装一个包先跑去「扩展管理」页
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.empty_import_btn = QPushButton("导入扩展包")
        self.empty_import_btn.setProperty("variant", "ghost")
        self.empty_import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.empty_import_btn.setToolTip("选择一个或多个 .zip / .jjm / .pack 扩展包")
        self.empty_import_btn.clicked.connect(
            lambda: self.importModsRequested.emit()
        )
        btn_row.addWidget(self.empty_import_btn)
        btn_row.addStretch(1)
        col.addLayout(btn_row)

        col.addSpacing(8)
        self.empty_hint = QLabel("扩展包只含 mod 内容，不含游戏本体")
        self.empty_hint.setObjectName("Faint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        col.addWidget(self.empty_hint)

        col.addStretch(1)
        return box

    def _paint_empty_icon(self) -> None:
        """空状态图标跟着主题重画（切浅色主题时不能还留着深色描边）。"""
        from .. import colors, icons

        try:
            pix = icons.pixmap("puzzle", colors.faint(), 34, 1.6)
        except Exception:  # noqa: BLE001 - 画个图标失败不该把页面搞崩
            return
        self.empty_icon.setPixmap(pix)

    # ------------------------------------------------------------ 扩展列表渲染

    def _order(self) -> list[str]:
        raw = (self.cfg.get("ui") or {}).get(ORDER_KEY)
        return [str(x) for x in raw] if isinstance(raw, list) else []

    def _save_order(self, ids: list[str]) -> None:
        self.cfg.setdefault("ui", {})[ORDER_KEY] = list(ids)
        try:
            cfg_mod.save(self.cfg)
        except OSError:
            pass

    def _ordered_mods(self, items: list[dict]) -> list[dict]:
        """按用户在前台排好的顺序输出；没排过的追加在后面。"""
        order = self._order()
        rank = {eid: i for i, eid in enumerate(order)}
        return sorted(items, key=lambda it: (rank.get(str(it.get("id")), 10**9), str(it.get("name") or "")))

    def refresh_mods(self) -> None:
        """重绘右栏扩展列表。作者为空时显式标「未标注」，不猜来源。"""
        from ....core import custom_lines as cl
        from ....core.ext.service import ExtensionService

        line = self.current_version()

        # 自定义线按接入分档说明右栏，而不是显示空列表让人以为「扩展丢了」。
        if cl.is_custom_id(line):
            self._row_ids = []
            lay = self.mods_holder_layout
            while lay.count() > 1:
                w = lay.takeAt(0).widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            entry = cl.get_line(self.cfg, line)
            tier = getattr(entry, "access_tier", "") if entry else ""
            if tier == cl.TIER_FULL:
                self.empty_title.setText("这条线可协商扩展")
                text = (
                    "改版作者提供了通过一致性校验的 line.json 描述，"
                    "启动器会按描述里的能力为 mod 做接入协商。"
                    "本启动器只做单机，描述或扩展含联机协议的一律拒绝接入。"
                )
                # 上次启动的协商报告：把「谁被跳过、为什么」直接摆出来，
                # 避免又出现战力 mod 那种「装载成功、实际没生效」的静默失效。
                import json as _json
                from ....core import paths as core_paths

                report = (core_paths.PROFILES / entry.profile_name()
                          / "custom" / "negotiation.json")
                try:
                    data = _json.loads(report.read_text(encoding="utf-8"))
                    skipped = [d for d in data.get("decisions") or []
                               if d.get("action") == "skip"]
                    text += (f"\n\n上次启动：挂载 {len(data.get('mounted') or [])} 个、"
                             f"跳过 {len(skipped)} 个（详情见 negotiation.json）。")
                    for d in skipped[:3]:
                        text += f"\n· {d.get('id')}：{d.get('reason')}"
                    if data.get("baseline_ready"):
                        text += ("\n\n已导入可插桩基线；插桩层生成器接入前，"
                                 "方法钩子类扩展仍会明确跳过，不会静默挂载。")
                except (OSError, ValueError, AttributeError):
                    pass
                self.empty_text.setText(text)
            else:
                self.empty_title.setText("这条线暂不加载扩展")
                note = (getattr(entry, "access_note", "") if entry else "") or (
                    "让改版作者随包提供 line.json 描述与反解工程索引，"
                    "通过校验后即可升级为可协商扩展。"
                )
                self.empty_text.setText("启动器目前只负责启动这条线。" + note)
            # 这条线本来就不该有扩展：再引导「导入扩展包」是自相矛盾的
            self.empty_hint.setVisible(False)
            self.empty_import_btn.setVisible(False)
            self._paint_empty_icon()
            self.mods_area.setVisible(False)
            self.mods_empty.setVisible(True)
            self.sort_ops.setVisible(False)
            self.wipe_ops.setVisible(False)
            self.mods_count.setVisible(False)
            for b in (self.mod_up_btn, self.mod_down_btn, self.mod_out_btn, self.mod_clear_btn):
                b.setEnabled(False)
            return

        try:
            installed = ExtensionService(self.cfg).installed()
        except Exception:  # noqa: BLE001
            installed = []
        # 开发者模式会整组剔除冲突包。不取这份结论，右栏就会把「启动时根本
        # 不挂载」的包也标成「已启用」，与启动日志对不上。
        from ....core.ext import effective as effective_mod

        try:
            view = effective_mod.of(self.cfg, line)
        except Exception:  # noqa: BLE001 - 判定失败不该让列表渲染不出来
            view = None
        items = self._ordered_mods([i for i in installed if _on_line(i, line)])
        self._row_ids = [str(i.get("id") or "") for i in items]
        self.empty_title.setText("还没有接入扩展")
        self.empty_text.setText("把扩展包拖进这个窗口，或点下面的按钮选择文件")
        self.empty_hint.setText("扩展包只含 mod 内容，不含游戏本体")
        self.empty_hint.setVisible(True)
        self.empty_import_btn.setVisible(True)
        self._paint_empty_icon()

        lay = self.mods_holder_layout
        while lay.count() > 1:  # 末尾保留 stretch
            w = lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        for idx, item in enumerate(items, start=1):
            lay.insertWidget(lay.count() - 1, self._mod_row(idx, item, view))

        total = len(installed)
        enabled = sum(1 for i in items if i.get("enabled", True))
        dropped = (
            sum(1 for i in items if str(i.get("id") or "") in view.dropped_set)
            if view is not None
            else 0
        )
        if dropped:
            active = max(0, enabled - dropped)
            # 1080 宽窗口下这行只有约 300px，写全「共…项 · …线…项 · 启用…」
            # 会被裁掉尾部的剔除数，所以压缩成短式，完整说法放 tooltip。
            self.mods_count.setText(
                f"共 {total} · {line} 线 {len(items)} · 生效 {active} / 剔除 {dropped}"
            )
            self.mods_count.setToolTip(
                f"共 {total} 项＝两条版本线合计（含另一条线的 {total - len(items)} 项）；\n"
                f"当前 {line} 线 {len(items)} 项，其中启用 {enabled} 项。\n"
                f"开发者模式校验不通过会整组剔除：本线剔除 {dropped} 项后实际生效 {active} 项，"
                "被剔除的不挂载（文件保留，卸载/停用照常可用）。\n"
                "停用的扩展不挂载，但文件保留。"
            )
        else:
            self.mods_count.setText(f"共 {total} 项 · {line} 线 {len(items)} 项 · 启用 {enabled}")
            self.mods_count.setToolTip(
                f"共 {total} 项＝两条版本线合计（含另一条线的 {total - len(items)} 项）；\n"
                f"当前 {line} 线 {len(items)} 项，其中启用 {enabled} 项。\n"
                "停用的扩展不挂载，但文件保留。"
            )

        # 显隐跟着内容走，别让用不上的控件占位——
        # 空列表时只保留空状态块，整张卡不再被四个灰按钮顶出可视区。
        wipeable = self._has_wipeable(line, items)
        self.mods_area.setVisible(bool(items))
        self.mods_empty.setVisible(not items)
        self.sort_ops.setVisible(bool(items))
        self.wipe_ops.setVisible(wipeable)
        self.mods_count.setVisible(bool(items))

        # 运行期锁优先：运行中一律禁用，不因为「有选中项」而放开
        if self.game_running():
            for b in (self.mod_up_btn, self.mod_down_btn, self.mod_out_btn, self.mod_clear_btn):
                b.setEnabled(False)
                b.setToolTip(f"游戏正在运行（{self._lock_detail}），退出后才能操作")
        else:
            has_sel = bool(self._selected_mod_id())
            for b in (self.mod_up_btn, self.mod_down_btn, self.mod_out_btn):
                b.setEnabled(has_sel)
            self.mod_clear_btn.setEnabled(wipeable)
            self._restore_mod_tooltips()

    def _wipe_tooltip(self) -> str:
        """整批清理按钮的说明：按当前线说清删什么、什么时候该用。"""
        line = self.current_version()
        parts = [
            f"删除 {line} 线的全部扩展（真删文件，不是移出列表），"
            "并清理该线的插桩缓存与安装中间目录。"
        ]
        parts.append(
            "适用于大规模更新的情况下——比如整批扩展换了新版本、或刚升级了游戏线。"
        )
        return "\n".join(parts)

    def _restore_mod_tooltips(self) -> None:
        """解除运行期锁后把按钮说明还原（锁定期间会被统一改成「游戏正在运行」）。"""
        self.mod_up_btn.setToolTip("与上一行交换加载顺序（靠前的先挂载）")
        self.mod_down_btn.setToolTip("与下一行交换加载顺序")
        self.mod_out_btn.setToolTip("从当前列表隐藏，不删文件")
        self.mod_clear_btn.setToolTip(self._wipe_tooltip())

    def _mod_row(self, index: int, item: dict, view=None) -> QWidget:
        from PySide6.QtWidgets import QCheckBox

        ext_id = str(item.get("id") or "")
        row = QWidget()
        row.setObjectName("ModRow")
        h = QHBoxLayout(row)
        # 内边距 8,5 -> 10,8：条目放大后 5px 上下太紧，两行文字几乎贴边
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(10)

        no = QLabel(f"{index:02d}")
        no.setObjectName("ModIndex")
        no.setFixedWidth(24)
        h.addWidget(no)

        col = QVBoxLayout()
        col.setSpacing(2)
        name = QLabel(str(item.get("name") or ext_id))
        name.setObjectName("ModName")
        name.setToolTip(ext_id)
        # 扩展名可能很长（实测「诅咒徽章（武器上方新增第 12 个装备栏 + 试作徽章）」
        # 单行 364px），不换行会把整个滚动容器撑宽，出现横向滚动条、
        # 右侧「已启用」与开关被挤出可视区。这里允许换行并允许被压窄。
        name.setWordWrap(True)
        name.setMinimumWidth(0)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(name)

        author = str(item.get("author") or "").strip()
        ver = str(item.get("version") or "—")
        entries = int(item.get("entry_count") or 0)
        meta_text = f"v{ver} · {entries} 条"
        # 作者缺失要如实标注，而不是留空让人以为「没有作者」
        meta_text += f" · 作者 {author}" if author else " · 作者未标注"
        meta = QLabel(meta_text)
        meta.setObjectName("ModMeta")
        # 同上：元信息行也不能成为撑宽的源头
        meta.setWordWrap(True)
        meta.setMinimumWidth(0)
        meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if not author:
            meta.setToolTip(
                "该扩展包的 manifest.v2.json 里 author 为空。\n"
                "作者名由扩展包自带，启动器不会代填，也不会从包名或来源路径猜测。"
            )
        col.addWidget(meta)
        h.addLayout(col, 1)

        enabled = bool(item.get("enabled", True))
        dropped = bool(view is not None and enabled and ext_id in view.dropped_set)
        if not enabled:
            state_text, state_on = "已停用", "0"
        elif dropped:
            state_text, state_on = "已剔除", "2"
        else:
            state_text, state_on = "已启用", "1"
        state = QLabel(state_text)
        state.setObjectName("ModState")
        state.setProperty("on", state_on)
        if dropped:
            state.setToolTip(view.text_of(ext_id))
        h.addWidget(state)

        sw = QCheckBox()
        sw.setChecked(enabled)
        sw.setCursor(Qt.CursorShape.PointingHandCursor)
        if self.game_running():
            sw.setEnabled(False)
            sw.setToolTip(f"游戏正在运行（{self._lock_detail}），退出后才能切换")
        else:
            sw.setToolTip("停用后启动时不挂载它（文件保留，可随时启用）")
        sw.clicked.connect(lambda checked, i=ext_id: self._toggle_mod(i, checked))
        h.addWidget(sw)

        row.setProperty("ext_id", ext_id)
        row.mousePressEvent = lambda _e, i=ext_id: self._select_mod(i)  # type: ignore[assignment]
        return row

    def _selected_mod_id(self) -> str:
        return str(getattr(self, "_sel_mod", "") or "")

    def _select_mod(self, ext_id: str) -> None:
        self._sel_mod = ext_id
        for child in self.mods_area.widget().findChildren(QWidget):
            if child.objectName() == "ModRow":
                child.setProperty("sel", "1" if child.property("ext_id") == ext_id else "0")
                child.style().unpolish(child)
                child.style().polish(child)
        has = bool(ext_id)
        for b in (self.mod_up_btn, self.mod_down_btn, self.mod_out_btn):
            b.setEnabled(has)

    def _move_selected(self, delta: int) -> None:
        ext_id = self._selected_mod_id()
        if not ext_id or ext_id not in self._row_ids:
            return
        ids = list(self._row_ids)
        i = ids.index(ext_id)
        j = i + delta
        if not (0 <= j < len(ids)):
            return
        ids[i], ids[j] = ids[j], ids[i]
        # 未在当前线出现的扩展也要保留在总顺序里，否则换线后顺序会丢
        head = [x for x in self._order() if x not in ids]
        self._save_order(head + ids)
        self.refresh_mods()
        self._select_mod(ext_id)

    def _remove_selected_from_list(self) -> None:
        ext_id = self._selected_mod_id()
        if not ext_id:
            return
        answer = QMessageBox.question(
            self,
            "移出列表",
            f"把「{ext_id}」从当前列表移出？\n\n"
            "这只是调整显示顺序（从列表隐藏），不会删除已安装的扩展文件。\n"
            "要真正删除请到「扩展管理」页卸载。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._save_order([x for x in self._row_ids if x != ext_id])
        self._sel_mod = ""
        self.refresh_mods()

    def set_game_running(self, running: bool, detail: str = "") -> None:
        """游戏运行期间锁定右栏的扩展开关与排序按钮。

        与扩展管理页同一套规则：运行中的实例已把覆盖层挂进内存，改启用集合
        会让「当前实例」和「下次组装」不一致。

        行内开关的重绘**不立刻做**：``refresh_mods()`` 会销毁重建全部扩展行
        控件，而这个回调在游戏退出瞬间会被触发——哪怕玩家正停在别的页，
        重建的是完全看不见的控件。改成标脏，等页面切到时再刷。
        """
        self._game_running = bool(running)
        self._lock_detail = str(detail or "")
        if running:
            for btn in (self.mod_up_btn, self.mod_down_btn, self.mod_out_btn, self.mod_clear_btn):
                btn.setEnabled(False)
                btn.setToolTip(f"游戏正在运行（{self._lock_detail}），退出后才能操作")
        else:
            self._restore_mod_tooltips()
            has_sel = bool(self._selected_mod_id())
            for btn in (self.mod_up_btn, self.mod_down_btn, self.mod_out_btn):
                btn.setEnabled(has_sel)
            self.mod_clear_btn.setEnabled(
                self._has_wipeable(self.current_version(), self._row_ids)
            )
        # 行内的启用开关逐行重绘。
        # 页面可见就立刻刷（玩家正看着它），看不见就标脏，等 showEvent。
        if hasattr(self, "mods_area"):
            if self.isVisible():
                self.refresh_mods()
            else:
                self._rows_dirty = True

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """页面被切到时补上欠下的重建。"""
        super().showEvent(event)
        if getattr(self, "_rows_dirty", False):
            self._rows_dirty = False
            self.refresh_mods()

    def game_running(self) -> bool:
        return bool(getattr(self, "_game_running", False))

    def _toggle_mod(self, ext_id: str, enabled: bool) -> None:
        from ....core.ext.service import ExtensionService

        # 按线启停：游戏库里看到的永远是当前版本线，改的也只能是这条线。
        line = self.current_version()
        try:
            ExtensionService(self.cfg).set_enabled(ext_id, enabled, line=line)
        except Exception as exc:  # noqa: BLE001 - 含运行期锁
            QMessageBox.information(self, "无法修改启用状态", str(exc))
        self.refresh_mods()

    def _has_wipeable(self, line: str, mods) -> bool:
        """这条线上有没有可清的东西（扩展或剧情包）。

        只看扩展列表会漏掉「扩展删光了、剧情包还在」的情况——那时按钮必须
        还能按，否则用户得跑去剧情库一个个删。
        """
        if mods:
            return True
        from ....core import uninstall as uninstall_mod

        try:
            return bool(uninstall_mod.line_assets(self.cfg, line).stories)
        except Exception:  # noqa: BLE001 - 读注册表失败不该顺带把界面搞崩
            return False

    def _wipe_line_mods(self) -> None:
        """删除当前线的全部扩展并清理缓存。

        对应「大规模更新」的场景：整批扩展换新版本。
        一个个点「移出列表」太慢，这里一次清空——但删的是用户装的东西，
        所以确认框必须把**名字和体积**都摆出来，而不是只问「确定吗」。
        """
        from ....core import uninstall as uninstall_mod
        from ....core.ext import service as ext_service_mod

        line = self.current_version()
        if self.game_running():
            QMessageBox.information(
                self, "游戏正在运行", "请先结束游戏，再删除扩展——运行中删覆盖层会让游戏直接崩。"
            )
            return

        assets = uninstall_mod.line_assets(self.cfg, line)
        if not assets.extensions:
            QMessageBox.information(
                self,
                "删除所有扩展并清理缓存",
                f"{line} 线当前没有安装任何扩展，无需清理。",
            )
            return

        total = assets.extra_bytes
        body = [f"将删除 {line} 线的全部扩展（{len(assets.extensions)} 个）"]
        if assets.extensions:
            body.extend(f"· {a.label()}" for a in assets.extensions)
        body.extend(
            [
                "",
                f"合计 {uninstall_mod.human_size(total)}；"
                "同时清理该线的插桩缓存与安装中间目录。",
                "",
                "适用于大规模更新的情况下——比如整批扩展都换了新版本。",
                "删掉的是安装结果（覆盖层）；你手上的扩展包（zip / .jjm）不受影响，"
                "需要时重新拖进来即可。角色存档不受影响。",
                "",
                "此操作不可撤销，确认继续？",
            ]
        )
        answer = QMessageBox.warning(
            self,
            "删除所有扩展并清理缓存",
            "\n".join(body),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.logMessage.emit("已取消删除所有扩展", "dim")
            return

        failures: list[str] = []
        removed_ext: list[str] = []
        if assets.extensions:
            try:
                report = ext_service_mod.service(self.cfg).uninstall_line(
                    [a.id for a in assets.extensions], line
                )
                removed_ext = list(report.removed)
                failures.extend(report.warnings)
            except Exception as exc:  # noqa: BLE001 - 失败要如实告诉用户
                failures.append(f"扩展：{exc}")

        count, purged, purge_failures = uninstall_mod.purge_line_workdirs(line)
        failures.extend(purge_failures)

        self.logMessage.emit(
            f"{line} 线：已删除 {len(removed_ext)} 个扩展"
            + f"；清理缓存 {count} 项（{uninstall_mod.human_size(purged)}）",
            "ok" if not failures else "warn",
        )
        for item in failures:
            self.logMessage.emit(f"  未完成：{item}", "warn")

        self.refresh_mods()
        if hasattr(self, "modsChanged"):
            self.modsChanged.emit()
        QMessageBox.information(
            self,
            "完成",
            f"已删除 {len(removed_ext)} 个扩展"
            + f"，并清理 {uninstall_mod.human_size(purged)} 缓存。"
            + ("\n\n未完成项：\n" + "\n".join(failures) if failures else ""),
        )

    def _rebuild_cache(self) -> None:
        """立即重建当前线的插桩缓存，并把结果告诉用户（不只是提示「下次启动」）。"""
        import time

        from ....core.ext import enable as ext_enable

        line = self.current_version()
        removed = ext_enable._purge_instrumented_cache(line)
        started = time.time()
        self.logMessage.emit(f"开始重建 {line} 线插桩缓存（已清除旧缓存 {removed} 份）…", "info")
        try:
            run_dir = paths.BUILDS / f"_rebuild_{line.replace('.', '')}"
            result = ext_enable.build_for_launch(self.cfg, line, run_dir)
            built = [p for p in result.packs if p.get("role") == "instrumented"]
            elapsed = time.time() - started
            hook_count = len(result.hooks or [])
        except Exception as exc:  # noqa: BLE001 - 失败要如实告诉用户
            self.logMessage.emit(f"重建失败：{exc}", "err")
            QMessageBox.warning(self, "重建当前缓存", f"重建失败：\n{exc}")
            return
        if built:
            msg = (
                f"已重建 {line} 线插桩缓存。\n\n"
                f"插桩点 {hook_count} 个，用时 {elapsed:.1f} 秒。\n"
                f"下次启动会直接使用这份缓存。"
            )
            self.logMessage.emit(f"插桩缓存已重建：{hook_count} 个插桩点，{elapsed:.1f} 秒", "ok")
        else:
            msg = (
                f"没有生成插桩缓存：当前 {line} 线没有需要插桩的扩展（勾子数为 0）。\n\n"
                "这不代表出错——纯覆盖层扩展不需要插桩。"
            )
            self.logMessage.emit(f"{line} 线无需插桩（钩子 0 个）", "dim")
        QMessageBox.information(self, "重建当前缓存", msg)
        self.refresh_mods()

    # ------------------------------------------------------------ 头部动作

    def _import_game_exe(self) -> None:
        """导入游戏本体：先探测 → 展示结论 → 登记 → 可选冒烟/重建索引。

        旧流程只写一个 ``path``，而 ``baseline_exists`` 是按 ``size`` 判存在的，
        导入完立刻被判「文件缺失」。现在探测阶段就把 kind/size/hash/条目数
        一起算出来，登记时整组写入。
        """
        # 自定义线没有「基线」概念，导入本体对它无意义——先拦下来，省得白选一次文件
        from ....core import custom_lines as cl

        if cl.is_custom_id(self.current_version()):
            QMessageBox.information(
                self,
                "这是自定义改版",
                "当前选中的是自定义改版游戏线，它直接启动自己的 exe，不需要登记基线。\n\n"
                "要新增一条改版线，请点「管理游戏线」。",
            )
            return
        meta = next((m for m in _versions(self.cfg) if m["key"] == self.current_version()), _versions(self.cfg)[0])
        wants_pck = str(meta.get("kind") or "") == "pck"
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择游戏数据包（.pck）" if wants_pck else "选择游戏本体（exe 或 pck）",
            "",
            "游戏数据包 (*.pck);;所有文件 (*)"
            if wants_pck
            else "游戏本体 (*.exe *.pck);;所有文件 (*)",
        )
        if not path:
            return

        from ..workers import TaskWorker

        self.logMessage.emit(f"正在识别 {Path(path).name}（读条目表，约 1 秒）…", "info")
        worker = TaskWorker(baseline_ops.probe, Path(path), count_entries=True)
        self._probe_worker = worker  # 保住引用，否则线程对象会被 GC
        worker.done.connect(lambda probe: self._on_baseline_probed(probe, meta))
        worker.failed.connect(self._on_baseline_probe_failed)
        worker.start()

    def _on_baseline_probe_failed(self, message: str, traceback_text: str = "") -> None:
        self.logMessage.emit(f"识别失败：{message}", "err")
        QMessageBox.warning(
            self,
            "无法识别这个文件",
            f"{message}\n\n"
            "游戏本体应当是一个 .pck，或是尾部内嵌了 pck 的 exe。"
            "如果你选的是安装包/启动器，请先解压到游戏目录再选里面的 pck/exe。",
        )

    def _on_baseline_probed(self, probe, meta: dict) -> None:
        from PySide6.QtWidgets import QDialog

        from .baseline_import import ProbeDialog

        line = str(meta["key"])
        issues = baseline_ops.validate(probe, line)
        dialog = ProbeDialog(probe, issues, line, str(meta["title"]), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._adopt_baseline(
            probe, meta, smoke=dialog.smoke_enabled(), rebuild=dialog.rebuild_enabled()
        )

    def _adopt_baseline(self, probe, meta: dict, *, smoke: bool, rebuild: bool) -> None:
        import copy

        bid = str(meta["baseline"])
        snapshot = copy.deepcopy((self.cfg.get("baselines") or {}).get(bid))
        try:
            baseline_ops.adopt(self.cfg, bid, probe)
            cfg_mod.save(self.cfg)
        except Exception as exc:  # noqa: BLE001
            if snapshot is None:
                self.cfg.setdefault("baselines", {}).pop(bid, None)
            else:
                self.cfg.setdefault("baselines", {})[bid] = snapshot
            QMessageBox.warning(self, "保存失败", str(exc))
            return

        baseline_ops.invalidate_caches()
        self.logMessage.emit(
            f"已登记 {meta['title']} 基线：{probe.path.name}（{probe.entry_count:,} 条）", "ok"
        )
        self.refresh()
        # 基线变了 → 主窗口必须重算就绪度。否则仅有本页更新，侧栏仍显示
        # 导入前的「无法启动 / 基线文件不存在」，用户得重启才能启动。
        self.baselineChanged.emit()
        if not smoke and not rebuild:
            # 一个都没勾：登记是完成了，但校验没做 —— 此时**不能启动**。
            # 旧版本这里直接结束，于是「导入完就能开」；现在必须明确告诉
            # 用户还差一步，否则他会以为是启动器坏了。
            QMessageBox.information(
                self,
                "导入完成，待校验",
                f"基线已指向：\n{probe.path}\n\n"
                f"识别结果：{probe.kind} · pack_version={probe.pack_version} · "
                f"{probe.entry_count:,} 条。\n\n"
                "这一步只是登记，还没校验。**校验通过前不能启动游戏**——\n"
                "否则可能在包还没读完时就启动，导致游戏进程崩溃。\n\n"
                "点右上角「校验本体」跑一次冒烟即可解除。",
            )
            return
        self._post_import_tasks(probe, meta, snapshot, do_smoke=smoke, do_rebuild=rebuild)

    def _post_import_tasks(
        self,
        probe,
        meta: dict,
        snapshot,
        *,
        do_smoke: bool,
        do_rebuild: bool,
        rollback_on_fail: bool = True,
    ) -> None:
        """导入后的后台校验：先冒烟，再重建索引（顺序不能反）。

        ``rollback_on_fail`` 只在「导入」时为真 —— 导入失败要退回原基线，
        免得把玩家一个能用的本体换成一个不行的。从「校验本体」发起的
        重试则不回滚：他要的就是「这份文件到底行不行」的结论。
        """
        from ....core import smoke as smoke_mod
        from ..workers import TaskWorker

        line = str(meta["key"])

        def task() -> dict:
            out: dict = {"smoke": None, "index": None}
            if do_smoke:
                out["smoke"] = smoke_mod.run(line, cfg=self.cfg, timeout=300).as_dict()
            if do_rebuild:
                out["index"] = [r.as_dict() for r in baseline_ops.rebuild_index(line)]
            return out

        worker = TaskWorker(task)
        self._import_task_worker = worker  # 保住引用
        doing = "冒烟" + ("、重建索引" if do_rebuild else "")
        self.logMessage.emit(f"正在后台跑{doing}，请稍候（3 GB 约 30–180 秒）…", "info")
        worker.done.connect(
            lambda out: self._on_post_import(
                out, meta, snapshot, rollback_on_fail=rollback_on_fail
            )
        )
        worker.failed.connect(
            lambda msg, tb: self._on_post_import_failed(msg, meta, snapshot)
        )
        worker.start()

    def _on_post_import_failed(self, message: str, meta: dict, snapshot) -> None:
        # 后台任务整体崩了：不撤销登记（多半是索引/环境问题），只如实提示。
        # 但状态必须落到 failed —— 否则它停在 pending 上，用户会一直看到
        # 「待校验」却不知道发生了什么。
        #
        # 这里不回滚：崩溃原因可能是磁盘/权限，把玩家原本能用的基线换掉
        # 反而更糟。状态是 failed，启动按钮本来就是禁用的。
        bid = str(meta["baseline"])
        baseline_ops.mark_failed(
            self.cfg, bid, by=baseline_ops.VERIFY_BY_TASK, note=str(message)[:200]
        )
        try:
            cfg_mod.save(self.cfg)
        except OSError:
            pass
        self.baselineChanged.emit()
        self.logMessage.emit(f"导入后续校验失败：{message}", "err")
        QMessageBox.warning(
            self,
            "后续校验未完成",
            f"{message}\n\n基线已登记但**校验未通过，暂时不能启动**。\n"
            "可点「校验本体」重试，或到「诊断」页看具体原因。",
        )

    def _on_post_import(
        self, out: dict, meta: dict, snapshot, *, rollback_on_fail: bool = True
    ) -> None:
        smoke = out.get("smoke") or {}
        index = out.get("index") or []
        bid = str(meta["baseline"])
        lines: list[str] = []
        if smoke:
            if smoke.get("ok"):
                lines.append(
                    f"冒烟通过（{smoke.get('nodes', 0)} 个节点，"
                    f"{float(smoke.get('seconds') or 0):.0f} 秒）"
                )
            else:
                lines.append("冒烟未通过：" + "；".join(smoke.get("reasons") or ["未知原因"]))
        if index:
            bad = [r for r in index if not r.get("ok")]
            if bad:
                lines.append("索引重建失败：" + "；".join(bad[0].get("errors") or []))
            else:
                lines.append(f"索引已重建（{index[0].get('entry_count', 0):,} 条）")

        # 判定以「冒烟」为准：它是真的启动一次游戏并挂载所有包，
        # 比索引重建更能证明这份本体可用。只跑了索引时才退而用索引 ——
        # 索引要把 pck 完整读一遍并算 sha256，能发现截断/损坏，
        # 但发现不了「引擎版本对不上」这类问题。
        #
        # 冒烟通过而索引失败时**不判失败**：索引写不进多半是磁盘权限/
        # 空间问题，与这份本体能不能跑无关，不该因此拦住玩家。
        if smoke:
            passed = bool(smoke.get("ok"))
            by = baseline_ops.VERIFY_BY_SMOKE
        elif index:
            passed = all(r.get("ok") for r in index)
            by = baseline_ops.VERIFY_BY_INDEX
        else:
            passed = False
            by = baseline_ops.VERIFY_BY_SMOKE

        if not passed:
            if rollback_on_fail:
                # 导入时失败 → 回滚到导入前的基线配置，避免留一个开不了的基线
                if snapshot is None:
                    self.cfg.setdefault("baselines", {}).pop(bid, None)
                else:
                    self.cfg.setdefault("baselines", {})[bid] = snapshot
                try:
                    cfg_mod.save(self.cfg)
                except OSError:
                    pass
                baseline_ops.invalidate_caches()
                self.refresh()
                # 回滚也改了基线配置，侧栏结论同样要跟着回到旧值
                self.baselineChanged.emit()
                self.logMessage.emit("导入校验未通过，已回滚基线登记", "err")
                QMessageBox.warning(
                    self,
                    "校验未通过，已撤销本次导入",
                    "\n".join(lines) + "\n\n原基线配置已恢复。\n"
                    "常见原因：这不是该引擎线的完整游戏包、文件损坏，或缺少配套 pck/exe。",
                )
                return

            # 不是导入（是从「校验本体」重试）→ 不回滚，把状态钉成 failed，
            # 让界面一直显示「校验未通过」，玩家才知道是这份文件的问题。
            baseline_ops.mark_failed(
                self.cfg, bid, by=by, note="；".join(lines)[:200]
            )
            try:
                cfg_mod.save(self.cfg)
            except OSError:
                pass
            self.refresh()
            self.baselineChanged.emit()
            self.logMessage.emit("本体校验未通过：" + "；".join(lines), "err")
            QMessageBox.warning(
                self,
                "校验未通过",
                "\n".join(lines)
                + "\n\n这份本体暂时不能启动。请重新导入，或到「诊断」页看具体原因。",
            )
            return

        baseline_ops.mark_verified(
            self.cfg, bid, by=by, note="；".join(lines)[:200]
        )
        try:
            cfg_mod.save(self.cfg)
        except OSError:
            pass
        baseline_ops.invalidate_caches()

        self.refresh()
        # 校验/索引完成：索引和基线都落盘了，侧栏的「条目数」与可用性
        # 都要按最新状态重算（校验通过前侧栏可能还停在旧结论上）。
        self.baselineChanged.emit()
        self.logMessage.emit("；".join(lines) if lines else "校验通过", "ok")
        QMessageBox.information(self, "校验通过，可以启动", "\n".join(lines) or "校验通过。")


    def _verify_baseline(self) -> None:
        """对当前线已登记的本体补跑校验。

        存在的理由：导入时允许只登记不校验（3 GB 的包有人不想等），
        但那样状态是 ``pending``，**启动按钮是禁用的**。这个按钮就是
        解除那道门禁的唯一入口，所以它必须一直在——否则玩家取消勾选
        之后会永久卡在「待校验」。
        """
        from ....core import custom_lines as cl

        line = self.current_version()
        if cl.is_custom_id(line):
            QMessageBox.information(
                self, "这是自定义改版", "自定义线不登记基线，无需校验。"
            )
            return

        meta = next((m for m in _versions(self.cfg) if m["key"] == line), None)
        if meta is None:
            return
        bid = str(meta["baseline"])
        base = (self.cfg.get("baselines") or {}).get(bid) or {}
        if not str(base.get("path") or ""):
            QMessageBox.information(
                self,
                "还没有导入本体",
                "先点「导入游戏本体」把本机的游戏包登记进来，再点这里校验。",
            )
            return
        if self._import_task_worker is not None and self._import_task_worker.isRunning():
            QMessageBox.information(self, "正在校验", "上一次校验还没结束，请稍候。")
            return

        # 重试前先把状态拉回 pending：如果是 failed，直接重跑会让界面
        # 一直停在「校验未通过」上，看起来像点了没反应。
        baseline_ops.mark_pending(self.cfg, bid, note="重新校验中…")
        try:
            cfg_mod.save(self.cfg)
        except OSError:
            pass
        self.baselineChanged.emit()

        self._post_import_tasks(
            None, meta, None, do_smoke=True, do_rebuild=True, rollback_on_fail=False
        )

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "使用说明",
            "本启动器只提供扩展（mod）内容，以独立扩展包形式分发，不包含游戏本体。\n\n"
            "1. 先在「设置」里指定你本机的游戏基线，或点右上角「导入游戏本体」"
            "（4.6 选 exe 或 v3 pck；4.7 选 v4 pck）；\n"
            "2. 把扩展包（.zip / .jjm / .pack）拖进窗口即可安装；\n"
            "3. 在「扩展管理」里启停扩展，右侧列表可排序；\n"
            "4. 回到本页点「启动游戏」。\n\n"
            "存档说明：原版游戏直接启动时，角色存档在\n"
            "%APPDATA%\\Godot\\app_userdata\\机甲旋风 · 单机\\；\n"
            "启动器为了不覆盖它，用自己的工程名存在\n"
            "profiles\\<存档档位>\\appdata\\Godot\\app_userdata\\机甲旋风 · 兼容底座 <线>\\。\n"
            "换了启动器发现角色不见了，点左栏的「导入角色存档…」把原版那份复制进来即可。\n\n"
            "扩展包自带 manifest.v2.json，作者名取自该文件的 author 字段；"
            "包里没写就显示「作者未标注」。",
        )

    # ------------------------------------------------------------ 版本

    def _initial_version(self) -> str:
        """恢复上次选的线；已不存在的自定义线自动退回内置线。"""
        from ....core import custom_lines as cl

        saved = str((self.cfg.get("ui") or {}).get("last_version") or cfg_mod.BUILTIN_LINE)
        if any(v["key"] == saved for v in _versions(self.cfg)):
            return saved
        if cl.is_custom_id(saved) and cl.get_line(self.cfg, saved) is not None:
            return saved
        return cfg_mod.BUILTIN_LINE

    def current_version(self) -> str:
        data = self.selector.currentData()
        return str(data) if data else _versions(self.cfg)[0]["key"]

    def set_version(self, key: str, emit: bool = True) -> None:
        # 按 data 找而不是枚举 VERSIONS：自定义线的 id 不在 VERSIONS 里
        idx = self.selector.findData(key)
        if idx < 0:
            return
        # 这里屏蔽信号是为了避免 setCurrentIndex 触发一次 _on_selector_changed
        # 后再由下面重复发 versionChanged。但屏蔽之后，_on_selector_changed
        # 里做的「重绘扩展列表」也不会跑——所以必须在这里显式补一次，
        # 否则切换版本时右边列表还停在上一条线的那几项（实测踩过）。
        blocked = self.selector.blockSignals(True)
        self.selector.setCurrentIndex(idx)
        self.selector.blockSignals(blocked)
        self._apply_detail(key)
        self.refresh_mods()
        # 换版本线 = 换 user:// 目录（内置线与自定义线的工程名不同），
        # 存档路径必须跟着刷新，否则界面上写的是上一条线的路径。
        self._update_path_hint()
        if emit:
            self.versionChanged.emit(key)

    def _on_selector_changed(self, _idx: int) -> None:
        key = self.current_version()
        self._apply_detail(key)
        self.refresh_mods()
        self._update_path_hint()
        self.versionChanged.emit(key)

    def _apply_detail(self, key: str) -> None:
        from ....core import custom_lines as cl

        # 自定义改版走单独一条展示路径：没有引擎版本、没有基线资源、没有扩展
        if cl.is_custom_id(key):
            # 自定义线不登记基线，也就没有校验状态；按钮必须跟着藏起来，
            # 否则玩家会点到一个对他毫无意义的入口。
            self.verify_btn.setVisible(False)
            self._apply_custom_detail(key)
            return

        meta = next((m for m in _versions(self.cfg) if m["key"] == key), _versions(self.cfg)[0])
        self.detail_badge.setText(meta["badge"])
        self.ver_title.setText(meta["title"])
        self.detail_title.setText(f"已关联 {meta['key']} · {meta['badge']}")
        baseline = cfg_mod.baseline(self.cfg, meta["baseline"]) if self._has_baseline(meta) else {}
        count = int(baseline.get("entry_count") or 0)
        # 文案要控制在一行内：这里换行会把左栏撑高，还容易在「不解包不复制」
        # 这种词组中间断开，看起来像排版坏了。资源条数已经能说明问题，
        # 「整包直挂」的细节挪进 tooltip。
        verify_state = baseline_ops.verify_state(self.cfg, str(meta.get("baseline") or ""))
        blocked = verify_state in baseline_ops.BLOCKING_VERIFY_STATES
        sub = (
            f"{meta['engine']}　·　原版资源 {count:,} 条"
            if count
            else f"{meta['engine']}　·　原版资源包"
        )
        # 校验状态直接写在副标题上：玩家看到「无法启动」时的第一反应是
        # 「是不是没导入本体」，把状态摆在这里能省掉一轮追问。
        if blocked:
            sub += f"　·　{baseline_ops.VERIFY_STATE_LABELS.get(verify_state, verify_state)}"
        self.detail_sub.setText(sub)
        if blocked:
            self.detail_sub.setToolTip(
                "本金体的校验状态："
                + baseline_ops.verify_summary(self.cfg, str(meta.get("baseline") or ""))
                + "\n校验通过前不能启动。点右上角「校验本体」补跑。"
            )
        else:
            self.detail_sub.setToolTip("整包直挂，不解包、不复制到其他目录")
        self._sync_verify_button(meta, baseline)

        layout = self.tag_row.layout()
        for pill in self.tags:
            layout.removeWidget(pill)  # type: ignore[union-attr]
            pill.setParent(None)
            pill.deleteLater()
        self.tags.clear()
        from ....core import categories as cat_mod

        cat_name = cat_mod.name_of(
            self.cfg, cat_mod.category_of_baseline(self.cfg, meta["baseline"])
        )
        tag_items = [(cat_name, "info")] + self._tag_items(meta)
        shown = tag_items[:4]
        for text, tone in shown:
            pill = Pill(text, tone)
            layout.addWidget(pill)  # type: ignore[union-attr]
            self.tags.append(pill)
        if len(tag_items) > len(shown):
            rest = "、".join(t for t, _ in tag_items[len(shown):])
            more = Pill(f"+{len(tag_items) - len(shown)}", "muted")
            more.setToolTip(rest)
            layout.addWidget(more)  # type: ignore[union-attr]
            self.tags.append(more)

        self._refresh_story_box(meta)

    def _sync_verify_button(self, meta: dict, baseline: dict) -> None:
        """「校验本体」按钮的可见性与文案。

        只在「已登记本体 + 状态是 pending/failed」时出现：

        - 没导入本体 → 该点的是「导入游戏本体」，校验无从谈起；
        - 已校验通过 → 没有可做的事，留着只会让人以为还有一步没完成。
        """
        bid = str(meta.get("baseline") or "")
        state = baseline_ops.verify_state(self.cfg, bid)
        show = bool(baseline.get("path")) and state in baseline_ops.BLOCKING_VERIFY_STATES
        self.verify_btn.setVisible(show)
        if not show:
            self.verify_btn.setText("校验本体")
            return
        label = baseline_ops.VERIFY_STATE_LABELS.get(state, state)
        self.verify_btn.setText(f"校验本体（{label}）")
        self.verify_btn.setToolTip(
            baseline_ops.verify_summary(self.cfg, bid)
            + "\n点这里跑一次挂载冒烟；通过后才能启动。"
        )

    def _has_baseline(self, meta: dict) -> bool:
        try:
            cfg_mod.baseline(self.cfg, meta["baseline"])
            return True
        except KeyError:
            return False

    # ------------------------------------------------------------ 4.7 剧情覆盖层

    def _story_target(self, meta: dict) -> tuple[str, Path | None]:
        """当前线剧情覆盖层的（配置里的相对路径, 解析后的绝对路径）。"""
        base = (self.cfg.get("baselines") or {}).get(str(meta.get("baseline") or "")) or {}
        raw = str(base.get("story_overlay") or "")
        if not raw:
            return "", None
        return raw, cfg_mod.resolve_path(raw)

    def _story_entry_count(self, path: Path) -> int:
        """剧情包条目数（按 路径+mtime+大小 缓存，避免每次切版本都重读目录）。"""
        try:
            st = path.stat()
        except OSError:
            return 0
        key = (path.as_posix(), st.st_mtime_ns, st.st_size)
        cache = getattr(self, "_story_cache", None)
        if cache and cache[:3] == key:
            return int(cache[3])
        try:
            from ....core.pck import PckReader

            count = len(PckReader.open(path).entries)
        except Exception:  # noqa: BLE001 - 读不了就显示 0，不拦界面
            count = 0
        self._story_cache = (*key, count)
        return count

    def _refresh_story_box(self, meta: dict) -> None:
        raw, path = self._story_target(meta)
        if not raw:
            self.story_box.setVisible(False)
            return
        self.story_box.setVisible(True)
        self.story_path.setText(raw)
        if path is None or not path.is_file():
            self.story_path.setToolTip("")
            self.story_info.setText("文件缺失：点「替换剧情包…」重新选择")
            self.story_pill.setText("缺失")
            self.story_pill.set_tone("err")
            return
        self.story_path.setToolTip(path.as_posix())
        size_mb = path.stat().st_size / 1024 / 1024
        count = self._story_entry_count(path)
        self.story_info.setText(f"已启用 · {size_mb:.1f} MB · {count} 条目")
        self.story_info.setToolTip(
            "启动 4.7 时自动挂载，顺序是 baseline → story → mod，"
            "所以剧情包不会盖掉 mod 的覆盖层。"
        )
        self.story_pill.setText("已启用")
        self.story_pill.set_tone("ok")

    def _open_story_dir(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        meta = next((m for m in _versions(self.cfg) if m["key"] == self.current_version()), None)
        if meta is None:
            return
        _raw, path = self._story_target(meta)
        if path is None:
            QMessageBox.information(self, "剧情覆盖层", "当前版本没有登记剧情覆盖层。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path.parent.as_posix()))

    def _pick_story(self) -> None:
        """选一个新的 .pck 覆盖剧情层（旧文件备份为 .bak）。"""
        import shutil

        meta = next((m for m in _versions(self.cfg) if m["key"] == self.current_version()), None)
        if meta is None:
            return
        raw, target = self._story_target(meta)
        if not raw or target is None:
            QMessageBox.information(self, "剧情覆盖层", "当前版本没有登记剧情覆盖层。")
            return

        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择剧情数据包（.pck）",
            str(target.parent),
            "数据包 (*.pck);;所有文件 (*)",
        )
        if not chosen:
            return
        src = Path(chosen)
        if src.resolve() == target.resolve():
            self.logMessage.emit("选择的剧情包就是当前文件，无需替换", "dim")
            return

        # 只做最必要的校验：magic + pack_version。剧情包不是完整本体，
        # 不能用 baseline_ops.probe（那套要求 38 万条目 + 工程文件）。
        try:
            from ....core.pck import PckReader

            reader = PckReader.open(src)
            entries = len(reader.entries)
            version = int(reader.header.pack_version)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "不是有效的数据包", f"读取失败：{exc}")
            return
        if version != 4:
            QMessageBox.warning(
                self,
                "不是 4.7 剧情包",
                f"这个包是 pack_version {version}；4.7 线只接受 v4 的 pck。",
            )
            return
        if entries <= 0:
            QMessageBox.warning(self, "数据包为空", "这个 pck 里没有条目，不能作为剧情层。")
            return

        size_mb = src.stat().st_size / 1024 / 1024
        answer = QMessageBox.question(
            self,
            "替换 4.7 剧情覆盖层",
            f"将用：\n{src}\n\n覆盖：\n{target}\n\n"
            f"新包 {size_mb:.1f} MB / {entries} 条目。旧文件会备份为 story.pck.bak。\n\n"
            "替换后，4.7 线上已安装扩展的覆盖层可能需要重装"
            "（补丁锚点是按旧剧情源码算的）。\n\n"
            "确认替换？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                bak = target.with_name(target.name + ".bak")
                try:
                    if bak.exists():
                        bak.unlink()
                    target.replace(bak)
                except OSError:
                    pass
            shutil.copy2(src, target)
        except OSError as exc:
            QMessageBox.critical(self, "替换失败", f"写入失败：{exc}")
            return

        self._story_cache = None
        self._refresh_story_box(meta)
        self.logMessage.emit(
            f"剧情覆盖层已替换：{src.name}（{entries} 条目）", "ok"
        )

    def _apply_custom_detail(self, key: str) -> None:
        """自定义改版的详情：只讲「它是什么、能不能启动」，不假装有扩展支持。"""
        from ....core import custom_lines as cl

        # 自定义线没有「基线/剧情层」概念
        self.story_box.setVisible(False)

        line = cl.get_line(self.cfg, key)
        if line is None:
            self.detail_badge.setText("?")
            self.ver_title.setText("自定义改版已移除")
            self.detail_title.setText("请重新选择版本线")
            self.detail_sub.setText("这条线已被移除")
            self._set_tags([("需重新选择", "err")])
            return

        self.detail_badge.setText("包" if line.is_pck else "改")
        self.ver_title.setText(line.name)
        self.detail_title.setText(
            "自定义资源包 · 底座挂载" if line.is_pck else "自定义改版 · 独立启动"
        )
        self.detail_sub.setText(line.file_name)
        self.detail_sub.setToolTip(line.path)
        from ....core import categories as cat_mod
        from ....core import custom_lines as cl

        cat_name = cat_mod.name_of(
            self.cfg, cat_mod.resolve(self.cfg, line.category or cat_mod.MOD_ID)
        )
        tags: list[tuple[str, str]] = [(cat_name, "info")]
        tags.append(("文件就绪", "ok") if line.exists else ("文件缺失", "err"))
        if line.is_pck:
            tags.append((f"{line.engine_line or '?'} 底座", "muted"))
        # 接入分档由闸门 + line.json 一致性校验决定，不再一律写「不加载扩展」
        tier = getattr(line, "access_tier", "") or ""
        if tier == cl.TIER_FULL:
            tags.append(("可协商扩展", "ok"))
        elif tier == cl.TIER_PROBE_ONLY:
            tags.append(("仅启动", "muted"))
        else:
            tags.append(("未验证", "muted"))
        if line.note:
            tags.append((line.note[:12], "info"))
        self._set_tags(tags)

    def _set_tags(self, items: list[tuple[str, str]]) -> None:
        """统一重建状态标签（内置线与自定义线共用）。"""
        layout = self.tag_row.layout()
        for pill in self.tags:
            layout.removeWidget(pill)  # type: ignore[union-attr]
            pill.setParent(None)
            pill.deleteLater()
        self.tags.clear()
        shown = items[:4]
        for text, tone in shown:
            pill = Pill(text, tone)
            layout.addWidget(pill)  # type: ignore[union-attr]
            self.tags.append(pill)
        if len(items) > len(shown):
            rest = "、".join(t for t, _ in items[len(shown):])
            more = Pill(f"+{len(items) - len(shown)}", "muted")
            more.setToolTip(rest)
            layout.addWidget(more)  # type: ignore[union-attr]
            self.tags.append(more)

    def _tag_items(self, meta: dict) -> list[tuple[str, str]]:
        tags: list[tuple[str, str]] = []
        state, _note = cfg_mod.baseline_health(self.cfg, meta["baseline"])
        if state == "ok":
            tags.append(("基线就绪", "ok"))
        elif state == "size_mismatch":
            # 文件在、版本换了：仍可尝试启动，但必须让人看见，不能显示成「就绪」
            tags.append(("大小不符", "warn"))
        else:
            tags.append(("基线缺失", "err"))
        suffix = meta["baseline"].split(".v")[-1]
        tags.append(
            ("索引已建", "ok") if (paths.BASELINE / f"index-{suffix}.json.gz").exists() else ("缺索引", "warn")
        )
        try:
            exe, pck = runner.host_paths(meta["key"])
            tags.append(("运行时就绪", "ok") if exe.exists() and pck.exists() else ("运行时缺失", "err"))
        except Exception:  # noqa: BLE001
            tags.append(("运行时缺失", "err"))
        try:
            from ....core.ext.service import ExtensionService

            installed = ExtensionService(self.cfg).installed()
            line = str(meta["key"])
            # 按 lines 判定，不再看顶层 baseline 镜像：同一扩展装了两条线时，
            # 顶层字段只保留「最近一次安装」的那条，会把另一条线算漏。
            usable = [
                i
                for i in installed
                if line in (i.get("lines") or {})
                or (not i.get("lines") and str(i.get("engine_line") or "") == line)
            ]
            tags.append((f"扩展 {len(usable)}", "info" if usable else "muted"))
        except Exception:  # noqa: BLE001
            pass
        return tags

    # ------------------------------------------------------------ 状态

    def _fill_profiles(self) -> None:
        # 填充期间会触发 currentTextChanged；这里必须挡住，否则会把
        # 列表里的第一项当成「用户的选择」写进配置，把玩家的档位改掉。
        self._filling_profiles = True
        self.profile_box.clear()
        base = paths.PROFILES
        names: list[str] = []
        if base.exists():
            names = sorted(p.name for p in base.iterdir() if p.is_dir())
        default = (self.cfg.get("profiles", {}) or {}).get("default", "default")
        if default not in names:
            names.insert(0, default)
        self.profile_box.addItems(names)
        idx = self.profile_box.findText(default)
        if idx >= 0:
            self.profile_box.setCurrentIndex(idx)
        self._filling_profiles = False

    def _on_profile_changed(self, name: str) -> None:
        """切换存档档位：写回配置，下次启动沿用。

        以前只改下拉框、不写 ``profiles.default``：本次启动用新档位，重启后
        又跳回旧档位——玩家看到的就是「刚导入的存档又没了」。
        """
        name = (name or "").strip()
        if name and not getattr(self, "_filling_profiles", False):
            if (self.cfg.get("profiles") or {}).get("default") != name:
                self.cfg.setdefault("profiles", {})["default"] = name
                try:
                    cfg_mod.save(self.cfg)
                except OSError:
                    pass
                self.logMessage.emit(f"已切换存档档位：{name}（下次启动沿用）", "info")
        self._update_path_hint()

    def current_profile(self) -> str:
        return self.profile_box.currentText() or "default"

    def refresh(self) -> None:
        cur = self.current_version()
        self._fill_selector()
        self.set_version(cur, emit=False)
        self._apply_detail(self.current_version())
        self.refresh_mods()

    def set_state(self, state: str, detail: str = "") -> None:
        mapping = {
            "starting": ("启动中", "warn", "正在挂载资源包…"),
            "running": ("运行中", "ok", detail or "游戏已启动"),
            "stopping": ("结束中", "warn", "等待进程退出…"),
            "exited": ("空闲", "muted", detail or "游戏已退出"),
        }
        label, tone, text = mapping.get(state, ("空闲", "muted", detail))
        self.state_pill.setText(label)
        self.state_pill.set_tone(tone)
        self.state_text.setText(text)
        self.stop_btn.setEnabled(state in ("starting", "running"))
        active = state in ("starting", "running", "stopping")
        # 状态切回空闲也不能无视主窗口的裁决（导入中 / 校验未通过 / 槽位满）
        self.launch_btn.setEnabled(not active and bool(self._readiness_ok))
        self.launch_btn.setText("运行中…" if active else "启动游戏")
        self.launch_btn.setToolTip(
            "" if (not active and self._readiness_ok) else self._readiness_reason
        )

    def set_launch_readiness(self, ok: bool, reason: str) -> None:
        """由主窗口统一裁决启动按钮（空闲时才生效，运行中仍保持禁用）。"""
        self._readiness_ok = bool(ok)
        self._readiness_reason = str(reason or "")
        if self.state_pill.text() in ("空闲", "已退出", "详情"):
            self.launch_btn.setEnabled(bool(ok))
            self.launch_btn.setToolTip("" if ok else self._readiness_reason)

    def set_instance_state(self, slot: int, state: str, info: str = "") -> None:
        # 玩家版没有实例面板，主窗口仍会按槽位广播状态，这里静默丢弃。
        if self.instance_panel is None:
            return
        self.instance_panel.set_state(slot, state, info)

    def _show_details(self) -> None:
        from ....core import custom_lines as cl

        key = self.current_version()
        if cl.is_custom_id(key):
            line = cl.get_line(self.cfg, key)
            self.state_pill.setText("详情")
            self.state_pill.set_tone("info")
            if line is None:
                self.state_text.setText("这条自定义改版已被移除")
                return
            size = 0
            try:
                size = line.exe_path.stat().st_size if line.exists else 0
            except OSError:
                size = 0
            self.state_text.setText(
                f"{line.name}｜{line.exe}｜{size:,} 字节｜自定义改版（不加载扩展）"
            )
            return
        meta = next((m for m in _versions(self.cfg) if m["key"] == key), None)
        if not meta:
            return
        baseline = cfg_mod.baseline(self.cfg, meta["baseline"])
        path = Path(baseline["path"])
        size = path.stat().st_size if path.exists() else 0
        self.state_pill.setText("详情")
        self.state_pill.set_tone("info")
        self.state_text.setText(
            f"{baseline.get('label', '')}｜{path}｜{size:,} 字节｜条目 {baseline.get('entry_count', 0):,}"
        )

    # ------------------------------------------------------------ 快捷入口

    def _update_path_hint(self) -> None:
        """刷新路径显示：左栏两条完整路径 + 快捷入口的短说明。"""
        from ....core import custom_lines as cl

        self._refresh_save_paths()
        profile = self.current_profile()
        key = self.current_version()
        if cl.is_custom_id(key):
            self.path_hint.setText(
                f"当前档位 {profile}　·　自定义改版：存档由该改版自己的工程名决定，不经过启动器"
            )
            return
        self.path_hint.setText(
            f"当前档位 {profile}　·　角色存档 appdata/{userdir.GODOT_VENDOR}/app_userdata/"
            f"{userdir.base_project_name(key)}/offline_roles.json"
        )

    def _open_profile_dir(self) -> None:
        """打开**真实**存档目录。

        以前打开的是 ``profiles/<档位>``——那里面只有 ``appdata``/``.history``，
        玩家点进去看不到自己的角色文件，反而以为「存档没了」。
        """
        from ....core import custom_lines as cl

        key = self.current_version()
        if cl.is_custom_id(key):
            self._open(paths.profile_dir(self.current_profile()))
            return
        self._open(userdir.launcher_dir(self.current_profile(), key, self.cfg))

    def _open_log_dir(self) -> None:
        self._open(paths.LOGS)

    def _open_root_dir(self) -> None:
        self._open(paths.ROOT)

    def _open(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))