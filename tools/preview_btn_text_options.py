#!/usr/bin/env python
"""圆形启动按钮文字样式候选对比。

用户两次反馈「启动游戏四个字不好看」：
  第一版  深棕实心 + 细白描边、字号 40 撑满内盘 → 发脏、闷
  第二版  白→金渐变 + 深棕 3px 描边 + 橙外发光、字号 32 → 仍不满意

「好看」主观，与其继续猜，不如一次给几个方向让用户选。
四个候选共用同一个按钮素材，只改文字：

  A  纯白 + 细描边，两行        —— 最干净，参考官网原版
  B  金属银渐变 + 冷灰描边      —— 呼应外圈银色机甲环
  C  一行横排「启动游戏」        —— 打破两行的呆板感
  D  白字 + 橙红外发光，无粗描边 —— 像按钮自己在发光
  E  压缩字距 + 上大字下小英文   —— 官网「开始 / START GAME」结构

输出 ``assets/ui/mecha/v2/_btn_text_options.png``。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "ui" / "mecha" / "v2"
OUT = ASSETS / "_btn_text_options.png"

sys.path.insert(0, str(ROOT / "tools"))
import preview_mecha_pages as P  # noqa: E402

SIZE = 300          # 每格按钮直径
CELL = 340
LABEL_H = 46
COLS, ROWS = 3, 2

STROKE_DARK = (96, 38, 0, 255)
STROKE_COOL = (28, 44, 70, 255)


def blank(size: int) -> Image.Image:
    """深蓝底 + 按钮素材。"""
    c = Image.new("RGBA", (size, size), (10, 22, 42, 255))
    rb = P.scaled(ASSETS / "round.png", size)
    P.paste_center(c, rb, size // 2, size // 2)
    return c


def text_layer(
    base: Image.Image,
    lines: list[tuple[str, int, tuple[int, int, int] | None]],
    *,
    line_h: int,
    top_offset: int = 0,
    fill_top=(255, 255, 255),
    fill_bottom=(255, 255, 255),
    stroke: tuple[int, int, int, int] = STROKE_DARK,
    stroke_w: int = 2,
    glow: tuple[int, int, int, int] | None = None,
    glow_w: int = 5,
    shadow: bool = True,
    letter_spacing: int = 0,
):
    """通用文字绘制。

    ``lines``: [(文本, 字号, 颜色覆盖 or None), ...]
    颜色为 None 时用 ``fill_top→fill_bottom`` 的纵向渐变。
    """
    size = base.width
    cx = cy = size // 2
    total_h = line_h * len(lines)
    top = cy - total_h // 2 + top_offset

    d_probe = ImageDraw.Draw(base)

    for i, (text, fsize, solid) in enumerate(lines):
        f = P.font(fsize, True)
        if letter_spacing:
            tw = sum(d_probe.textlength(ch, font=f) for ch in text)
            tw += letter_spacing * max(0, len(text) - 1)
        else:
            tw = d_probe.textlength(text, font=f)
        x = cx - tw / 2
        y = top + i * line_h

        m = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(m)
        if letter_spacing:
            cur = x
            for ch in text:
                md.text((cur, y), ch, font=f, fill=255)
                cur += d_probe.textlength(ch, font=f) + letter_spacing
        else:
            md.text((x, y), text, font=f, fill=255)

        # 下沉阴影
        if shadow:
            sh = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            sd = ImageDraw.Draw(sh)
            sd.text((x + 1, y + 2), text, font=f, fill=(70, 26, 0, 130))
            sh = sh.filter(ImageFilter.GaussianBlur(1.8))
            sh.putalpha(Image.fromarray(
                (np.array(sh)[:, :, 3].astype(np.float32) * 0.6).astype(np.uint8)
            ))
            base.alpha_composite(sh)

        # 外发光（可选）
        if glow:
            gl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            gd = ImageDraw.Draw(gl)
            gd.text((x, y), text, font=f, fill=(0, 0, 0, 0),
                    stroke_width=glow_w, stroke_fill=glow)
            gl = gl.filter(ImageFilter.GaussianBlur(3.2))
            base.alpha_composite(gl)

        # 描边
        st = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        sd2 = ImageDraw.Draw(st)
        sd2.text((x, y), text, font=f, fill=stroke,
                 stroke_width=stroke_w, stroke_fill=stroke)
        base.alpha_composite(st)

        # 字身：纯色或纵向渐变
        body = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        bd = ImageDraw.Draw(body)
        y0 = max(0, int(y) - 2)
        y1 = min(size, int(y) + fsize + 8)
        for gy in range(y0, y1):
            t = (gy - y0) / max(1, (y1 - y0) - 1)
            if solid:
                col = (*solid, 255)
            else:
                col = (
                    int(fill_top[0] + (fill_bottom[0] - fill_top[0]) * t),
                    int(fill_top[1] + (fill_bottom[1] - fill_top[1]) * t),
                    int(fill_top[2] + (fill_bottom[2] - fill_top[2]) * t),
                    255,
                )
            bd.line([(0, gy), (size, gy)], fill=col)
        body.putalpha(m)
        base.alpha_composite(body)
    return base


def option_a() -> Image.Image:
    """纯白 + 细描边，两行。最干净，接近官网原版。"""
    c = blank(SIZE)
    return text_layer(
        c, [("启动", 33, (255, 253, 248)), ("游戏", 33, (255, 253, 248))],
        line_h=40, stroke=STROKE_DARK, stroke_w=2, shadow=True,
    )


def option_b() -> Image.Image:
    """金属银渐变 + 冷灰描边，呼应外圈银色机甲环。"""
    c = blank(SIZE)
    return text_layer(
        c, [("启动", 33, None), ("游戏", 33, None)],
        line_h=40,
        fill_top=(255, 255, 255), fill_bottom=(186, 205, 224),
        stroke=STROKE_COOL, stroke_w=2, shadow=True,
    )


def option_c() -> Image.Image:
    """一行横排，打破两行呆板感。"""
    c = blank(SIZE)
    return text_layer(
        c, [("启动游戏", 27, (255, 253, 248))],
        line_h=40, top_offset=0,
        stroke=STROKE_DARK, stroke_w=2, shadow=True, letter_spacing=2,
    )


def option_d() -> Image.Image:
    """白字 + 橙红外发光，去掉粗描边，像按钮自己在发光。"""
    c = blank(SIZE)
    return text_layer(
        c, [("启动", 34, (255, 252, 240)), ("游戏", 34, (255, 252, 240))],
        line_h=42,
        stroke=(150, 58, 0, 210), stroke_w=1,
        glow=(255, 120, 30, 190), glow_w=5, shadow=False,
    )


def option_e() -> Image.Image:
    """官网结构：中文两字放大 + 下方英文横条（**用户最终选定**）。

    与初版候选的区别：候选图里英文是裸的深棕小字压在蜂窝纹上，
    实测发糊。定稿给英文加了深棕圆角底条 + 白色拉字距小字，
    清晰且更还原官网。实际页面由 ``P.draw_launch_button`` 绘制。
    """
    # 传纯底色，让 draw_launch_button 自己贴按钮，避免素材叠两次
    c = Image.new("RGBA", (SIZE, SIZE), (10, 22, 42, 255))
    return P.draw_launch_button(c, SIZE // 2, SIZE // 2, SIZE)


def option_f() -> Image.Image:
    """金橙渐变 + 深棕细描边，比第二版更克制（无外发光）。"""
    c = blank(SIZE)
    return text_layer(
        c, [("启动", 32, None), ("游戏", 32, None)],
        line_h=40,
        fill_top=(255, 250, 225), fill_bottom=(255, 196, 74),
        stroke=STROKE_DARK, stroke_w=2, shadow=True,
    )


OPTIONS = [
    ("A  纯白 + 细描边（官网感）", option_a),
    ("B  金属银渐变（呼应机甲环）", option_b),
    ("C  一行横排「启动游戏」", option_c),
    ("D  白字 + 橙红发光", option_d),
    ("E  「启动」大字 + START GAME", option_e),
    ("F  金橙渐变（克制版）", option_f),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    grid = Image.new("RGB", (CELL * COLS, (CELL + LABEL_H) * ROWS), P.BG)
    d = ImageDraw.Draw(grid)
    for i, (label, fn) in enumerate(OPTIONS):
        col, row = i % COLS, i // COLS
        gx, gy = col * CELL, row * (CELL + LABEL_H)
        im = fn().convert("RGB")
        # 缩到 CELL 内的按钮区域居中
        grid.paste(im, (gx + (CELL - SIZE) // 2, gy + 8))
        d.rectangle([gx, gy + CELL, gx + CELL, gy + CELL + LABEL_H],
                    fill=(14, 32, 58))
        tw = d.textlength(label, font=P.font(13, True))
        d.text((gx + (CELL - tw) / 2, gy + CELL + 14), label,
               font=P.font(13, True), fill=P.TEXT)
    grid.save(OUT)
    print(f"已输出：{OUT}  {grid.size}")
    for i, (label, _) in enumerate(OPTIONS):
        print(f"  {chr(65+i)}. {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())