"""通用控件：导航按钮、卡片、状态徽标、日志面板。"""

from __future__ import annotations

import time
from typing import Iterable

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

from .assets import chevron
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import colors, icons, theme


# ---------------------------------------------------------------- 导航


class NavButton(QPushButton):
    """左侧导航项：图标 + 文字，选中时图标与文字一起变亮。"""

    def __init__(self, icon_name: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__("  " + text, parent)
        self.setObjectName("NavButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(theme.NAV_ITEM_H)
        self.setIconSize(QSize(18, 18))
        self._icon_name = icon_name
        self._palette = theme.DARK
        self.toggled.connect(self._refresh_icon)
        self._refresh_icon(False)

    def apply_palette(self, p: theme.Palette) -> None:
        self._palette = p
        self._refresh_icon(self.isChecked())

    def _refresh_icon(self, checked: bool) -> None:
        color = self._palette.accent if checked else self._palette.text_dim
        self.setIcon(icons.icon(self._icon_name, color, 18, 1.7))


# ---------------------------------------------------------------- 徽标


class Pill(QLabel):
    """小圆角标签，用于显示状态（已就绪 / 校验通过 / 待接入）。

    颜色走 :mod:`colors`，随主题切换；原先写死的 GitHub 深色系（纯绿/纯橙）
    压在柳江紫底上既不协调，切浅色主题时对比度也会崩。
    """

    def __init__(self, text: str, tone: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.set_tone(tone)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_tone(self, tone: str) -> None:
        bg, fg = colors.tone_colors(tone)
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {colors.tint(fg, 0.35)};"
            f"border-radius:9px; padding:2px 9px; font-size:11px;"
        )


# ---------------------------------------------------------------- 卡片


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class GameCard(Card):
    """游戏版本卡片：图标 + 标题/副标题/标签 + 启动按钮。"""

    launchRequested = Signal(str)
    detailsRequested = Signal(str)

    def __init__(self, key: str, title: str, subtitle: str, badge: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.setFixedHeight(118)

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(16)

        self.badge = QLabel(badge)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedSize(56, 56)
        self.badge.setStyleSheet(
            "background:#12345c; color:#bfe4ff; border:1px solid #2f74b8;"
            "border-radius:12px; font-size:15px; font-weight:700;"
        )
        root.addWidget(self.badge)

        mid = QVBoxLayout()
        mid.setSpacing(5)
        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("CardSub")
        self.subtitle.setWordWrap(True)
        mid.addWidget(self.title)
        mid.addWidget(self.subtitle)

        tags = QHBoxLayout()
        tags.setSpacing(6)
        self.tags: list[Pill] = []
        tag_row = QWidget()
        tag_row.setLayout(tags)
        tags.setContentsMargins(0, 2, 0, 0)
        mid.addWidget(tag_row)
        root.addLayout(mid, 1)

        right = QVBoxLayout()
        right.setSpacing(6)
        self.launch_btn = QPushButton("启动")
        self.launch_btn.setProperty("variant", "primary")
        self.launch_btn.setFixedSize(104, 38)
        self.launch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_btn.clicked.connect(lambda: self.launchRequested.emit(self.key))
        self.detail_btn = QPushButton("详情")
        self.detail_btn.setProperty("variant", "ghost")
        self.detail_btn.setFixedSize(104, 30)
        self.detail_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.detail_btn.clicked.connect(lambda: self.detailsRequested.emit(self.key))
        right.addWidget(self.launch_btn)
        right.addWidget(self.detail_btn)
        right.addStretch(1)
        root.addLayout(right)

    def set_tags(self, items: Iterable[tuple[str, str]]) -> None:
        layout = self.layout().itemAt(1).layout().itemAt(2).widget().layout()
        for pill in self.tags:
            pill.deleteLater()
        self.tags.clear()
        for text, tone in items:
            pill = Pill(text, tone)
            layout.addWidget(pill)  # type: ignore[union-attr]
            self.tags.append(pill)

    def set_busy(self, busy: bool, text: str = "启动中…") -> None:
        self.launch_btn.setEnabled(not busy)
        self.launch_btn.setText(text if busy else "启动")


# ---------------------------------------------------------------- 日志


class LogView(QPlainTextEdit):
    """只读日志面板；按行着色，限制总行数避免长期运行吃内存。

    颜色统一从 :mod:`colors` 取（原先写死的是 GitHub 深色系），
    这样日志里的「绿/橙/红」与界面其它状态色是同一套色号。

    为什么要有节流层
    ----------------

    游戏的 stdout/stderr 是直连过来的：``proc.line.connect(self.log)``。
    第三方扩展 ``jjxf.floating_cannons`` 每帧抛一次错（``charge_hud.gd:175``
    每帧 ``refresh()`` 触发钩子，钩子把入参当 ``{arguments: ...}`` 用），
    实测一次会话灌进来 **78,831 行 / 7.5MB**，其中 ``SCRIPT ERROR`` 8,552 条，
    折合约 **285 行/秒**。每一行都 ``appendHtml`` + 强制滚到底，
    UI 线程被吃满，「关游戏」时积压的队列一次性渲染，卡到界面没反应。

    三层处理
    --------

    1. **批量 flush**：日志先进缓冲区，100ms 的 ``QTimer`` 统一刷一次。
       285 行/秒 摊到 10 次/秒，每次只处理约 28 行。
    2. **重复折叠**：见 :meth:`_scan_blocks`——既折叠「紧挨着的同一行」，
       也折叠「整块重复的报错」。
    3. **条件跟随滚动**：只有滚动条本来就在底部时才跟随到底。
       往上翻日志时不会被新行拽走——这是读日志时的基本预期。
    """

    #: 缓冲区里最多留多少行就开始强制定案（防止一直凑不满一个块）。
    _BLOCK_MAX = 24

    def _color(self, level: str) -> str:
        return {
            "info": colors.text(),
            "ok": colors.ok(),
            "warn": colors.warn(),
            "err": colors.err(),
            "dim": colors.faint(),
        }.get(level, colors.text())

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogView")
        self.setReadOnly(True)
        self.setMaximumBlockCount(4000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.setFont(f)

        #: 待刷的日志。元素是 ``[文本, 级别, 连续重复次数]``。
        self._hold: list[list] = []
        #: 已经定案的行数（``_hold`` 的下标）。之前的内容不会再参与块识别。
        self._finalized = 0
        #: 块指纹 -> 出现次数。指纹是块内所有文本拼起来的哈希。
        self._block_counts: dict[str, int] = {}
        #: 本次会话累计被折叠掉的行数
        self._folded_total = 0
        #: flush 节流间隔（毫秒）
        self._flush_ms = 100
        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self.flush)
        self._flush_timer.start(self._flush_ms)

    # ------------------------------------------------------------ 入队

    def log(self, text: str, level: str = "info") -> None:
        """入队一行日志。不立即渲染——由 100ms 定时器批量刷。"""
        if not text:
            return
        # 紧挨着的同一行：直接累加计数，不占新行
        if self._hold and self._hold[-1][0] == text and self._hold[-1][1] == level:
            self._hold[-1][2] += 1
            return
        self._hold.append([text, level, 0])
        self._scan_blocks()

    def _is_indented(self, text: str) -> bool:
        """缩进行 = 上一个块的续行（Godot 的 backtrace 就是这个形状）。"""
        return bool(text[:1].isspace())

    def _scan_blocks(self) -> None:
        """识别「整块重复」并折叠。

        为什么要按块而不是按行：第三方扩展每帧抛同一条错，每次都带
        5~6 行 backtrace，块内相邻行都不同（是 backtrace 的不同帧），
        所以「紧挨着的同一行」一条都对不上。按块比对才折叠得掉。

        块的形状：**一个非缩进行开头，后面跟若干缩进续行**。
        块的结束由「下一个非缩进行」界定——所以只有等下一块的开头
        到了，才能确定上一块到此为止。

        指纹 = 块内所有文本拼起来的哈希。指纹见过就是整块重复。
        """
        # 已经攒够一块完整的了才定案：必须有下一个非缩进来界定边界。
        # 块首行自己就是非缩进行，所以从 _finalized + 1 往后找。
        while True:
            end = self._find_next_block_start(self._finalized + 1)
            if end is None:
                break
            self._finalize_block(self._finalized, end)
        # 一直凑不满一个块（后面全是缩进续行）时，别让缓冲区无限涨
        if len(self._hold) - self._finalized > self._BLOCK_MAX:
            self._finalize_block(self._finalized, len(self._hold))

    def _find_next_block_start(self, from_idx: int) -> int | None:
        """从 ``from_idx`` 开始找下一个「非缩进行」的下标——它是下一块的开头。

        找不到返回 None（说明还没攒够一块）。
        """
        for i in range(from_idx, len(self._hold)):
            if not self._is_indented(self._hold[i][0]):
                return i
        return None

    def _finalize_block(self, start: int, end: int) -> None:
        """把 ``_hold[start:end]`` 当作一个完整块定案：见过就折叠。"""
        if end <= start:
            self._finalized = max(self._finalized, end)
            return
        # 块首行必须是非缩进行——它标记着「这是一个新块的开始」。
        # 如果首行是缩进（说明上一块没被正确界定），就跳过这一行，
        # 把它并进下一块，避免把块切碎导致指纹永远对不上。
        if self._is_indented(self._hold[start][0]):
            self._finalized = start + 1
            return
        block = self._hold[start:end]
        # 单行块一律不折叠。安装日志里「[ 45%] 生成覆盖层 pck」这类进度行
        # 每装一个包都会重复一次，按块折叠会把整片进度吞掉、只留一行
        # 「（整块重复 15 次）」——2026-11-13 的玩家日志就是这样，报障时
        # 完全看不出装到哪一步。要折叠的是「一行报错 + 若干缩进 backtrace」
        # 那种多行块（第三方扩展每帧抛同一条错就是这个形状）。
        # 相邻的同一行已经由 log() 的计数合并处理，这里不需要再管。
        if len(block) < 2:
            self._finalized = end
            return
        fingerprint = "\n".join(item[0] for item in block)
        seen = self._block_counts.get(fingerprint, 0)
        if seen == 0:
            self._block_counts[fingerprint] = 1
            self._finalized = end
            return
        # 命中重复块：整块撤掉，只留一条计数标记
        del self._hold[start:end]
        self._hold.insert(start, [f"（整块重复 {seen} 次）", block[0][1], 0])
        self._block_counts[fingerprint] = seen + 1
        self._folded_total += len(block) - 1
        self._finalized = start + 1

    # ------------------------------------------------------------ 渲染

    def flush(self) -> None:
        """把缓冲区里的日志一次性写进控件。"""
        if not self._hold:
            return
        batch = self._hold
        self._hold = []
        self._finalized = 0

        # 只有滚动条本来就在底部才跟随到底。玩家往上翻日志时，
        # 新行不该把视口拽走。
        bar = self.verticalScrollBar()
        stick = bar.value() >= bar.maximum() - 2

        html_parts: list[str] = []
        for text, level, repeat in batch:
            color = self._color(level)
            stamp = time.strftime("%H:%M:%S")
            safe = (
                text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            suffix = f"（重复 {repeat} 次）" if repeat else ""
            html_parts.append(
                f'<span style="color:{colors.faint()}">{stamp}</span>&nbsp;'
                f'<span style="color:{color}">{safe}</span>'
                f'<span style="color:{colors.faint()}">{suffix}</span>'
            )
        # 一次 appendHtml 写一整批——比每行一次少几千次控件更新
        self.appendHtml("<br>".join(html_parts))
        if stick:
            bar.setValue(bar.maximum())

    def flush_now(self) -> None:
        """立即把缓冲区刷干净（窗口关闭、导出日志前用）。"""
        self.flush()

    def clear_all(self) -> None:
        """清空控件**和**缓冲区。

        只调 ``clear()`` 不够——节流层里攒着的旧行会在下一次 flush 时
        又冒出来，看着像「清了个寂寞」。
        """
        self._hold.clear()
        self._finalized = 0
        self._block_counts.clear()
        self._folded_total = 0
        self.clear()

    def log_lines(self, text: str, level: str = "dim") -> None:
        for line in text.splitlines():
            if line.strip():
                self.log(line, level)


# ---------------------------------------------------------------- 页头


class PageHeader(QWidget):
    """页面顶部：标题 + 副标题 + 右侧操作区。

    副标题必须允许换行，否则会横向撑爆整页。实测「剧情库」的副标题
    「导入剧情工程目录或官方分区 zip（data / scenes / scripts / ui / fonts /
    social），打成覆盖层叠加到基线上。」单行最小宽度 924px，加上右侧两个
    按钮后 PageHeader 达 1125px——远超 1180 窗口减去侧栏后的可用宽度，
    表现就是整页右侧被裁掉一截（截图里「导入剧情包」按钮只剩一半）。
    这里统一给副标题开启换行并允许被压窄，一次修好所有页面。
    """

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(3)
        self.title = QLabel(title)
        self.title.setObjectName("PageTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("PageSub")
        # 副标题是可变的说明文字，绝不该由它决定页面最小宽度
        self.subtitle.setWordWrap(True)
        self.subtitle.setMinimumWidth(0)
        self.subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        left.addWidget(self.title)
        left.addWidget(self.subtitle)
        root.addLayout(left, 1)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        root.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFixedHeight(1)
    line.setFrameShape(QFrame.Shape.HLine)
    return line


def section_title(text: str, hint: str = "") -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    label = QLabel(text)
    # 颜色交给 QSS 的 #SectionTitle 规则：原先写死 theme.DARK.text，
    # 一旦切浅色主题就是「近白字压浅底」，几乎看不见。
    label.setObjectName("SectionTitle")
    row.addWidget(label)
    if hint:
        h = QLabel(hint)
        h.setObjectName("Faint")
        # hint 必须能换行，否则它会把整页的**最小宽度**撑到自己的整句长度。
        #
        # 实测（2026-11-14）：build 页那句「builds/release-* 下的自测副本；
        # 双击行可打开目录。对外发布的是扩展包，不是这里」单行 517 px，
        # 于是整页 minimumSizeHint 卡在 715 px —— 940 宽的窗口装不下，
        # 出现横向滚动条。低配机器（1366x768 + 150% 缩放）可用宽度只有
        # 911，这条提示直接把该页变成左右拖动的。
        h.setWordWrap(True)
        # 换行标签的默认 sizeHint 仍按整句算，给个 Ignored 的水平策略，
        # 让布局按「可用宽度」而不是「整句宽度」分配。
        h.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(h, 1)
    row.addStretch(1)
    return w


def hline_color(p: theme.Palette) -> QColor:
    return QColor(p.border)


def placeholder_notice(text: str) -> QLabel:
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    # 不加 wordWrap 时，容器一窄这条说明就会被水平截掉一截（剧情库页实测如此）
    label.setWordWrap(True)
    label.setStyleSheet(
        "color:#6b747e; font-size:12px; border:1px dashed #2b3037;"
        "border-radius:10px; padding:26px;"
    )
    return label


def dot(color: str, size: int = 8) -> QPixmap:
    """画一个小圆点，用于状态指示。"""
    px = QPixmap(size * 2, size * 2)
    px.setDevicePixelRatio(2.0)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(0, 0, size, size)
    p.end()
    return px


# ---------------------------------------------------------------- 版本选择器


class _VersionRow(QFrame):
    """下拉列表里的一行：徽标 + 标题/副标题 + 状态。"""

    picked = Signal(str)

    def __init__(self, meta: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = str(meta["key"])
        self.setObjectName("VersionRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(62)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(12)

        badge = QLabel(str(meta.get("badge", self.key)))
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(38, 38)
        badge.setStyleSheet(
            "background:#12345c; color:#bfe4ff; border:1px solid #2f74b8;"
            "border-radius:9px; font-size:12px; font-weight:700;"
        )
        row.addWidget(badge)

        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(str(meta.get("title", self.key)))
        title.setStyleSheet("font-size:13px; font-weight:600;")
        sub = QLabel(str(meta.get("subtitle", "")))
        sub.setObjectName("Faint")
        col.addWidget(title)
        col.addWidget(sub)
        row.addLayout(col, 1)

        self.state = Pill(str(meta.get("state_text", "")), str(meta.get("state_tone", "muted")))
        row.addWidget(self.state)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self.key)
        super().mouseReleaseEvent(event)


class VersionSelector(QFrame):
    """点击展开的版本选择器（不是原生 QComboBox，便于放徽标与状态）。"""

    versionChanged = Signal(str)

    def __init__(self, versions: list[dict], current: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Selector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(64)
        self._versions = versions
        self._current = current
        self._popup: QFrame | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)

        self.badge = QLabel("")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedSize(40, 40)
        self.badge.setStyleSheet(
            "background:#12345c; color:#bfe4ff; border:1px solid #2f74b8;"
            "border-radius:10px; font-size:13px; font-weight:700;"
        )
        row.addWidget(self.badge)

        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = QLabel("")
        self.title.setStyleSheet("font-size:14px; font-weight:600;")
        self.sub = QLabel("")
        self.sub.setObjectName("Faint")
        col.addWidget(self.title)
        col.addWidget(self.sub)
        row.addLayout(col, 1)

        self.chevron = QLabel()
        self.chevron.setFixedSize(12, 12)
        self.chevron.setPixmap(QPixmap(chevron(theme.DARK.text_dim, 12)))
        row.addWidget(self.chevron)

        self._apply_current()

    # ------------------------------------------------------------ 状态

    def set_versions(self, versions: list[dict]) -> None:
        self._versions = versions
        self._apply_current()

    def current_key(self) -> str:
        return self._current

    def set_current(self, key: str, emit: bool = True) -> None:
        if not any(v["key"] == key for v in self._versions):
            return
        changed = key != self._current
        self._current = key
        self._apply_current()
        if changed and emit:
            self.versionChanged.emit(key)

    def _meta(self, key: str) -> dict:
        return next((v for v in self._versions if v["key"] == key), self._versions[0])

    def _apply_current(self) -> None:
        if not self._versions:
            return
        meta = self._meta(self._current)
        self.badge.setText(str(meta.get("badge", meta["key"])))
        self.title.setText(str(meta.get("title", meta["key"])))
        self.sub.setText(str(meta.get("subtitle", "")))

    # ------------------------------------------------------------ 展开

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_popup()
        super().mouseReleaseEvent(event)

    def _toggle_popup(self) -> None:
        if self._popup is not None:
            self._close_popup()
            return

        popup = QFrame(self.window())
        popup.setObjectName("Popup")
        popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        popup.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        popup.setFixedWidth(self.width())

        lay = QVBoxLayout(popup)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(2)
        for meta in self._versions:
            row = _VersionRow(meta, popup)
            row.picked.connect(self._on_picked)
            lay.addWidget(row)
        popup.adjustSize()

        pos = self.mapToGlobal(self.rect().bottomLeft())
        popup.move(pos.x(), pos.y() + 6)
        popup.show()
        self._popup = popup

    def _on_picked(self, key: str) -> None:
        self._close_popup()
        self.set_current(key)

    def _close_popup(self) -> None:
        if self._popup is not None:
            self._popup.close()
            self._popup.deleteLater()
            self._popup = None


# ---------------------------------------------------------------- 扩展状态栏


class ExtStatusBar(QFrame):
    """扩展导入进度条：常驻在底栏上方，拖拽安装与手动导入共用。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ExtStatus")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(34)

        row = QHBoxLayout(self)
        row.setContentsMargins(24, 0, 24, 0)
        row.setSpacing(10)

        self.icon = QLabel()
        self.icon.setFixedSize(14, 14)
        row.addWidget(self.icon)

        self.label = QLabel("扩展：空闲")
        self.label.setObjectName("Faint")
        # 原来写死 470px：状态文字一长就被截断、还把进度条挤变形。
        # 改成占满剩余空间 + 末尾省略，完整内容放 tooltip。
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.label.setToolTip("扩展：空闲")
        row.addWidget(self.label, 1)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        row.addWidget(self.bar, 1)

        self.result = Pill("待命", "muted")
        row.addWidget(self.result)

        self._idle()

    # ------------------------------------------------------------ 状态切换

    def _set_text(self, text: str) -> None:
        """文字按当前可用宽度末尾省略，完整内容进 tooltip。

        安装合集包时状态文字能到上百字符，不省略会把进度条挤成一条缝。
        """
        self.label.setToolTip(text)
        metrics = self.label.fontMetrics()
        width = self.label.width() or 420
        self.label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, width))

    def _idle(self) -> None:
        self.icon.setPixmap(dot("#3a4149", 8))
        self._set_text("扩展：空闲")
        self.bar.setValue(0)
        self.bar.setVisible(False)
        self.result.setText("待命")
        self.result.set_tone("muted")

    def start(self, title: str) -> None:
        self.icon.setPixmap(dot("#3d8bfd", 8))
        self._set_text(f"扩展：正在导入 {title}")
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.result.setText("进行中")
        self.result.set_tone("info")

    def set_progress(self, percent: int, text: str) -> None:
        self.bar.setVisible(True)
        self.bar.setValue(max(0, min(100, int(percent))))
        self._set_text(f"扩展：{text}（{int(percent)}%）")

    def finish(self, ok: bool, message: str, *, result_text: str = "") -> None:
        """收尾。``result_text`` 用来放「3/4」这类批量计数，空串沿用旧文案。"""
        self.icon.setPixmap(dot("#3fb950" if ok else "#f85149", 8))
        self._set_text(f"扩展：{message}")
        self.bar.setValue(100 if ok else self.bar.value())
        if ok:
            self.result.setText(result_text or "已安装")
            self.result.set_tone("ok")
        else:
            self.result.setText(result_text or "失败")
            self.result.set_tone("err")

    # ------------------------------------------------------------ 批量

    def start_batch(self, total: int) -> None:
        """批量导入开场：徽标直接显示 ``0/N``，用户立刻知道这批有几个。"""
        total = max(1, int(total))
        self.icon.setPixmap(dot("#3d8bfd", 8))
        self._set_text(f"扩展：批量导入 0/{total}")
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.result.setText(f"0/{total}")
        self.result.set_tone("info")

    def set_batch(self, index: int, total: int, name: str, percent: int = 0) -> None:
        """第 ``index`` 个（从 1 开始）/ 共 ``total`` 个。"""
        total = max(1, int(total))
        percent = max(0, min(100, int(percent)))
        self.bar.setVisible(True)
        self.bar.setValue(percent)
        self._set_text(f"扩展：[{int(index)}/{total}] {name}（{percent}%）")
        self.result.setText(f"{int(index)}/{total}")
        self.result.set_tone("info")

    def warn(self, message: str) -> None:
        self.icon.setPixmap(dot("#d29922", 8))
        self._set_text(f"扩展：{message}")
        self.result.setText("注意")
        self.result.set_tone("warn")


# ---------------------------------------------------------------- 错误窗口


def error_dialog(parent, title: str, message: str, detail: str = "") -> None:
    """统一的「出错了」窗口：一句话原因 + 可复制的完整日志 + 打开日志目录。

    为什么不用 ``QMessageBox.setDetailedText``：它的详情区在 Windows 原生
    样式下**不能选中复制**，而这段内容的唯一用途就是复制出去发给别人。
    所以这里用只读 ``QPlainTextEdit`` 自己搭一个，并把「复制日志」做成
    主按钮——出问题时用户第一眼就该看到它。
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication, QDialog

    from ...core import paths

    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(680)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(18, 16, 18, 14)
    lay.setSpacing(10)

    head = QLabel(message)
    head.setWordWrap(True)
    lay.addWidget(head)

    payload = message if not detail.strip() else f"{message}\n\n{detail.strip()}"
    body = QPlainTextEdit()
    body.setReadOnly(True)
    body.setPlainText(payload)
    body.setMinimumHeight(240)
    lay.addWidget(body, 1)

    row = QHBoxLayout()
    row.setSpacing(8)
    copy_btn = QPushButton("复制日志")
    copy_btn.setProperty("variant", "primary")

    def _copy() -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(payload)
            copy_btn.setText("已复制")
            from PySide6.QtCore import QTimer

            QTimer.singleShot(1500, lambda: copy_btn.setText("复制日志"))

    copy_btn.clicked.connect(_copy)
    row.addWidget(copy_btn)

    open_btn = QPushButton("打开日志目录")
    open_btn.setProperty("variant", "ghost")
    open_btn.clicked.connect(
        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.LOGS)))
    )
    row.addWidget(open_btn)
    row.addStretch(1)

    close_btn = QPushButton("关闭")
    close_btn.setProperty("variant", "ghost")
    close_btn.clicked.connect(dlg.accept)
    row.addWidget(close_btn)
    lay.addLayout(row)

    dlg.exec()
