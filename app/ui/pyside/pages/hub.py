"""HUB 主页视图 —— 状态面板隐藏时占据右侧内容区。

为什么放在右侧内容区，而不是新增一个导航页
------------------------------------------
2026-11-09 用户要求：**默认打开就是 HUB**（立绘完整 + 只有启动按钮），
点导航才进各个功能页。而左侧栏（LOGO/标题/圆形启动按钮）本来就是常驻的，
所以「HUB」本质上是 **``right`` 容器在隐藏态时显示的替代内容**，
不是一个独立页面。

三个区域**各自独立可隐藏**（2026-11-09 用户圈出红框要求）
--------------------------------------------------------
============================  ==================================
区域                          配置键
============================  ==================================
扩展与状态面板                 ``ui.hub_hide_stats``
当前配置条                     ``ui.hub_hide_config``
底部扩展状态条 + 页脚          ``ui.hub_hide_statusbar``
============================  ==================================

三者**互不影响**，都默认隐藏（用户诉求是「不影响整体观感」）。
全隐藏时 HUB 只剩左侧圆形启动按钮 —— 就是最干净的那一版。

视觉内容
--------
刻意保持半透明、面积小。素材缺失时全部走纯色降级。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import mecha_assets as assets

C_TEXT = QColor("#EBF6FF")
C_TEXT_DIM = QColor("#A0C3E4")
C_FAINT = QColor("#648CB4")
C_GREEN = QColor("#4CD696")
C_AMBER = QColor("#FFB838")
C_CYAN = QColor("#00A8FF")
C_BORDER = QColor("#143E6C")
C_CARD = QColor(11, 28, 54, 226)


class _Stat(QWidget):
    """统计格：大数字 + 小标题。"""

    def __init__(self, value: str, label: str, color: QColor,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = value
        self._label = label
        self._color = color
        self.setMinimumSize(96, 52)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

    def set_value(self, value: str) -> None:
        if value != self._value:
            self._value = value
            self.update()

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        p.setPen(QPen(QColor(25, 80, 140), 1))
        p.setBrush(QColor(6, 20, 40, 200))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)

        p.setFont(QFont("Microsoft YaHei", 15, QFont.Weight.Bold))
        p.setPen(self._color)
        p.drawText(QPointF(11, 29), self._value)

        p.setFont(QFont("Microsoft YaHei", 8))
        p.setPen(C_FAINT)
        p.drawText(QPointF(11, 45), self._label)
        p.end()


class _Panel(QFrame):
    """带科技蓝边框的面板容器（优先用 panel.png）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pix = assets.pixmap("panel")

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        r = QRectF(self.rect())
        if self._pix.isNull():
            p.setPen(QPen(C_BORDER, 1))
            p.setBrush(QColor(9, 24, 46, 220))
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        else:
            p.drawPixmap(self.rect(), self._pix)
        p.end()


class HubView(QWidget):
    """HUB 视图。只负责展示，不含任何业务逻辑。

    ``MainWindow`` 在 ``_sync_launch_button`` 等时机调 :meth:`set_stats`
    / :meth:`set_config` 刷新内容。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HubView")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        # 右侧内容区：左上留白避开品牌条，右下留白避开导航条。
        # 左边距 40 是为了不顶到左侧栏的圆形启动按钮（实测 24 太近，
        # 面板边框和按钮外圈几乎贴上）。
        outer.setContentsMargins(40, 16, 26, 20)
        outer.setSpacing(10)
        outer.addStretch(1)

        # ---------- 状态面板
        self.panel = _Panel()
        self.panel.setMinimumHeight(196)
        self.panel.setMaximumHeight(240)
        self.panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        pv = QVBoxLayout(self.panel)
        # panel.png 边框实测约 22px 厚，右上角还有一块斜切的银色装饰。
        # 内边距必须 > 24，否则标题和右上角结论会被压住（踩过两次：
        # 第一次 26/14 标题被压，第二次 36/30 右上角「全部正常」仍被压）。
        pv.setContentsMargins(36, 34, 52, 26)
        pv.setSpacing(10)

        head = QHBoxLayout()
        t = QLabel("扩展与状态")
        t.setStyleSheet(
            "color:#EBF6FF; font-size:13px; font-weight:700;"
            " background:transparent;"
        )
        head.addWidget(t)
        head.addStretch(1)
        self.verdict = QLabel("")
        self.verdict.setStyleSheet(
            "color:#4CD696; font-size:10px; background:transparent;"
        )
        head.addWidget(self.verdict)
        pv.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.stat_ext = _Stat("–", "已装扩展", C_GREEN)
        self.stat_conf = _Stat("–", "冲突", C_GREEN)
        self.stat_story = _Stat("4.7", "剧情线", C_CYAN)
        for w in (self.stat_ext, self.stat_conf, self.stat_story):
            row.addWidget(w)
        pv.addLayout(row)

        self.summary = QLabel("")
        self.summary.setStyleSheet(
            "color:#648CB4; font-size:10px; background:transparent;"
        )
        self.summary.setWordWrap(True)
        pv.addWidget(self.summary)
        pv.addStretch(1)

        outer.addWidget(self.panel)

        # ---------- 当前配置
        self.config = QLabel("")
        self.config.setStyleSheet(
            "color:#A0C3E4; font-size:10px; background:transparent;"
            " padding:10px 14px; border:1px solid #143E6C;"
            " border-radius:9px;"
        )
        self.config.setWordWrap(True)
        outer.addWidget(self.config)
        outer.addStretch(2)

    # ---------------------------------------------------------- 刷新接口

    def set_stats(self, ext: int, conflicts: int, story: str = "4.7") -> None:
        self.stat_ext.set_value(str(ext))
        self.stat_conf.set_value(str(conflicts))
        self.stat_conf.set_color(C_GREEN if conflicts == 0 else C_AMBER)
        self.stat_story.set_value(story)

    def set_summary(self, text: str, ok: bool = True) -> None:
        self.summary.setText(text)
        self.verdict.setText("全部正常" if ok else "有异常")
        self.verdict.setStyleSheet(
            f"color:{'#4CD696' if ok else '#FFB838'}; font-size:10px;"
            " background:transparent;"
        )

    def set_config(self, rows: list[tuple[str, str]]) -> None:
        self.config.setText(
            "当前配置　" + "　".join(f"{k} {v}" for k, v in rows)
        )

    # ---------------------------------------------------------- 三个区域的显隐

    def set_region_visible(
        self, region: str, visible: bool
    ) -> None:
        """按区域显隐。``region`` 取 ``stats`` / ``config`` / ``statusbar``。

        ``statusbar`` 不归这个视图管（它是主窗口的 ``ext_status`` 和页脚），
        由 ``MainWindow`` 自己处理 —— 这里只留接口占位，避免调用方判断。
        """
        if region == "stats":
            self.panel.setVisible(visible)
        elif region == "config":
            self.config.setVisible(visible)