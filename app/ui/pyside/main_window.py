"""主窗口：侧栏（含左下启动按钮）+ 页面栈 + 扩展状态栏 + 日志抽屉 + 拖拽安装。

整体布局参考 PCL 启动器（左窄右宽、页头在内容区顶部、日志收在底部），
控件与绘制全部自写。
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ... import edition as _edition
from ...core import config as cfg_mod
from ...core import paths, readiness as readiness_mod, runner
from ...core.errors import LOG_HINT
from ...core.logging import current_log_file
from . import creator_asset
from . import features, icons, mecha_assets, theme
from .assets import chevron
from .dnd import drop_paths
from .mecha_shell import (
    MechaBrandBar,
    MechaLaunchButton,
    MechaNavButton,
    MechaRoot,
    MechaStatusCapsule,
)
from .pages.build import BuildPage
from .pages.diagnostics import DiagnosticsPage
from .pages.extensions import ExtensionsPage
from .pages.hub import HubView
from .pages.library import VERSIONS, LibraryPage
from .pages.minigames import MinigamesPage
from .pages.settings import SettingsPage
from .widgets import ExtStatusBar, LogView, NavButton, error_dialog
from .workers import GameProcess, InstallWorker

#: 日志区高度策略（像素）
#: - ``LOG_MIN_H``：拖动时的最小高度。必须 >= 抽屉的自然最小高度（实测 122px：
#:   标题行 26px 按钮 + 上下内边距 + 间距）
#: - ``LOG_DEFAULT_H``：用户没拖过、且空间不够时的兜底高度（折叠态另算）
#: - ``LOG_COLLAPSED_H``：折叠态高度，只露标题行
#: - ``LOG_AUTO_H``：空间富裕时（窗口高 >= 900）展开到这个高度
#:
#: 设计取舍：默认**收起**。原因是页面内容本身就高（游戏库实测需 765px），
#: 760 高的窗口扣掉固定日志区后只剩 421px，页面必须滚动才能看全——
#: 用户反馈的「内容显示不全」有一半来自这里，而不是控件宽度。
#: 日志是排障时才需要看的东西，默认收起、需要时拖出来或点展开更合理。
LOG_MIN_H = 122
LOG_DEFAULT_H = 220
LOG_COLLAPSED_H = 40
LOG_AUTO_H = 220
LOG_MAX_RATIO = 0.45  # 日志最多占内容区高度的比例
LOG_PAGE_MIN_H = 300  # 页面区至少保留的高度，避免日志把内容挤没
#: 窗口高度达到这个值才自动展开日志区，否则默认折叠
LOG_AUTO_EXPAND_MIN_H = 860
#: 页面内容「装得下」所需的高度门槛：页面需要 666px 时，
#: 窗口至少 900 才够；低于此值只折叠日志还不够，页面仍需滚动，
#: 因此展开日志要以「页面仍能完整显示」为前提（见 _init_log_size_once）
LOG_PAGE_FULL_H = 672

NAV_ITEMS = [
    ("library", "游戏库", "library"),
    ("extensions", "扩展管理", "puzzle"),
    ("minigames", "小游戏", "gamepad"),
    ("build", "打包导出", "box"),
    ("diagnostics", "诊断", "pulse"),
    ("settings", "设置", "gear"),
]


#: 纯净模式下隐藏的导航项（作者/排障向）
PURE_MODE_HIDDEN = ("build", "diagnostics")


def _icon_candidates() -> list[Path]:
    """``launcher.ico`` 可能出现的位置，按优先级排。

    打包后 PyInstaller 把它放在 ``_internal/app/``（即 ``sys._MEIPASS`` 下）；
    开发态就是项目根的 ``app/``。
    """
    out: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        out.append(Path(meipass) / "app" / "launcher.ico")
    out.append(Path(__file__).resolve().parents[3] / "app" / "launcher.ico")
    return out


def _app_icon() -> QIcon:
    """启动器图标：统一取 ``app/launcher.ico``。

    以前这里是代码现画的蓝底白三角，而 exe 内嵌的是青色旋风、任务栏又各
    取自一套，三处图标根本不是同一个。现在图标源只有 ``app/launcher.png``
    一份，由 ``tools/make_icon.py`` 生成 ico，exe 内嵌与窗口/任务栏都用它。
    """
    for cand in _icon_candidates():
        if not cand.is_file():
            continue
        icon = QIcon(str(cand))
        if not icon.isNull():
            return icon
    return _fallback_icon()


def _fallback_icon() -> QIcon:
    """图标文件缺失时的兜底，保证窗口不会顶着一个空白图标。"""
    px = QPixmap(128, 128)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#3d8bfd"))
    p.drawRoundedRect(4, 4, 120, 120, 26, 26)
    p.end()
    return QIcon(px)


def _stable(value):
    """把配置里的嵌套结构转成**顺序稳定**的可哈希表示。

    字典的迭代顺序不保证稳定，直接 ``repr()`` 同一个配置两次可能得到
    不同字符串，缓存就永远命中不了。这里按 key 排序后重建，
    列表保持原序（顺序本身有意义）。
    """
    if isinstance(value, dict):
        return tuple(sorted((str(k), _stable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_stable(v) for v in value)
    if isinstance(value, set):
        return tuple(sorted(str(v) for v in value))
    return value


def _tint_titlebar(window) -> bool:
    """把 Windows 原生标题栏染成主题紫，返回是否至少成功设了一项。

    深色界面上顶着一条系统浅灰标题栏是明显的破绽，但换掉整条标题栏意味着
    自己实现最小化/最大化/关闭、拖动、贴边分屏，成本和风险都不划算。
    ``DwmSetWindowAttribute`` 能在保留原生行为的前提下改色：

    - 属性 20 / 19：沉浸式深色模式（Win11 用 20，Win10 1809+ 用 19）
    - 属性 35：标题栏底色（仅 Win11，COLORREF 即 0x00BBGGRR）
    - 属性 36：标题文字色（仅 Win11）
    - 属性 34：窗口描边色（仅 Win11）

    成功与否只影响外观，不影响功能，所以失败一律静默降级。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = int(window.winId())
        dwm = ctypes.windll.dwmapi
    except Exception:
        return False

    def set_attr(attr: int, value: int) -> bool:
        try:
            raw = ctypes.c_int(value)
            return (
                dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), ctypes.c_uint(attr),
                    ctypes.byref(raw), ctypes.sizeof(raw),
                )
                == 0
            )
        except Exception:
            return False

    def colorref(hex_color: str) -> int:
        h = hex_color.lstrip("#")
        return int(h[4:6] + h[2:4] + h[0:2], 16)

    ok = False
    # 先开深色模式（老系统只认 19），再刷具体颜色（只有 Win11 支持）
    for attr in (20, 19):
        if set_attr(attr, 1):
            ok = True
            break
    if set_attr(35, colorref(theme.DARK.sidebar)):
        ok = True
    if set_attr(36, colorref(theme.DARK.text)):
        ok = True
    if set_attr(34, colorref(theme.DARK.border)):
        ok = True
    return ok


class RootWidget(MechaRoot):
    """窗口根容器 —— 背景绘制全部交给 :class:`MechaRoot`。

    机甲风改版：内置立绘 + 从左到右的渐变暗罩（左侧能读文字、右侧
    几乎不遮）。保留这个类名是为了让 ``apply_background`` / 自检脚本
    里所有 ``RootWidget`` 引用都不用改。

    用户设了自定义背景图时，MechaRoot 会完全沿用旧的「等比铺满 +
    整片压黑」逻辑，设置页那个功能不会被破坏。
    """

    # 继承 MechaRoot 全部行为，不覆盖任何方法。


