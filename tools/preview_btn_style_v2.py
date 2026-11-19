#!/usr/bin/env python
"""研究「启动」字样怎样才符合原版观感。

问题背景
--------
用户两次说圆形按钮上的字样「违和」。回顾走过的路：

1. 深棕实心 + 细白描边、字号 40 → 发脏、像贴上去的
2. 白→金渐变 + 深棕粗描边 + 橙色外发光 → 糊
3. 纯白 + 细描边，两行 → 干净但偏「UI 控件」，不像游戏按钮
4. E 方案：大字 + START GAME 横条，底色改银白金属渐变 → 仍违和

**违和的根因（这次想清楚了）**：
游戏 UI 的按钮字通常是**扁平、无描边、单一亮色**，靠底色对比，
而不是 Web UI 那种「带描边 + 金属渐变 + 阴影」的立体处理。
我一直在往「精致」方向堆效果，反而离原版越来越远。

三个符合原版的方向
------------------
A  **纯白无描边**（最接近游戏 UI）：白色粗体直接压在橙盘上，
   只加一层极淡的暖色投影保证在亮处也可读。
B  **白色 + 极细深棕描边 1px**：比 A 稍稳，小尺寸下不糊。
C  **白字 + 下方细分隔线 + 小英文**：还原官网「开始 / START GAME」
   的上下结构，但两点都不加粗描边。

输出 ``assets/ui/mecha/v2/_btn_style_v2.png``。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "ui" / "mecha" / "v2"
OUT = ASSETS / "_btn_style_v2.png"

sys.path.insert(0, str(ROOT / "tools"))
import preview_mecha_pages as P  # noqa: E402

SIZE = 300
CELL = 340
LABEL = 44
YAHEI_B = r"C:\Windows\Fonts\msyhbd.ttc"
HEI = r"C:\Windows\Fonts\simhei.ttf"


def blank() -> Image.Image:
    c = Image.new("RGBA", (SIZE, SIZE), (10, 22, 42, 255))
    rb = P.scaled(ASSETS / "round.png", SIZE)
    P.paste_center(c, rb, SIZE // 2, SIZE // 2)
    return c


def draw(
    text: str,
    fpath: str,
    fsize: int,
    *,
    color=(255, 255, 255),
    stroke: tuple | None = None,
    stroke_w: int = 0,
    soft_shadow: bool = True,
    y_off: int = 0,
    en: str = "",
    en_y: int | None = None,
    divider: bool = False,
) -> Image.Image:
    c = blank()
    f = ImageFont.truetype(fpath, fsize)
    d = ImageDraw.Draw(c)
    tw = d.textlength(text, font=f)
    x = SIZE / 2 - tw / 2
    y = SIZE / 2 - fsize * 0.62 + y_off

    # 极淡暖色投影：只为在亮橙蜂窝上保住可读性，不做立体感
    if soft_shadow:
        sh = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((x, y + 1), text, font=f,
                                fill=(120, 50, 0, 90))
        sh = sh.filter(ImageFilter.GaussianBlur(2.4))
        c.alpha_composite(sh)

    if stroke and stroke_w:
        d = ImageDraw.Draw(c)
        d.text((x, y), text, font=f, fill=stroke,
               stroke_width=stroke_w, stroke_fill=stroke)

    d = ImageDraw.Draw(c)
    d.text((x, y), text, font=f, fill=color)

    if divider:
        d = ImageDraw.Draw(c)
        dy = y + fsize + 6
        d.line([(SIZE / 2 - 34, dy), (SIZE / 2 + 34, dy)],
               fill=(255, 255, 255, 150), width=2)

    if en:
        d = ImageDraw.Draw(c)
        ef = ImageFont.truetype(YAHEI_B, 10)
        ew = d.textlength(en, font=ef)
        ey = en_y if en_y is not None else y + fsize + (14 if divider else 6)
        d.text((SIZE / 2 - ew / 2, ey), en, font=ef,
               fill=(255, 255, 255, 225))
    return c


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    cases = [
        ("A 纯白 · 无描边（最像游戏UI）",
         draw("启动", YAHEI_B, 62, soft_shadow=True)),
        ("B 纯白 · 1px 深棕描边",
         draw("启动", YAHEI_B, 62, stroke=(120, 45, 0, 200), stroke_w=1)),
        ("C 白字 + 分隔线 + 小英文",
         draw("启动", YAHEI_B, 54, en="START GAME", divider=True, y_off=-8)),
        ("D 黑体 · 纯白无描边",
         draw("启动", HEI, 60, soft_shadow=True)),
        ("E 纯白 · 字号70（更醒目）",
         draw("启动", YAHEI_B, 70, soft_shadow=True)),
        ("F 白字 + 小英文（无分隔线）",
         draw("启动", YAHEI_B, 56, en="START GAME", y_off=-8)),
    ]

    grid = Image.new("RGB", (CELL * 3, (CELL + LABEL) * 2), P.BG)
    gd = ImageDraw.Draw(grid)
    for i, (label, im) in enumerate(cases):
        col, row = i % 3, i // 3
        gx, gy = col * CELL, row * (CELL + LABEL)
        grid.paste(im.convert("RGB"), (gx + (CELL - SIZE) // 2, gy + 6))
        gd.rectangle([gx, gy + CELL, gx + CELL, gy + CELL + LABEL],
                     fill=(14, 32, 58))
        tw = gd.textlength(label, font=P.font(12, True))
        gd.text((gx + (CELL - tw) / 2, gy + CELL + 14), label,
                font=P.font(12, True), fill=P.TEXT)
    grid.save(OUT)
    print(f"已输出：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())