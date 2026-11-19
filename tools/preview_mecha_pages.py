#!/usr/bin/env python
"""机甲风启动器 **四个页面** 的界面示意图生成器。

为什么单独做一个多页版（而不是改 ``preview_mecha_hub_v2.py``）
------------------------------------------------------------
四个页面共享同一套外壳：背景、LOGO、创作者标识、底部导航、状态栏。
如果每个页面各写一份，改配色或改导航就得改四处，必然漂移。
所以这里把外壳抽成 :func:`draw_shell`，每个页面只画自己的内容区。

四个页面
--------
==================  ====================================================
``game``    游戏库(HUB)   圆形启动按钮在左侧中间 + 右侧扩展状态面板
``ext``     扩展管理       左：已装扩展列表 右：选中扩展的详情
``diag``    诊断           体检项目清单 + effect-report 未生效项
``settings``设置           分组开关（开发者模式、扩展模式、路径等）
==================  ====================================================

按 2026-11-08 反馈修正的两处
----------------------------
1. **「设置」导航按钮与「当前配置」卡片重叠**：把配置卡片上移到
   y=584 以上、底部导航整体下移到 y=672，并把导航按钮宽度收窄到 164。
2. **标题右侧加创作者标识**：圆形头像 + 「柳江巷」+ B 站电视机图标。
   头像复用项目里 ``creator_asset.py`` 内联的横幅资源（已导出为
   ``avatar.png``），名字沿用代码里的「柳江巷」。

用法::

    python tools/preview_mecha_pages.py

输出 ``assets/ui/mecha/v2/pages/*.png``，以及一张四宫格总览 ``_all_pages.png``。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "ui" / "mecha" / "v2"
OUT = ASSETS / "pages"

W, H = 1180, 760

# ---------------------------------------------------------------- 配色
BG = (8, 18, 36)
BG_DEEP = (5, 12, 26)
CARD = (11, 28, 54)
CARD_HI = (16, 40, 74)
BORDER = (20, 62, 108)
CYAN = (0, 190, 255)
CYAN_DIM = (70, 150, 210)
TEXT = (235, 246, 255)
TEXT_DIM = (160, 195, 228)
TEXT_FAINT = (100, 140, 180)
ORANGE = (255, 154, 16)
GREEN = (76, 214, 150)
AMBER = (255, 196, 60)
RED = (240, 96, 110)

NAV_LABELS = ["游戏库", "扩展管理", "诊断", "设置"]
NAV_KEYS = ["game", "ext", "diag", "settings"]
NAV_Y = 672
NAV_W, NAV_H = 164, 52
NAV_GAP = 10
NAV_X0 = 36


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    cands = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    for c in cands:
        if Path(c).is_file():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def rrect(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def blend_line(canvas: Image.Image, pts, color, alpha: float):
    """半透明叠加直线（PIL 的 fill=(r,g,b,a) 是覆盖，不是混合）。"""
    arr = np.array(canvas, dtype=np.float32)
    h, w = arr.shape[:2]
    (x0, y0), (x1, y1) = pts
    n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for i in range(n):
        t = i / max(1, n - 1)
        x = int(round(x0 + (x1 - x0) * t))
        y = int(round(y0 + (y1 - y0) * t))
        if 0 <= x < w and 0 <= y < h:
            arr[y, x, :3] = arr[y, x, :3] * (1 - alpha) + np.array(
                color, dtype=np.float32
            ) * alpha
            arr[y, x, 3] = 255
    canvas.paste(Image.fromarray(arr.astype(np.uint8), "RGBA"))


def paste_center(base: Image.Image, img: Image.Image, cx: int, cy: int):
    base.alpha_composite(img, (cx - img.width // 2, cy - img.height // 2))


def scaled(path: Path, width: int) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    if im.width == width:
        return im
    return im.resize(
        (width, max(1, int(im.height * width / im.width))), Image.LANCZOS
    )


# ---------------------------------------------------------------- 背景


def _smoothstep(t: float) -> float:
    """0→1 的平滑插值，避免渐变出现「折线感」。"""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def draw_background(canvas: Image.Image):
    """铺背景立绘 + **从左到右的渐变暗罩**。

    2026-11-08 用户反馈「红色区域做成渐变，能让背景图显示完全」，
    针对的是原来那块**死黑竖带**。成因是第一版这么写::

        if x < 330: fade = 0.0        # ← 背景 alpha 直接归零
        elif x < 600: fade = (x-330)/270
        ...
        arr[:, x, 3] *= fade

    x<330 区域背景完全消失，只剩纯 BG 色；330~600 是线性突变，
    两者拼起来就是一条边界生硬的纯色板，机甲左边整个被切掉。

    **现在的做法：背景图始终完整贴上，再叠一层渐变暗罩。**
    - 遮罩透明度从左侧 0.80 平滑降到右侧 0.06（smoothstep，不是线性）
    - 左侧暗是为了让标题/按钮文字可读，但**仍能看见背景纹理**
    - 右侧几乎不遮，立绘完整展现
    - 全程没有硬边，也没有任何区域被替换成纯色

    顶部品牌区的压暗也改成局部渐变，不再画一条 0~430 的硬边色带。
    """
    bg_src = Image.open(ASSETS / "background.jpg").convert("RGB")
    scale = H / bg_src.height
    bg_big = bg_src.resize(
        (int(bg_src.width * scale), H), Image.LANCZOS
    )
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.paste(bg_big.convert("RGBA"), (40, 0))
    canvas.alpha_composite(layer)

    # --- 渐变暗罩（背景始终可见，只是左边更暗）
    mask = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    md = ImageDraw.Draw(mask, "RGBA")
    for x in range(W):
        t = _smoothstep(x / W)
        # 左端 0.86：实测 0.80 时左侧机甲手臂太亮，和圆形按钮叠在一起
        # 显得杂乱；0.86 仍能隐约看见纹理，但按钮和标题能稳住。
        a = 0.86 * (1.0 - t) + 0.04 * t
        md.line([(x, 0), (x, H)], fill=(*BG_DEEP, int(255 * a)))
    canvas.alpha_composite(mask)

    # --- 顶部品牌区：局部渐变压暗（纵向 + 横向都羽化，无硬边）
    top = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(top, "RGBA")
    for y in range(150):
        vy = 1.0 - _smoothstep(y / 150)
        for x in range(0, 520, 4):
            vx = 1.0 - _smoothstep(max(0, x - 300) / 220)
            td.rectangle([x, y, x + 4, y + 1],
                         fill=(*BG_DEEP, int(255 * 0.62 * vy * vx)))
    canvas.alpha_composite(top)

    # --- 底部状态条压暗（同样渐变）
    bot = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bot, "RGBA")
    for y in range(H - 76, H):
        k = _smoothstep((y - (H - 76)) / 76)
        bd.line([(0, y), (W, y)], fill=(*BG_DEEP, int(255 * 0.82 * k)))
    canvas.alpha_composite(bot)

    # --- 科技感细网格（只右侧，极低透明度）
    for x in range(380, W, 64):
        blend_line(canvas, ((x, 0), (x, H)), (0, 190, 255), 0.028)
    for y in range(0, H, 64):
        blend_line(canvas, ((380, y), (W, y)), (0, 190, 255), 0.028)

    # --- 原图自带小 LOGO：羽化补丁盖掉，避免与左上品牌重复。
    # 实测位置：原图 x 520~740 / y 600~660 → 画布 x 588~821 / y 633~696。
    # 遮罩是在「背景已完整贴上」之后做的，所以补丁只影响那一小块，
    # 不会再把左半边切成纯色。
    arr = np.array(canvas).astype(np.float32)
    lx0, lx1, ly0, ly1 = 556, 856, 606, 716
    feather = 26
    for yy in range(ly0, ly1):
        edge = min(yy - ly0, ly1 - yy) / feather
        for xx in range(lx0, lx1):
            edge_x = min(xx - lx0, lx1 - xx) / feather
            k = min(1.0, edge, edge_x) * 0.94
            arr[yy, xx, :3] = (
                arr[yy, xx, :3] * (1 - k)
                + np.array(BG, np.float32) * k
            )
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


# ---------------------------------------------------------------- 启动按钮


def draw_launch_button(base: Image.Image, cx: int, cy: int, size: int = 244):
    """圆形启动按钮 + **LOGO 银白金属渐变**大字。

    演变过程（用户三次反馈，全部记下来避免重走）：

    1. 初版深棕实心 + 细白描边、字号 40 撑满内盘 → 发脏、闷
    2. 白→金渐变 + 3px 描边 + 橙外发光 → 字糊虚，英文压蜂窝纹
    3. 六个候选中用户选 **E**：「启动」大字 + ``START GAME`` 横条
    4. **11-09 追加**：大字改用 LOGO 的**颜色风格**，明确「不要斜切，
       字体正常」。提取的是配色而非造型：

       * 上白 → 一道横向高光 → 下灰的**银色金属渐变**
       * 深蓝灰主描边 + 浅银内描边（双层边）
       * 底字雅黑粗（比黑体饱满，接近 LOGO 厚重感）
       * **不加青蓝外发光** —— 那是 LOGO 背景框的效果，压在橙盘上
         会突兀发虚（候选 B 无发光最干净）

    英文横条保持定稿 E：深棕圆角底 + 白色拉字距小字。

    候选对比：``tools/preview_btn_text_options.py`` → ``_btn_text_options.png``
    金属配色候选：``tools/preview_mecha_font.py`` → ``_font_options.png``

    这个函数同时被 ``page_game`` 和 ``preview_mecha_toggle`` 共用。
    """
    rb = scaled(ASSETS / "round.png", size)
    paste_center(base, rb, cx, cy)

    scale = size / 244.0
    f_cn = ImageFont.truetype(
        r"C:\Windows\Fonts\msyhbd.ttc", max(14, int(42 * scale))
    )
    f_en = font(max(7, int(10 * scale)), True)
    bx0, by0 = cx - size // 2, cy - size // 2
    probe = ImageDraw.Draw(base)

    # --- 中文大字「启动」：银白金属渐变
    text = "启动"
    tw = probe.textlength(text, font=f_cn)
    x = cx - tw / 2
    y = cy - int(42 * scale)

    def _layer(fill, stroke_w=0):
        ly = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(ly).text(
            (x - bx0, y - by0), text, font=f_cn, fill=fill,
            stroke_width=stroke_w, stroke_fill=fill,
        )
        return ly

    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).text((x - bx0, y - by0), text, font=f_cn, fill=255)

    sh = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(sh).text(
        (x - bx0 + 1, y - by0 + 2), text, font=f_cn,
        fill=(24, 40, 62, 140),
    )
    sh = sh.filter(ImageFilter.GaussianBlur(1.6))
    base.alpha_composite(sh, (bx0, by0))

    base.alpha_composite(_layer((62, 32, 12, 255), 3), (bx0, by0))
    base.alpha_composite(_layer((206, 222, 236, 255), 1), (bx0, by0))

    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    y0 = max(0, int(y - by0) - 4)
    y1 = min(size, int(y - by0) + int(58 * scale))
    for gy in range(y0, y1):
        t = (gy - y0) / max(1, (y1 - y0) - 1)
        if t < 0.42:
            k = t / 0.42
            col = (int(250 - 18 * k), int(253 - 20 * k), int(255 - 18 * k), 255)
        elif t < 0.53:                       # 横向高光带
            col = (255, 255, 255, 255)
        else:
            k = (t - 0.53) / 0.47
            col = (int(232 - 88 * k), int(238 - 82 * k), int(242 - 68 * k), 255)
        gd.line([(0, gy), (size, gy)], fill=col)
    grad.putalpha(m)
    base.alpha_composite(grad, (bx0, by0))

    # --- 英文横条：深棕底 + 白色拉字距小字
    small = "S T A R T   G A M E"
    sw = probe.textlength(small, font=f_en)
    bar_w = sw + int(18 * scale)
    bar_h = max(12, int(17 * scale))
    bar_y = cy + int(23 * scale)
    d = ImageDraw.Draw(base, "RGBA")
    rrect(d,
          (cx - bar_w / 2, bar_y, cx + bar_w / 2, bar_y + bar_h),
          bar_h // 2,
          fill=(104, 38, 4, 225), outline=(214, 108, 26, 190))
    d.text((cx - sw / 2, bar_y + (bar_h - 13 * scale) / 2),
           small, font=f_en, fill=(255, 238, 208, 255))
    return base


def paste_bilibili(base: Image.Image, x: int, cy: int, width: int = 62):
    """贴高清 bilibili 字标，按 ``cy`` 垂直居中。

    素材是 512x236 的粉色字标（不是小电视），已去白底并锐化。
    为什么不用早先手画的小电视：用户给了官方字标，且手画的那个
    在 22px 下只是个粉色方块，认不出来。
    """
    icon = Image.open(ASSETS / "bilibili.png").convert("RGBA")
    h = max(1, int(icon.height * width / icon.width))
    icon = icon.resize((width, h), Image.LANCZOS)
    base.alpha_composite(icon, (x, cy - h // 2))


def draw_creator(base: Image.Image, x: int, y: int):
    """创作者标识：圆形头像 + 名字 + 徽章 + B 站字标。

    **对称修正（2026-11-08 用户指出「不对称」）**，原来的问题是：

    1. 头像偏上，底板下方留白比上方多；
    2. 左侧「柳江巷」+「创作者」是**上下两行**，而右侧「B站主页」
       是**单行居中** —— 左右两组视觉重心不在一个高度；
    3. 「创作者」徽章胶囊过宽，占了名字下面整行，和右侧紧凑排布不搭。

    现在改成：

    * 底板高 40，头像 32px **精确居中**（上下各留 4px）
    * 名字与徽章**同一行并排**，整组垂直居中
    * B 站图标与「B站主页」文字也是同一行、垂直居中
    * 左右两组用一条竖直分隔线分开，各自内部独立居中
    * 底板左右各留 12px 内边距
    * **右侧只放 bilibili 字标，不写「B站主页」文字**（2026-11-08 用户要求）。
      底板宽度随之收窄到 230，否则右边会空一大块。
    """
    d = ImageDraw.Draw(base, "RGBA")
    bh = 40
    rrect(d, (x, y, x + 230, y + bh), bh // 2,
          fill=(10, 26, 50, 225), outline=(24, 78, 128, 255))
    cy = y + bh // 2

    # --- 左组：头像 + 名字 + 徽章，全部按 cy 垂直居中
    av = Image.open(ASSETS / "avatar.png").convert("RGBA")
    av = av.resize((32, 32), Image.LANCZOS)
    base.alpha_composite(av, (x + 10, cy - 16))
    d = ImageDraw.Draw(base, "RGBA")

    name_f = font(14, True)
    d.text((x + 48, cy - 10), "柳江巷", font=name_f, fill=TEXT)
    nw = d.textlength("柳江巷", font=name_f)

    badge_x = x + 48 + nw + 8
    rrect(d, (badge_x, cy - 9, badge_x + 44, cy + 9), 9,
          fill=(90, 60, 180, 225))
    d.text((badge_x + 6, cy - 8), "创作者", font=font(9),
           fill=(235, 225, 255))

    # --- 分隔线（放在两组正中间）
    sep_x = x + 152
    d.line([(sep_x, y + 9), (sep_x, y + bh - 9)],
           fill=(40, 90, 140, 200))

    # --- 右组：只有 bilibili 字标，按 cy 垂直居中。
    # 字标宽 56，从 sep_x+12 起 → 到 sep_x+68；底板右边界 x+230，
    # 右留白约 12px，与左边头像的 10px 基本对称。
    paste_bilibili(base, sep_x + 12, cy, width=56)


# ---------------------------------------------------------------- 外壳


def draw_shell(active: str) -> Image.Image:
    """画四个页面共用的外壳，返回可继续作画的 RGBA 画布。"""
    canvas = Image.new("RGBA", (W, H), (*BG, 255))
    # 背景 + 渐变暗罩 + 网格 + 小 LOGO 补丁，全部在 draw_background 里
    canvas = draw_background(canvas)

    d = ImageDraw.Draw(canvas, "RGBA")

    # ---- 品牌 LOGO
    logo = scaled(ASSETS / "logo.png", 300)
    canvas.alpha_composite(logo, (26, 4))
    d = ImageDraw.Draw(canvas, "RGBA")

    # ---- 版本徽标
    badge = "v1.7 · 统一版"
    bf = font(12)
    bw = d.textlength(badge, font=bf)
    rrect(d, (W - 26 - bw - 20, 24, W - 26, 50), 12,
          fill=(*CARD_HI, 235), outline=(*BORDER, 255))
    d.text((W - 26 - bw - 10, 31), badge, font=bf, fill=TEXT_DIM)

    # ---- 页面标题 + 创作者标识（标题右侧）
    title = {
        "game": "新起点启动器",
        "ext": "扩展管理",
        "diag": "诊断",
        "settings": "设置",
    }[active]
    d.text((34, 108), title, font=font(24, True), fill=TEXT)

    # 创作者区放在标题**右边**，测量标题宽度后紧跟。
    # y=102 让 40px 高的底板与 24px 标题在同一条视觉中线上。
    tw = d.textlength(title, font=font(24, True))
    draw_creator(canvas, int(34 + tw + 22), 102)

    # 副标题（只有游戏库页有这一行游戏信息，其它页换成页面说明）
    sub = {
        "game": "机甲旋风 1.45 · 原版   |   已关联 · 380,259 条目 · 指纹匹配",
        "ext": "管理已安装的扩展包，查看冲突、体检结果与适配状态",
        "diag": "检查基线、覆盖层与扩展补丁的实际生效情况",
        "settings": "启动器行为、扩展校验档位与路径配置",
    }[active]
    d.text((34, 148), sub, font=font(11), fill=TEXT_FAINT)
    d.ellipse([34, 170, 42, 178], fill=(*GREEN, 255))
    d.text((50, 166), "引擎就绪 · 两线可用", font=font(11), fill=GREEN)

    # ---- 底部导航（宽度收窄，避开右侧卡片）
    for i, label in enumerate(NAV_LABELS):
        x0 = NAV_X0 + i * (NAV_W + NAV_GAP)
        box = (x0, NAV_Y, x0 + NAV_W, NAV_Y + NAV_H)
        bar = scaled(ASSETS / "btn_bar.png", NAV_W)
        if bar.height != NAV_H:
            bar = bar.resize((NAV_W, NAV_H), Image.LANCZOS)
        canvas.alpha_composite(bar, (x0, NAV_Y))
        d = ImageDraw.Draw(canvas, "RGBA")
        f = font(18, True)
        w_ = d.textlength(label, font=f)
        d.text((x0 + (NAV_W - w_) / 2, NAV_Y + (NAV_H - 30) / 2),
               label, font=f, fill=(45, 22, 0, 255),
               stroke_width=1, stroke_fill=(255, 224, 150, 255))
        if NAV_KEYS[i] == active:
            rrect(d, box, 10, outline=(255, 220, 120, 230), width=2)

    # ---- 底部状态条
    d = ImageDraw.Draw(canvas, "RGBA")
    d.line([(0, H - 26), (W, H - 26)], fill=(*BORDER, 200))
    d.ellipse([34, H - 18, 41, H - 11], fill=(*GREEN, 255))
    d.text((48, H - 21), "引擎就绪 · 两线可用", font=font(10), fill=TEXT_FAINT)
    hint = "启动器不含游戏本体"
    hw = d.textlength(hint, font=font(10))
    d.text((W - 26 - hw, H - 21), hint, font=font(10), fill=TEXT_FAINT)
    return canvas


# ---------------------------------------------------------------- 页面：游戏库


def page_game(c: Image.Image) -> Image.Image:
    d = ImageDraw.Draw(c, "RGBA")

    # 圆形启动按钮（左侧中间）—— 样式统一走 draw_launch_button
    btn_cx, btn_cy = 180, 400
    draw_launch_button(c, btn_cx, btn_cy, 244)
    d = ImageDraw.Draw(c, "RGBA")
    d.text((btn_cx - 30, btn_cy + 132), "尚未启动", font=font(11),
           fill=TEXT_DIM)

    # 右侧科技蓝面板
    px0, py0 = 690, 208
    pw, ph = 452, 228
    panel = scaled(ASSETS / "panel.png", pw)
    panel = panel.resize((pw, ph), Image.LANCZOS)
    c.alpha_composite(panel, (px0, py0))
    d = ImageDraw.Draw(c, "RGBA")

    ix0, ix1 = px0 + 30, px0 + pw - 30
    d.text((ix0, py0 + 20), "扩展与状态", font=font(15, True), fill=TEXT)
    d.line([(ix0, py0 + 44), (ix1, py0 + 44)], fill=(30, 100, 165, 200))

    sy = py0 + 54
    for i, (num, label, color) in enumerate([
        ("25", "已装扩展", GREEN), ("0", "冲突", GREEN), ("4.7", "剧情线", CYAN)
    ]):
        sx = ix0 + i * 130
        rrect(d, (sx, sy, sx + 118, sy + 52), 7,
              fill=(6, 20, 40, 220), outline=(25, 80, 140, 255))
        d.text((sx + 10, sy + 4), num, font=font(20, True), fill=color)
        d.text((sx + 10, sy + 32), label, font=font(10), fill=TEXT_FAINT)

    ly = sy + 62
    d.text((ix0, ly), "已接入", font=font(10, True), fill=TEXT_DIM)
    ly += 18
    for i, (name, color) in enumerate([
        ("装备兵工厂", GREEN), ("六联浮游炮", GREEN),
        ("宝石镶嵌", ORANGE), ("宠物经验修复", GREEN),
    ]):
        col, row = i % 2, i // 2
        bx = ix0 + col * ((ix1 - ix0) // 2)
        by = ly + row * 28
        rrect(d, (bx, by, bx + (ix1 - ix0) // 2 - 8, by + 24), 5,
              fill=(8, 24, 46, 210), outline=(22, 70, 120, 180))
        d.ellipse([bx + 9, by + 9, bx + 15, by + 15], fill=color)
        d.text((bx + 22, by + 5), name, font=font(10), fill=TEXT)

    # 当前配置卡片 —— 紧贴面板下方 14px，且底部离导航(y672)留足距离。
    # 第一版放在 y=590，正好和「设置」按钮撞上（用户截图里红框指出的）。
    cy0 = 450
    rrect(d, (px0, cy0, px0 + pw, cy0 + 96), 10,
          fill=(*CARD, 238), outline=(*BORDER, 255))
    d.text((px0 + 18, cy0 + 10), "当前配置", font=font(12, True), fill=TEXT)
    for i, (k, v) in enumerate([
        ("存档档位", "默认"), ("扩展模式", "宽松校验"), ("游戏线", "4.6 / 4.7")
    ]):
        yy = cy0 + 32 + i * 19
        d.text((px0 + 18, yy), k, font=font(10), fill=TEXT_FAINT)
        d.text((px0 + 96, yy), v, font=font(10), fill=TEXT_DIM)

    d.text((px0 + 4, cy0 + 104), "已装扩展全部通过体检 · 4.7 适配 22/30",
           font=font(10), fill=GREEN)
    return c


# ---------------------------------------------------------------- 通用卡片工具


def card(d, box, title: str, subtitle: str = ""):
    """统一的内容卡片：半透明深蓝底 + 细边框 + 标题。

    标题在 y+12、副标题在 y+38（原来是 y+36，实测会和标题的降部
    挤在一起，看起来像两行字叠着）。有副标题时内容起点必须 >= y+62，
    各页面自己控制，不然第一行会压到副标题。
    """
    x0, y0, x1, y1 = box
    rrect(d, box, 10, fill=(9, 24, 46, 232), outline=(*BORDER, 255))
    d.line([(x0 + 12, y0 + 1), (x1 - 12, y0 + 1)], fill=(30, 110, 180, 170))
    d.text((x0 + 16, y0 + 12), title, font=font(14, True), fill=TEXT)
    if subtitle:
        d.text((x0 + 16, y0 + 38), subtitle, font=font(10), fill=TEXT_FAINT)


def chip(d, x, y, label, color=GREEN):
    """状态小圆点 + 文字。"""
    d.ellipse([x, y + 4, x + 7, y + 11], fill=color)
    d.text((x + 12, y), label, font=font(11), fill=TEXT_DIM)


# ---------------------------------------------------------------- 页面：扩展管理


def page_ext(c: Image.Image) -> Image.Image:
    d = ImageDraw.Draw(c, "RGBA")

    # 顶部工具条
    rrect(d, (34, 196, 1146, 240), 8, fill=(9, 24, 46, 230),
          outline=(*BORDER, 255))
    d.text((50, 207), "已安装 25 个 · 全部启用", font=font(12), fill=TEXT_DIM)
    for i, (label, x) in enumerate([("导入扩展包", 900), ("体检", 1020)]):
        rrect(d, (x, 204, x + 112, 232), 7,
              fill=(*CARD_HI, 255), outline=(35, 110, 180, 255))
        tw = d.textlength(label, font=font(11))
        d.text((x + (112 - tw) / 2, 211), label, font=font(11), fill=TEXT)

    # 左：扩展列表（内容从 y+64 起，避开副标题）
    card(d, (34, 252, 600, 656), "扩展列表", "按加载顺序排列 · 点击查看详情")
    rows = [
        ("装备兵工厂", "1.4.1", GREEN, True),
        ("六联浮游炮", "2.0.3", GREEN, False),
        ("宝石镶嵌与宝石矿场", "2.0.0", AMBER, False),
        ("奥丽宠物", "1.5.42", GREEN, False),
        ("次元裂缝", "1.2.0", GREEN, False),
        ("宠物经验获取重做", "1.0.0", GREEN, False),
        ("战力系统", "1.1.0", GREEN, False),
    ]
    ry = 316
    for name, ver, color, sel in rows:
        if sel:
            rrect(d, (46, ry, 588, ry + 40), 6,
                  fill=(20, 56, 100, 235), outline=(0, 190, 255, 220))
        else:
            rrect(d, (46, ry, 588, ry + 40), 6, fill=(7, 20, 40, 200))
        d.ellipse([58, ry + 16, 66, ry + 24], fill=color)
        d.text((76, ry + 11), name, font=font(12), fill=TEXT)
        vw = d.textlength(ver, font=font(10))
        d.text((576 - vw, ry + 13), ver, font=font(10), fill=TEXT_FAINT)
        ry += 46

    # 右：选中扩展详情（无副标题，内容可以从 y+34 起）
    card(d, (616, 252, 1146, 656), "扩展详情", "")
    d.text((636, 292), "装备兵工厂", font=font(19, True), fill=TEXT)
    d.text((636, 310), "jjxf.better_forge  ·  v1.4.1  ·  4.6 线",
           font=font(11), fill=TEXT_FAINT)
    rrect(d, (636, 336, 716, 358), 12, fill=(20, 90, 60, 235))
    d.text((648, 340), "已启用", font=font(11), fill=GREEN)

    d.line([(636, 374), (1126, 374)], fill=(*BORDER, 255))
    detail = [
        ("文件", "93 个"),
        ("文本补丁", "8 / 8 命中"),
        ("方法钩子", "0（不使用）"),
        ("脚本成员", "9 处"),
        ("位置冲突", "0"),
        ("生效报告", "全部生效"),
    ]
    dy = 388
    for k, v in detail:
        d.text((636, dy), k, font=font(11), fill=TEXT_FAINT)
        d.text((760, dy), v, font=font(11), fill=TEXT_DIM)
        dy += 24

    d.line([(636, 542), (1126, 542)], fill=(*BORDER, 255))
    d.text((636, 552), "4.7 适配", font=font(11, True), fill=TEXT_DIM)
    chip(d, 636, 574, "需作者处理：hud.gd 函数体重写", AMBER)

    for i, (label, x) in enumerate([("停用", 636), ("卸载", 756), ("打开目录", 876)]):
        rrect(d, (x, 610, x + 108, 640), 7,
              fill=(*CARD_HI, 255), outline=(*BORDER, 255))
        tw = d.textlength(label, font=font(11))
        d.text((x + (108 - tw) / 2, 618), label, font=font(11), fill=TEXT)
    return c


# ---------------------------------------------------------------- 页面：诊断


def page_diag(c: Image.Image) -> Image.Image:
    d = ImageDraw.Draw(c, "RGBA")

    # 顶部汇总条
    rrect(d, (34, 196, 1146, 246), 8, fill=(9, 24, 46, 230),
          outline=(*BORDER, 255))
    for i, (num, label, color) in enumerate([
        ("12", "体检项", CYAN), ("11", "正常", GREEN),
        ("1", "异常", AMBER), ("0", "失败", GREEN),
    ]):
        sx = 56 + i * 270
        d.text((sx, 206), num, font=font(22, True), fill=color)
        d.text((sx + 48, 214), label, font=font(11), fill=TEXT_DIM)

    # 左：体检清单（右边界 692，与右卡片 x=716 留 24px 间隙）
    card(d, (34, 258, 692, 656), "体检项目", "只读检查 · 不修改任何数据")
    checks = [
        ("基线 1.45 指纹", "匹配", GREEN),
        ("基线 4.7 指纹", "匹配", GREEN),
        ("覆盖层完整性", "25 / 25 可读", GREEN),
        ("扩展注册表", "25 条有效", GREEN),
        ("位置冲突", "0 处", GREEN),
        ("扩展补丁 4.6", "全部生效", GREEN),
        ("扩展补丁 4.7", "1 处未生效", AMBER),
        ("存档隔离", "已启用", GREEN),
        ("引擎可执行文件", "已关联", GREEN),
    ]
    ry = 318
    for name, result, color in checks:
        rrect(d, (46, ry, 680, ry + 34), 6, fill=(7, 20, 40, 200))
        d.ellipse([58, ry + 13, 66, ry + 21], fill=color)
        d.text((76, ry + 9), name, font=font(11), fill=TEXT)
        rw = d.textlength(result, font=font(11))
        d.text((668 - rw, ry + 9), result, font=font(11), fill=color)
        ry += 36

    # 右：异常详情
    card(d, (716, 258, 1146, 656), "异常详情", "来自 effect-report.json")

    rrect(d, (732, 316, 1130, 396), 8,
          fill=(70, 50, 12, 220), outline=(180, 140, 40, 255))
    d.text((746, 326), "扩展补丁 4.7 · 1 处未生效",
           font=font(12, True), fill=AMBER)
    d.text((746, 350), "jjxf.better_forge —— hud.gd 锚点失配",
           font=font(11), fill=TEXT_DIM)
    d.text((746, 370), "扩展显示已启用，但游戏里不会有变化",
           font=font(10), fill=TEXT_FAINT)

    d.text((732, 410), "建议操作", font=font(12, True), fill=TEXT)
    for i, line in enumerate([
        "1. 确认该包是否已出 4.7 适配版",
        "2. 未适配则联系作者，或暂时停用",
        "3. 详细锚点见「扩展管理 → 详情」",
    ]):
        d.text((732, 426 + i * 22), line, font=font(11), fill=TEXT_DIM)

    d.line([(732, 506), (1130, 506)], fill=(*BORDER, 255))
    d.text((732, 516), "最近一次运行", font=font(11, True), fill=TEXT_DIM)
    for i, (k, v) in enumerate([
        ("运行目录", "builds/run46-20261108-193609"),
        ("扩展挂载", "18 个 mod / 27 个 pack"),
        ("跳过补丁", "0"),
    ]):
        yy = 540 + i * 22
        d.text((732, yy), k, font=font(10), fill=TEXT_FAINT)
        d.text((836, yy), v, font=font(10), fill=TEXT_DIM)

    for i, (label, x) in enumerate([("重新体检", 732), ("打开日志", 852)]):
        rrect(d, (x, 610, x + 108, 640), 7,
              fill=(*CARD_HI, 255), outline=(*BORDER, 255))
        tw = d.textlength(label, font=font(11))
        d.text((x + (108 - tw) / 2, 618), label, font=font(11), fill=TEXT)
    return c


# ---------------------------------------------------------------- 页面：设置


def page_settings(c: Image.Image) -> Image.Image:
    d = ImageDraw.Draw(c, "RGBA")

    groups = [
        (34, 196, 574, 656, "启动器", [
            ("开发者模式", "关闭", "开启后显示诊断工具与高级开关"),
            ("状态面板默认隐藏", "开启", "隐藏右侧扩展面板，点边缘标签召唤"),
            ("扩展校验档位", "宽松", "4.7 线固定使用完全校验"),
            ("启动后自动体检", "开启", "每次启动前检查覆盖层完整性"),
        ]),
        (600, 196, 1146, 656, "路径与数据", [
            ("1.45 游戏本体", "已关联", "game/mchGame.exe"),
            ("4.7 剧情包", "已关联", "game/jjxf47.pck"),
            ("存档隔离", "已启用", "每个档位独立目录，不碰原版存档"),
            ("缓存目录", "自动", "builds/ · 可手动清理"),
        ]),
    ]

    for x0, y0, x1, y1, title, rows in groups:
        card(d, (x0, y0, x1, y1), title, "")
        ry = y0 + 54
        for label, value, desc in rows:
            rrect(d, (x0 + 12, ry, x1 - 12, ry + 84), 8,
                  fill=(7, 20, 40, 210), outline=(20, 62, 108, 220))
            d.text((x0 + 26, ry + 12), label, font=font(12, True), fill=TEXT)
            d.text((x0 + 26, ry + 36), desc, font=font(10), fill=TEXT_FAINT)

            # 开关 / 值胶囊
            is_switch = value in ("关闭", "开启")
            if is_switch:
                on = value == "开启"
                bx, by = x1 - 92, ry + 14
                col = (24, 96, 66, 255) if on else (30, 46, 70, 255)
                rrect(d, (bx, by, bx + 64, by + 26), 13, fill=col)
                kx = bx + 40 if on else bx + 8
                d.ellipse([kx, by + 4, kx + 18, by + 22],
                          fill=(235, 246, 255, 255))
            else:
                vw = d.textlength(value, font=font(11))
                bx = x1 - 30 - vw - 20
                rrect(d, (bx, ry + 12, x1 - 30, ry + 40), 8,
                      fill=(*CARD_HI, 255), outline=(*BORDER, 255))
                d.text((bx + 10, ry + 19), value, font=font(11),
                       fill=TEXT_DIM)
            ry += 96

    # 底部说明放在**右卡片内部下沿**（卡片底 656，说明 y 636），
    # 不要放在 y=660 —— 那正好和底部导航(y672)的「设置」按钮撞上。
    d.text((616, 636), "更改后立即生效 · 部分路径项需重启启动器",
           font=font(10), fill=TEXT_FAINT)
    return c


PAGES = {
    "game": page_game,
    "ext": page_ext,
    "diag": page_diag,
    "settings": page_settings,
}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    OUT.mkdir(parents=True, exist_ok=True)

    shots = []
    for key, draw_fn in PAGES.items():
        canvas = draw_shell(key)
        canvas = draw_fn(canvas)
        dst = OUT / f"{key}.png"
        canvas.convert("RGB").save(dst)
        print(f"  {key:9} -> {dst.name}")
        shots.append((key, canvas.convert("RGB")))

    # 四宫格总览（每张缩到 50%）
    sw, sh = W // 2, H // 2
    grid = Image.new("RGB", (sw * 2, sh * 2), BG)
    for i, (key, im) in enumerate(shots):
        small = im.resize((sw, sh), Image.LANCZOS)
        grid.paste(small, ((i % 2) * sw, (i // 2) * sh))
    all_path = ASSETS / "_all_pages.png"
    grid.save(all_path)
    print(f"\n四宫格总览 -> {all_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())