class MainWindow(QMainWindow):
    def __init__(self, cfg: dict) -> None:
        super().__init__()
        self.cfg = cfg
        #: 实例槽位 -> 进程；slot 1 是主实例（兼容原有 self.proc 用法）
        self.instances: dict[int, GameProcess] = {}
        self.instance_meta: dict[int, dict] = {}
        self.instance_slots = features.max_instances(cfg)
        self.proc = self._ensure_instance(1)
        self.mini_proc = GameProcess(self)
        self.mini_proc.line.connect(self.log)
        self._current_line = ""
        self._install_worker: InstallWorker | None = None
        #: 批量安装队列。忙碌时不再「拒绝」，而是追加到这里——过去
        #: `isRunning()` 直接把第二包挡掉，只在日志里留一句警告，这就是
        #: 「一次拖多个只装一个」的另一半原因。
        #: 每项是 ``(包路径, 目标引擎线)``：入队时就锁定目标线，
        #: 之后用户切版本不会改变已排队任务的落点。
        self._install_queue: list[tuple[Path, str]] = []
        #: 当前批次的目标线（跟随主页所选版本），仅用于日志与收尾提示。
        self._install_target_line = ""
        #: 兼容性预检后「本包是否允许部分兼容」的一次性开关。
        #: 由 :meth:`_precheck_line_compat` 置位，取用后立即复位。
        self._allow_partial_next = False
        #: 所有还在跑的 worker 都要留引用，否则 QThread 被回收会直接崩。
        self._install_workers: list = []
        self._install_active = False
        self._install_index = 0
        self._install_failed: list[dict] = []
        self._install_last_message = ""
        #: 日志抽屉折叠态。用 splitter 尺寸表达，拖动与折叠共用同一套机制。
        self._log_collapsed = False
        #: 折叠前记下的日志高度，展开时还原（不依赖 splitter 的历史尺寸）
        self._log_expanded_h = LOG_DEFAULT_H
        self._logger = logging.getLogger("ui")

        #: 「游戏是否在跑」的状态缓存，供 _poll_mod_lock 比对。
        self._mod_lock_state = False
        #: 就绪度缓存：``(线, 配置修订号) -> Readiness``。
        #: 一次状态变化内只算一次 evaluate()——退出回调链里以前会算两遍。
        self._readiness_cache: dict[tuple[str, str], object] = {}
        #: 配置修订号缓存。配置一变就重算，使就绪度缓存自动失效。
        self._cfg_rev_cache: str | None = None
        #: 定时兜底：每 3 秒核一次进程表。
        #:
        #: 以前 _poll_mod_lock 只是个没人挂载的死方法——进程被外部杀掉
        #: （任务管理器强杀、游戏自己崩）时，界面状态不会更新，扩展按钮
        #: 会一直灰着、一直提示「游戏正在运行」，直到重启启动器。
        #:
        #: 挂定时器的前提是查询足够快：现在走 Toolhelp32 原生快照（毫秒级），
        #: 旧实现用 tasklist 全表扫描要几百毫秒，每 3 秒卡一次界面更难受。
        self._mod_lock_timer = QTimer(self)
        self._mod_lock_timer.timeout.connect(self._poll_mod_lock)
        self._mod_lock_timer.start(3000)

        # 产品名统一走 edition.app_name()（2026-11-08 改名「新起点启动器」）。
        # 别在这里写死字符串 —— 改名时漏一处就会出现「标题叫新名字、
        # 任务管理器里还是旧名字」这种半改状态。
        self.setWindowTitle(_edition.app_name())
        self.setWindowIcon(_app_icon())
        # 默认尺寸按**屏幕可用区**算，不再写死 1180x760。
        #
        # 2026-11-09 用户反馈「默认大小还是有点太大」：他的屏幕是
        # 1536x864（125% 缩放），1180x760 占了近 3/4，桌面被压得很满。
        #
        # 取屏幕短边的 62%，再夹在 900~1180 之间：
        #   · 1536x864  → 0.62*864 = 536，被下限抬到 900x600
        #   · 1920x1080 → 0.62*1080 = 670，抬到 900x600
        #   · 2560x1440 → 893，仍是 900x600
        #   · 4K        → 收在上限 1180x760
        # 宽高比固定 1180:760（≈1.55），保证立绘不变形。
        #
        # 为什么不用固定 900x600：大屏上会显小得可怜。按短边比例算，
        # 小屏不挤、大屏不空。
        avail = self.screen().availableGeometry() if self.screen() else None
        MIN_W, MIN_H = 940, 560
        if avail is not None and avail.height() > 0:
            # 高度按屏幕短边 62%，夹在 560..720。
            # 为什么上限收到 720 而不是 760：用户 1536x864 屏上 760 太满，
            # 720 是他截图里那种「留出桌面」的观感。
            h = max(MIN_H, min(720, int(avail.height() * 0.62)))
            w = int(h * 1180 / 760)
            # 宽度不能低于最小宽度，否则 setMinimumSize 会反向把窗口撑宽，
            # 与高度不再成比例（实测 931x600 会被强制拉成 1020x600）。
            w = max(MIN_W, min(1180, w))
            # 屏幕确实放不下时（超小屏），以屏幕为准而不是硬撑出去
            if avail.width() > 0:
                w = min(w, avail.width())
                h = min(h, avail.height())
            self.resize(w, h)
        else:
            self.resize(1180, 760)

        # 最小尺寸。
        #
        # **2026-11-14 下调到 940x560**（原 1060x600）。原因有两个：
        #
        # 1. 低配机器很常见 1366x768 屏，而且开着 125%/150% 缩放。系统
        #    「可用区」这时只有约 1093x614（125%）或 911x512（150%）——
        #    原下限 1060x600 在 150% 缩放下**比可用区还大**，Qt 会强行
        #    把窗口画到屏幕外，右下角的启动按钮直接看不见。玩家反馈的
        #    「按钮点不到」有一部分就是这个。
        # 2. 940x560 正好是游戏本体的视口尺寸（``project.godot`` 里
        #    ``viewport_width/height``），窗口不应比游戏画面还大一圈。
        #
        # 原来 1060 的依据是「游戏库页左表格 + 右『已接入扩展』并排需要
        # 1060 才不出横向滚动条」。那个页面已改成右侧可折叠（宽度不够时
        # 自动收起扩展栏），所以下限可以降。其余五页在 940 宽下均无横向
        # 滚动条（逐 10px 实测到 900）。
        self.setMinimumSize(MIN_W, MIN_H)
        self.setAcceptDrops(True)

        self.root = RootWidget()
        self.setCentralWidget(self.root)
        # 机甲风外壳：外层纵向（内容区 + 横跨全宽的橙色导航条）。
        # 内容区仍是原来的横向骨架（左栏 + 右侧页面/日志/状态），
        # 所以 576 行往后所有业务方法引用的控件层级都不变。
        outer = QVBoxLayout(self.root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())

        right = QWidget()
        right.setObjectName("ContentHost")
        right.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.right = right  # 状态面板隐藏/召唤时要切它的可见性
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.stack.setObjectName("PageStack")
        self.stack.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self.stack.setAutoFillBackground(False)
        self.pages: dict[str, QWidget] = {}
        self.library = LibraryPage(cfg)
        self.library.launchRequested.connect(self._launch)
        self.library.stopRequested.connect(self._stop)
        self.library.versionChanged.connect(self._on_version_changed)
        self.library.instanceStartRequested.connect(self._start_instance)
        self.library.instanceStopRequested.connect(self._stop_instance)
        # 游戏库页没有日志控件，缓存清理/重建的进度转发到主窗口日志抽屉
        self.library.logMessage.connect(self.log)
        # 管理游戏线：对话框由主窗口持有，改完让游戏库刷新下拉
        self.library.manageLinesRequested.connect(self._manage_custom_lines)
        self.library.manageCategoriesRequested.connect(self._manage_categories)
        # 库页右栏空状态里的「导入扩展包」：借扩展管理页的安装流程，
        # 不在这里另起一套进度/结论 UI（两套必然走形）
        self.library.importModsRequested.connect(self._open_mod_import)
        # 库页里也有会改安装结果的操作（「删除所有扩展并清理缓存」），
        # 改完要重算就绪度与启动按钮，否则按钮还停在「启动 9 个扩展」
        self.library.modsChanged.connect(self._on_mods_changed)
        # 导入/回滚游戏本体后必须重算就绪度：库页只重绘自己，侧栏的
        # 「无法启动」与启动按钮由本窗口裁决，不连这条就表现为「导入成功
        # 但启动不了，得关掉重启」。
        self.library.baselineChanged.connect(self._on_baseline_changed)
        # 换存档档位后底栏那行「当前版本 · 存档 xxx」要跟着变，
        # 否则界面显示的还是旧档位，玩家会以为切换没生效。
        self.library.profile_box.currentTextChanged.connect(
            lambda _t: self._sync_launch_button()
        )
        self.extensions = None  # 占位，下面立即创建
        self.extensions = ExtensionsPage(cfg, self.log, install_handler=self.install_extension)
        # 构造即同步一次：用户不动版本下拉就点「整体校验」时，也得按界面上
        # 实际选中的线跑，而不是配置里上次的记忆。
        self.extensions.set_current_line(self.library.current_version())
        # 扩展增删/启停后要重算启动可用性，否则按钮状态会停留在旧结论上
        self.extensions.modsChanged.connect(self._on_mods_changed)
        self.build_page = BuildPage(cfg, self.log)
        self.diagnostics = DiagnosticsPage(cfg, self.log)
        self.minigames = MinigamesPage(cfg, self.log, self.mini_proc, self.library.current_profile)
        self.settings = SettingsPage(
            cfg,
            self.log,
            on_saved=self._on_cfg_saved,
            on_background_changed=self.apply_background,
            on_data_changed=self._on_mods_changed,
        )

        # 每个页面外面包一层滚动区。
        # 之前是直接把页面塞进 QStackedWidget：窗口高度不够时 Qt 会硬压控件
        # （实测 LibraryPage 需要 657px 却只给到 504px，设置页的 PathRow 甚至被压到 5px），
        # 表现就是文字被裁、控件重叠看不全。改成滚动后，装不下就滚动，不再压缩。
        self.page_bodies: dict[str, QWidget] = {}
        for key, widget in (
            ("library", self.library),
            ("extensions", self.extensions),
            ("minigames", self.minigames),
            ("build", self.build_page),
            ("diagnostics", self.diagnostics),
            ("settings", self.settings),
        ):
            wrapper = QWidget()
            wrapper.setObjectName("PageBody")
            # QScrollArea.setWidget() 会把容器设成 autoFillBackground=True，
            # 让它用调色板 Window 色整片填充。背景由 RootWidget 自绘
            # （纯色或用户自定义背景图），容器必须不参与绘制才能透出来，
            # 否则就是一大片跟主题无关的底色。
            wrapper.setAutoFillBackground(False)
            lay = QVBoxLayout(wrapper)
            lay.setContentsMargins(24, 20, 24, 14)
            lay.addWidget(widget)
            # 把「布局需要的尺寸」变成 wrapper 的最小尺寸，
            # QScrollArea 才知道什么时候该出滚动条。
            lay.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setWidget(wrapper)
            # 纵向优先滚动；横向只在内容确实放不下时才出现
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

            self.pages[key] = area
            self.page_bodies[key] = widget
            self.stack.addWidget(area)

        # 内容区：HUB 视图 ⇄ 功能页栈。
        #
        # 2026-11-09 用户要求「默认打开就是 HUB，点游戏库才进详情页」。
        # 原来的实现是隐藏整个 ``right``（右侧全空），左侧栏单独占满窗口；
        # 那样窗口右半边是一片背景，看着像没加载完。
        #
        # 现在改成 ``QStackedWidget`` 双视图：
        #   index 0 = 功能页（split：页面栈 + 日志抽屉）
        #   index 1 = HUB 视图（低密度信息卡）
        # 隐藏态显示 HUB，点导航自动切回功能页。
        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("ContentStack")
        # QStackedWidget 默认会用调色板 Window 色填充，等于在右半区
        # 铺一层不透明深蓝、把背景立绘切掉（2026-11-09 用户反馈的
        # 「左栏边界那条竖线」就是它从 x=300 开始的硬边）。
        # QSS 里也加了 ``QStackedWidget#ContentStack { transparent }``，
        # 这里再设一次属性，双保险。
        self.content_stack.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self.content_stack.setAutoFillBackground(False)

        self.hub_view = HubView()
        self.content_stack.addWidget(self._build_log_drawer_shell())

        self.content_stack.addWidget(self.hub_view)
        right_layout.addWidget(self.content_stack, 1)
        self.ext_status = ExtStatusBar()
        right_layout.addWidget(self.ext_status)
        # 存成属性：HUB 态要把这两条收起来（它们是功能页的附属信息，
        # 留在 HUB 上会破坏「只有立绘 + 启动按钮」的观感）
        self._footer = self._build_footer()
        right_layout.addWidget(self._footer)
        body.addWidget(right, 1)
        outer.addLayout(body, 1)
        outer.addWidget(self._build_navbar())

        # 初始视图：默认进 HUB（``ui.status_panel_hidden`` 默认 True）。
        # 先 _switch 把导航选中态建好，再按配置决定是否收起到 HUB ——
        # 顺序不能反：_switch 内部会自动展开功能页。
        self._switch("library")
        self._apply_panel_visibility()
        self._sync_launch_button()
        self.library.refresh()
        self.diagnostics.refresh()
        self.apply_background()
        self._apply_panel_visibility()
        self.log("启动器已就绪。选好版本点左侧「启动」，或把扩展包直接拖进来安装。", "ok")

    # ------------------------------------------------------------ 侧栏

    def _build_brand_card(self) -> QWidget:
        """创作者标识卡片：圆形头像 + 名字 + 创作者徽章 + 版本行。

        文字一律走 QLabel，由字体渲染，所以放大、换缩放比例都不会糊。
        """
        card = QFrame()
        card.setObjectName("BrandCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(9, 8, 9, 8)
        row.setSpacing(10)

        avatar = QLabel()
        avatar.setObjectName("BrandAvatar")
        avatar.setFixedSize(46, 46)
        # 源像素 64 > 目标物理像素（100% 下 44、125% 下 55），永远是缩小，不会糊
        avatar.setScaledContents(True)
        avatar.setPixmap(creator_asset.avatar_pixmap(64))
        row.addWidget(avatar, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(5)
        name = QLabel("柳江巷")
        name.setObjectName("BrandName")
        head.addWidget(name, 0, Qt.AlignmentFlag.AlignVCenter)
        badge = QLabel("创作者")
        badge.setObjectName("BrandBadge")
        head.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        col.addLayout(head)

        meta = QLabel("V1.45.1 · 兼容启动器")
        meta.setObjectName("BrandMeta")
        col.addWidget(meta)

        proto = QLabel("协议 1 · 接口 0.1.4")
        proto.setObjectName("BrandMeta")
        col.addWidget(proto)

        row.addLayout(col, 1)
        return card

    def _build_sidebar(self) -> QWidget:
        """机甲风左栏：LOGO + 标题 + 创作者胶囊 + 圆形启动按钮 + 状态。

        **业务接口一个不少**（576 行往后全靠它们）：

        * ``self.side_launch`` —— 仍是 QPushButton 子类，setText / setEnabled /
          setToolTip 照常；只是绘制换成圆形机甲按钮
        * ``self.side_hint`` / ``self.side_status`` —— 仍是 QLabel，
          ``_sync_launch_button`` 往里写文字不受影响
        * ``self.nav_buttons`` / ``self.nav_group`` —— 保留，但按钮移到
          底部横向导航条（由 ``_build_footer`` 构建）后填充

        为什么把导航移走：示意图是底部横排橙色按钮，左栏只承担启动。
        但 ``nav_buttons`` 的填充时机必须留在 ``__init__`` 里 —— 所以
        这里只建容器，按钮由 ``_build_navbar`` 挂进去，字典引用不变。
        """
        from .mecha_shell import MechaCreatorChip

        side = QWidget()
        side.setObjectName("MechaSide")
        side.setFixedWidth(300)
        # **不要整栏半透明底板**（2026-11-09 用户指出「不要有这种黑影重叠」）。
        #
        # 原来铺了一层 rgba(6,16,32,0.42) 的深蓝，本意是让文字压在机甲手臂上
        # 也读得清。实际效果是一条 300px 宽的暗色方块盖在立绘上，
        # 右边缘与背景交界处有一道明显竖线，像「黑影重叠」。
        #
        # 现在改成**完全透明**：左栏的文字/LOGO/按钮各自带底色或描边，
        # 直接浮在背景图上。可读性靠元素自身保证（标题有深色描边、
        # 按钮有实心底、提示文字压在暗部区域），不靠整栏压暗。
        side.setStyleSheet("QWidget#MechaSide { background: transparent; }")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(20, 12, 12, 12)
        lay.setSpacing(0)

        # LOGO（位图缩小，保持比例）
        logo = QLabel()
        logo.setObjectName("MechaLogo")
        logo.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        px = mecha_assets.scaled("logo", 250)
        if not px.isNull():
            logo.setPixmap(px)
            logo.setFixedSize(px.width(), px.height())
        lay.addWidget(logo, 0, Qt.AlignmentFlag.AlignLeft)

        # 标题 + 创作者胶囊
        title = QLabel(_edition.app_name())
        title.setObjectName("MechaTitle")
        title.setStyleSheet(
            "color:#EBF6FF; font-size:20px; font-weight:700; background:transparent;"
        )
        lay.addSpacing(2)
        lay.addWidget(title, 0, Qt.AlignmentFlag.AlignLeft)

        lay.addSpacing(6)
        lay.addWidget(MechaCreatorChip(), 0, Qt.AlignmentFlag.AlignLeft)

        # 副标题（版本 / 指纹）
        self.side_meta = QLabel("机甲旋风 1.45 · 原版")
        self.side_meta.setObjectName("MechaMeta")
        self.side_meta.setStyleSheet(
            "color:#648CB4; font-size:11px; background:transparent;"
        )
        lay.addSpacing(8)
        lay.addWidget(self.side_meta, 0, Qt.AlignmentFlag.AlignLeft)

        lay.addStretch(1)

        # 圆形启动按钮（业务方法照常 setText / setEnabled）
        self.side_launch = MechaLaunchButton()
        self.side_launch.setToolTip("")
        self.side_launch.clicked.connect(
            lambda: self._launch(self.library.current_version())
        )
        lay.addWidget(self.side_launch, 0, Qt.AlignmentFlag.AlignHCenter)

        self.side_hint = QLabel("")
        self.side_hint.setObjectName("BrandSub")
        # 左栏整块暗罩撤掉后（2026-11-09 用户要求「不影响背景图展示」），
        # 这行小字会压在亮色立绘上、看不清。改成**只给文字加窄圆角衬底**，
        # 不再拉一整块暗色矩形 —— 视觉上是一个小胶囊，不影响立绘。
        self.side_hint.setStyleSheet(
            "color:#C8DCF0; font-size:11px;"
            " background:rgba(6,16,32,0.62);"
            " border-radius:7px; padding:3px 10px;"
        )
        self.side_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.side_hint.setWordWrap(True)
        lay.addSpacing(6)
        lay.addWidget(self.side_hint)

        self.side_status = QLabel("● 就绪")
        self.side_status.setObjectName("BrandSub")
        self.side_status.setStyleSheet(
            "color:#4CD696; font-size:11px;"
            " background:rgba(6,16,32,0.62);"
            " border-radius:7px; padding:2px 9px;"
        )
        self.side_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(2)
        lay.addWidget(self.side_status)
        lay.addStretch(1)
        return side

    # ------------------------------------------------------------ 版本联动

    def _on_mods_changed(self) -> None:
        """扩展列表/启停/升级后重算可用性并同步库页标签。"""
        self.library.refresh()
        self._sync_launch_button()

    def _on_baseline_changed(self) -> None:
        """游戏本体登记/回滚后的联动刷新。

        与 :meth:`_on_mods_changed` 的区别：基线变更可能**不改变配置内容**
        （例如冒烟失败回滚成与原来完全相同的快照），此时 ``_cfg_revision()``
        算出的修订号不变、就绪度缓存不会自动失效，必须显式清一次。
        同时库页的「已关联 · 条目数」与诊断页的基线状态也要跟着走。
        """
        self.invalidate_readiness()
        self.library.refresh()
        self.diagnostics.refresh()
        self._sync_launch_button()

    def _on_version_changed(self, key: str) -> None:
        self.cfg.setdefault("ui", {})["last_version"] = key
        try:
            cfg_mod.save(self.cfg)
        except OSError:
            pass
        # 扩展页的「整体校验」要按界面上选中的线跑，不能只信配置记忆。
        self.extensions.set_current_line(key)
        self._sync_launch_button()
        # **切版本后要重算扩展统计**：4.6 和 4.7 各自装了多少个扩展是不同的，
        # 胶囊/HUB 上的数字必须跟着变。不加这一句的话，切到 4.7 仍显示
        # 4.6 的数量（2026-11-10 用户反馈「右下角数字重复计算」的成因之一）。
        self._refresh_hub()
        self.log(f"已切换到 {key}", "dim")

    def _readiness(self) -> "readiness_mod.Readiness":
        """拿当前线的就绪度，**同一状态变化内只算一次**。

        ``readiness.evaluate()`` 要读 19 份 manifest + 做 O(n²) 的重叠判定，
        本身就不便宜。以前一次状态变化会调它两遍（``_on_instance_state``
        一次、``_on_instance_finished`` 又一次），退出瞬间两遍叠在一起，
        正好是「关游戏卡顿」的第二个原因。

        这里按 ``(线, 配置修订号)`` 缓存。配置修订号取配置文件内容的哈希——
        配置一变（改了扩展启停、装了新包）就自动失效，不用手动清缓存。
        """
        key = self.library.current_version()
        rev = self._cfg_revision()
        cached = self._readiness_cache.get((key, rev))
        if cached is None:
            cached = readiness_mod.evaluate(self.cfg, key)
            self._readiness_cache = {(key, rev): cached}
        return cached

    def _cfg_revision(self) -> str:
        """配置的修订号——内容变了就换，用来让 readiness 缓存失效。

        直接对配置**递归**序列化后算哈希。只排顶层键是不够的：扩展启停
        存在嵌套字典里，只看顶层就检测不到变化，缓存会一直用旧的就绪度。

        代价极小（配置是几十项的字典），换来「任何地方改了配置都会
        自动让就绪度缓存失效」——不用去各个页面找该在哪里手动清缓存。
        """
        try:
            import hashlib

            blob = repr(_stable(self.cfg)).encode("utf-8")
            return hashlib.sha1(blob).hexdigest()[:12]
        except Exception:  # noqa: BLE001 - 算不出就退化成「每次都算」
            return ""

    def invalidate_readiness(self) -> None:
        """配置变了就清掉就绪度缓存（内部按修订号自动失效，这里留个显式入口）。"""
        self._readiness_cache.clear()

    def _sync_launch_button(self) -> None:
        """按钮可用性与提示全部由「就绪度 + 空闲槽位」决定。

        以前只看进程是否在跑，于是扩展冲突/基线缺失时按钮照样可点、
        点了没反应（BUG-1）。现在不可启动时直接禁用并写明原因。

        导入扩展期间**一律禁用**：这段窗口里 packs/ 的覆盖层还在写、
        注册表还没落盘，启动只会读到半装状态。判定放在最前面，连
        ``readiness.evaluate`` 都不必跑（导入时它的结论也未必准）。
        """
        if self._install_active:
            self.side_launch.setEnabled(False)
            self.side_launch.setText("导入中…")
            self.side_launch.setToolTip("正在导入扩展包，导入完成后才能启动游戏")
            self.side_hint.setText("导入扩展包中 · 完成后可启动")
            self.side_hint.setToolTip("")
            self.side_status.setText("● 导入中")
            self.library.set_launch_readiness(False, "正在导入扩展包，请稍候再启动")
            return

        key = self.library.current_version()
        meta = next((v for v in VERSIONS if v["key"] == key), None)
        name = meta["title"].replace("机甲旋风 ", "") if meta else key
        t0 = time.perf_counter()
        readiness = self._readiness()
        self._log_timing("readiness.evaluate", (time.perf_counter() - t0) * 1000)
        free = self._free_slot() is not None

        if not readiness.ok:
            self.side_launch.setEnabled(False)
            self.side_launch.setText("无法启动")
            self.side_launch.setToolTip(readiness.detail())
            self.side_hint.setText(readiness.reason())
            self.side_status.setText("● 不可启动")
        elif not free:
            self.side_launch.setEnabled(False)
            self.side_launch.setText("运行中…")
            self.side_launch.setToolTip(self._full_hint(long=True))
            self.side_hint.setText(self._full_hint(long=False))
        else:
            self.side_launch.setEnabled(True)
            dropped = len(
                {
                    str(pid)
                    for item in readiness.dropped
                    for pid in (item.get("ids") or [])
                    if str(pid)
                }
            )
            # 按钮只有 ~230px，「启动 4.6 · 原版（剔除 13 个）」实测被裁成
            # 「（剔除 1」。剔除数量改到下一行的 side_hint，按钮保持原样。
            self.side_launch.setText(f"启动 {name}")
            if dropped:
                self.side_launch.setToolTip(
                    "开发者模式将强制剔除：\n"
                    + "\n".join(f"· {line}" for line in readiness.drop_lines())
                )
            else:
                self.side_launch.setToolTip("")
            running = sum(1 for p in self.instances.values() if p.running)
            hint = f"当前版本 {key}　·　存档 {self.library.current_profile()}"
            if running:
                hint += f"　·　已开 {running}/{self.instance_slots}"
            if dropped:
                # 左栏只有 ~230px，写全「开发者模式将剔除 13 个，剩 5 个可启动」
                # 会被裁成「…剔除 1」。短句 + 明细进 tooltip。
                hint += f"　·　剔除 {dropped} 个"
                self.side_hint.setToolTip(
                    readiness.drop_summary()
                    + "\n"
                    + "\n".join(f"· {x}" for x in readiness.drop_lines())
                )
            else:
                self.side_hint.setToolTip("")
            self.side_hint.setText(hint)
            if not running:
                self.side_status.setText("● 就绪")

        # 游戏库页那个「启动」按钮同步受控，两处结论必须一致
        self.library.set_launch_readiness(readiness.ok and free, readiness.reason())

    # ------------------------------------------------------------ 背景

    def apply_background(self) -> None:
        ui = self.cfg.get("ui") or {}
        path = str(ui.get("background_image") or "")
        dim = int(ui.get("background_dim") or 40)
        ok = self.root.set_background(path, dim)
        app = QApplication.instance()
        if app is not None:
            # 机甲风：内置立绘也算「有背景」，让卡片走半透明分支，
            # 否则一整块不透明深蓝会把立绘盖掉。
            translucent = self.root.has_background()
            app.setPalette(theme.qpalette(theme.DARK))
            app.setStyleSheet(
                theme.qss(
                    theme.DARK,
                    chevron(theme.DARK.text_dim),
                    translucent=translucent,
                )
            )
        if path and not ok:
            self.log(f"背景图加载失败：{path}", "warn")
        self._sync_launch_button()

    # ------------------------------------------------------------ 拖拽安装

    def _drop_paths(self, event) -> list[Path]:
        """一次拖入的所有扩展包（保序去重、过滤后缀）。"""
        return drop_paths(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        found = self._drop_paths(event)
        if found:
            event.acceptProposedAction()
            label = f"{len(found)} 个包" if len(found) > 1 else found[0].name
            self.ext_status.warn(f"松手即安装：{label}")
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._drop_paths(event):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.ext_status.warn("已取消拖入")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        found = self._drop_paths(event)
        if not found:
            event.ignore()
            return
        event.acceptProposedAction()
        self.install_extension(found)

    # ------------------------------------------------------------ 扩展安装

    def _confirm_import(self, count: int, line: str) -> bool:
        """导入前的知情确认：说清「要等」与「这段时间不能启动游戏」。

        为什么必须弹：导入不是纯读写小文件——它要重写 ``packs/<id>/overlay-*.pck``、
        改注册表，合集包还要逐个装完再跑一次引擎校验。过去这一整段期间
        「启动」按钮照常可点，玩家在导入中途开游戏，读到的就是装了一半的
        状态（覆盖层还在写、注册表还没落盘），轻则少挂一个扩展，重则起不来。

        文案按用户 2026-11-17 的明确要求写死这句话，别再改写：
        「这可能需要一些时间进行导入，在此期间无法启动游戏。」

        返回 False 表示用户取消，调用方直接放弃本批（不排队、不占状态）。
        """
        from ...core import custom_lines as cl

        if cl.is_custom_id(line):
            # 自定义改版没有扩展体系，走不到这里；保留分支只为不误弹。
            return True

        running = sum(1 for p in self.instances.values() if p.running)
        if running:
            # 反方向也必须拦：游戏读着覆盖层时改写它，比「导入期间启动」更危险
            # （正在跑的进程已经把 pck 句柄握住了，替换文件不会生效，玩家会
            #  以为「装了没反应」）。让玩家先退出，再导入。
            QMessageBox.information(
                self,
                "游戏正在运行",
                f"游戏正在运行（已开 {running} 个实例），此时不能导入扩展。\n\n"
                "请先退出游戏，等进程完全结束后再导入。",
            )
            return False

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("导入扩展包")
        box.setText("这可能需要一些时间进行导入，在此期间无法启动游戏。")
        detail = (
            f"即将导入 {count} 个包到 {line} 线。\n\n"
            "导入期间启动按钮会禁用；导入完成后自动恢复，届时再启动游戏即可。"
        )
        if count > 1:
            detail += "\n\n拖入多个包或合集包时会逐个安装，耗时比单个包更长。"
        box.setInformativeText(detail)
        start_btn = box.addButton("开始导入", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        # 默认落在「取消」：误按回车就把一批包装进去，代价比多点一次大
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        return box.clickedButton() is start_btn

    def install_extension(self, package) -> None:
        """安装入口：单个包、多个包都走这里。

        忙碌时**入队**而不是拒绝：过去用 ``isRunning()`` 直接挡掉第二个包，
        用户拖 5 个只会静默装 1 个。现在排队位置会写进日志与状态栏，
        用户能看见「还有 3 个在等」。

        安装目标是**主页当前选中的引擎线**。过去这里不传 ``target_line``，
        后端按清单自己声明的基线推断，于是「主页面在 A 线、扩展却装进了 B 线」
        ——装完在 A 线上根本挂不上。
        队列按「包 + 目标线」成对存，中途切版本不会污染已在排队的任务。
        """
        packages = [package] if isinstance(package, (str, Path)) else list(package or [])
        packages = [Path(p) for p in packages if p]
        if not packages:
            return
        line = self.library.current_version()
        # 知情确认排在入队之前：用户取消时队列与状态一个字节都不该动
        if not self._confirm_import(len(packages), line):
            self.log(f"已取消导入（{len(packages)} 个包）", "dim")
            self.ext_status.warn("已取消导入")
            return
        self._install_target_line = line
        self._install_queue.extend((pkg, line) for pkg in packages)

        if self._install_active:
            waiting = len(self._install_queue) - len(packages)
            self.log(f"已加入安装队列：{len(packages)} 个包，前面还有 {waiting} 个", "dim")
            self.ext_status.warn(
                f"已排队 {len(self._install_queue)} 个包，当前任务完成后自动开始"
            )
            return

        self._switch("extensions")
        self._install_active = True
        self._install_index = 0
        self._install_failed = []
        # 立刻把启动按钮钉成「导入中…」：不要等第一个进度回调，
        # 那之间隔着一次基线预热（0.5～1 秒），正好是用户最容易手快点启动的窗口。
        self._sync_launch_button()
        self.log(f"安装目标版本：{line}（跟随主页当前选择的版本）", "dim")
        self._warm_baseline_index()
        self._start_next_install_guarded()

    def _start_next_install_guarded(self) -> None:
        """推进安装队列，异常时把批次状态复位。

        没有这层的后果很具体：任何一步抛异常（配置坏了、磁盘满、QThread 起
        不来），``_install_active`` 会一直是 True，启动按钮永远停在
        「导入中…」，玩家只能重启启动器才能再开游戏。
        QTimer 那条路径（装完一个自动起下一个）尤其容易被漏掉。
        """
        try:
            self._start_next_install()
        except Exception as exc:  # noqa: BLE001
            self._install_active = False
            self._sync_launch_button()
            self.log(f"导入任务启动失败：{exc}", "err", detail=traceback.format_exc())
            self.log(f"出错环节：启动导入任务 · 日志文件：{current_log_file()}", "dim")

    def _warm_baseline_index(self) -> None:
        """在主线程预热基线索引，把冷加载从安装线程里挪走。

        为什么非要放在主线程：``path_set()`` 冷启动要解压 15MB 的
        ``index-1.45.json.gz`` 再解析 38 万条路径，主线程实测 0.5 秒。
        同一个调用放到 QThread 里，主线程的进度动画、日志刷新会持续抢
        GIL，实测被放大到 75 秒以上（约 160 倍），表现就是「拖进第一个
        包之后卡住不动」。预热后 worker 里直接命中 ``lru_cache``，接近零
        成本。代价是点下安装后主线程会顿 0.5～1 秒，比卡几分钟划算得多。
        """
        from ...core import baseline as baseline_mod

        # 两条线都要预热。原先只预 4.6，4.7 玩家第一次拖包进来时
        # 仍要吃那一次 38 万条索引解析（实测主线程 0.5 秒、worker 里更久），
        # 表现就是「拖进去之后卡住」。
        for line in (cfg_mod.known_lines(self.cfg) or (cfg_mod.BUILTIN_LINE,)):
            t0 = time.time()
            try:
                baseline_mod.path_set(line)
            except FileNotFoundError:
                # 没有该线索引是正常情况（只装了单线），不必打扰用户
                continue
            except Exception as exc:  # noqa: BLE001
                self.log(f"预加载 {line} 基线索引失败：{exc}", "warn")
                continue
            cost = time.time() - t0
            if cost > 0.2:
                self.log(f"已预加载 {line} 基线索引（{cost:.1f}s）", "dim")

    def _precheck_line_compat(self, package: Path, line: str) -> bool:
        """安装前按目标线做一次静态兼容性预检。返回 False 表示放弃这个包。

        为什么需要这一步：安装目标线现在跟随主页所选版本，用户很自然会在
        另一条线下拖入一个原本只声明内置线的扩展。严格模式（``allow_partial``
        关闭）只要有一条补丁没命中就整体失败，而失败信息埋在日志里。
        这里先算一遍告诉用户「能装 / 只能部分装 / 装不了」，并给出选择。

        探测只读、不写盘，毫秒到百毫秒级，可以放在主线程。
        """
        try:
            from ...core.ext import compat

            probe = compat.probe(package, line, self.cfg)
        except Exception as exc:  # noqa: BLE001 - 探测失败不该拦住安装
            self.log(f"兼容性预检跳过（{exc}）", "dim")
            return True

        if probe.sources_missing:
            # 这一条比后面十几条「没有这个文件」有用得多：它同时解释了原因
            # 和办法（换带源码的本体 / 配反解工程）。
            self.log(
                f"{line} 本体不含 .gd 源码（只有 .gdc 字节码），补丁类扩展无法应用；"
                f"请换带源码的 {line} 本体，或在设置里指定该线反解工程。",
                "err",
            )

        if probe.verdict == compat.COMPATIBLE or probe.verdict == compat.UNKNOWN:
            return True

        mode = cfg_mod.normalize_mode(cfg_mod.extension_mode(self.cfg))

        if probe.verdict == compat.INCOMPATIBLE:
            detail = "；".join(probe.failures[:3]) or "目标线里找不到补丁锚点"
            self.log(
                f"{package.name} 与 {line} 不兼容（补丁命中 "
                f"{probe.patch_ok}/{probe.patch_total}）：{detail}",
                "err",
            )
            if cfg_mod.is_strict(mode):
                # 完全校验：不弹技术弹窗，直接剔除 —— 确定性优先，
                # 不提供「强制安装」这个口子。
                why = "完全校验已拒绝"
                self.log(
                    f"{why} {package.name}：不适配 {line}，跳过安装", "warn"
                )
                return False
            answer = QMessageBox.question(
                self,
                "扩展与当前版本不兼容",
                f"{package.name} 在 {line} 上补丁命中 {probe.patch_ok}/{probe.patch_total}。\n\n"
                f"{detail}\n\n"
                f"仍要强制安装吗？强制安装会跳过没命中的补丁，"
                f"该扩展的部分功能可能失效甚至导致游戏起不来。\n"
                f"更稳妥的做法是把主页版本切到这个扩展声明的引擎线再安装。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.log(f"已跳过 {package.name}（不兼容 {line}）", "warn")
                return False
            # 用户坚持：降级为「允许部分兼容」，否则必定失败
            self._allow_partial_next = True
            return True

        # PARTIAL：能装，但会跳过失配补丁，需要用户知情
        detail = "；".join(probe.failures[:3]) or "部分补丁未命中"
        self.log(
            f"{package.name} 在 {line} 上仅有部分补丁命中"
            f"（{probe.patch_ok}/{probe.patch_total}）：{detail}",
            "warn",
        )
        if cfg_mod.is_strict(mode):
            # 完全校验不提供「部分兼容」：只部分命中就意味着装上也缺功能，
            # 直接剔除，避免带着半残扩展进游戏。
            why = "完全校验已拒绝"
            self.log(
                f"{why} {package.name}：只部分适配 {line}，跳过安装", "warn"
            )
            return False
        answer = QMessageBox.question(
            self,
            "扩展只部分兼容当前版本",
            f"{package.name} 在 {line} 上的补丁命中 {probe.patch_ok}/{probe.patch_total}。\n\n"
            f"{detail}\n\n"
            f"继续安装会跳过失配的补丁（该部分功能不生效），并在装完后强制跑一次挂载冒烟。\n"
            f"是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.log(f"已跳过 {package.name}", "dim")
            return False
        self._allow_partial_next = True
        return True

    def _start_next_install(self) -> None:
        if not self._install_queue:
            self._finish_install_batch()
            return

        package, line = self._install_queue.pop(0)
        self._install_index += 1
        index = self._install_index
        total = index + len(self._install_queue)
        name = package.name

        # 安装前先按目标线探一次兼容性。严格模式下补丁没命中会直接失败，
        # 但失败信息埋在长日志里，用户只会看到「装不上」。这里提前把结论
        # 摆出来，并让用户决定是跳过还是放弃——比事后猜原因好得多。
        if not self._precheck_line_compat(package, line):
            self._install_index -= 1
            self._start_next_install()
            return

        if total > 1:
            self.ext_status.start_batch(total)
            self.ext_status.set_batch(index, total, name, 0)
            self.log(f"开始安装（{index}/{total}）：{package} → {line}", "info")
        else:
            self.ext_status.start(name)
            self.log(f"开始安装扩展：{package} → {line}", "info")

        # 单个包保持严格（真跑一次冒烟）；批量为了速度跳过，装完由
        # 「整体校验」兜底。批量里绝不能把单包也降级——那会让
        # 「装了却用不了」重新变成常态。
        # allow_partial 由预检决定：只有用户明确接受「部分兼容」才放开，
        # 否则保持严格（补丁没命中就是失败，而不是装个半残的包）。
        allow_partial = bool(getattr(self, "_allow_partial_next", False))
        self._allow_partial_next = False
        worker = InstallWorker(
            self.cfg,
            package,
            verify_smoke=(total == 1),
            target_line=line,
            allow_partial=allow_partial,
        )
        worker.progress.connect(
            lambda pct, label, i=index, t=total, n=name: self._on_batch_progress(
                i, t, n, pct, label
            )
        )
        worker.done.connect(
            lambda result, i=index, t=total, n=name: self._on_batch_item(i, t, n, result)
        )
        worker.failed.connect(
            lambda msg, tb, i=index, n=name: self._on_batch_failed(i, n, msg, tb)
        )
        self._install_worker = worker
        # 必须留住引用：QThread 在运行中被回收会直接崩。
        self._install_workers.append(worker)
        worker.start()

    def _on_batch_progress(
        self, index: int, total: int, name: str, percent: int, label: str
    ) -> None:
        if total > 1:
            self.ext_status.set_batch(index, total, name, percent)
        else:
            self.ext_status.set_progress(percent, label)
        self.log(f"[{percent:>3}%] {label}", "dim")

    def _on_batch_item(self, index: int, total: int, name: str, result) -> None:
        ok = bool(getattr(result, "ok", False))
        message = getattr(result, "message", "") or "安装结束"
        self._install_last_message = message
        self.log(message, "ok" if ok else "err")
        for issue in getattr(result, "issues", []) or []:
            # 扩展安装给的是 dict（level/message），剧情导入给的是纯字符串
            # （它是给人读的「注意」文案）。两种都要能显示，不能因为一个是
            # 字符串就整批崩在 UI 线程里。
            if isinstance(issue, dict):
                level = issue.get("level")
                # 不同来源的 issue 键名不一致：清单/基线用 message，联机检查用
                # reason。只读 message 会打出「注意：None」（2026-11-13 实测）。
                text = (
                    issue.get("message")
                    or issue.get("reason")
                    or issue.get("detail")
                    or ""
                )
            else:
                level, text = "warn", str(issue)
            if not str(text).strip():
                continue
            if level == "warn":
                self.log(f"  注意：{text}", "warn")
            elif level == "error":
                # 合集包里单个成员失败要单独说清，否则用户只看到「失败 3 个」
                # 却不知道是哪三个、为什么。
                self.log(f"  失败：{text}", "err")
        # 组合检查结论：装完就告诉用户冲不冲，不等启动才发现
        for conflict in getattr(result, "conflicts", []) or []:
            level = str(conflict.get("level") or "warn")
            self.log(
                f"  冲突检查：{conflict.get('message')}",
                "err" if level == "error" else "warn",
            )
        if not ok:
            # 开发者版要能直接从日志看出「哪一步炸了」。只打人话文案的话，
            # 一个 install.overlay 背后可能是补丁断言、解包、回读任何一环，
            # 排查只能靠猜（2026-11-05 就为这个白跑了一轮：界面说「等作者
            # 用 1.6 重新打包」，真实原因是 expect 断言抛异常）。玩家版保持干净。
            try:
                from ... import edition as edition_mod

                detail = str(getattr(result, "detail", "") or "").strip()
                code = str(getattr(result, "code", "") or "").strip()
                if detail and edition_mod.is_dev():
                    self.log(f"  技术明细：{code or 'install'}：{detail}", "err")
            except Exception:  # noqa: BLE001
                pass
            self._install_failed.append({"name": name, "message": message})
        elif str(getattr(result, "status", "") or "") == "needs_hooks":
            # 首次导入必然是待插桩：插桩在启动组装时才做，安装阶段没有
            # 可插的目标。这句话必须说清楚，否则玩家会以为装坏了。
            line = str(getattr(result, "engine_line", "") or self._install_target_line)
            self.log(
                f"  待插桩：这个扩展要用方法钩子，插桩在启动游戏时进行。"
                f"现在显示「待插桩」是正常现象——启动一次 {line} 线后会自动变成"
                f"「已就绪」，那时才代表钩子注册成功、扩展真正生效。",
                "info",
            )
        self.extensions.reload_installed(force=True)  # 安装刚改了注册表
        self._on_mods_changed()
        # 等一下再起下一个：当前 worker 的 finished 还没跑完，
        # 立刻换人会让 _install_worker 指向一个仍在收尾的线程。
        QTimer.singleShot(0, self._start_next_install_guarded)

    def _on_batch_failed(self, index: int, name: str, message: str, tb: str) -> None:
        self.ext_status.finish(False, f"安装失败：{message}")
        self.log(f"安装失败：{message}", "err")
        if tb:
            # 堆栈只进文件，不打进界面——一次异常就能刷掉 4000 行上限。
            self.log(tb.strip().splitlines()[-1], "err", detail=tb)
        self._install_failed.append({"name": name, "message": message})
        QTimer.singleShot(0, self._start_next_install_guarded)

    def _finish_install_batch(self) -> None:
        self._install_active = False
        # 先解锁启动按钮，再走后面的结果汇总/报错弹窗 —— 那些分支里有
        # 多个 return，漏掉一处按钮就永远停在「导入中…」（玩家只能重启启动器）。
        self._sync_launch_button()
        # 回收已经结束的 worker：批次里每装一个就 append 一次，不清理的话
        # 长时间使用会一直攒着 QThread 对象。对已被销毁的包装 isRunning
        # 会抛 RuntimeError，逐个兜住。
        alive = []
        for worker in self._install_workers:
            try:
                if worker.isRunning():
                    alive.append(worker)
            except RuntimeError:
                continue
        self._install_workers = alive
        total = self._install_index
        failed = self._install_failed
        if total <= 1 and not failed:
            self.ext_status.finish(True, self._install_last_message or "安装完成")
            return
        if not failed:
            self.ext_status.finish(
                True, f"{total}/{total} 个全部安装完成", result_text=f"{total}/{total}"
            )
            self.log(f"批量安装完成：{total} 个全部成功", "ok")
            return

        self.ext_status.finish(
            False,
            f"{len(failed)}/{total} 个失败",
            result_text=f"{total - len(failed)}/{total}",
        )
        lines = "\n".join(
            f"{i + 1}. {f['name']} — {f['message']}" for i, f in enumerate(failed)
        )
        self.log(f"批量安装结束：成功 {total - len(failed)}，失败 {len(failed)}", "err")
        self.log(lines, "err")
        error_dialog(
            self,
            "批量安装未全部完成",
            f"{len(failed)}/{total} 个包安装失败，明细如下。",
            f"{lines}\n\n日志文件：{current_log_file()}\n\n{LOG_HINT}",
        )

    # ------------------------------------------------------------ 日志

    def _build_log_drawer(self) -> QWidget:
        drawer = QWidget()
        drawer.setObjectName("LogDrawer")
        drawer.setAutoFillBackground(False)
        # 高度不再写死：由外层 QSplitter 拖动决定（见 _build_log_drawer_shell）。
        # 这里只给一个下限，保证折叠后仍能露出标题行。
        drawer.setMinimumHeight(LOG_MIN_H)
        lay = QVBoxLayout(drawer)
        lay.setContentsMargins(24, 4, 24, 4)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        label = QLabel("运行日志")
        label.setObjectName("Muted")
        head.addWidget(label)
        head.addStretch(1)

        # 三个日志出口：复制（最快，粘进对话）、保存（发文件）、打开目录
        # （要看完整历史文件）。出问题时用户第一眼就该看到「复制」。
        self.copy_log_btn = QPushButton("复制")
        self.copy_log_btn.setProperty("variant", "ghost")
        self.copy_log_btn.setFixedHeight(26)
        self.copy_log_btn.setStyleSheet("padding: 0 10px;")
        self.copy_log_btn.setToolTip("把当前运行日志复制到剪贴板，直接粘贴给 AI 排查")
        self.copy_log_btn.clicked.connect(self._copy_log)
        head.addWidget(self.copy_log_btn)

        self.save_log_btn = QPushButton("保存")
        self.save_log_btn.setProperty("variant", "ghost")
        self.save_log_btn.setFixedHeight(26)
        self.save_log_btn.setStyleSheet("padding: 0 10px;")
        self.save_log_btn.setToolTip("把当前运行日志另存为 .txt")
        self.save_log_btn.clicked.connect(self._save_log)
        head.addWidget(self.save_log_btn)

        self.open_log_btn = QPushButton("打开目录")
        self.open_log_btn.setProperty("variant", "ghost")
        self.open_log_btn.setFixedHeight(26)
        self.open_log_btn.setStyleSheet("padding: 0 10px;")
        self.open_log_btn.setToolTip("打开 logs 目录，里面有每次运行的完整日志文件")
        self.open_log_btn.clicked.connect(self._open_log_dir)
        head.addWidget(self.open_log_btn)

        self.clear_log_btn = QPushButton("清空")
        self.clear_log_btn.setProperty("variant", "ghost")
        # 高度必须和 QSS 的内边距匹配。全局 QPushButton 是 padding:7px 16px +
        # border 1px，13px 字体的行高约 18px，所以自然高度 ≈ 18+14+2 = 34px。
        # 之前写死 24px，扣掉内边距只剩 8px 给文字 → 字被压扁、字间距被撑开，
        # 看起来像「清 空」两个字分开。这里改成和自然高度一致，并压低横向内边距，
        # 让按钮紧凑但不挤字。
        self.clear_log_btn.setFixedHeight(26)
        self.clear_log_btn.setStyleSheet("padding: 0 10px;")
        self.clear_log_btn.clicked.connect(self._clear_log)
        head.addWidget(self.clear_log_btn)
        self.collapse_btn = QPushButton("收起")
        self.collapse_btn.setProperty("variant", "ghost")
        self.collapse_btn.setFixedHeight(26)
        self.collapse_btn.setStyleSheet("padding: 0 10px;")
        self.collapse_btn.clicked.connect(self._toggle_log)
        head.addWidget(self.collapse_btn)
        lay.addLayout(head)

        self.log_view = LogView()
        lay.addWidget(self.log_view, 1)
        self._drawer = drawer
        return drawer

    def _build_log_drawer_shell(self) -> QSplitter:
        """把「页面区 ↔ 日志区」做成可拖动分隔的两段。

        为什么改成 QSplitter：日志原本是 ``setFixedHeight(150)`` 的死高度，
        用户想多看几行日志没有任何办法——150px 扣掉标题行只够显示 5~6 行。
        改成 splitter 后可以用鼠标拖动分隔条任意调整，并且：
        - 拖到最上方可把日志放大到接近整窗，便于排查长日志；
        - 高度会记住（写进 cfg["ui"]["log_height"]），下次启动还原；
        - 折叠按钮仍然可用，等价于把分隔条拖到底部。

        手柄做得比默认粗一点（7px）并单独上色，否则深色主题下几乎看不见
        可拖动的位置——这是「不知道能拖」的常见原因。
        """
        split = QSplitter(Qt.Orientation.Vertical)
        split.setObjectName("MainSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(7)

        split.addWidget(self.stack)
        self._drawer_shell = self._build_log_drawer()
        split.addWidget(self._drawer_shell)
        split.setStretchFactor(0, 1)   # 页面区优先获得多余空间
        split.setStretchFactor(1, 0)

        # 恢复上次拖动的高度；用户拖过就以用户为准，没拖过则按窗口高度决定：
# 高窗口（>=860）自动展开到 LOG_AUTO_H，矮窗口默认折叠，把空间全给页面。
        ui = self.cfg.get("ui") or {}
        try:
            saved = int(ui.get("log_height") or 0)
        except (TypeError, ValueError):
            saved = 0
        if saved >= LOG_MIN_H:
            height = saved
            collapsed = False
        elif self.height() >= LOG_AUTO_EXPAND_MIN_H:
            height = LOG_AUTO_H
            collapsed = False
        else:
            height = LOG_COLLAPSED_H
            collapsed = True
        cap = min(int(self.height() * LOG_MAX_RATIO), max(LOG_MIN_H, self.height() - LOG_PAGE_MIN_H))
        if not collapsed:
            height = max(LOG_MIN_H, min(height, cap))
        if collapsed:
            # 折叠态必须先放开最小高度，否则 setSizes 会被抽屉的自然最小高度
            # （实测 122px）顶回去，折叠看起来没生效。
            self._log_collapsed = True
            self.log_view.setVisible(False)
            self._drawer_shell.setMinimumHeight(0)
            # 直接钉死高度：只靠 setSizes 不够 —— splitter 在窗口 show
            # 之前没有真实高度，Qt 会按子控件自然最小高度（122px）回填，
            # 实测 760 高窗口里抽屉被撑到 176px，库页底部卡片被切。
            # setFixedHeight 是强约束，任何 resize 都不会被顶开。
            self._drawer_shell.setMaximumHeight(LOG_COLLAPSED_H)
            self._drawer_shell.setFixedHeight(LOG_COLLAPSED_H)
            self.collapse_btn.setText("展开")
        else:
            # 展开态要解掉固定高度，否则用户拖不动分隔条
            self._drawer_shell.setMinimumHeight(LOG_MIN_H)
            self._drawer_shell.setMaximumHeight(16_777_215)
        split.setSizes([max(240, self.height() - height), height])

        split.splitterMoved.connect(self._on_log_splitter_moved)
        self._log_splitter = split

        # 折叠态再补一次：``split.setSizes`` 在窗口 show 之前执行时，
        # splitter 还没有真实高度，Qt 会按子控件的自然最小高度顶回去
        # （实测 760 高窗口里折叠态本该 40px，实际却被撑到 176px，
        # 库页底部卡片直接被切）。show 之后再排一次才生效。
        if collapsed:
            from PySide6.QtCore import QTimer as _QTimer

            _QTimer.singleShot(0, lambda: self._reapply_collapsed(split))
        return split

    def _reapply_collapsed(self, split) -> None:
        """窗口显示后重新压一次折叠高度。见调用处说明。"""
        if not getattr(self, "_log_collapsed", False):
            return
        total = split.height()
        if total <= 0:
            return
        self._drawer_shell.setMinimumHeight(0)
        split.setSizes([total - LOG_COLLAPSED_H, LOG_COLLAPSED_H])

    #: 日志区允许的最小高度（折叠态）与默认高度
    def _init_log_size_once(self) -> None:
        """首次显示时按真实窗口高度决定日志区初始状态（只执行一次）。

        为什么不能只在 ``__init__`` 里判断：那时窗口还没被布局撑开到最终
        高度，``self.height()`` 拿到的是构造时的默认值，于是「高窗口自动
        展开日志」的判断永远不成立。这里在真正显示后再定一次：
        - 用户拖过/展开过（配置里有 log_height）→ 尊重用户
        - 窗口够高（>= LOG_AUTO_EXPAND_MIN_H）→ 自动展开到 LOG_AUTO_H
        - 否则折叠，把空间全给页面

        **注意**：本方法由 :meth:`showEvent` 调用，不要自己定义 showEvent——
        本类末尾另有一个 showEvent（处理标题栏着色），重复定义会互相覆盖，
        症状是「写在前面那个 showEvent 永远不执行」。
        """
        split = getattr(self, "_log_splitter", None)
        if split is None or getattr(self, "_log_size_fixed", False):
            return
        self._log_size_fixed = True

        ui = self.cfg.get("ui") or {}
        try:
            saved = int(ui.get("log_height") or 0)
        except (TypeError, ValueError):
            saved = 0
        total = sum(split.sizes())
        if total <= 0:
            return

        if saved >= LOG_MIN_H:
            target = max(LOG_MIN_H, min(saved, max(LOG_MIN_H, total - LOG_PAGE_MIN_H)))
            self._expand_log_to(target)
        elif self.height() >= LOG_AUTO_EXPAND_MIN_H:
            # 自动展开时也要给页面留够：游戏库页面实测需要 666px 才完整显示。
            # 若剩余空间不足 LOG_PAGE_FULL_H，宁可不展开——「页面显示完整」
            # 比「日志默认展开」更重要，日志随时可以手动拖出来。
            spare = total - LOG_PAGE_FULL_H
            if spare < LOG_MIN_H:
                return  # 空间不够，保持折叠
            target = max(LOG_MIN_H, min(LOG_AUTO_H, spare))
            self._expand_log_to(target)
        # 其余情况保持 __init__ 设定的折叠态

    def _expand_log_to(self, height: int) -> None:
        """把日志区展开到指定高度（内部工具，避免与 _toggle_log 的语义混淆）。"""
        split = getattr(self, "_log_splitter", None)
        if split is None:
            return
        total = sum(split.sizes())
        height = max(LOG_MIN_H, min(int(height), max(LOG_MIN_H, total - LOG_PAGE_MIN_H)))
        self._log_collapsed = False
        self.log_view.setVisible(True)
        self._drawer_shell.setMinimumHeight(LOG_MIN_H)
        self.collapse_btn.setText("收起")
        split.setSizes([total - height, height])

    def _apply_log_ratio(self) -> None:
        """按当前窗口高度把日志区收敛到允许范围内。

        规则：日志不超过内容区的 45%（而不是原来的 75%），且页面区至少留
        LOG_PAGE_MIN_H。这样窗口越小、日志让出越多，页面始终能看清；
        用户手动拖大过的话，拖动操作会覆盖这里的收敛（splitterMoved 记录新值）。
        """
        split = getattr(self, "_log_splitter", None)
        if split is None or self._log_collapsed:
            return
        total = sum(split.sizes())
        if total <= 0:
            return
        cur = self._drawer_shell.height()
        cap = min(int(total * LOG_MAX_RATIO), max(LOG_MIN_H, total - LOG_PAGE_MIN_H))
        target = max(LOG_MIN_H, min(cur, cap))
        if abs(target - cur) < 2:
            return
        split.setSizes([total - target, target])

    def _on_log_splitter_moved(self, _pos: int, _index: int) -> None:
        """拖动结束后把高度写进配置，下次启动还原。

        只记录「非折叠态」的高度：折叠时高度很小，若也记下来，
        下次启动就只剩一条标题栏，用户会以为日志丢了。
        """
        if self._log_collapsed:
            return
        h = self._drawer_shell.height()
        if h < LOG_MIN_H:
            return
        self.cfg.setdefault("ui", {})["log_height"] = int(h)
        try:
            cfg_mod.save(self.cfg)
        except OSError:
            pass

    def _toggle_log(self) -> None:
        """折叠/展开日志。用 splitter 尺寸实现，与拖动共用同一套机制。"""
        split = getattr(self, "_log_splitter", None)
        if split is None:
            return
        sizes = split.sizes()
        total = sum(sizes) or 1
        if not self._log_collapsed:
            # 折叠：记住当前高度，再把日志压到最小。
            # 必须先隐藏 log_view 并放开最小高度限制——抽屉的自然最小高度是
            # 122px，不放开的话 setSizes 会被 Qt 顶回 122，折叠看起来「没折」。
            self._log_expanded_h = max(LOG_DEFAULT_H, self._drawer_shell.height())
            self._log_collapsed = True
            self.log_view.setVisible(False)
            self._drawer_shell.setMinimumHeight(0)
            # 与初始化时一致：钉死高度，否则自然最小高度会把折叠顶开
            self._drawer_shell.setMaximumHeight(LOG_COLLAPSED_H)
            self._drawer_shell.setFixedHeight(LOG_COLLAPSED_H)
            split.setSizes([total - LOG_COLLAPSED_H, LOG_COLLAPSED_H])
            self.collapse_btn.setText("展开")
        else:
            self._log_collapsed = False
            self.log_view.setVisible(True)
            self._drawer_shell.setMinimumHeight(LOG_MIN_H)
            # 解掉折叠时设的固定高度，否则展开后拖不动分隔条
            self._drawer_shell.setMaximumHeight(16_777_215)
            h = getattr(self, "_log_expanded_h", LOG_DEFAULT_H)
            h = min(h, max(LOG_MIN_H, total - 160))
            split.setSizes([total - h, h])
            self.collapse_btn.setText("收起")

    #: 界面级别 -> 标准 logging 级别。界面用「ok」表示成功，标准 logging
    #: 没有对应概念，按 INFO 记即可。
    _LEVELS = {
        "err": logging.ERROR,
        "warn": logging.WARNING,
        "ok": logging.INFO,
        "info": logging.INFO,
        "dim": logging.DEBUG,
    }

    def log(self, text: str, level: str = "info", *, detail: str | None = None) -> None:
        """写一行界面日志，并同步进文件日志。

        同步落盘是「把日志发给别人」的前提：过去 ``log`` 只进 ``LogView``
        （内存，上限 4000 行），窗口一关就没了，用户只能拍屏幕。
        ``detail`` 放完整堆栈——只进文件，不进界面。
        """
        if hasattr(self, "log_view"):
            self.log_view.log(text, level)
        logger = getattr(self, "_logger", None)
        if logger is not None:
            logger.log(self._LEVELS.get(level, logging.INFO), text)
            if detail:
                logger.error(detail)

    def _clear_log(self) -> None:
        """清空日志。

        队列也要一起清——否则点完「清空」之后，节流层里攒着的旧行
        会在下一次 flush 时又冒出来，看着像「清了个寂寞」。
        """
        view = getattr(self, "log_view", None)
        if view is None:
            return
        view.clear_all()

    def _copy_log(self) -> None:
        # 先把队列里没刷完的写进控件，否则复制出来的日志会少最后几行
        self.log_view.flush_now()
        text = self.log_view.toPlainText()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        lines = len(text.splitlines())
        self.log(f"已复制 {lines} 行日志到剪贴板，可直接粘贴给 AI", "ok")

    def _save_log(self) -> None:
        default = str(paths.LOGS / f"launcher-log-{time.strftime('%Y%m%d-%H%M%S')}.txt")
        target, _ = QFileDialog.getSaveFileName(
            self, "保存运行日志", default, "文本文件 (*.txt)"
        )
        if not target:
            return
        try:
            self.log_view.flush_now()
            Path(target).write_text(self.log_view.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self.log(f"保存日志失败：{exc}", "err")
            return
        self.log(f"日志已保存：{target}", "ok")

    def _open_log_dir(self) -> None:
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.LOGS)))

    # ------------------------------------------------------------ 底栏

    def _build_footer(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(theme.FOOTER_H)
        # 机甲风下底栏改半透明：原来那条不透明深紫横杠会把背景立绘硬切断
        bar.setStyleSheet(
            "background:rgba(8,18,36,0.55); border-top:1px solid rgba(20,62,108,0.7);"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(24, 0, 24, 0)
        lay.setSpacing(10)
        self.foot_left = QLabel("就绪")
        self.foot_left.setObjectName("Faint")
        lay.addWidget(self.foot_left)
        lay.addStretch(1)
        from ... import __version__, edition

        version = QLabel(
            f"v{__version__}　·　build {edition.build_id()}　·　拖入扩展包可直接安装"
        )
        version.setObjectName("Faint")
        lay.addWidget(version)
        return bar

    # ------------------------------------------------------------ 机甲导航条 / 状态面板

    def _build_navbar(self) -> QWidget:
        """底部橙色导航条（横跨全宽）+ 右下角状态胶囊。

        ``nav_buttons`` / ``nav_group`` 仍是原来那套契约：
        ``_switch`` 会 setChecked，``app.py::_selftest`` 会遍历
        ``NAV_ITEMS`` 调 ``_switch`` 并读 ``window.pages[key]``。
        """
        bar = QWidget()
        bar.setObjectName("MechaNavbar")
        # 2026-11-09 用户反馈「按钮稍微小一些，不用太大」：
        # 条高 76→66、按钮 164x52→134x44。原来 6 个按钮几乎顶满整宽，
        # 视觉上抢戏；缩小后立绘露出的面积更大。
        bar.setFixedHeight(66)
        bar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(24, 8, 24, 10)
        lay.setSpacing(10)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for index, (key, text, _icon_name) in enumerate(NAV_ITEMS):
            btn = MechaNavButton(text)
            btn.setFixedSize(134, 44)
            btn.clicked.connect(lambda _=False, k=key: self._switch(k))
            self.nav_group.addButton(btn, index)
            self.nav_buttons[key] = btn
            lay.addWidget(btn)
        lay.addStretch(1)

        # 纯净模式：只保留「游戏库 / 扩展管理 / 小游戏 / 设置」四项
        # （2026-11-09 用户要求）。被隐藏的是「打包导出」和「诊断」——
        # 这两个是作者/排障用的，玩家日常不需要。
        #
        # 注意**不删按钮、只隐藏**：``_switch(key)`` 仍能切到这些页，
        # 诊断页被体检流程内部调用时也不会因为控件不存在而崩。
        self._apply_pure_mode()

        # 状态胶囊：隐藏态时它是「召唤」入口，展开态时变成「收起」
        self.status_capsule = MechaStatusCapsule()
        # 初值先置 0 并标记「待刷新」：真实数量由 _refresh_hub() 统一写入。
        #
        # 这里原来写死 ``set_state(25, ...)``，是因为开发机上恰好装了 25 个
        # 扩展 —— 全新安装（0 个扩展）时会显示「25 扩展 · 全部正常」，
        # 与右栏「还没有接入扩展」自相矛盾（2026-11-09 用户截图反馈）。
        # **写死演示数据是最容易漏到线上的 bug**，初值必须是 0。
        self.status_capsule.set_state(0, True, "全部正常")
        self.status_capsule.clicked.connect(self._toggle_panel)
        lay.addWidget(self.status_capsule, 0, Qt.AlignmentFlag.AlignBottom)
        return bar

    @staticmethod
    def _panel_hidden(cfg: dict) -> bool:
        """``ui.status_panel_hidden`` 的读法，默认 **True**（默认 HUB 观感）。

        为什么默认隐藏：用户要的是「背景立绘完整、点按钮才召唤面板」。
        显式写了 False 才常驻展开。和 ``features.developer_only`` 一样
        是隐式键 —— config 无 schema 校验，新增键不会被拒也不会丢。
        """
        raw = (cfg.get("ui") or {}).get("status_panel_hidden")
        return True if raw is None else bool(raw)

    def _apply_pure_mode(self) -> None:
        """按 ``ui.pure_mode`` 显示/隐藏作者向导航项。

        纯净模式（默认开）只留：游戏库 / 扩展管理 / 小游戏 / 设置。
        「打包导出」「诊断」是作者与排障用的，普通玩家用不到。

        实现上是 ``setVisible`` 而不是移除控件 —— 详见 ``_build_navbar``
        里的说明：诊断页会被体检流程内部调用，控件必须始终存在。
        """
        raw = (self.cfg.get("ui") or {}).get("pure_mode")
        pure = True if raw is None else bool(raw)
        for key in PURE_MODE_HIDDEN:
            btn = self.nav_buttons.get(key) if hasattr(self, "nav_buttons") else None
            if btn is not None:
                btn.setVisible(not pure)

    def _apply_panel_visibility(self) -> None:
        """按配置决定显示 HUB 视图还是功能页。

        隐藏态 = **HUB 视图**（默认观感：立绘完整，只有左下启动按钮 +
        右下角落一处低调状态位），不是把右侧留空。
        点任意导航按钮会自动切到对应功能页 —— 功能一页不丢。
        """
        hidden = self._panel_hidden(self.cfg)
        self._show_hub(hidden)
        if hasattr(self, "status_capsule"):
            self.status_capsule.set_expanded(not hidden)

    def _show_hub(self, hub: bool) -> None:
        """切换内容区视图：``True`` = HUB，``False`` = 功能页栈。

        HUB 内部三个区域各自独立可隐藏（``ui.hub_hide_*``），
        隐藏的是元素本身而不是整个视图 —— 全隐藏时右侧只剩立绘。
        """
        if not hasattr(self, "content_stack"):
            return
        self._apply_hub_regions()
        self.content_stack.setCurrentIndex(1 if hub else 0)
        if hub:
            self._refresh_hub()

    @staticmethod
    def _hub_region_hidden(cfg: dict, key: str) -> bool:
        """``ui.hub_hide_*`` 的读法，默认 **True**（默认隐藏，观感优先）。"""
        raw = (cfg.get("ui") or {}).get(key)
        return True if raw is None else bool(raw)

    def _apply_hub_regions(self) -> None:
        """把三个开关同步到 HUB 视图与主窗口状态条。

        * ``hub_hide_stats``     → HubView 的状态面板
        * ``hub_hide_config``    → HubView 的当前配置条
        * ``hub_hide_statusbar`` → ``ext_status`` + 页脚（属主窗口，不是 HubView）

        三者互不影响；任何一个隐藏都只是「少一块 UI」，功能不丢
        （统计在诊断页、配置在游戏库页、扩展状态在扩展管理页都能看到）。
        """
        if not hasattr(self, "hub_view"):
            return
        hide_stats = self._hub_region_hidden(self.cfg, "hub_hide_stats")
        hide_config = self._hub_region_hidden(self.cfg, "hub_hide_config")
        hide_status = self._hub_region_hidden(self.cfg, "hub_hide_statusbar")

        self.hub_view.set_region_visible("stats", not hide_stats)
        self.hub_view.set_region_visible("config", not hide_config)

        # 状态条与页脚在全窗口层面隐藏：它们不属于 HUB 视图
        for attr in ("ext_status", "_footer"):
            w = getattr(self, attr, None)
            if w is not None:
                w.setVisible(not hide_status)

    def _refresh_hub(self) -> None:
        """把扩展统计/配置摘要刷到 HUB 视图。

        **数字必须按「当前引擎线」统计，不能数全部已安装。**

        2026-11-10 修：原来这里用 ``ExtensionService(cfg).installed()`` 的
        长度，那是**所有线**加起来的总数。同一个包可以同时装在 4.6 和 4.7
        两条线上（各有独立的 overlay-46/overlay-47），于是被重复计入 ——
        界面上显示 27，而 4.6 实际是 25 个、4.7 只有 8 个。

        现在改成 ``enable.enabled_mods(cfg, line)``：它按线裁剪注册表条目、
        过滤掉不适配当前线的包，得到的就是「这条线上真正会加载的扩展数」。

        顺带解决了另一个隐患：切换版本时数字会自动跟着变（``_switch``
        会调到这里），不用额外挂信号。
        """
        if not hasattr(self, "hub_view"):
            return

        # 当前引擎线：优先用运行中的线，否则用库页选中的版本。
        line = str(getattr(self, "_current_line", "") or "") or self.library.current_version()

        installed: list = []
        try:
            from ...core.ext import enable as enable_mod

            installed = list(enable_mod.enabled_mods(self.cfg, line))
        except Exception:  # noqa: BLE001 - 扩展目录不可读时按 0 处理
            installed = []

        ext_count = len(installed)
        # ``installed()`` 返回的是 **dict 列表**（键：id/name/version/
        # enabled/status...），不是对象。初版用 ``getattr(i, "id", "")``
        # 取到 25 个空串，于是算出「24 处冲突」这种离谱数字（实测截图里
        # 就是这么显示出来的）。这里按字典取，并且只统计真正重复的 id。
        ids = [
            str(i.get("id") or "") if isinstance(i, dict) else ""
            for i in installed
        ]
        ids = [i for i in ids if i]
        conflicts = len(ids) - len(set(ids))
        self.hub_view.set_stats(ext_count, conflicts)

        # 底部状态胶囊与 HUB 同源。
        #
        # 它原来只有构造时那一次 set_state，之后再也不更新 ——
        # 装/卸扩展后胶囊数字不动，是同一个 bug 的另一面。
        # 放在这里更新，是因为 _refresh_hub 已经是「扩展数量」的
        # 唯一汇总点（切换页面、装卸扩展、启动完成都会走到）。
        if hasattr(self, "status_capsule"):
            self.status_capsule.set_state(
                ext_count, conflicts == 0,
                "全部正常" if conflicts == 0 else f"{conflicts} 处冲突",
            )

        self.hub_view.set_summary(
            f"{ext_count} 个扩展已接入 · "
            + ("未发现冲突" if conflicts == 0 else f"{conflicts} 处冲突"),
            ok=(conflicts == 0),
        )

        key = self.library.current_version()
        meta = next((v for v in VERSIONS if v["key"] == key), None)
        line = meta["title"] if meta else key
        self.hub_view.set_config([
            ("游戏线", line.replace("机甲旋风 ", "")),
            ("存档档位", self.library.profile_box.currentText() or "默认"),
        ])

    def _toggle_panel(self) -> None:
        """右下角胶囊：在 HUB 与功能页之间切换，并**立即落盘**。

        落盘方式和 ``SettingsPage._on_dev_mode_toggled`` 一致：
        以磁盘最新内容为底改写，避免把界面里未保存的草稿一起写回去。
        """
        currently_hub = self.content_stack.currentIndex() == 1
        new_hidden = currently_hub          # HUB 态 → 切到功能页
        self._show_hub(new_hidden)
        self.status_capsule.set_expanded(not new_hidden)

        self.cfg.setdefault("ui", {})["status_panel_hidden"] = bool(new_hidden)
        try:
            disk = cfg_mod.load()
            disk.setdefault("ui", {})["status_panel_hidden"] = bool(new_hidden)
            cfg_mod.save(disk)
        except (OSError, ValueError) as exc:
            self.log(f"状态面板偏好保存失败：{exc}", "warn")

    def _switch(self, key: str) -> None:
        """切到某个功能页；**再点已选中的同一个按钮则回到 HUB**。

        2026-11-09 用户：「打开了游戏库、扩展管理这些之后也要能恢复到
        刚打开启动器的默认样式」。

        交互设计（与主流 App 的 tab 行为一致）：
        * HUB 态点任意导航 → 进该功能页
        * 已在功能页 A，点另一个 B → 切到 B
        * 已在功能页 A，**再点 A** → 收起回 HUB（恢复默认样式）

        为什么这样做而不是加一个「返回」按钮：底部导航已经占满，
        再加按钮会破坏「按钮不要太大」的观感；复用同一个 tab 是零成本的。
        """
        already_same = (
            hasattr(self, "content_stack")
            and self.content_stack.currentIndex() == 0
            and self.stack.currentWidget() is self.pages.get(key)
        )
        if already_same:
            # 再点同一个 → 回 HUB，并把选中态清掉（否则回到默认样式后
            # 还挂着一个高亮按钮，看起来「没退干净」）
            self._show_hub(True)
            self.status_capsule.set_expanded(False)
            if hasattr(self, "nav_group"):
                self.nav_group.setExclusive(False)
                btn_same = self.nav_buttons.get(key)
                if btn_same is not None:
                    btn_same.setChecked(False)
                self.nav_group.setExclusive(True)
            return

        # 从 HUB 态点导航，自动切到功能页
        if hasattr(self, "content_stack") and self.content_stack.currentIndex() != 0:
            self._show_hub(False)
            self.status_capsule.set_expanded(True)
        elif hasattr(self, "right") and not self.right.isVisible():
            # 兼容旧路径：right 被整体隐藏时也要恢复
            self.right.setVisible(True)
            self.status_capsule.set_expanded(True)
        self.stack.setCurrentWidget(self.pages[key])
        btn = self.nav_buttons.get(key)
        if btn and not btn.isChecked():
            btn.setChecked(True)
        if key == "diagnostics":
            self.diagnostics.refresh()
        elif key == "build":
            self.build_page.refresh()
        elif key == "extensions":
            self.extensions.reload_installed()

    # ------------------------------------------------------------ 启动

    def _ensure_instance(self, slot: int) -> GameProcess:
        """取（或建）某个槽位的进程，信号里带上槽位号。"""
        proc = self.instances.get(slot)
        if proc is not None:
            return proc
        proc = GameProcess(self)
        proc.line.connect(self.log)
        proc.stateChanged.connect(lambda state, s=slot: self._on_instance_state(s, state))
        proc.finished.connect(lambda summary, s=slot: self._on_instance_finished(s, summary))
        self.instances[slot] = proc
        return proc

    def _free_slot(self) -> int | None:
        """第一个空闲槽位；全部占用则返回 None（达到本版本的实例上限）。"""
        for index in range(1, self.instance_slots + 1):
            proc = self.instances.get(index)
            if proc is None or not proc.running:
                return index
        return None

    def _full_hint(self, *, long: bool) -> str:
        """实例占满时的提示。

        玩家版只有 1 个槽位，说「双开已满」等于自曝内部工具；这里按槽位数
        分两种说法，开发者版才提「多开」。
        """
        total = self.instance_slots
        if total <= 1:
            return "游戏正在运行，先结束它再启动。" if long else "游戏正在运行"
        if long:
            return f"已有 {total} 个实例在运行（上限 {total} 个），先结束一个再启动。"
        return f"实例已满 {total}/{total}"

    def _profile_for_slot(self, slot: int) -> str:
        """每个实例用独立 profile，否则两个实例会写同一份 user:// 存档。"""
        base = self.library.current_profile() or "default"
        return base if slot == 1 else f"{base}_s{slot}"

    def _set_instance_state(self, slot: int, state: str, info: str = "") -> None:
        self.library.set_instance_state(slot, state, info)

    def _manage_custom_lines(self) -> None:
        """打开「管理游戏线」对话框，关闭后刷新游戏库下拉。"""
        from .pages.custom_lines_dialog import CustomLinesDialog

        before = self.library.current_version()
        dlg = CustomLinesDialog(self.cfg, self)
        dlg.exec()
        self.library.reload_lines()
        # 当前选中的线若被移除，reload_lines 已回退到内置线；这里补一条日志
        after = self.library.current_version()
        if after != before:
            self.log(f"版本线已切换为 {after}（原来的选择已不存在）", "info")
        self._sync_launch_button()

    def _open_mod_import(self) -> None:
        """库页右栏空状态点「导入扩展包」：切到扩展管理页再弹文件选择。

        为什么不就地弹框：安装要显示进度、逐包结论和失败原因，这些控件都在
        扩展管理页；在库页再复制一套只会让两处行为不一致。
        """
        self._switch("extensions")
        self.extensions.pick_install()

    def _manage_categories(self) -> None:
        """打开「管理分类」对话框，关闭后刷新筛选与分组。"""
        from .pages.category_dialog import CategoryDialog

        before = self.library.current_version()
        dlg = CategoryDialog(self.cfg, self)
        dlg.exec()
        self.library.reload_categories()
        after = self.library.current_version()
        if after != before:
            self.log(f"分类调整后当前游戏切换为 {after}", "info")
        self._sync_launch_button()

    def _launch_custom(self, line_id: str, slot: int | None = None) -> None:
        """启动自定义改版：直接执行它的 exe，只做存档隔离。

        **不碰** ``readiness.evaluate`` / ``build_for_launch`` / ``make_packset``——
        那几个函数是按内置引擎线写的，改版进去只会得到
        「扩展装错线」的混乱结果。这里刻意是一条最短路径。
        """
        import os

        from ...core import custom_lines as cl

        line = cl.get_line(self.cfg, line_id)
        if line is None:
            self.log("这条自定义改版已被移除，请重新选择版本线。", "err")
            self._sync_launch_button()
            return
        if not line.exists:
            self.log(f"自定义改版的文件不在了：{line.exe}", "err")
            self._sync_launch_button()
            return

        if slot is None:
            slot = self._free_slot()
            if slot is None:
                self.log(self._full_hint(long=True), "warn")
                self._sync_launch_button()
                return
        proc = self._ensure_instance(slot)
        if proc.running:
            self.log(f"实例 {slot} 正在运行，先结束它。", "warn")
            return

        try:
            plan = cl.prepare_launch(self.cfg, line, paths.PROFILES)
        except Exception as exc:  # noqa: BLE001
            self.log(f"启动准备失败：{exc}", "err")
            self.library.set_state("exited", "启动失败")
            self._set_instance_state(slot, "exited", "启动失败")
            self._sync_launch_button()
            return
        for warning in plan.warnings:
            self.log(f"自定义线：{warning}", "warn")

        env = os.environ.copy()
        if plan.env_appdata is not None:
            # 与内置线同一套隔离方式：把 APPDATA 指到独立目录
            env["APPDATA"] = str(plan.env_appdata)
            env["LOCALAPPDATA"] = str(plan.env_appdata)
        program = plan.command[0]
        args = plan.command[1:]

        self._current_line = line_id
        self.instance_meta[slot] = {
            "line": line_id,
            "profile": line.profile_name(),
            "custom": True,
        }
        self._set_instance_state(slot, "starting", f"{line.name} · 存档 {line.profile_name()}")
        if slot == 1:
            self.library.set_state("starting")
            self.foot_left.setText(f"启动中 · {line.name}")
        self.log(
            f"实例 {slot} 启动自定义{'资源包' if line.is_pck else '改版'}「{line.name}」：{program}"
            + (f" {' '.join(args)}" if args else ""),
            "info",
        )
        if line.is_pck:
            self.log(
                f"  用 {line.engine_line} 底座引擎挂载 {line.file_name}，不加载任何扩展。",
                "dim",
            )
        else:
            self.log("  这条线不加载任何扩展（自定义改版自带完整游戏文件）。", "dim")
        proc.start_external(
            program,
            args,
            cwd=str(Path(program).parent),
            env=env,
            label=line.name,
        )
        self._sync_launch_button()
        self._sync_mod_lock()

    def _launch(self, line: str, slot: int | None = None) -> None:
        """启动一个实例；``slot`` 为 None 时自动挑空闲槽位。"""
        # 导入进行中一律不启动。界面上按钮已经禁用，这里再拦一道：
        # 快捷键、托盘菜单、残留的信号连接都能绕过禁用态，而导入窗口里
        # packs/ 的覆盖层正在被重写、注册表还没落盘。
        if self._install_active:
            self.log("正在导入扩展包，导入完成后再启动游戏。", "warn")
            QMessageBox.information(
                self,
                "正在导入扩展包",
                "这可能需要一些时间进行导入，在此期间无法启动游戏。\n\n"
                "请等导入完成（启动按钮恢复为「启动」）后再启动。",
            )
            return

        # 自定义改版走完全独立的启动路径：不评估扩展就绪度、不组装 packset。
        # 它自带完整游戏文件，启动器只负责「执行 + 存档隔离」。
        from ...core import custom_lines as cl

        if cl.is_custom_id(line):
            self._launch_custom(line, slot)
            return

        readiness = readiness_mod.evaluate(self.cfg, line)
        if not readiness.ok:
            self.log(f"无法启动：{readiness.reason()}", "err")
            for extra in readiness.blockers[1:]:
                self.log(f"  {extra}", "err")
            detail = (
                "阻断原因：\n- "
                + "\n- ".join(readiness.blockers)
                + f"\n\n日志文件：{current_log_file()}\n\n{LOG_HINT}"
            )
            error_dialog(self, "无法启动", readiness.reason(), detail)
            self._sync_launch_button()
            return
        for warning in readiness.warnings:
            self.log(f"扩展：{warning}", "warn")

        if slot is None:
            slot = self._free_slot()
            if slot is None:
                self.log(self._full_hint(long=True), "warn")
                self._sync_launch_button()
                return
        proc = self._ensure_instance(slot)
        if proc.running:
            self.log(f"实例 {slot} 正在运行，先结束它。", "warn")
            return

        # 覆盖提醒放在「有空位」之后、「组装」之前：先确认能开，再问要不要开。
        # 内存检查排在最前：它是「这台机器本身够不够」，与选哪些扩展无关，
        # 而且玩家最该先知道的就是这个（4.7 要 7 GB 可用，8 GB 机器很紧）。
        if not self._confirm_memory(line):
            self._sync_launch_button()
            return
        # 开发者模式先确认「剔除谁」——这个不能记住，每次启动都要让人看见。
        if not self._confirm_drops(readiness, line):
            self._sync_launch_button()
            return
        if not self._confirm_overlaps(readiness, line):
            self._sync_launch_button()
            return

        bid = readiness_mod.baseline_id(line)
        profile = self._profile_for_slot(slot)
        mods = None
        try:
            # 运行目录带槽位号：同一秒启动两个实例也不会撞名
            rd = runner.run_dir_for(f"run{line.replace('.', '')}-s{slot}")
            base_packs = runner.baseline_packs(self.cfg, bid)

            from ...core.ext import enable as ext_enable

            mods = ext_enable.build_for_launch(self.cfg, line, rd)
            for warning in mods.warnings:
                self.log(f"扩展：{warning}", "warn")
            if mods.mods:
                self.log(
                    "启用扩展：" + "、".join(str(m.get("id")) for m in mods.mods)
                    + f"（钩子 {len(mods.hooks)} 个）",
                    "info",
                )
            packset = runner.make_packset(
                rd / "packset.json",
                packset_id=f"{bid}-{line}-s{slot}",
                line=line,
                # 顺序＝优先级（后者覆盖前者）：基线 → 扩展 → 合并层 → 插桩层。
                # （1.4 起剧情层已移除，原本位于基线与扩展之间。）
                packs=[*base_packs, *mods.packs],
                baseline_id=bid,
            )
        except Exception as exc:  # noqa: BLE001
            from ...core import messages as msg_mod

            diag = msg_mod.diagnostic_id(str(exc))
            self.log(f"启动准备失败（{diag}）：{exc}", "err", detail=traceback.format_exc())
            # 关键：失败也要把状态机复位，否则按钮会一直卡在「启动中」
            self.library.set_state("exited", "启动失败")
            self._set_instance_state(slot, "exited", "启动失败")
            self._sync_launch_button()
            if not cfg_mod.is_strict(cfg_mod.extension_mode(self.cfg)):
                error_dialog(
                    self,
                    "没能启动游戏",
                    "启动器准备游戏文件时出错了。",
                    f"诊断码：{diag}\n日志文件：{current_log_file()}\n\n{LOG_HINT}",
                )
            else:
                detail = (
                    f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
                    f"\n诊断码：{diag}\n日志文件：{current_log_file()}\n\n{LOG_HINT}"
                )
                error_dialog(self, "启动准备失败", str(exc), detail)
            return

        self._current_line = line
        # 兜底重启时要把「已重试过」和「累计崩溃次数」带进下一轮，
        # 否则每次重启都当成「第一次」，会无限循环。
        carry = getattr(self, "meta_auto_retry", None) or {}
        self.meta_auto_retry = None
        meta = {"line": line, "profile": profile, "run_dir": str(rd)}
        # 本次真正插桩成功的扩展 id：进程稳定运行后把它们翻成「已就绪」。
        # 放在 meta 里而不是闭包里，是为了退出时还能补一次（窗口关得快时
        # 5 秒定时器可能还没触发）。
        meta["ready_ids"] = list(getattr(mods, "ready_ids", []) or [])
        if carry and carry.get("line") == line:
            meta["auto_retried"] = bool(carry.get("auto_retried"))
            meta["crashes"] = int(carry.get("crashes") or 0)
        self.instance_meta[slot] = meta
        self._set_instance_state(slot, "starting", f"{line} · 存档 {profile}")
        if slot == 1:
            self.library.set_state("starting")
            self.foot_left.setText(f"启动中 · {line}")
            self.side_status.setText("● 启动中")
        self._sync_launch_button()
        self.log(f"实例 {slot} 准备启动 {line}，存档 profile = {profile}", "info")
        proc.start(
            self.cfg,
            line,
            packset=packset,
            main_scene="res://scenes/start_page.tscn",
            smoke=False,
            profile=profile,
            log_path=rd / "mount_report.json",
            mods_path=mods.mods_json if mods else None,
        )
        self._mark_ready_later(slot, line, meta["ready_ids"])

    #: 启动后多久把「待插桩」翻成「已就绪」。
    #:
    #: 必须晚于秒退判定窗口（3 秒）：脚本解析失败会在 autoload 阶段秒退，
    #: 那种情况下钩子并没有真正加载，标成就绪等于骗人。
    READY_AFTER_SECONDS = 5.0

    def _mark_ready_later(self, slot: int, line: str, ids) -> None:
        """进程稳定运行到 ``READY_AFTER_SECONDS`` 后，把待插桩翻成已就绪。"""
        ids = [str(x) for x in (ids or []) if str(x)]
        if not ids:
            return

        def _apply() -> None:
            proc = self.instances.get(slot)
            if proc is None or not getattr(proc, "running", False):
                return
            self._mark_ready_now(line, ids, quiet=True)

        QTimer.singleShot(int(self.READY_AFTER_SECONDS * 1000), _apply)

    def _mark_ready_now(self, line: str, ids, *, quiet: bool = False) -> None:
        """把启动成功的扩展状态写回注册表（幂等）。"""
        ids = [str(x) for x in (ids or []) if str(x)]
        if not ids or not line:
            return
        try:
            from ...core.ext.service import ExtensionService

            n = ExtensionService(self.cfg).mark_launched(line, ids)
        except Exception as exc:  # noqa: BLE001 - 状态写回失败不该影响游戏
            self.log(f"扩展状态写回失败（不影响本次游戏）：{exc}", "warn")
            return
        if n and not quiet:
            self.log(f"{line} 线 {n} 个扩展的钩子已生效，状态转为已就绪。", "ok")
        elif n:
            self.log(f"{line} 线 {n} 个扩展转为已就绪（钩子插桩成功）。", "ok")

    def _confirm_memory(self, line: str) -> bool:
        """启动前看一眼可用内存；明显不够时提醒一句，但不拦。

        为什么这件事必须做在启动器里
        ---------------------------
        4.7 实测（2026-11-11）：峰值**提交** 9972 MB、峰值工作集 6765 MB。
        8 GB 物理机光物理就差 1780 MB，只能靠页面文件顶 —— 机械盘上的
        表现就是「选完角色进游戏时整个界面冻住几十秒」，玩家描述成
        「卡死」，然后以为启动器坏了。

        过去启动器对此一句话都不说：玩家点启动、等两分钟、看到黑屏，
        完全不知道问题出在内存。现在改成事先说清楚，并给出可操作的选项。

        **只提醒不阻断**：4.7 在页面文件开大的 8 GB 机器上是能玩的
        （只是慢），硬拦会把能玩的人挡在外面 —— 那比「卡」更糟。
        """
        from ...core import memcheck

        verdict = memcheck.check(line)
        if not verdict.warn:
            return True

        self.log(f"内存：{verdict.title}", "warn")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("可用内存偏少")
        box.setText(verdict.title)
        box.setInformativeText(
            verdict.detail
            + "\n\n仍然可以启动，但卡顿与长时间停顿的可能性明显更高。"
        )
        start_btn = box.addButton("仍然启动", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        # 默认落在「取消」：内存不够时误按回车进游戏的代价比多点一次大
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        if box.clickedButton() is not start_btn:
            self.log("已取消启动（可用内存偏少）", "warn")
            return False
        self.log(
            f"内存不足仍继续启动：可用 {memcheck._human(verdict.avail)}"
            f" / 建议 {memcheck._human(verdict.need)}",
            "warn",
        )
        return True

    def _confirm_drops(self, readiness, line: str) -> bool:
        """开发者模式：启动前必须列出「哪些扩展会被剔除」。

        这个弹窗**没有**「以后不再提示」——开发者模式的语义就是每次都要
        看见不一致，记住了就等于把矛盾藏起来。
        """
        if not readiness.dropped:
            return True
        detail = "\n".join(f"· {item}" for item in readiness.drop_lines())
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("开发者模式 — 将剔除扩展")
        box.setText(f"{line} 线：{readiness.drop_summary()}")
        box.setInformativeText(
            "以下扩展未通过校验，本次启动会把它们整组剔除。"
            "同一处内容被多个包改动时，先装后装一个都不保留，"
            "也不存在「谁覆盖谁」：\n\n"
            f"{detail}\n\n"
            "此模式下不能保证导入的 mod 都能正常运行。想体验完整版，请等待 mod 作者适配；"
            "也可以截图本提示，把冲突的 mod 发给 AI 协助分析。"
        )
        start_btn = box.addButton("剔除并启动", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        if box.clickedButton() is not start_btn:
            self.log(f"已取消启动（开发者模式将剔除 {len(readiness.dropped)} 组扩展）", "warn")
            return False
        self.log(
            "开发者模式剔除后启动：" + "、".join(str(pid) for pid in readiness.dropped_ids),
            "warn",
        )
        return True

    def _confirm_overlaps(self, readiness, line: str) -> bool:
        """启动前的处置入口。

        普通模式**不弹窗**：自动取舍 + 一句日志，玩家不该替作者做判断。
        宽松校验给一次处理机会，跳过也能开。
        开发者校验在导入阶段就已经完成准入判定；这里再确认一次处置结果，
        没处理完仍然拒绝启动。
        """
        mode = cfg_mod.extension_mode(self.cfg)
        units = list(getattr(readiness, "units", []) or [])
        pending = [u for u in units if u.get("action") == "pending"]
        degraded = [u for u in units if u.get("action") == "degraded"]

        if not cfg_mod.is_strict(mode):
            if degraded:
                self.log(
                    f"扩展：{len(degraded)} 处互相影响，已自动取舍；相关功能可能不完整",
                    "warn",
                )
            return True

        if not pending and not degraded:
            return True

        from ...core.ext import decisions as decisions_mod
        from ...core.ext import enable as enable_mod
        from .conflicts_dialog import ConflictDialog

        pairs = enable_mod.enabled_manifests(self.cfg, line)
        meta = {pid: data for pid, data in pairs}
        dlg = ConflictDialog(units, line=line, meta=meta, parent=self)
        accepted = dlg.exec() == ConflictDialog.DialogCode.Accepted
        if accepted:
            store = decisions_mod.Store.load(cfg_mod.paths.CONFIG_DIR / "decisions.json")
            for decision in getattr(dlg, "decisions", []):
                store.put(decision)
            store.save()
            self.log(f"已保存 {len(getattr(dlg, 'decisions', []))} 条冲突处置", "info")
            return True

        if cfg_mod.is_strict(mode):
            self.log(f"完全校验：仍有 {len(pending)} 处未处理，已阻止启动", "err")
            error_dialog(
                self,
                "还有冲突没处理",
                f"有 {len(pending)} 处扩展改了同一处，需要先处理才能启动。",
                "到「扩展管理」页面点「处理冲突…」，逐项选择停用、兼容或依赖。",
            )
            return False

        self.log(f"宽松校验：{len(pending)} 处未处理，按默认继续", "warn")
        return True

    def _start_instance(self, slot: int) -> None:
        self._launch(self.library.current_version(), slot=int(slot))

    def _stop_instance(self, slot: int) -> None:
        proc = self.instances.get(int(slot))
        if proc is not None and proc.running:
            proc.stop()

    def _stop(self, slot: int = 1) -> None:
        self._stop_instance(slot)

    def _on_instance_state(self, slot: int, state: str) -> None:
        meta = self.instance_meta.get(slot) or {}
        info = f"{meta.get('line', '')} · 存档 {meta.get('profile', '')}" if meta else ""
        if state == "running":
            if slot == 1:
                self.library.set_state("running", f"{self._current_line} 已启动")
                self.foot_left.setText(f"运行中 · {self._current_line}")
                self.side_status.setText("● 运行中")
            self._set_instance_state(slot, "running", info)
        elif state == "exited":
            if slot == 1:
                self.library.set_state("exited")
                self.foot_left.setText("就绪")
                self.side_status.setText("● 就绪")
            self._set_instance_state(slot, "exited", info or "已退出")
        else:
            self._set_instance_state(slot, state, info)
        self._sync_launch_button()
        # 退出时强制重扫进程表：缓存里可能还留着「游戏在跑」的旧快照，
        # 不 force 就会把界面多锁半秒——玩家看到「游戏都关了，开关还是灰的」。
        self._sync_mod_lock(force=(state == "exited"))

    def _sync_mod_lock(self, force: bool = False) -> None:
        """把「是否有游戏在跑」广播给会改启用集合的页面。

        判定用进程表而不是界面状态：CLI 启动的实例、启动器重启后遗留的进程、
        崩溃后没走完退出回调的进程，界面上都显示「空闲」，但游戏其实还在跑。
        用进程表兜底，才能保证「锁」不会因为界面状态漏记而失效。

        ``force=True`` 强制重新扫描进程表。退出瞬间必须用 force：
        缓存里可能还留着「游戏在跑」的旧快照（0.5 秒 TTL），
        不强制刷新就会把界面多锁半秒——玩家看到的是「游戏都关了，
        开关还是灰的」。
        """
        from ...core import runtime_lock

        t0 = time.perf_counter()
        snap = runtime_lock.snapshot(force=force)
        running = bool(snap.pids)
        detail = ""
        if running:
            names = runtime_lock.describe_running()
            pids = ", ".join(map(str, snap.pids))
            detail = f"PID {pids}" if pids else names
            if names and names != "游戏进程":
                detail = f"{names}（PID {pids}）" if pids else names
        t1 = time.perf_counter()
        self.library.set_game_running(running, detail)
        t2 = time.perf_counter()
        self.extensions.set_game_running(running, detail)
        t3 = time.perf_counter()
        self._mod_lock_state = running
        self._log_timing("进程快照", (t1 - t0) * 1000)
        self._log_timing("library 重建", (t2 - t1) * 1000)
        self._log_timing("extensions 重建", (t3 - t2) * 1000)

    def _poll_mod_lock(self) -> None:
        """定时兜底：进程被外部杀掉时界面状态可能不更新，这里补一次。

        挂在 3 秒定时器上。查询本身走 Toolhelp32 原生快照（毫秒级），
        不会卡 UI——这是改成原生 API 之后才能安全挂定时器的前提。
        """
        from ...core import runtime_lock

        snap = runtime_lock.snapshot()
        now = bool(snap.pids)
        if now != getattr(self, "_mod_lock_state", False):
            self._sync_mod_lock(force=True)

    def _log_timing(self, label: str, ms: float) -> None:
        """把关键路径耗时写进日志，用于验收「关游戏时卡顿」是否解决。

        超过 100ms 才记——正常路径都是毫秒级，记太多反而淹没日志。
        """
        if ms >= 100.0:
            self.log(f"[计时] {label} {ms:.0f} ms", "dim")

    def _try_auto_fallback(self, slot: int, summary: dict, meta: dict) -> bool:
        """秒退时自动降级并重启。返回 True 表示「已接管，别再报异常」。

        为什么在这里做而不是启动前预检：预检要让玩家多等 20~60 秒，
        换来的只是「卡住」这张新截图。这里的判定是**并行观察**，玩家
        不需要多等任何时间。

        防死循环：同一组合只自动重试一次，靠 ``meta["auto_retried"]`` 标记。
        第二次失败就落到 L2/L3，不会无限重启。
        """
        from ...core.ext import autofallback
        from ...core.ext import enable as enable_mod
        from ...core.ext import verify_mark

        # 自定义改版没有扩展概念，不做兜底
        line = str(meta.get("line") or "")
        if not line or meta.get("custom"):
            return False

        if not autofallback.crashed_early(summary):
            return False

        already = bool(meta.get("auto_retried"))
        try:
            crashes = int(meta.get("crashes") or 0) + 1
        except (TypeError, ValueError):
            crashes = 1

        # 收集「可疑」：没有有效验证记录的 + 停用名单里已有的
        unverified: list[str] = []
        try:
            code = verify_mark.code_digest_of(self.cfg, line)
            for pid, _data in enable_mod.enabled_manifests(self.cfg, line):
                root = paths.PACKS / pid
                v = verify_mark.judge(
                    self.cfg, line, pid, root if root.is_dir() else None, code_digest=code or None
                )
                if not v.ok:
                    unverified.append(pid)
        except Exception:  # noqa: BLE001 - 判定失败就不降级，别乱停包
            unverified = []

        plan = autofallback.decide(
            crashes=crashes, unverified=unverified, already_retried=already
        )
        if not plan.active:
            return False

        if plan.manual:
            self.log(plan.message, "err")
            self.library.set_state("exited", "启动失败（需要修复）")
            return False

        try:
            if plan.level == autofallback.LEVEL_ALL:
                ids = [pid for pid, _ in enable_mod.enabled_manifests(self.cfg, line)]
            else:
                ids = plan.disable
            enable_mod.set_temp_disabled(line, ids, plan.message)
        except Exception as exc:  # noqa: BLE001
            self.log(f"自动停用失败（将按原样继续）：{exc}", "warn")
            return False

        self.log(plan.message, "warn")
        self.library.set_state("exited", "已自动重启")
        self.meta_auto_retry = {"line": line, "crashes": crashes, "auto_retried": True}

        # 重启一次。用 QTimer 错开一点，避免和「退出处理」同一帧内再拉起进程
        from PySide6.QtCore import QTimer

        QTimer.singleShot(300, lambda: self._launch(line))
        return True

    def _on_instance_finished(self, slot: int, summary: dict) -> None:
        rc = summary.get("returncode")
        secs = summary.get("seconds")
        tag = f"实例 {slot}"
        ok = bool(summary.get("ok"))

        # 自动兜底：秒退时自动停用可疑扩展并重启，让玩家**看不到**失败。
        #
        # 判定必须在下面这套「正常/手动/异常」日志之前做：兜底成功的话，
        # 玩家不该看到「游戏异常退出」那行红字（那正是截图发群的来源）。
        meta_before = dict(self.instance_meta.get(slot) or {})
        handled = self._try_auto_fallback(slot, summary, meta_before)

        if ok:
            self.log(f"{tag} 游戏退出，退出码 {rc}，运行 {secs} 秒。", "ok")
            # 正常退出同样说明插桩加载成功：补一次状态写回，避免窗口关得快、
            # 5 秒定时器没来得及触发时一直停在「待插桩」。
            if float(secs or 0) >= self.READY_AFTER_SECONDS:
                self._mark_ready_now(
                    str(meta_before.get("line") or ""),
                    meta_before.get("ready_ids") or [],
                )
            if slot == 1:
                self.library.set_state("exited", f"上次运行 {secs} 秒")
            # 正常跑够久 → 说明这组扩展是好的，清掉上次的临时停用记录
            try:
                if float(secs or 0) >= 30.0:
                    from ...core.ext import enable as enable_mod

                    if enable_mod.temp_disabled(str(meta_before.get("line") or "")):
                        enable_mod.clear_temp_disabled(str(meta_before.get("line") or ""))
                        self.log("本次启动正常，已恢复上次被临时停用的扩展", "info")
            except Exception:  # noqa: BLE001
                pass
        elif summary.get("killed"):
            self.log(f"{tag} 游戏已被结束（{secs} 秒）。", "warn")
            if slot == 1:
                self.library.set_state("exited", "已手动结束")
        elif handled:
            # 兜底已经接管：不要再说「异常退出」，那会让玩家以为出了问题
            if slot == 1:
                self.library.set_state("exited", "已自动重启")
        else:
            self.log(f"{tag} 游戏异常退出，退出码 {rc}。", "err")
            for err in summary.get("errors") or []:
                self.log(f"  {err}", "err")
            if slot == 1:
                self.library.set_state("exited", f"异常退出（{rc}）")
        saves = summary.get("saves") or {}
        delta = saves.get("delta") or {}
        if delta.get("total"):
            self.log(saves.get("summary") or "存档有变化", "info")
            backup = saves.get("backup") or ""
            if backup:
                self.log(f"  启动前已备份到 {backup}", "dim")

        # 成功即删：这次启动的工作目录只在运行期间有用（packset/mods/mount_report），
        # 游戏正常退出后就没价值了。异常退出则保留现场——mount_report.json 是
        # 排查「为什么没挂上」的第一手证据，删早了等于把现场也删了。
        run_dir = (self.instance_meta.get(slot) or {}).get("run_dir")
        self.instance_meta.pop(slot, None)
        if run_dir:
            if ok:
                from ...core import housekeeping

                if housekeeping.cleanup_run(run_dir, ok=True):
                    self.log("已清理本次启动的工作目录", "dim")
            else:
                self.log(
                    "本次启动的工作目录已保留（含 mount_report.json），7 天后自动清理",
                    "dim",
                )
        self._sync_launch_button()

    # ------------------------------------------------------------ 配置

    def _on_cfg_saved(self) -> None:
        self.cfg.clear()
        self.cfg.update(cfg_mod.load())
        self.library.cfg = self.cfg
        self.extensions.cfg = self.cfg
        self.build_page.cfg = self.cfg
        self.diagnostics.cfg = self.cfg
        self.library.refresh()
        self.diagnostics.refresh()
        self.build_page.refresh()
        self._sync_launch_button()
        # 设置页改动的即时生效：
        #  · 纯净模式 → 导航按钮显隐
        #  · HUB 三区段 → 视图内元素显隐
        #
        # 这两项都**不会**把用户从当前页面抽走，可以无条件应用。
        self._apply_pure_mode()
        self._apply_hub_regions()

        # 「状态面板默认隐藏」则要谨慎：用户正在设置页操作，
        # 强制收起会把刚点的页面抽走 —— 只在**当前仍是 HUB**时应用新值。
        on_hub = (
            hasattr(self, "content_stack")
            and self.content_stack.currentIndex() == 1
        )
        if on_hub:
            self._apply_panel_visibility()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if not getattr(self, "_titlebar_tinted", False):
            # winId 只有真正建出原生窗口后才有意义，必须等 show 之后
            self._titlebar_tinted = _tint_titlebar(self)
        self._init_log_size_once()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        busy = [p for p in self.instances.values() if p.running]
        if busy or self.mini_proc.running:
            if getattr(self, "_closing", False):
                # 已经在收尾流程里，别让重复的 close() 把等待打断
                event.ignore()
                return
            self._closing = True
            self._closing_deadline = time.time() + 12
            self.log(f"窗口关闭，正在结束 {len(busy)} 个运行中的实例…", "warn")
            for proc in busy:
                proc.stop()
            if self.mini_proc.running:
                self.mini_proc.stop()
            # 原先固定 1.2s 后直接 close：进程通常还没退，Qt 退出时把它硬杀，
            # 正在写的 shader_cache 只写一半 → 下次启动角色立绘全白。
            # 现在等进程真退（最多 12s），退不掉由 stop() 的 5s 定时强杀兜底，
            # 强杀后 _on_finished 会清缓存，这里再补一次。
            QTimer.singleShot(300, self._wait_children_then_close)
            event.ignore()
            return
        # 关窗前把队列里没刷完的日志写出去，否则最后几行会丢
        if hasattr(self, "log_view"):
            self.log_view.flush_now()
        super().closeEvent(event)

    def _wait_children_then_close(self) -> None:
        """等游戏进程退出，再清理缓存并真正关窗。"""
        alive = [p for p in self.instances.values() if p.running]
        if self.mini_proc.running:
            alive.append(self.mini_proc)
        if alive and time.time() < getattr(self, "_closing_deadline", 0):
            QTimer.singleShot(300, self._wait_children_then_close)
            return
        self._purge_caches_on_exit()
        self.close()

    def _purge_caches_on_exit(self) -> None:
        """退出前把本次跑过的存档档位的着色器缓存清掉。

        清缓存不是「优化」，是防坏缓存：只要这一次不是正常退出（强杀、
        启动器被关、崩溃），留下的缓存下一次启动就会让全部材质变白。
        正常退出时清掉也只是下次多编译 1–3 秒。
        """
        from ...core import runner

        seen: set[tuple[str, str | None]] = set()
        for meta in getattr(self, "instance_meta", {}).values():
            line = meta.get("line")
            profile = meta.get("profile")
            if not line or (line, profile) in seen:
                continue
            seen.add((line, profile))
            freed = runner.purge_shader_cache(self.cfg, line, profile)
            if freed:
                self.log(f"退出前已清 {line} 着色器缓存 {freed / 1048576:.1f} MB", "dim")
