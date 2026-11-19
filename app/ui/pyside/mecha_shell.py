"""机甲风外壳的自绘控件。

设计边界（重要）
----------------
这个模块**只做视觉**，不碰任何业务逻辑。所有控件都保持与旧控件相同的
对外接口，这样 ``MainWindow`` 里 576 行以后的业务方法一行都不用改：

============================  ============================================
类名                          替代谁 / 必须保持什么
============================  ============================================
:class:`MechaRoot`            ``RootWidget``：``set_background`` / ``set_base_color``
                              / ``has_background`` / ``resizeEvent``
:class:`MechaLaunchButton`    ``side_launch``：仍是 ``QPushButton``，
                              ``setText`` / ``setEnabled`` / ``setToolTip`` / ``clicked`` 全可用
:class:`MechaNavButton`       ``NavButton``：``clicked`` / ``setChecked`` / ``isChecked``
:class:`MechaBrandBar`        左上 LOGO + 标题 + 创作者 + bilibili 字标
:class:`MechaStatusCapsule`   右下角状态胶囊（也是召唤按钮）
============================  ============================================

为什么 ``MechaLaunchButton`` 必须仍是 QPushButton：
``MainWindow._sync_launch_button`` 会调 ``setText("无法启动")`` /
``setText("运行中…")`` / ``setEnabled(False)``。如果是普通 QWidget，
这几处全得改，业务回归面就大了。现在按钮照常收这些调用，
绘制时按自身状态决定画什么字。

素材缺失时所有控件都优雅降级成纯色/文字，界面照常可用。
"""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QWidget

from . import mecha_assets as assets
from . import theme

#: 机甲深蓝底
C_BG = QColor("#081224")
C_BG_DEEP = QColor("#050c1a")
C_CYAN = QColor("#00a8ff")
C_TEXT = QColor("#ebf6ff")
C_TEXT_DIM = QColor("#a0c3e4")
C_GREEN = QColor("#4cd696")
C_AMBER = QColor("#ffb838")
C_ORANGE = QColor("#ff9a10")


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------- 背景


