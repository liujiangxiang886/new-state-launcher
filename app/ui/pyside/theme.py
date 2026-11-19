"""界面主题：配色、字体、尺寸常量与 QSS。

布局参考 PCL 启动器（左侧图标导航 + 右侧内容区 + 底部状态条），
但视觉与实现全部自写，不使用其任何代码或资源。

配色取舍（v4，「机甲旋风」——呼应玩家提供的官网素材）：

- 底：深蓝黑（#081224），层次靠明度差，不靠纯黑纯白。
  原 v3 是深紫黑(#160e24)，配青蓝 LOGO / 银色机甲会发脏，整套换成深蓝系。
- 主强调色 = 科技青蓝（#00a8ff → #4dc8ff）：导航选中、开关、数字徽章、强调文字；
- 次强调色 = 金橙（#ff9a10 → #ffb838）：主启动按钮、底部橙色导航条；
- 状态色 = 绿（#4cd696，就绪/已启用）、红（#f0606e，危险）；
- 中性色统一带蓝调，整体协调而非冷灰。

LIGHT 配色保留 v3 的紫色系不动：它是 ``--light`` 命令行用的备用主题，
不参与机甲风外壳；改它只会扩大这次改动的回归面。
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Palette:
    window: str
    sidebar: str
    sidebar_hover: str
    sidebar_active: str
    card: str
    card_hover: str
    border: str
    border_strong: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_press: str
    success: str
    warn: str
    danger: str
    shadow: str
    #: 半透明面板色，只在「背景图模式」下由 qss() 填；为空时各处回退到 card
    panel: str = ""
    #: 渐变用的第二强调色（粉橙渐变的橙色端），不参与单色填充
    accent2: str = ""


#: 主按钮渐变——琥珀橙（参考图的大启动按钮），深字可达高对比度
PRIMARY_GRADIENT = (
    "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f0a030, stop:1 #ffc24d)"
)
PRIMARY_GRADIENT_HOVER = (
    "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f5ab38, stop:1 #ffce6b)"
)
PRIMARY_GRADIENT_PRESS = (
    "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #d98c1f, stop:1 #f0a030)"
)


DARK = Palette(
    # 机甲风：深蓝底 + 科技青蓝 + 金橙（取自官网素材实测色）。
    # 原来的深紫黑(#160e24)配青蓝 LOGO 会发脏，必须整套换。
    window="#081224",
    sidebar="#0a1830",
    sidebar_hover="#102845",
    sidebar_active="#14345c",
    card="#0b1c36",
    card_hover="#102a4e",
    border="#143e6c",
    border_strong="#1d5892",
    text="#EBF6FF",
    text_dim="#A0C3E4",
    # 蓝灰：压在卡片底色 #0b1c36 上对比度约 4.8:1，达 WCAG AA 正文要求
    text_faint="#648CB4",
    accent="#00a8ff",
    accent_hover="#4dc8ff",
    accent_press="#0088d8",
    success="#4cd696",
    warn="#ff9a10",
    danger="#f0606e",
    shadow="#050c1a",
    accent2="#ffb838",
)

LIGHT = Palette(
    window="#f3eefb",
    sidebar="#fbf7ff",
    sidebar_hover="#f1e9fb",
    sidebar_active="#e9dcfa",
    card="#ffffff",
    card_hover="#faf6ff",
    border="#e7dcf2",
    border_strong="#d6c2ec",
    text="#241634",
    text_dim="#5c4a72",
    text_faint="#8a78a0",
    accent="#7c3aed",
    accent_hover="#8b5cf6",
    accent_press="#6d28d9",
    success="#1f9d52",
    warn="#b9760a",
    danger="#d6453f",
    shadow="#e4d8f0",
    accent2="#e8933f",
)

#: 尺寸（与 QSS 里保持一致）
SIDEBAR_W = 196
NAV_ITEM_H = 46
HEADER_H = 64
FOOTER_H = 38
CARD_RADIUS = 10
CONTENT_MARGIN = 22


def qpalette(p: Palette):
    """构造与主题一致的 QPalette。

    为什么必须要这一步：QSS 只覆盖了显式声明过的控件类型。像
    ``QScrollArea`` 内部包页面用的裸 ``QWidget``、日志抽屉这类容器，
    Qt 会拿**应用默认调色板**填充。默认调色板是浅色的（#efefef），
    结果就是深紫卡片浮在一片浅灰上、浅色标题文字压在浅底上完全看不清。

    把调色板整体设成深紫体系后，任何「没被 QSS 管到」的控件回退到的
    也是深色，不会再出现浅色破洞。QSS 优先级高于调色板，所以已被
    样式覆盖的控件外观不受影响。
    """
    from PySide6.QtGui import QColor, QPalette

    pal = QPalette()
    window = QColor(p.window)
    card = QColor(p.card)
    text = QColor(p.text)
    faint = QColor(p.text_faint)

    pal.setColor(QPalette.ColorRole.Window, window)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, card)
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(p.sidebar))
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, card)
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.BrightText, QColor(p.danger))
    pal.setColor(QPalette.ColorRole.ToolTipBase, card)
    pal.setColor(QPalette.ColorRole.ToolTipText, text)
    pal.setColor(QPalette.ColorRole.PlaceholderText, faint)
    pal.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.Link, QColor(p.accent_hover))
    pal.setColor(QPalette.ColorRole.LinkVisited, QColor(p.accent))

    # 禁用态统一压暗，避免只剩浅灰看不清
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(QPalette.ColorGroup.Disabled, role, faint)

    return pal


def _rgba(hex_color: str, alpha: float) -> str:
    """#rrggbb -> rgba(r,g,b,a)，用于背景图模式下的半透明面板。"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:.2f})"


