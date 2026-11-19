#!/usr/bin/env python
"""把「启动」做成 LOGO 那种**银白金属渐变**配色（字体不斜切）。

参考字形（用户给的「机甲旋风」LOGO 局部）特征：

* 向右倾斜约 12°（italic / shear）
* 笔画方正、末端锐利斜切，不是圆润的黑体
* **上白下灰的银色金属渐变**，中间有一道横向高光
* 深色描边 + 青蓝色细外发光

系统里没有这种专用美术字，所以用「黑体底 + 仿射斜切 +
金属渐变 + 高光带 + 双层描边」还原。

用法::

    python tools/preview_mecha_font.py

输出 ``assets/ui/mecha/v2/_font_options.png``（几种底字/斜切角度对比）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "ui" / "mecha" / "v2"
OUT = ASSETS / "_font_options.png"

sys.path.insert(0, str(ROOT / "tools"))
import preview_mecha_pages as P  # noqa: E402

SIZE = 300
CELL = 340
LABEL = 46
FONTS = {
    "黑体": r"C:\Windows\Fonts\simhei.ttf",
    "雅黑粗": r"C:\Windows\Fonts\msyhbd.ttc",
    "等线粗": r"C:\Windows\Fonts\Dengb.ttf",
    "Impact": r"C:\Windows\Fonts\impact.ttf",
}


def shear_layer(layer: Image.Image, angle_deg: float) -> Image.Image:
    """对整层做水平斜切（向右倾）。

    PIL 的 AFFINE 是「输出像素 → 输入像素」的映射，
    ``x_in = x_out - tan(angle) * (y_out - cy)`` 让字形顶部相对底部右移。
    """
    k = np.tan(np.deg2rad(angle_deg))
    cy = layer.height / 2
    # (a, b, c, d, e, f): x_in = a*x + b*y + c ; y_in = d*x + e*y + f
    data = (1.0, -k, k * cy, 0.0, 1.0, 0.0)
    return layer.transform(
        layer.size, Image.Transform.AFFINE, data,
        resample=Image.Resampling.BICUBIC,
    )


def metal_text(
    base: Image.Image,
    text: str,
    font_path: str,
    *,
    angle: float = 12,
    fsize: int = 52,
    top_y: int | None = None,
    with_glow: bool = True,
):
    """在按钮上画一个斜切银白金属字。"""
    d_probe = ImageDraw.Draw(base)
    f = ImageFont.truetype(font_path, fsize)
    tw = d_probe.textlength(text, font=f)
    cx = base.width / 2
    x = cx - tw / 2
    y = top_y if top_y is not None else cx - fsize * 0.62

    # --- 1 字形 mask（黑体底）
    m = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(m).text((x, y), text, font=f, fill=255)
    m = shear_layer(m, angle)

    # --- 2 外发光（青蓝，呼应 LOGO）
    if with_glow:
        glow = shear_layer(
            _stroke_layer(text, f, x, y, (0, 190, 255, 150), 7), angle
        ).filter(ImageFilter.GaussianBlur(4.0))
        base.alpha_composite(glow)

    # --- 3 深色主描边
    stroke = shear_layer(
        _stroke_layer(text, f, x, y, (26, 52, 78, 255), 5), angle
    )
    base.alpha_composite(stroke)

    inner = shear_layer(
        _stroke_layer(text, f, x, y, (200, 220, 235, 255), 2), angle
    )
    base.alpha_composite(inner)

    # --- 4 银白金属渐变：上白 → 中亮 → 下灰
    grad = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    y0, y1 = int(y) - 8, int(y) + fsize + 16
    for gy in range(max(0, y0), min(SIZE, y1)):
        t = (gy - y0) / max(1, (y1 - y0) - 1)
        if t < 0.42:
            k = t / 0.42
            col = (int(250 - 20 * k), int(253 - 22 * k), int(255 - 20 * k), 255)
        elif t < 0.54:      # 横向高光带
            col = (255, 255, 255, 255)
        else:
            k = (t - 0.54) / 0.46
            col = (int(230 - 92 * k), int(236 - 86 * k), int(240 - 72 * k), 255)
        gd.line([(0, gy), (SIZE, gy)], fill=col)
    grad.putalpha(m)
    base.alpha_composite(grad)
    return base


def _stroke_layer(text, f, x, y, color, width) -> Image.Image:
    """生成一层带描边的文字（用于斜切前）。"""
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((x, y), text, font=f, fill=color,
           stroke_width=width, stroke_fill=color)
    return layer


def blank() -> Image.Image:
    c = Image.new("RGBA", (SIZE, SIZE), (10, 22, 42, 255))
    rb = P.scaled(ASSETS / "round.png", SIZE)
    P.paste_center(c, rb, SIZE // 2, SIZE // 2)
    return c


#: 用户明确「不要斜切，就这个颜色风格，字体正常」——
#: 所以 angle 一律 0，只对比金属渐变的强弱与描边/发光配色。
CASES = [
    ("A 银白渐变 + 青蓝发光", "黑体", 0, 52, True),
    ("B 银白渐变 + 无发光", "黑体", 0, 52, False),
    ("C 银白渐变 · 雅黑粗", "雅黑粗", 0, 52, True),
    ("D 银白渐变 · 等线粗", "等线粗", 0, 52, True),
    ("E 银白渐变 · 字号58", "黑体", 0, 58, True),
    ("F 银白渐变 · 字号46", "黑体", 0, 46, True),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    cols, rows = 3, 2
    grid = Image.new("RGB", (CELL * cols, (CELL + LABEL) * rows), P.BG)
    gd = ImageDraw.Draw(grid)
    for i, (label, fname, angle, fsize, glow) in enumerate(CASES):
        c = blank()
        c = metal_text(
            c, "启动", FONTS[fname], angle=angle, fsize=fsize,
            with_glow=glow,
        )
        # 英文横条（沿用定稿 E）
        d = ImageDraw.Draw(c, "RGBA")
        small = "S T A R T   G A M E"
        sf = P.font(11, True)
        sw = d.textlength(small, font=sf)
        bw = sw + 20
        bh = 19
        by = SIZE // 2 + 22
        P.rrect(d, (SIZE // 2 - bw / 2, by, SIZE // 2 + bw / 2, by + bh),
                bh // 2, fill=(104, 38, 4, 225), outline=(214, 108, 26, 190))
        d.text((SIZE // 2 - sw / 2, by + 3), small, font=sf,
               fill=(255, 238, 208, 255))

        col, row = i % cols, i // cols
        gx, gy = col * CELL, row * (CELL + LABEL)
        grid.paste(c.convert("RGB"), (gx + (CELL - SIZE) // 2, gy + 6))
        gd.rectangle([gx, gy + CELL, gx + CELL, gy + CELL + LABEL],
                     fill=(14, 32, 58))
        tw = gd.textlength(label, font=P.font(12, True))
        gd.text((gx + (CELL - tw) / 2, gy + CELL + 16), label,
                font=P.font(12, True), fill=P.TEXT)
    grid.save(OUT)
    print(f"已输出：{OUT}  {grid.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())