class MechaRoot(QWidget):
    """窗口根容器：机甲立绘 + 从左到右的渐变暗罩。

    与旧 ``RootWidget`` 的关系：接口完全兼容，``MainWindow.apply_background``
    照常调 ``set_background(path, dim)``。

    行为差异：

    * 用户**没设**自定义背景图（``ui.background_image`` 为空）时，
      画内置的机甲立绘 —— 这就是新 UI 的默认观感
    * 用户**设了**自定义背景图时，完全沿用旧逻辑（等比铺满 + 压暗）
    * 内置背景下额外叠一层从左到右的渐变暗罩：左侧暗到能读文字，
      右侧几乎不遮、立绘完整。**全程无硬边**，也不把任何区域替换成纯色

    ``dim_percent`` 在内置背景下作为「暗罩强度微调」：0 时左侧最暗 0.72，
    100 时 0.92。自定义背景图时仍是原来整片压黑的比例。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self._pix = QPixmap()
        self._dim = 0.55
        self._base = QColor(theme.DARK.window)
        self._cache: QPixmap | None = None
        self._custom = False
        self._mecha_cache: QPixmap | None = None
        self._mecha_cache_size = QSize()
        self._logo_patch: QPixmap | None = None
        self._logo_patch_size = QSize()

    def set_base_color(self, color: str) -> None:
        self._base = QColor(color)
        self.update()

    def set_background(self, path: str, dim_percent: int) -> bool:
        self._dim = max(0, min(100, int(dim_percent))) / 100.0
        self._cache = None
        self._mecha_cache = None
        if not path:
            self._pix = QPixmap()
            self._custom = False
            self.update()
            return True
        pix = QPixmap(path)
        if pix.isNull():
            self._pix = QPixmap()
            self._custom = False
            self.update()
            return False
        self._pix = pix
        self._custom = True
        self.update()
        return True

    def has_background(self) -> bool:
        # 内置机甲图也算「有背景」，让 QSS 走 translucent 分支
        if self._custom:
            return not self._pix.isNull()
        return assets.path("background") is not None

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._cache = None
        self._mecha_cache = None
        self._logo_patch = None
        super().resizeEvent(event)

    def _logo_mask(self, w: int, h: int) -> QPixmap:
        """预生成「小 LOGO 遮盖」的羽化补丁，缓存起来。

        逐像素 fillRect 33000 次若每次 paintEvent 都跑会拖慢界面；
        这里生成一次 QPixmap，之后只 drawPixmap。
        """
        if self._logo_patch is not None and self._logo_patch_size == QSize(w, h):
            return self._logo_patch
        px = QPixmap(w, h)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        lx0 = int(w * 0.47)
        lx1 = int(w * 0.73)
        ly0 = int(h * 0.79)
        ly1 = int(h * 0.94)
        feather = max(8, int(w * 0.022))
        for yy in range(max(0, ly0), min(h, ly1)):
            ey = min(yy - ly0, ly1 - yy) / feather
            if ey <= 0:
                continue
            for xx in range(max(0, lx0), min(w, lx1)):
                ex = min(xx - lx0, lx1 - xx) / feather
                k = min(1.0, ey, ex) * 0.94
                c = QColor(5, 12, 26)
                c.setAlpha(int(255 * k))
                p.fillRect(QRect(xx, yy, 1, 1), c)
        p.end()
        self._logo_patch = px
        self._logo_patch_size = QSize(w, h)
        return px

    def _scaled_custom(self) -> QPixmap:
        if self._cache is not None:
            return self._cache
        target = self.size()
        if target.width() <= 0 or target.height() <= 0:
            return QPixmap()
        self._cache = self._pix.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        return self._cache

    def _mecha(self) -> QPixmap:
        """把内置立绘按「高度铺满、左侧定位」缩放并缓存。

        原图现在是 **1920x1080**（2026-11-09 换成高清版，原为 1280x720）。
        按 H/1080 等比缩放后与画布坐标对齐，右侧自然裁切。

        **关键：按 devicePixelRatio 出图。**
        原先直接用逻辑尺寸（``self.size()``）建 QPixmap，在 125%/150%
        缩放的屏幕上等于「按逻辑像素画、再由系统放大到物理像素」——
        就算换成 1920 的源图也照样糊。这里把输出位图按 DPR 放大，
        再设 ``setDevicePixelRatio``，让 Qt 按物理像素绘制，才是真高清。
        """
        if self._mecha_cache is not None and self._mecha_cache_size == self.size():
            return self._mecha_cache
        src = assets.pixmap("background")
        if src.isNull():
            return QPixmap()
        h = self.height()
        if h <= 0:
            return QPixmap()

        dpr = float(self.devicePixelRatioF() or 1.0)
        dpr = max(1.0, min(dpr, 3.0))       # 夹住，防止异常值炸内存

        # **必须按「宽高取大」的 cover 方式缩放，不能只按高度。**
        #
        # 原图 1920x1080 是 16:9，窗口一旦比 16:9 更宽（如最大化到
        # 1550x800，比例 1.94:1），只按高度算出来的立绘宽只有 1422px，
        # 加 offset 也才到 x=1474，而窗口宽 1550 —— **右侧 76px 露出底色**，
        # 形成一条明显竖线（2026-11-09 用户反馈「右边有一块背景没填充完」）。
        #
        # cover 规则：缩放系数取 max(窗口宽/图宽, 窗口高/图高)，
        # 保证两个方向都不留空，多出的部分由外层 QPixmap 自然裁切。
        target_w = int(round(self.width() * dpr))
        target_h = int(round(h * dpr))
        scale = max(
            target_w / max(1, src.width()),
            target_h / max(1, src.height()),
        )
        new_w = max(1, int(round(src.width() * scale)))
        new_h = max(1, int(round(src.height() * scale)))
        scaled = src.scaled(
            new_w, new_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        out = QPixmap(target_w, target_h)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # **不留任何 offset，图必须从 x=0 开始铺。**
        #
        # 原来画 ``offset_x = 窗口宽*0.034``（1180 时约 40px），
        # 本意是「让立绘偏左构图、右侧留星空」。但 cover 缩放后图已经
        # 比窗口宽，再右移反而把**左边缘推出画面**，左侧留下一条空白
        # （用户 2026-11-09 截图标注「左边有一部分没铺满」）。
        #
        # 现在：水平居中（溢出部分左右均分裁切），保证画布被完全覆盖。
        # 垂直同理居中。两个方向都不会出现空白边。
        draw_x = (target_w - new_w) // 2
        draw_y = (target_h - new_h) // 2
        p.drawPixmap(QPoint(draw_x, draw_y), scaled)
        p.end()

        # 告诉 Qt「这张图是 dpr 倍密度」，它才会按逻辑尺寸贴回去
        out.setDevicePixelRatio(dpr)
        self._mecha_cache = out
        self._mecha_cache_size = self.size()
        return out

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.fillRect(self.rect(), self._base)

        if self._custom and not self._pix.isNull():
            # ---- 用户自定义背景：完全沿用旧逻辑
            scaled = self._scaled_custom()
            if not scaled.isNull():
                x = (scaled.width() - self.width()) // 2
                y = (scaled.height() - self.height()) // 2
                p.drawPixmap(QPoint(-x, -y), scaled)
                alpha = int(255 * self._dim)
                if alpha > 0:
                    p.fillRect(self.rect(), QColor(0, 0, 0, alpha))
            p.end()
            return

        # ---- 内置机甲背景
        mecha = self._mecha()
        if not mecha.isNull():
            p.drawPixmap(QPoint(0, 0), mecha)
        else:
            # 素材缺失：纯色降级，后面的暗罩不必画
            p.end()
            return

        w, h = self.width(), self.height()
        if w <= 0:
            p.end()
            return

        # 从左到右的渐变暗罩。逐列画会有 1180 次 fillRect，
        # 实测一次重绘 <3ms，且只在 resize 时算，够快。
        #
        # **2026-11-09 用户三次反馈「左栏那块半透明黑色板块要剔除、
        # 只留元素按钮、不要影响背景图」** —— 原来左侧峰值 0.72~0.92，
        # 等于在左半边盖了一块近乎不透明的深色（截图里就是一块暗色矩形）。
        #
        # 现在降到 0.30~0.42，只保留「文字压得住背景」的最低量，
        # 机甲立绘在左栏区域仍然清晰可见。可读性由元素自身保证
        # （标题有深色描边、按钮有实心底、胶囊有实心圆角底）。
        dim_left = 0.30 + 0.12 * self._dim   # dim_percent 0→0.30 / 100→0.42
        dim_right = 0.02
        for x in range(w):
            t = _smoothstep(x / w)
            a = dim_left * (1.0 - t) + dim_right * t
            p.fillRect(QRect(x, 0, 1, h), QColor(5, 12, 26, int(255 * a)))

        # 顶部品牌区局部渐变压暗（纵向 + 横向都羽化，无硬边）
        top_h = min(150, h)
        for y in range(top_h):
            vy = 1.0 - _smoothstep(y / top_h)
            if vy <= 0:
                break
            span = min(w, int(w * 0.44))
            grad = QLinearGradient(QPointF(0, y), QPointF(span, y))
            c = QColor(5, 12, 26)
            c.setAlpha(int(255 * 0.28 * vy))   # 0.55 -> 0.28，见上
            grad.setColorAt(0.0, c)
            c2 = QColor(c)
            c2.setAlpha(0)
            grad.setColorAt(1.0, c2)
            p.fillRect(QRect(0, y, span, 1), QBrush(grad))

        # 底部压暗：**只在最底部做很窄的羽化，不做整条横带**。
        #
        # 2026-11-09 用户两次反馈「角色下面那块黑色」「只留背景图」：
        # 原来 bot_h = 76px、alpha 高达 0.82，等于在底边盖了一条
        # 76px 高的深色横带，横贯整个宽度 —— 截图里就是一块黑条。
        #
        # 现在收到 34px、峰值 alpha 0.55，且曲线改为平方衰减
        # （底部最暗、往上迅速消失），只为让导航按钮压得住背景，
        # 不再形成可见的矩形块。
        bot_h = min(34, h)
        for i in range(bot_h):
            y = h - bot_h + i
            k = _smoothstep(i / max(1, bot_h)) ** 2
            c = QColor(5, 12, 26)
            c.setAlpha(int(255 * 0.42 * k))
            p.fillRect(QRect(0, y, w, 1), c)

        # **不再画「小 LOGO 遮盖」补丁。**
        #
        # 这个补丁是给**旧背景图**（1280x720，右下角自带一枚小 LOGO）
        # 准备的：在 x 47%~73%、y 79%~94% 的位置铺一块
        # QColor(5,12,26)、alpha 最高 0.94 的近不透明深色方块去盖住它。
        #
        # 换成 1920x1080 的新背景图后，那块区域只有星空和机甲部件，
        # **没有任何 LOGO 或水印**，补丁就变成了一块突兀的黑色矩形
        # —— 用户 2026-11-09 连续三次截图反馈「角色下面那块黑色」
        # 「只留背景图」，就是它。
        #
        # 保留 _logo_mask() 方法本身（万一以后换回带 LOGO 的图还能用），
        # 但默认不画。
        p.end()


# ---------------------------------------------------------------- 圆形启动按钮


class MechaLaunchButton(QPushButton):
    """圆形「启动」按钮，位图 + 银白金属渐变字。

    **必须保持 QPushButton 接口**：业务方法会 ``setText`` / ``setEnabled`` /
    ``setToolTip``。这里不覆盖 ``text`` 的语义，但绘制时按状态选文案：

    ====================  ==================
    ``isEnabled()``      绘制
    ====================  ==================
    False + text含无法     「无法启动」
    False + 其它           「运行中…」
    True                  「启动」+ START GAME
    ====================  ==================

    素材缺失时降级成一个普通的橙色圆按钮，功能不受影响。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MechaLaunch")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._size = 244
        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._pix_cache: QPixmap | None = None
        self._pix_width = 0

        # --- 呼吸发光（2026-11-09 用户要求「导航选中效果和启动按钮一样」）
        #
        # 与 ``MechaNavButton`` 用**完全相同**的机制：0.35→1.0→0.35 的
        # 三角波 + setLoopCount(-1)。两边共用一个参数表，视觉才真的统一；
        # 各写一套迟早会漂移成两种节奏。
        #
        # 启动按钮的呼吸是**常驻**的（不是只在悬停时）—— 它是全场唯一
        # 的行动点，一直微微脉动符合「邀请点击」的语义。
        # 悬停时亮度更高一档，作为额外反馈。
        self._glow = 0.0
        self._glow_anim: QPropertyAnimation | None = None
        self._hovered = False
        self.setMouseTracking(True)
        self._start_glow()

    # ---- 呼吸发光（与 MechaNavButton 同一套参数）
    def _get_glow(self) -> float:
        return self._glow

    def _set_glow(self, value: float) -> None:
        self._glow = float(value)
        self.update()

    #: Qt 动画必须通过真正的 Qt 属性驱动
    glow = Property(float, _get_glow, _set_glow)

    def _start_glow(self) -> None:
        """启动常驻呼吸。

        与导航按钮**同样的关键帧写法**：不能用
        ``setStartValue/EndValue + setLoopCount(-1) + finished 反向`` ——
        无限循环的动画永远不发 finished，值会卡在起点（上一轮踩过）。
        """
        if self._glow_anim is not None:
            return
        anim = QPropertyAnimation(self, b"glow", self)
        anim.setDuration(1600)          # 与导航按钮一致
        anim.setKeyValueAt(0.0, 0.35)
        anim.setKeyValueAt(0.5, 1.0)
        anim.setKeyValueAt(1.0, 0.35)
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        anim.setLoopCount(-1)
        anim.start()
        self._glow_anim = anim

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def _button_pix(self) -> QPixmap:
        if self._pix_cache is not None and self._pix_width == self.width():
            return self._pix_cache
        px = assets.scaled("round", self.width())
        self._pix_cache = px
        self._pix_width = self.width()
        return px

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        pix = self._button_pix()
        if pix.isNull():
            # 降级：纯色圆 + 文字
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(C_ORANGE))
            p.drawEllipse(self.rect().adjusted(4, 4, -4, -4))
            self._draw_text(p, fallback=True)
            p.end()
            return

        # --- 呼吸光晕：画在圆盘**外缘以内**再叠位图
        #
        # 为什么不是画在圆盘外面：``setFixedSize(_size, _size)`` 让控件
        # 正好等于圆盘直径，任何超出矩形的绘制都会被 Qt 裁掉（一条都看不见）。
        # 所以改成「在圆盘边缘画一圈暖色光环，再把圆盘位图盖上去」，
        # 光环只在圆盘边缘几像素露出，视觉上就是一圈呼吸光。
        #
        # 只在可用时发光：不能启动还一直亮，会被误读成「可以点」。
        if self.isEnabled() and self._glow > 0.01:
            base = 1.0 if self._hovered else 0.62
            inten = self._glow * base
            c = QPointF(self.width() / 2, self.height() / 2)
            half = self.width() / 2.0
            # **必须用描边画环，不能用实心椭圆**：上一版用了
            # ``setPen(NoPen) + setBrush(橙)`` 再 ``drawEllipse(r > 半径)``，
            # 结果整个控件矩形被橙色填满，成了一个大橙方块（实测截图）。
            # 这里改成「无填充 + 粗描边」，半径收在控件内，只留一道光环。
            # 分两部分叠加，缺一不可：
            #
            # ① **边缘光环**（描边）：让轮廓「亮起来」。
            # ② **整体暖色蒙版**（实心、低 alpha）：圆盘是银灰金属，
            #    只画边缘光环几乎看不出来（实测截图上完全被金属吃掉）。
            #    叠一层暖色才让整颗按钮有「被光浸透」的感觉 ——
            #    这正是导航按钮「整块提亮」的同一个做法。
            #
            # 注意 ① 的半径必须收在控件内（half - 描边半宽），否则
            # 圆会超出矩形，四角被裁成方块（这个坑踩过两次）。
            # ②用实心椭圆时半径要小于 half，否则同样溢出。
            edge = 92.0
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(4):
                lw = 4.0 - i * 0.8
                rr = half - lw / 2 - 1.0 - i * 2.4
                if rr <= 0:
                    continue
                a = int(edge * inten * (1 - i / 4.0))
                if a <= 0:
                    continue
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(255, 172, 52, a), lw))
                p.drawEllipse(c, rr, rr)

            # ② 整体提亮：内盘本来就是橙色，再叠橙色等于没变化
            #    （实测看不出呼吸）。真正让「发光」成立的是**亮度**变化：
            #    叠一层极淡的暖白（而非纯白，避免发灰），
            #    配合边缘光环，形成「整颗在呼吸」的观感。
            fill_r = half * 0.52
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 246, 214, int(46 * inten)))
            p.drawEllipse(c, fill_r, fill_r)

        p.drawPixmap(QPoint(0, 0), pix)

        # 不可用时压暗 —— **必须裁成圆形**。
        #
        # 原来是 ``p.fillRect(self.rect(), ...)``，直接填满 244x244 的
        # **整个矩形**。而圆盘位图四角是透明的（round.png alpha=0），
        # 于是四角被涂成半透明深色，看上去就是「按钮外面罩着一块黑方框」。
        #
        # 用户 2026-11-09 反复反馈「那个黑色很明显、删不掉吗」，
        # 并误以为是「控件底纹」—— 其实是一行 fillRect 的范围写错了。
        # 正确做法：把绘制裁剪到圆形区域内，只压暗圆盘本身。
        if not self.isEnabled():
            clip = QPainterPath()
            r = self.width() / 2.0
            clip.addEllipse(QPointF(r, r), r - 1, r - 1)
            p.save()
            p.setClipPath(clip)
            p.fillRect(self.rect(), QColor(4, 10, 22, 130))
            p.restore()

        self._draw_text(p, fallback=False)
        # 不画键盘焦点圈：启动按钮默认会拿到窗口焦点，画出来就是一圈
        # 突兀的青蓝椭圆（实测截图里很明显）。键盘可达性靠 QSS 的
        # :focus 状态即可，不必在自绘里再画一遍。
        p.end()

    def _draw_text(self, p: QPainter, *, fallback: bool) -> None:
        """画「启动」大字。

        **2026-11-09 最终定稿：深黑字 + 极淡亮色衬底。**

        走过的弯路（全部保留，避免以后再往「精致」方向堆效果）：

        1. 深棕实心 + **细白描边** + 字号撑满 → 发脏、像贴上去的
        2. 白→金渐变 + 深棕粗描边 + 橙色外发光 → 糊成一团
        3. 银白金属渐变 + 双层描边 + 下沉阴影 → 精致但违和
        4. 纯白粗体无描边 → 干净了，但**用户仍觉白色违和**

        **关键区分（第 1 版失败不等于深色不行）**：
        第 1 版发脏的真正原因是「细白描边 + 字号 40 撑满内盘」，
        不是深色本身。去掉描边、字号收到 44 之后，深黑字非常干净 ——
        这也符合橙色底盘上的常规对比关系（深字压亮底）。

        最终参数：
        * **深黑 #1A1000**（近纯黑，略带暖调，避免在橙底上发青）
        * 无描边、无渐变、无发光
        * 一层极淡的**亮色衬底**（白 alpha 38，向下偏移 2px）：
          橙盘中心那团高光很亮，黑字压上去边缘会「陷」进去；
          垫一层微亮底能把字形从高光里托出来，且不显脏
        * ``START GAME`` 同色，透明度略降

        候选对比见 ``tools/preview_btn_style_v2.py`` → ``_btn_style_v2.png``。
        """
        cx, cy = self.width() / 2, self.height() / 2
        scale = self.width() / 244.0
        enabled = self.isEnabled()
        txt = self.text() or ""

        if not enabled:
            # 不可用态：与「开始」同一套字（深棕扁平粗体），
            # 只是换成状态文案。
            #
            # 2026-11-09 用户截图指出「无法启动」字体违和 ——
            # 原来是浅米色 `#FFF4DC` 压在压暗的橙盘上，发白发虚，
            # 和「开始」的深棕完全不是一套。现在统一成同一个颜色常量、
            # 同一套字号逻辑（四字时自动略小，避免溢出内盘）。
            # 与「开始」同一套字（深棕扁平粗体），但禁用态要单独处理：
            #
            # 禁用态整颗按钮叠了一层 alpha 130 的暗罩，橙色盘被压成暗褐色，
            # 深棕 #482808 压上去几乎糊成一片（实测对比度不足）。
            # 这里用**暖金 #C98A2E**：在暗罩下仍然清楚，
            # 又属于同一橙金色系（不是原来的浅米白）。
            ink = QColor(201, 138, 46)
            label = "无法启动" if "无法" in txt else "运行中"
            # 英文与可用态同一套样式，只是文案改成不可用语义
            en = "CANNOT START" if "无法" in txt else "RUNNING"
            n = len(label)
            # 「开始」是 2 字 28pt。四字按 2/n 折算后再降一档（×1.0）：
            # 之前用 ×1.15 留余量，实测四字仍偏大，字形下缘顶到英文条。
            pt = max(10, int(28 * scale * (2.0 / max(2, n))))
            font = QFont("Microsoft YaHei", pt)
            font.setBold(True)
            probe = QPainterPath()
            probe.addText(QPointF(0, 0), font, label)
            b = probe.boundingRect()
            path = QPainterPath()
            path.addText(
                QPointF(
                    cx - b.width() / 2 - b.left(),
                    # 上移量与可用态一致，否则四字中文会顶到英文条上
                    cy + b.height() / 2 - b.bottom() - 16 * scale,
                ),
                font,
                label,
            )
            p.fillPath(path, ink)
            self._draw_en_bar(p, cx, cy, en, ink, dy=7)
            return

        # --- 「开始」+「-START GAME-」，与游戏主界面原版完全一致
        #
        # 2026-11-09 用户给了官网主界面截图红框，要求「就用原本的字样」。
        # 原版实测特征（截图放大 3x 确认）：
        #   · 中文「开始」两字，深棕粗黑体，**无描边无阴影**
        #   · 下方 ``-START GAME-``：深棕小字，**两侧带短横线**，
        #     整行压在一根半透明浅色横条上（横条比字略宽）
        #   · 中文约 60px/内盘 340px ≈ 18%；英文约 30px ≈ 9%
        #
        # 之前几版都做错了方向：写「启动游戏」四字、给英文加圆角底条、
        # 用白字配黑描边…… 其实原版结构非常简单：两字 + 带横线的小英文。
        C_TEXT_INK = QColor(72, 40, 8)   # 原版实测深棕（比导航按钮浅一档）

        # 中文「开始」：字号 28 —— 按原版实测「宽度约占内盘 50%」定，
        # 24pt 只有 42%，偏小；30pt 会压到英文条。
        size = max(12, int(28 * scale))
        text = "开始"
        font = QFont("Microsoft YaHei", size)
        font.setBold(True)
        probe = QPainterPath()
        probe.addText(QPointF(0, 0), font, text)

        b = probe.boundingRect()
        x = cx - b.width() / 2 - b.left()
        # 中文重心偏上，下方紧接英文条（原版两者挨得很近）
        y = cy + b.height() / 2 - b.bottom() - 12 * scale

        main = QPainterPath()
        main.addText(QPointF(x, y), font, text)
        # 纯色扁平：原版就是这样，任何描边/渐变都会「违和」
        p.fillPath(main, C_TEXT_INK)

        # 英文小字：``-START GAME-``，可/不可用态共用同一套横条样式
        self._draw_en_bar(p, cx, cy, "START GAME", C_TEXT_INK)

    def _draw_en_bar(
        self, p: QPainter, cx: float, cy: float, small: str, ink: QColor,
        *, dy: int = 0,
    ) -> None:
        """画「-XXX-」形式的英文小字：深色字 + 两侧短横线，**无底条**。

        2026-11-09 用户：「开始的英文字样要去掉那个透明的边框」。
        原来文字后面垫了一条 `(255,240,205,62)` 的浅色矩形，
        本意是模拟官网那根浅色带，实际在橙色盘上看起来像一块「脏补丁」，
        而且四角是直角、和圆盘格格不入。现在完全去掉，只留两侧横线。

        ``dy``：额外下移量（像素）。禁用态中文是四个字，横向占满后
        下缘必然更靠近英文条，靠「上移中文」解决不了，
        必须把英文条整体下移拉开间距。
        """
        scale = self.width() / 244.0
        f2 = QFont("Microsoft YaHei", max(6, int(9 * scale)))
        f2.setBold(True)
        p.setFont(f2)
        fm2 = p.fontMetrics()
        sw = fm2.horizontalAdvance(small)

        bar_h = int(15 * scale)
        bar_y = cy + int(13 * scale) + int(dy * scale)

        # 横线 + 文字（无底条）
        p.setPen(QPen(ink, max(1, 1.5 * scale)))
        ty = bar_y + bar_h / 2
        p.drawLine(
            QPointF(cx - sw / 2 - int(3 * scale), ty),
            QPointF(cx - sw / 2 - int(9 * scale), ty),
        )
        p.drawLine(
            QPointF(cx + sw / 2 + int(3 * scale), ty),
            QPointF(cx + sw / 2 + int(9 * scale), ty),
        )
        p.setPen(ink)
        p.drawText(
            QRectF(cx - sw / 2, bar_y, sw, bar_h),
            Qt.AlignmentFlag.AlignCenter,
            small,
        )


