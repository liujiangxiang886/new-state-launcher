#!/usr/bin/env python
"""用抠好的素材拼一张 HUB 界面示意图。

目的不是「最终成品」，而是**让讨论有对照物**：
布局位置、层级、配色、按钮尺寸是否合适，看一眼就能判断。

用法::

    python tools/preview_mecha_hub.py

输出 ``assets/ui/mecha/_hub_mock.png``（1180x760，与启动器窗口一致）。

布局（按 2026-11-08 需求）::

    ┌───────────────────────────────────────────────────────┐
    │  [LOGO]                                    [版本徽标] │
    │                                                       │
    │  ┌── 游戏卡片 ────────────────────────────────┐        │
    │  │ 机甲旋风 1.45 · 原版                       │        │
    │  │ 已关联 · 380,259 条目                      │        │
    │  │                                            │        │
    │  │  ╭────────╮   ← 启动按钮在左侧中间         │        │
    │  │  │  启动  │                                │        │
    │  │  │  游戏  │                                │        │
    │  │  ╰────────╯                                │        │
    │  └────────────────────────────────────────────┘        │
    │                                                       │
    │  [游戏库] [扩展] [诊断] [设置]                          │
    └───────────────────────────────────────────────────────┘

为什么启动按钮放左侧中间而不是居中：右侧要留给状态信息（扩展数、
存档、日志入口），按钮居中会把信息挤到边角。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "ui" / "mecha"

W, H = 1180, 760

# ---------------------------------------------------------------- 配色
# 深紫黑底沿用现有主题，强调色换成从素材里取的真实橙
BG = (22, 14, 36)
BG2 = (29, 20, 48)
CARD = (39, 26, 64)
CARD_HI = (50, 31, 82)
BORDER = (58, 40, 92)
BORDER_HI = (82, 56, 128)
TEXT = (236, 232, 245)
TEXT_DIM = (183, 169, 208)
TEXT_FAINT = (126, 111, 160)
ORANGE = (236, 148, 7)
ORANGE_HI = (255, 190, 60)
GREEN = (76, 196, 124)
RED = (239, 93, 122)
CYAN = (95, 218, 226)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """找一个能显示中文的字体。

    Windows 上 msyhbd.ttc(微软雅黑粗体) 一定有；退化到 simhei、
    最后用 PIL 默认位图字体（此时中文会变方框，但至少不崩）。
    """
    cands = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for c in cands:
        if Path(c).is_file():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def rrect(d: ImageDraw.ImageDraw, box, r: int, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def gradient_box(d: ImageDraw.ImageDraw, box, c1, c2, r: int = 10):
    """纵向渐变圆角块。逐行画，够用且不引入 numpy 依赖。"""
    x0, y0, x1, y1 = box
    h = max(1, y1 - y0)
    for i in range(h):
        t = i / h
        col = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
        d.line([(x0, y0 + i), (x1, y0 + i)], fill=col)
    # 圆角用同色描边盖回去
    rrect(d, box, r, outline=None)
    # 用矩形裁角：四角画背景色小方块
    for cx, cy in ((x0, y0), (x1 - r, y0), (x0, y1 - r), (x1 - r, y1 - r)):
        d.rectangle([cx, cy, cx + r, cy + r], fill=BG)


def blend_line(canvas: Image.Image, pts, color, alpha: float):
    """在 RGBA 画布上画「半透明叠加」的直线。

    **为什么不能直接用 ImageDraw 的 fill=(r,g,b,a)**：
    PIL 的 draw 是**覆盖**语义，不是 alpha 混合。给 (255,255,255,3)
    画上去的结果是像素直接变成 (255,255,255,3) —— 即一条几乎全白
    的线，Alpha 通道还被写坏了。所以必须手工混合。

    ``alpha`` 取 0~1 的浮点，内部按比例和目标像素做 lerp。
    """
    import numpy as _np

    arr = _np.array(canvas, dtype=_np.float32)
    h, w = arr.shape[:2]
    (x0, y0), (x1, y1) = pts
    n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for i in range(n):
        t = i / max(1, n - 1)
        x = int(round(x0 + (x1 - x0) * t))
        y = int(round(y0 + (y1 - y0) * t))
        if 0 <= x < w and 0 <= y < h:
            arr[y, x, :3] = arr[y, x, :3] * (1 - alpha) + _np.array(
                color, dtype=_np.float32
            ) * alpha
            arr[y, x, 3] = 255
    canvas.paste(Image.fromarray(arr.astype(_np.uint8), "RGBA"))


def draw_scanlines(d: ImageDraw.ImageDraw, box, step: int = 4, alpha: int = 14):
    """细横线纹理，模拟屏幕扫描感。低透明度叠加。"""
    x0, y0, x1, y1 = box
    for y in range(y0, y1, step):
        d.line([(x0, y), (x1, y)], fill=(255, 255, 255, alpha))


def paste_center(base: Image.Image, img: Image.Image, cx: int, cy: int):
    base.alpha_composite(img, (cx - img.width // 2, cy - img.height // 2))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    if not ASSETS.is_dir():
        print(f"找不到素材目录：{ASSETS}")
        return 2

    canvas = Image.new("RGBA", (W, H), BG + (255,))
    d = ImageDraw.Draw(canvas, "RGBA")

    # ---------------------------------------------------- 背景层
    # 顶部一条极暗的紫色带，给 LOGO 一个落脚区
    for i in range(96):
        t = i / 96
        col = tuple(int(BG2[k] + (BG[k] - BG2[k]) * t) for k in range(3))
        d.line([(0, i), (W, i)], fill=col + (255,))

    # 右侧光晕：用横向渐变叠加，不做多边形（多边形边缘太硬，
    # 第一版就是一块突兀的紫斑）。
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    glow_w = 420
    for i in range(glow_w):
        x = W - glow_w + i
        t = i / glow_w  # 从透明到最浓
        a = int(18 * (t ** 2))
        od.line([(x, 0), (x, H)], fill=(150, 110, 230, a))
    canvas = Image.alpha_composite(canvas, overlay)
    d = ImageDraw.Draw(canvas, "RGBA")

    # 网格暗纹：只做「质感」，alpha 极低。
    # 注意必须用 blend_line —— PIL 的 fill=(r,g,b,a) 是覆盖不是混合。
    for x in range(0, W, 60):
        blend_line(canvas, ((x, 0), (x, H)), (255, 255, 255), 0.022)
    for y in range(0, H, 60):
        blend_line(canvas, ((0, y), (W, y)), (255, 255, 255), 0.022)
    d = ImageDraw.Draw(canvas, "RGBA")

    # ---------------------------------------------------- 顶部：LOGO + 版本
    logo_p = ASSETS / "logo.png"
    if logo_p.is_file():
        logo = Image.open(logo_p).convert("RGBA")
        scale = 300 / logo.width
        logo = logo.resize(
            (300, max(1, int(logo.height * scale))), Image.LANCZOS
        )
        canvas.alpha_composite(logo, (34, 22))
        logo_bottom = 22 + logo.height
    else:
        d.text((34, 40), "新起点启动器", font=font(34, True), fill=TEXT)
        logo_bottom = 96

    # 版本徽标（右上）
    badge = "v1.7 · 统一版"
    bf = font(14)
    bw = d.textlength(badge, font=bf)
    rrect(d, (W - 34 - bw - 24, 30, W - 34, 58), 14, fill=(*CARD_HI, 255),
          outline=(*BORDER_HI, 255))
    d.text((W - 34 - bw - 12, 36), badge, font=bf, fill=TEXT_DIM)

    # ---------------------------------------------------- 左侧主卡片
    card_x0, card_y0 = 34, max(logo_bottom + 16, 150)
    card_x1, card_y1 = 700, 560
    rrect(d, (card_x0, card_y0, card_x1, card_y1), 14,
          fill=(*CARD, 255), outline=(*BORDER, 255), width=1)
    # 卡片顶部高光边
    d.line([(card_x0 + 14, card_y0 + 1), (card_x1 - 14, card_y0 + 1)],
           fill=(*BORDER_HI, 160))

    # 标题行
    d.text((card_x0 + 24, card_y0 + 22), "机甲旋风 1.45 · 原版",
           font=font(22, True), fill=TEXT)
    d.text((card_x0 + 24, card_y0 + 56), "已关联 · 380,259 条目 · 指纹匹配",
           font=font(13), fill=TEXT_DIM)

    # 状态点
    d.ellipse([card_x1 - 130, card_y0 + 26, card_x1 - 118, card_y0 + 38],
              fill=(*GREEN, 255))
    d.text((card_x1 - 110, card_y0 + 24), "就绪", font=font(13), fill=GREEN)

    # 分割线
    d.line([(card_x0 + 24, card_y0 + 88), (card_x1 - 24, card_y0 + 88)],
           fill=(*BORDER, 255))

    # ------------------------------------------ 启动按钮（左侧中间）
    #
    # 布局改过一次：初版把按钮放在卡片左半边居中、右侧堆信息，
    # 结果按钮的圆顶到了副标题那行（「已关联 · 380,259 条目」）。
    #
    # 现在按「左右分区」排：左半区只有圆形按钮，垂直居中对齐卡片
    # 的**内容区**（表头以下）；右半区从「当前配置」起，全部信息纵向排列。
    # 两个区互不重叠，也就不会再有压字的问题。
    body_top = card_y0 + 96
    btn_cx = card_x0 + 155
    btn_cy = (body_top + card_y1) // 2
    round_p = ASSETS / "round.png"
    if round_p.is_file():
        rb = Image.open(round_p).convert("RGBA")
        target_h = 228
        s = target_h / rb.height
        rb = rb.resize((int(rb.width * s), target_h), Image.LANCZOS)
        paste_center(canvas, rb, btn_cx, btn_cy)
    else:
        d.ellipse([btn_cx - 114, btn_cy - 114, btn_cx + 114, btn_cy + 114],
                  fill=(*ORANGE, 255))

    # 按钮上写字（素材里文字已清空，这里叠字证明可替换）
    #
    # 字号配比：素材原字是「开始」(大) + 「START GAME」(小)。
    # 中文两字并排比四字小标更占宽，所以「启动游戏」拆成两行、
    # 字号统一，视觉重量才压得住这个直径 210px 的内盘。
    f_btn = font(40, True)
    for i, line in enumerate(("启动", "游戏")):
        tw_ = d.textlength(line, font=f_btn)
        d.text(
            (btn_cx - tw_ / 2, btn_cy - 44 + i * 44),
            line,
            font=f_btn,
            fill=(255, 255, 255),
            stroke_width=2,
            stroke_fill=(150, 70, 0),
        )

    # ------------------------------------------ 右侧信息区（卡片内）
    # 起点与按钮区共享 body_top：两栏从同一条水平线开始，
    # 视觉上才有对齐感。
    info_x = card_x0 + 316
    d.text((info_x, body_top + 12), "当前配置", font=font(14, True), fill=TEXT)
    rows = [
        ("存档档位", "默认"),
        ("已接入扩展", "25 个 · 全部生效"),
        ("扩展模式", "宽松校验"),
    ]
    ry = body_top + 42
    for label, value in rows:
        d.text((info_x, ry), label, font=font(13), fill=TEXT_FAINT)
        d.text((info_x + 96, ry), value, font=font(13), fill=TEXT_DIM)
        ry += 26

    # 次要按钮组（卡片底部右侧）
    sec_y = card_y1 - 52
    for i, (label, w_) in enumerate([("版本详情", 96), ("导入本体", 96)]):
        bx = info_x + i * (w_ + 12)
        rrect(d, (bx, sec_y, bx + w_, sec_y + 34), 8,
              fill=(*CARD_HI, 255), outline=(*BORDER_HI, 255))
        tw_ = d.textlength(label, font=font(13))
        d.text((bx + (w_ - tw_) / 2, sec_y + 9), label, font=font(13),
               fill=TEXT_DIM)

    # 卡片下方的存档条：原本这里空着一大片，视觉不平衡。
    # 分割线从 info_x 起，**不要从卡片左边界起** —— 左边是圆形按钮，
    # 线从左边走会横穿按钮（实测踩过，一眼看出是叠错层）。
    shortcut_y = card_y1 - 128
    d.line([(info_x - 10, shortcut_y - 14), (card_x1 - 24, shortcut_y - 14)],
           fill=(*BORDER, 255))
    d.text((info_x, shortcut_y - 2), "存档与入口", font=font(12, True),
           fill=TEXT_FAINT)
    chips = ["本地存档", "打开目录", "查看日志"]
    cx_ = info_x
    for label in chips:
        tw_ = d.textlength(label, font=font(12))
        cw_ = int(tw_ + 26)
        rrect(d, (cx_, shortcut_y + 20, cx_ + cw_, shortcut_y + 48), 7,
              fill=(*BG2, 255), outline=(*BORDER, 255))
        d.text((cx_ + 13, shortcut_y + 27), label, font=font(12), fill=TEXT_DIM)
        cx_ += cw_ + 8

    # ---------------------------------------------------- 右侧栏
    side_x0, side_y0 = 724, card_y0
    side_x1, side_y1 = W - 34, 560
    rrect(d, (side_x0, side_y0, side_x1, side_y1), 14,
          fill=(*CARD, 255), outline=(*BORDER, 255))
    d.line([(side_x0 + 14, side_y0 + 1), (side_x1 - 14, side_y0 + 1)],
           fill=(*BORDER_HI, 160))

    d.text((side_x0 + 22, side_y0 + 20), "扩展与状态",
           font=font(16, True), fill=TEXT)

    # 迷你统计块
    stats = [("25", "已装扩展", GREEN), ("0", "冲突", GREEN),
             ("4.7", "剧情线", CYAN)]
    sy = side_y0 + 58
    for i, (num, label, color) in enumerate(stats):
        sx = side_x0 + 22 + i * 136
        rrect(d, (sx, sy, sx + 124, sy + 76), 10,
              fill=(*BG2, 255), outline=(*BORDER, 255))
        d.text((sx + 14, sy + 12), num, font=font(26, True), fill=color)
        d.text((sx + 14, sy + 48), label, font=font(12), fill=TEXT_FAINT)

    # 扩展列表（示意 4 行）
    ly = sy + 96
    d.text((side_x0 + 22, ly), "已接入", font=font(13, True), fill=TEXT_DIM)
    ly += 26
    items = [
        ("装备兵工厂", "1.4.1", GREEN),
        ("六联浮游炮", "2.0.3", GREEN),
        ("宝石镶嵌", "2.0.0", ORANGE_HI),
        ("宠物经验修复", "1.0.0", GREEN),
    ]
    for name, ver, color in items:
        rrect(d, (side_x0 + 22, ly, side_x1 - 22, ly + 34), 8,
              fill=(*BG2, 220), outline=None)
        d.ellipse([side_x0 + 34, ly + 14, side_x0 + 42, ly + 22], fill=color)
        d.text((side_x0 + 52, ly + 9), name, font=font(13), fill=TEXT)
        vw = d.textlength(ver, font=font(11))
        d.text((side_x1 - 34 - vw, ly + 11), ver, font=font(11), fill=TEXT_FAINT)
        ly += 40

    # 侧栏底部提示：原本这块空着，看起来像内容没加载完
    tip_y = side_y1 - 52
    d.line([(side_x0 + 22, tip_y - 12), (side_x1 - 22, tip_y - 12)],
           fill=(*BORDER, 255))
    d.text((side_x0 + 22, tip_y), "已装扩展全部通过体检", font=font(12),
           fill=GREEN)
    d.text((side_x0 + 22, tip_y + 20), "4.7 适配：22 / 30 可用",
           font=font(12), fill=TEXT_FAINT)

    # ---------------------------------------------------- 底部导航
    nav_y0 = 592
    navs = [
        ("游戏库", "library", True),
        ("扩展管理", "puzzle", False),
        ("诊断", "pulse", False),
        ("设置", "gear", False),
    ]
    nx = 34
    nav_f = font(15)
    for label, _key, active in navs:
        tw_ = d.textlength(label, font=nav_f)
        cw = int(tw_ + 48)
        if active:
            # 选中态：橙色下划线 + 略亮的底
            rrect(d, (nx, nav_y0, nx + cw, nav_y0 + 44), 10,
                  fill=(*CARD_HI, 255))
            d.line([(nx + 12, nav_y0 + 44), (nx + cw - 12, nav_y0 + 44)],
                   fill=(*ORANGE, 255), width=3)
            d.text((nx + 24, nav_y0 + 12), label, font=nav_f, fill=TEXT)
        else:
            d.text((nx + 24, nav_y0 + 12), label, font=nav_f, fill=TEXT_FAINT)
        nx += cw + 8

    # 底部状态条
    d.line([(0, H - 44), (W, H - 44)], fill=(*BORDER, 255))
    d.ellipse([34, H - 30, 42, H - 22], fill=(*GREEN, 255))
    d.text((50, H - 33), "引擎就绪 · 两线可用", font=font(12), fill=TEXT_FAINT)
    hint = "启动器不含游戏本体"
    hw = d.textlength(hint, font=font(12))
    d.text((W - 34 - hw, H - 33), hint, font=font(12), fill=TEXT_FAINT)

    out = ASSETS / "_hub_mock.png"
    canvas.convert("RGB").save(out)
    print(f"已输出：{out}  {W}x{H}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())