def qss(p: Palette, arrow: str = "", translucent: bool = False) -> str:
    """生成全局样式表。用 property 选择器区分按钮变体，避免每个按钮单独设样式。

    ``arrow`` 是下拉箭头的 PNG 路径——QSS 接管外观后 Qt 不再画默认箭头，
    必须由样式表显式给出 ``::down-arrow`` 的图片。
    """
    arrow_rule = (
        f'QComboBox::down-arrow {{ image: url("{arrow}"); width: 10px; height: 10px; }}'
        if arrow
        else ""
    )
    if translucent:
        # 只让侧栏与大卡片半透明。输入框/表格必须保持不透明——
        # QLineEdit 的半透明背景会和调色板底色合成，深色主题下会翻白（踩过）。
        # 透明度不能太高：0.72 时背景图的亮区会透进来，浅色文字压不住，
        # 实测就是「文字看不清」。0.88 仍能透出背景纹理，同时保住对比度。
        p = replace(p, sidebar=_rgba(p.sidebar, 0.88), panel=_rgba(p.card, 0.86))
    return f"""
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    outline: none;
}}

QWidget#Root {{
    background: {p.window};
    color: {p.text};
}}

/* ---------------- 侧边栏 ---------------- */

QWidget#Sidebar {{
    background: {p.sidebar};
    border-right: 1px solid {p.border};
}}

/* 品牌区：机甲风下改半透明深蓝，让背景立绘能透出来 */
QWidget#Brand {{
    background: transparent;
    border: none;
}}

QFrame#BrandCard {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(10,30,58,0.88), stop:1 rgba(14,42,76,0.88));
    border: 1px solid {p.border_strong};
    border-radius: 10px;
}}

QLabel#BrandName {{
    color: #f2f9ff;
    font-size: 16px;
    font-weight: 700;
}}

QLabel#BrandBadge {{
    color: #9fd8ff;
    font-size: 9px;
    background: rgba(0, 168, 255, 0.20);
    border: 1px solid rgba(0, 168, 255, 0.48);
    border-radius: 7px;
    padding: 1px 5px;
}}

QLabel#BrandMeta {{
    color: #8fb6d8;
    font-size: 10px;
}}

QLabel#BrandSub {{
    color: {p.text_faint};
    font-size: 11px;
}}

QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-left: 3px solid transparent;
    border-radius: 8px;
    color: {p.text_dim};
    text-align: left;
    padding-left: 11px;
    font-size: 13px;
}}
QPushButton#NavButton:hover {{
    background: {p.sidebar_hover};
    color: {p.text};
}}
QPushButton#NavButton:checked {{
    background: {p.sidebar_active};
    border-left: 3px solid {p.accent};
    color: {p.text};
    font-weight: 600;
}}

/* ---------------- 通用 ---------------- */

QLabel#PageTitle {{
    font-size: 20px;
    font-weight: 600;
    color: {p.text};
}}
QLabel#PageSub {{
    font-size: 12px;
    color: {p.text_faint};
}}
QLabel#Muted {{ color: {p.text_dim}; font-size: 12px; }}
QLabel#Faint {{ color: {p.text_faint}; font-size: 11px; }}
QLabel#CardTitle {{ font-size: 16px; font-weight: 600; color: {p.text}; }}
QLabel#CardSub {{ font-size: 12px; color: {p.text_dim}; }}
QLabel#SectionTitle {{ font-size: 13px; font-weight: 600; color: {p.text}; }}
/* 存档路径这类「要复制给别人的等宽文字」：换行但不许撑宽页面 */
QLabel#PathText {{
    color: {p.text_dim};
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 11px;
    background: {p.sidebar};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 5px 8px;
}}

QFrame#Card {{
    background: {p.panel or p.card};
    border: 1px solid {p.border};
    border-radius: {CARD_RADIUS}px;
}}
QFrame#Card:hover {{
    background: {p.card_hover};
    border: 1px solid {p.border_strong};
}}

QFrame#Divider {{
    background: {p.border};
    max-height: 1px;
    border: none;
}}

/* ---------------- 按钮 ---------------- */

QPushButton {{
    background: {p.card};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    color: {p.text};
    padding: 7px 16px;
    font-size: 13px;
}}
QPushButton:hover {{ background: {p.card_hover}; border-color: {p.accent}; }}
QPushButton:disabled {{ color: {p.text_faint}; border-color: {p.border}; }}

/* ---------------- 自绘机甲控件：必须清掉 QSS 背景 ---------------- */

/* 这几个都是 ``QPushButton`` / ``QWidget`` 子类，靠 paintEvent 自己画图。
   通用 ``QPushButton`` 规则会给它们铺一层**不透明卡片底**，
   自绘的圆形/圆角图形盖不住整块矩形，四个角就露出深色方块
   （2026-11-09 用户两次截图反馈「黑色方框」：启动按钮周围一块 244x244、
   底部导航条一条 1080x66）。

   ``setAttribute(WA_TranslucentBackground)`` **挡不住 QSS 背景** ——
   QSS 的 background 优先于该属性。必须在 QSS 里显式写 transparent，
   且要放在通用规则**之后**（同优先级下后者覆盖前者）。

   不用 ``border: none`` 之外的修饰：这些控件的描边/底色由 paintEvent 负责。 */
QPushButton#MechaLaunch,
QPushButton#MechaLaunch:hover,
QPushButton#MechaLaunch:pressed,
QPushButton#MechaLaunch:disabled {{
    background: transparent;
    border: none;
}}

QPushButton#MechaNav,
QPushButton#MechaNav:hover,
QPushButton#MechaNav:pressed,
QPushButton#MechaNav:checked {{
    background: transparent;
    border: none;
}}

QPushButton#MechaCapsule,
QPushButton#MechaCapsule:hover,
QPushButton#MechaCapsule:pressed {{
    background: transparent;
    border: none;
}}

/* 导航条容器与左栏：本身不画任何东西，只是布局容器 */
QWidget#MechaNavbar {{
    background: transparent;
    border: none;
}}

/* ---------------- 布局容器：必须透明，否则会盖住背景立绘 ---------------- */

/* **这一类是最隐蔽的**：QStackedWidget / QWidget 容器默认不画背景时
   会回退到调色板 Window 色（主题里是 #081224），相当于给整个右半区
   铺一层不透明深蓝，把机甲立绘切掉一块、并在左栏边界留下一条竖线。

   2026-11-09 用户连续反馈「左栏那块半透明黑色板块」——查到最后
   不是遮罩、不是左栏控件，而是 ``ContentStack`` 这个 QStackedWidget
   从 x=300 开始铺了一层实色。它既没设 WA_TranslucentBackground，
   QSS 里也没有规则（``QStackedWidget`` 默认吃 ``QWidget`` 的通配规则）。 */
QStackedWidget#ContentStack {{
    background: transparent;
    border: none;
}}

QStackedWidget#PageStack {{
    background: transparent;
    border: none;
}}

QWidget#ContentHost {{
    background: transparent;
    border: none;
}}

QPushButton[variant="primary"] {{
    background: {PRIMARY_GRADIENT};
    border: 1px solid transparent;
    color: #1a1020;
    font-weight: 700;
}}
QPushButton[variant="primary"]:hover {{ background: {PRIMARY_GRADIENT_HOVER}; border-color: transparent; }}
QPushButton[variant="primary"]:pressed {{ background: {PRIMARY_GRADIENT_PRESS}; }}
QPushButton[variant="primary"]:disabled {{
    background: {p.border};
    border-color: {p.border};
    color: {p.text_faint};
}}

QPushButton[variant="ghost"] {{
    background: transparent;
    border: 1px solid {p.border_strong};
    color: {p.text_dim};
}}
QPushButton[variant="ghost"]:hover {{ color: {p.text}; border-color: {p.accent}; }}

QPushButton[variant="danger"] {{
    background: transparent;
    border: 1px solid {p.danger};
    color: {p.danger};
}}

/* ---------------- 输入 ---------------- */

QLineEdit, QComboBox, QSpinBox {{
    background: {p.card};
    border: 1px solid {p.border_strong};
    border-radius: 7px;
    color: {p.text};
    padding: 6px 10px;
    selection-background-color: {p.accent};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {p.accent}; }}
QComboBox::drop-down {{ width: 22px; border-left: 1px solid {p.border}; }}
{arrow_rule}
QComboBox QAbstractItemView {{
    background: {p.card};
    border: 1px solid {p.border_strong};
    color: {p.text};
    selection-background-color: {p.accent};
}}

/* ---------------- 日志 ---------------- */

QPlainTextEdit#LogView {{
    background: #16110f;
    border: 1px solid {p.border};
    border-radius: 8px;
    color: #e6d6da;
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 11px;
    padding: 8px;
}}

/* ---------------- 表格 ---------------- */

QTableWidget, QTreeWidget {{
    background: {p.card};
    alternate-background-color: {p.sidebar};
    border: 1px solid {p.border};
    border-radius: 8px;
    gridline-color: {p.border};
    color: {p.text};
    font-size: 12px;
    selection-background-color: {p.accent};
    selection-color: #ffffff;
}}
QHeaderView::section {{
    background: {p.sidebar};
    color: {p.text_dim};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 6px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background: {p.sidebar}; border: none; }}

/* ---------------- 游戏库：已接入扩展列表 ---------------- */

/* 空状态：虚线框暗示「可以把文件拖到这里」，比一行灰字更清楚。
   描边用 border_strong 而不是 border：后者在深色卡片上几乎看不见（实测）。 */
QFrame#ModEmpty {{
    background: transparent;
    border: 1px dashed {p.border_strong};
    border-radius: 10px;
}}
QLabel#ModEmptyTitle {{
    color: {p.text};
    font-size: 13px;
    font-weight: 600;
}}
QLabel#ModEmptyText {{
    color: {p.text_dim};
    font-size: 11px;
}}

QScrollArea#ModScroll, QWidget#ModList {{
    background: transparent;
}}

QWidget#ModRow {{
    background: {p.card_hover};
    border: 1px solid {p.border};
    border-radius: 8px;
}}
QWidget#ModRow:hover {{
    border: 1px solid {p.border_strong};
}}
QWidget#ModRow[sel="1"] {{
    background: {p.sidebar_active};
    border: 1px solid {p.accent};
}}

QLabel#ModIndex {{
    color: {p.text_faint};
    font-size: 12px;
    font-weight: 600;
}}

QLabel#ModName {{
    color: {p.text};
    /* 13 -> 14（2026-11-09 用户要求「字体适中一些」）：
       右栏从 13 份加到 17 份后每行变宽，13px 显得偏小、留白过多 */
    font-size: 14px;
    font-weight: 600;
}}

QLabel#ModMeta {{
    color: {p.text_faint};
    font-size: 11px;
}}

QLabel#ModState {{
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 8px;
}}
QLabel#ModState[on="1"] {{
    color: {p.success};
    background: rgba(76, 196, 124, 0.14);
}}
QLabel#ModState[on="0"] {{
    color: {p.text_faint};
    background: rgba(126, 111, 160, 0.14);
}}
/* 开发者模式下会被整组剔除：与「停用」区分开，用警示色 */
QLabel#ModState[on="2"] {{
    color: {p.warn};
    background: rgba(240, 160, 48, 0.16);
}}

/* ---------------- 版本选择器 ---------------- */

QFrame#Selector {{
    background: {p.panel or p.card};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QFrame#Selector:hover {{
    background: {p.card_hover};
    border: 1px solid {p.accent};
}}
QFrame#Popup {{
    background: {p.card};
    border: 1px solid {p.border_strong};
    border-radius: 10px;
}}
QFrame#VersionRow {{
    background: transparent;
    border: none;
    border-radius: 8px;
}}
QFrame#VersionRow:hover {{
    background: {p.sidebar_hover};
}}

/* ---------------- 扩展状态栏 ---------------- */

QFrame#ExtStatus {{
    background: {p.sidebar};
    border-top: 1px solid {p.border};
}}

/* ---------------- 滚动条 ---------------- */

QScrollArea {{ background: transparent; border: none; }}

/* ---------------- 分隔条（日志区可拖动） ----------------
   日志抽屉与页面区之间由 QSplitter 分隔，用户拖动即可调整日志高度。
   默认手柄是纯背景色、完全看不见，用户不会知道这里能拖——
   所以画一条细线 + hover 时变亮，明确提示「可拖动」。 */
QSplitter#MainSplit::handle:vertical {{
    background: {p.border};
    height: 7px;
    margin: 0 1px;
}}
QSplitter#MainSplit::handle:vertical:hover {{
    background: {p.accent};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    /* 深紫底上不能用高对比色：内容只超出一点点时把手会几乎撑满高度，
       颜色一亮就成了一条贯穿整屏的浅紫竖条，比没滚动条还显眼。
       但也不能用 border（#2c1d45）——压在 #271a40 的卡片上等于消失，
       列表被裁掉半行时玩家根本看不出这栏还能滚。折中取 border_strong。 */
    background: {p.border_strong};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QProgressBar {{
    background: {p.border};
    border: none;
    border-radius: 2px;
    height: 4px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 2px;
}}

QToolTip {{
    background: {p.card};
    color: {p.text};
    border: 1px solid {p.border_strong};
    padding: 5px 8px;
}}

/* ---------------- 对话框（QMessageBox / QDialog） ----------------
   不写这段时的真实表现（用户截图反馈）：Qt 用**原生浅色底板**画对话框，
   而文字颜色来自本主题调色板（#ECE8F5 近白），于是「近白字压白底」，
   「导入游戏 EXE」确认框几乎读不出字。

   这里显式给对话框底色与正文色，让弹窗和主界面是同一套配色；
   同时管住 Qt 内部自带的 QLabel（消息正文 / 详细说明 / 图标区），
   它们不会走 #Muted、#Faint 这些自定义 objectName 规则。 */
QDialog, QMessageBox {{
    background: {p.window};
    color: {p.text};
}}
QDialog QLabel, QMessageBox QLabel {{
    color: {p.text};
    background: transparent;
}}
QMessageBox QLabel#qt_msgbox_label {{
    color: {p.text};
    font-size: 13px;
}}
QMessageBox QLabel#qt_msgbox_informativelabel {{
    color: {p.text_dim};
    font-size: 12px;
}}
QDialog QTextEdit, QMessageBox QTextEdit {{
    background: {p.card};
    color: {p.text_dim};
    border: 1px solid {p.border};
    border-radius: 6px;
}}
QDialog QCheckBox, QMessageBox QCheckBox {{
    color: {p.text_dim};
}}
QDialogButtonBox {{ background: transparent; }}
QDialogButtonBox QPushButton {{ min-width: 76px; }}
"""