# ---------------------------------------------------------------- 橙色导航条


class MechaNavButton(QPushButton):
    """底部橙色导航按钮：位图底 + 深棕文字。

    保持 ``QPushButton`` 的 checkable / checked 语义，``_switch`` 里的
    ``setChecked`` / ``isChecked`` 照常工作。素材缺失时降级成 QSS 按钮。
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("MechaNav")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._cache: QPixmap | None = None
        self._cache_w = 0
        self._cache_h = 0

        # --- 悬停/选中反馈：柔和闪烁（2026-11-09 用户要求）
        #
        # 原来只有「选中」时画一圈静态黄框，鼠标移上去毫无反应，
        # 点下去也不明显。用户要的是「鼠标移上去和开始按钮一样闪烁」。
        #
        # 实现方式：一个 0→1→0 的三角波亮度，用 QPropertyAnimation 驱动
        # 一个 0..100 的整数属性，每次变化触发 update() 重绘。
        # 为什么不用 QTimer：动画可被 setChecked 状态变化自然接续，
        # 也不用自己管 tick 生命周期。
        self._glow = 0.0
        self._glow_anim: QPropertyAnimation | None = None
        self.setMouseTracking(True)

    # ---- 闪烁用的可动画属性
    def _get_glow(self) -> float:
        return self._glow

    def _set_glow(self, value: float) -> None:
        self._glow = float(value)
        self.update()

    #: Qt 动画需要真正的 Qt 属性，用 Property 声明
    glow = Property(float, _get_glow, _set_glow)

    def _start_glow(self) -> None:
        """启动循环明暗呼吸（悬停或选中时）。

        **关键修正**：原来用 ``setStartValue/setEndValue`` + ``setLoopCount(-1)``
        再靠 ``finished`` 反向，但**无限循环的动画永远不会发 finished**，
        所以 glow 一直停在起始值 0.35 不动 —— 实测验证就是这样失败的。

        正确做法：用 ``setKeyValueAt`` 描述一个完整的呼吸周期
        （0.35 → 1.0 → 0.35），再 ``setLoopCount(-1)`` 无限重复，
        不需要 finished 回调。
        """
        if self._glow_anim is not None:
            return
        anim = QPropertyAnimation(self, b"glow", self)
        anim.setDuration(1600)          # 一个完整来回
        anim.setKeyValueAt(0.0, 0.35)
        anim.setKeyValueAt(0.5, 1.0)
        anim.setKeyValueAt(1.0, 0.35)
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        anim.setLoopCount(-1)           # 无限重复这个周期
        anim.start()
        self._glow_anim = anim

    def _stop_glow(self) -> None:
        if self._glow_anim is not None:
            self._glow_anim.stop()
            self._glow_anim.deleteLater()
            self._glow_anim = None
        self._glow = 0.0
        self.update()

    def _sync_glow(self) -> None:
        """按「悬停 or 选中」决定是否需要呼吸。"""
        if self.underMouse() or self.isChecked():
            self._start_glow()
        else:
            self._stop_glow()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().enterEvent(event)
        self._sync_glow()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().leaveEvent(event)
        self._sync_glow()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt 命名
        super().setChecked(checked)
        self._sync_glow()

    def _bar(self, w: int, h: int) -> QPixmap:
        if self._cache is not None and self._cache_w == w and self._cache_h == h:
            return self._cache
        px = assets.pixmap("btn_bar")
        if px.isNull():
            self._cache = QPixmap()
        else:
            self._cache = px.scaled(
                w, h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._cache_w, self._cache_h = w, h
        return self._cache

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bar = self._bar(self.width(), self.height())
        if not bar.isNull():
            p.drawPixmap(QPoint(0, 0), bar)
            # 呼吸高亮：悬停或选中时按 _glow 亮度画一圈暖金描边 + 内发光。
            # 未悬停未选中时 _glow=0，完全不画 —— 保持默认样式干净。
            if self._glow > 0.01:
                # --- 整块橙金发光（2026-11-09 用户要求与启动按钮统一）
                #
                # 原来只画一圈细描边，视觉上是「被框住了」，而启动按钮是
                # 「整颗在发光」—— 两者不是同一种语言。改成三层叠加：
                #   1. 底色整体提亮（暖橙，低透明度 → 像被光浸透）
                #   2. 内侧描边（亮金，中等粗细 → 勾勒轮廓）
                #   3. 外侧光晕（大圆角、外扩几像素 → 溢出感）
                #
                # 亮度统一挂 self._glow，与启动按钮同一个 0.35→1.0→0.35
                # 三角波、同样 1600ms，两边节奏完全同步。
                g = self._glow
                r = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)

                # 1) 整块提亮
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 156, 40, int(46 * g)))
                p.drawRoundedRect(r, 10, 10)

                # 2) 内侧亮金描边
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(255, 216, 120, int(120 + 130 * g)),
                              1.4 + 1.0 * g))
                p.drawRoundedRect(r, 10, 10)

                # 3) 内侧扩散光：**不能向外扩**，控件尺寸等于底色条，
                #    画到矩形外会被 Qt 直接裁掉（看不到任何效果）。
                #    改为在矩形内缘叠一层更宽、更淡的描边，做出「光从
                #    边缘往里渗」的观感 —— 视觉上等价于外发光。
                inner = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
                p.setPen(QPen(QColor(255, 178, 60, int(70 * g)), 5.0))
                p.drawRoundedRect(inner, 11, 11)
            if not self.isEnabled():
                # 同上：裁剪到圆角矩形，不要填满直角矩形
                clip2 = QPainterPath()
                clip2.addRoundedRect(
                    QRectF(0, 0, self.width(), self.height()), 10, 10
                )
                p.save()
                p.setClipPath(clip2)
                p.fillRect(self.rect(), QColor(4, 10, 22, 140))
                p.restore()
            # 字号按按钮高度自适应：原来写死 16pt，按钮从 164x52
            # 缩到 134x44 后四字会顶到边框（实测「扩展管理」被裁）。
            # 52 高时对应 16pt，等比换算并夹在 11~16 之间。
            pt = max(11, min(16, int(round(self.height() * 16 / 52))))
            f = QFont("Microsoft YaHei", pt)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(45, 22, 0))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        else:
            # 降级：用主题 QSS，让 Qt 自己画
            self.setObjectName("NavButton")
            self.style().drawControl(
                self.style().ControlElement.CE_PushButtonBevel,
                None, p, self,
            )
            self.style().drawItemText(
                p, self.rect(), Qt.AlignmentFlag.AlignCenter,
                self.palette(), self.isEnabled(), self.text(),
            )
            self.setObjectName("MechaNav")
        p.end()


def draw_creator_chip(p: QPainter, x: int, y: int) -> int:
    """画创作者胶囊，返回宽度。

    **2026-11-09 用户指定的布局**：左侧圆形头像，右侧分上下两行 ——
    上半行 bilibili 字标、下半行「创作者」字样。

    为什么去掉「柳江巷」昵称：胶囊已经贴在「新起点启动器」标题下方，
    昵称在这里是冗余信息；换成 bilibili 字标后，一眼就知道
    「点头像/字标能去 B 站」，比原来「昵称+徽章+分隔线+字标」四段
    挤在一行里清爽得多（原来 230px 宽塞四样东西，很挤）。

    尺寸：200 × 44。头像 32px 居中偏左，右列上下各占一半。
    """
    # 方案 B（2026-11-09 用户选定）：
    #   头像 40px + 「柳江巷」粗体 + 下方「创作者」小字 + 右侧粉色 bilibili 26px
    # 尺寸 212x56，圆角 14（不是全高胶囊 —— 内容两行，全高胶囊会显得太圆）
    w, h = 212, 56
    p.setPen(QPen(QColor(24, 78, 128), 1))
    p.setBrush(QColor(10, 26, 50, 235))
    p.drawRoundedRect(QRect(x, y, w, h), 14, 14)

    # --- 左：圆形头像（40px）
    av = assets.pixmap("avatar")
    av_r = QRectF(x + 8, y + (h - 40) / 2, 40, 40)
    if not av.isNull():
        clip = QPainterPath()
        clip.addEllipse(av_r)
        p.save()
        p.setClipPath(clip)
        target = av.scaled(
            40, 40, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        p.drawPixmap(QPoint(int(av_r.x()), int(av_r.y())), target)
        p.restore()
    p.setPen(QPen(C_CYAN, 1.8))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(av_r)

    # --- 中间两行：昵称 / 创作者
    cx = int(av_r.x() + 40 + 10)
    f = QFont("Microsoft YaHei", 12)
    f.setBold(True)
    p.setFont(f)
    p.setPen(C_TEXT)
    p.drawText(QPoint(cx, y + 24), "柳江巷")

    # 「创作者」三个字要**和上面的昵称等宽**（用户 2026-11-09 要求）。
    #
    # 不能靠放大字号实现 —— 那会让副标题盖过主标题的层级。
    # 正确做法是逐字排布并拉开字间距：先量出昵称宽度，
    # 再把三个字均匀分布在这个宽度上（末字右缘对齐）。
    f2 = QFont("Microsoft YaHei", 8)
    p.setFont(f2)
    p.setPen(QColor(150, 186, 220))
    sub = "创作者"
    fm2 = p.fontMetrics()
    # 昵称宽度用与昵称相同的粗体字测量
    f_name = QFont("Microsoft YaHei", 12)
    f_name.setBold(True)
    name_w = QFontMetrics(f_name).horizontalAdvance("柳江巷")
    # 三个字本身的宽度 + 需要的总间隔
    chars = [fm2.horizontalAdvance(c) for c in sub]
    text_w = sum(chars)
    gap = (name_w - text_w) / (len(sub) - 1) if len(sub) > 1 else 0
    gap = max(0.0, gap)
    xx = float(cx)
    for ch, cw in zip(sub, chars):
        p.drawText(QPointF(xx, y + 42), ch)
        xx += cw + gap

    # --- 右：粉色 bilibili（新素材，等比缩到高 26）
    bili = assets.scaled("bilibili", 78)
    if not bili.isNull():
        bh = 26
        bw = int(bili.width() * (bh / max(1, bili.height())))
        bili = bili.scaled(
            bw, bh, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        p.drawPixmap(QPoint(x + w - bw - 12, y + (h - bh) // 2), bili)

    return w


class MechaCreatorChip(QWidget):
    """独立的创作者胶囊控件（侧栏用）。

    点击 bilibili 字标区域会打开创作者主页（2026-11-09 用户提供链接）。
    只把**右侧 bilibili 字标**做成热区，而不是整颗胶囊 ——
    整颗可点会让用户误以为点头像/昵称也有反应。
    """

    #: 创作者 B 站主页（用户 2026-11-09 提供）
    CREATOR_URL = "https://space.bilibili.com/1278623966"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MechaCreator")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(212, 56)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("")
        self._hover = False
        self.setMouseTracking(True)

    def _bili_hit(self, pos) -> bool:
        """整颗胶囊都是热区。

        旧布局是「昵称 + 徽章 + bilibili」横排，只有右侧字标可点
        （避免用户以为点头像也有反应）。新布局右侧两行全是 B 站信息
        （字标 + 创作者），头像紧挨着，再切一块热区反而别扭 ——
        整颗可点、光标变手型，语义统一。
        """
        return 0 <= pos.x() <= 212 and 0 <= pos.y() <= 56

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        over = self._bili_hit(event.position().toPoint())
        if over != self._hover:
            self._hover = over
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if over
                else Qt.CursorShape.ArrowCursor
            )
            self.setToolTip("打开创作者 B 站主页" if over else "")
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._hover:
            self._hover = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.setToolTip("")
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._bili_hit(event.position().toPoint()):
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(self.CREATOR_URL))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        draw_creator_chip(p, 0, 0)
        # 悬停时在 bilibili 字标下划一条强调线，提示可点
        if self._hover:
            p.setPen(QPen(C_CYAN, 1))
            p.drawLine(QPoint(146, 46), QPoint(202, 46))
        p.end()


# ---------------------------------------------------------------- 顶部品牌条


class MechaBrandBar(QWidget):
    """左上品牌区：LOGO + 标题 + 创作者胶囊 + bilibili 字标。

    纯展示，无业务耦合。素材缺失时只画文字，不会留空。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MechaBrand")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(168)
        from ... import edition

        self._title = edition.app_name()
        self._subtitle = ""
        self._status = ""
        self._bili_rect: QRect | None = None
        self._hover_bili = False
        self.setMouseTracking(True)

    #: 创作者 B 站主页（与 MechaCreatorChip 同一地址）
    CREATOR_URL = "https://space.bilibili.com/1278623966"

    def _bili_hit(self, pos) -> bool:
        return self._bili_rect is not None and self._bili_rect.contains(pos)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        over = self._bili_hit(event.position().toPoint())
        if over != self._hover_bili:
            self._hover_bili = over
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if over
                else Qt.CursorShape.ArrowCursor
            )
            self.setToolTip("打开创作者 B 站主页" if over else "")
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._bili_hit(event.position().toPoint()):
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(self.CREATOR_URL))

    def set_titles(self, title: str, subtitle: str = "", status: str = "") -> None:
        self._title = title
        self._subtitle = subtitle
        self._status = status
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # LOGO（固定 300 宽，与示意图一致）
        logo = assets.scaled("logo", 300)
        if not logo.isNull():
            p.drawPixmap(QPoint(26, 4), logo)

        # 标题
        f = QFont("Microsoft YaHei", 24)
        f.setBold(True)
        p.setFont(f)
        p.setPen(C_TEXT)
        p.drawText(QPoint(34, 108), self._title)

        # 创作者胶囊紧跟标题右侧（复用侧栏同一套绘制）
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(self._title)
        chip_x = 34 + tw + 22
        draw_creator_chip(p, chip_x, 102)
        # 记下 bilibili 字标在**本控件内**的位置，供 _bili_hit 判定
        self._bili_rect = QRect(chip_x + 160, 102, 70, 40)

        # 副标题 / 状态
        y = 148
        if self._subtitle:
            f2 = QFont("Microsoft YaHei", 11)
            p.setFont(f2)
            p.setPen(QColor(100, 140, 180))
            p.drawText(QPoint(34, y), self._subtitle)
        if self._status:
            p.setBrush(QBrush(C_GREEN))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(38, y + 16), 4, 4)
            f3 = QFont("Microsoft YaHei", 11)
            p.setFont(f3)
            p.setPen(C_GREEN)
            p.drawText(QPoint(50, y + 20), self._status)
        p.end()


