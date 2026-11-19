#!/usr/bin/env python
"""「扩展与状态」面板可隐藏 / 可召唤 —— 两态对比示意图。

需求（2026-11-08 用户提议）
--------------------------
截图里红框只框住右侧那块**科技蓝「扩展与状态」面板**，不含下方
「当前配置」卡片。用户希望这块可以隐藏，留一个按钮召唤出来，
这样机甲立绘不被挡住。

设计要点
--------
* **红框里的面板 + 它下方的「当前配置」卡片一起隐藏**
  （用户追加要求：「他下面的也做隐藏吧」）。隐藏后画面里
  只剩圆形启动按钮和一条边缘竖标签，机甲立绘完整展现。
* 召唤入口**放在右下角底部状态条上方的空白**（用户 11-09 指定位置），
  做成横向胶囊；原先贴右边缘中部的竖标签会挡住立绘，已弃用。
* 胶囊上带状态小圆点（绿=全部正常 / 琥珀=有异常）+ 扩展数，
  这样即使面板藏起来，玩家也能一眼知道有没有问题。
* 两态都保留左上「简洁模式」的暗示：展开态按钮写「收起」，
  隐藏态写「状态」+ 数字徽章。

用法::

    python tools/preview_mecha_toggle.py

输出 ``assets/ui/mecha/v2/_toggle_states.png``（左右对比）+ 两张单页。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "ui" / "mecha" / "v2"
OUT = ASSETS / "_toggle_states.png"

sys.path.insert(0, str(ROOT / "tools"))
# 复用多页生成器里的外壳与页面函数，避免第二套配色/布局漂移
import preview_mecha_pages as P  # noqa: E402


def draw_toggle_tab(base: Image.Image, *, expanded: bool):
    """状态显示 / 召唤入口。

    2026-11-09 用户反馈：原来**贴右边缘中部的竖标签**挡住了立绘，
    要求移到右下角红框标出的位置（底部状态条上方那片空白），
    改成横向胶囊。这样右侧从 LOGO 到立绘完全无遮挡。

    * 展开态：面板右上角一个 30x24 的「›」收起按钮
    * 隐藏态：右下角横向胶囊，含状态点 + 扩展数 + 体检结论 + 展开箭头
    """
    d = ImageDraw.Draw(base, "RGBA")
    W, H = P.W, P.H

    if expanded:
        bx, by = 690 + 452 - 40, 208 + 8
        P.rrect(d, (bx, by, bx + 30, by + 24), 6,
                fill=(8, 26, 50, 235), outline=(0, 190, 255, 220))
        d.text((bx + 9, by + 3), "›", font=P.font(17, True), fill=P.TEXT)
    else:
        # 隐藏态：右下角横向胶囊。
        # 位置 x 944~1154 / y 692~722 —— 导航按钮最右到 x=722，
        # 底部状态条在 y=734，这块正好空着，不挡任何东西。
        cw, ch = 210, 30
        cx = W - 26 - cw
        cy = 692
        P.rrect(d, (cx, cy, cx + cw, cy + ch), ch // 2,
                fill=(8, 26, 50, 238), outline=(0, 190, 255, 210))
        d = ImageDraw.Draw(base, "RGBA")

        # 状态点（绿 = 全部正常 / 琥珀 = 有异常）
        d.ellipse([cx + 13, cy + 11, cx + 21, cy + 19], fill=(*P.GREEN, 255))
        # 一行结论文字
        d.text((cx + 29, cy + 8), "25 扩展 · 全部正常",
               font=P.font(11), fill=P.TEXT_DIM)
        # 右侧展开箭头
        d.text((cx + cw - 22, cy + 5), "‹",
               font=P.font(15, True), fill=P.CYAN)


def build_game_page(*, expanded: bool) -> Image.Image:
    """生成游戏库页，可选是否显示「扩展与状态」面板。"""
    c = P.draw_shell("game")

    if expanded:
        # 完整页面（含面板 + 当前配置）
        c = P.page_game(c)
    else:
        # 隐藏态：只画圆形启动按钮，面板和当前配置卡片都不画。
        # 右下角完全留给背景立绘 —— 这正是用户要的效果。
        btn_cx, btn_cy = 180, 400
        P.draw_launch_button(c, btn_cx, btn_cy, 244)
        d = ImageDraw.Draw(c, "RGBA")
        d.text((btn_cx - 30, btn_cy + 132), "尚未启动",
               font=P.font(11), fill=P.TEXT_DIM)

    draw_toggle_tab(c, expanded=expanded)
    return c.convert("RGB")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    shown = build_game_page(expanded=True)
    hidden = build_game_page(expanded=False)
    shown.save(ASSETS / "_panel_shown.png")
    hidden.save(ASSETS / "_panel_hidden.png")

    # 左右对比图，顶部加标题条说明两种状态。
    #
    # 画布高度必须是 ``bar + sh`` 而不是 ``sh*2 + bar*2`` ——
    # 两态是**左右并排**，不是上下堆叠。第一版写成了 2x2 的高度，
    # 结果下半部分全空（实测一眼就能看出）。
    sw, sh = P.W // 2, P.H // 2
    bar = 34
    grid = Image.new("RGB", (sw * 2, bar + sh), P.BG)
    d = ImageDraw.Draw(grid)
    labels = ["展开态 · 面板可见（当前设计）", "隐藏态 · 只留边缘竖标签（新提议）"]
    for i, (im, label) in enumerate([(shown, labels[0]), (hidden, labels[1])]):
        x = i * sw
        d.text((x + 12, 9), label, font=P.font(15, True), fill=P.TEXT)
        small = im.resize((sw, sh), Image.LANCZOS)
        grid.paste(small, (x, bar))
    grid.save(OUT)
    print(f"已输出：{OUT}")
    print(f"  {ASSETS / '_panel_shown.png'}")
    print(f"  {ASSETS / '_panel_hidden.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())