# ---------------------------------------------------------------- 状态胶囊


class MechaStatusCapsule(QPushButton):
    """右下角状态胶囊：状态点 + 扩展数 + 展开箭头。

    ``clicked`` 由 MainWindow 连到面板召唤/收起。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MechaCapsule")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(210, 30)
        # 初值必须是 0，不能写任何具体数字。
        #
        # 这里原来是 ``self._count = 25`` —— 那是开发机上的扩展数，
        # 被打包发出去后，全新安装（0 个扩展）的玩家也会看到「25 扩展」。
        # 上次只改了 main_window 里的 set_state(25, ...)，漏了控件自身的
        # 默认值，于是「改完还是 25」（2026-11-09 用户二次反馈）。
        # **同一个魔数在两个地方各有一份，改一处不够。**
        self._count = 0
        self._ok = True
        self._text = "全部正常"
        self._expanded = False
        # 悬停态：箭头移除后，可点击性全靠它表达
        self._hover = False

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def set_state(self, count: int, ok: bool, text: str = "") -> None:
        self._count = int(count)
        self._ok = bool(ok)
        self._text = text or ("全部正常" if ok else "有异常")
        self.update()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 悬停时描边与底色一起提亮 —— 这是「可以点」的主要提示，
        # 比原来那个静态 `>` 箭头清楚得多（移上去还能看到手型光标）。
        if self._hover:
            p.setPen(QPen(QColor(0, 214, 255, 255), 1.4))
            p.setBrush(QColor(16, 44, 78, 245))
        else:
            p.setPen(QPen(QColor(0, 190, 255, 210), 1))
            p.setBrush(QColor(8, 26, 50, 238))
        p.drawRoundedRect(
            QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 15, 15
        )
        dot = C_GREEN if self._ok else C_AMBER
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QPointF(17, 15), 4, 4)

        f = QFont("Microsoft YaHei", 11)
        p.setFont(f)
        p.setPen(C_TEXT_DIM)
        p.drawText(QPoint(29, 20), f"{self._count} 扩展 · {self._text}")

        # 不再画 `›` / `‹` 箭头。
        #
        # 它原来是「点击胶囊可以展开/收起面板」的唯一提示，但实际效果很糟：
        #   · 一个小 `>` 看着像装饰符号，用户根本不会联想到「可点击」
        #   · 方向语义也反直觉 —— `>` 在左对齐语境里像「前往下一页」，
        #     实际是「收起到 HUB」
        # 2026-11-09 用户直接问「有实际用途么？没有就清理掉」，
        # 说明这个提示完全没起作用。
        #
        # 可点击性改由**悬停光标 + 高亮边框**表达（见 _hover）：
        # 鼠标移上去变手型、描边变亮，比一个静态符号清楚得多。
        p.end()
