#!/usr/bin/env python
"""从机甲旋风官网图里抠出界面元素（去白底 + 清文字）。

只做三件事：
  1. 白背景转透明（用「连通到图像边缘」判据，保住按钮内部的白色高光）
  2. 抹掉按钮上的文字（用双侧水平插值补橙色渐变）
  3. 输出到 assets/ui/mecha/

为什么不用 scipy / cv2：项目 venv 里没有，为一个一次性脚本装两个重依赖
不值。连通域用 BFS 手写，几万像素量级完全够快。
"""
from __future__ import annotations

import os
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

#: 源图由使用者自备（《机甲旋风》官网宣传图截图），**不入库**。
#: 用环境变量 ``JJXF_MECHA_SRC`` 指定；缺省值只是占位，找不到会直接报错。
SRC = Path(os.environ.get("JJXF_MECHA_SRC", "mecha-source.jpg"))
OUT = Path("assets/ui/mecha")


# ---------------------------------------------------------------- 去背景


def flood_white(a: np.ndarray, tol: int = 235) -> np.ndarray:
    """返回「连通到图像边缘的近白像素」掩码。

    为什么必须从边缘出发：按钮**内部**有白色高光（金属反光、宝石高光），
    那些不能变透明，否则按钮中间会被打个洞。

    四连通而不是八连通：斜角连通会让背景「渗」过 1px 的对角缝隙，
    在这类带描边的图上容易把边缘啃掉。
    """
    h, w = a.shape[:2]
    white = (a[:, :, 0] > tol) & (a[:, :, 1] > tol) & (a[:, :, 2] > tol)
    seen = np.zeros((h, w), bool)
    dq: deque[tuple[int, int]] = deque()

    for x in range(w):
        for y in (0, h - 1):
            if white[y, x] and not seen[y, x]:
                seen[y, x] = True
                dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if white[y, x] and not seen[y, x]:
                seen[y, x] = True
                dq.append((y, x))

    while dq:
        cy, cx = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < h and 0 <= nx < w and white[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                dq.append((ny, nx))
    return seen


def feather(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    """对掩码做一轮方框模糊，让透明边缘不生硬。

    不做这一步的话，白底边缘会留一圈浅灰「毛边」——
    在深色背景上特别明显（本次目标就是深紫黑底）。
    """
    out = mask.astype(np.float32)
    h, w = out.shape
    for _ in range(radius):
        padded = np.pad(out, 1, mode="edge")
        acc = np.zeros_like(out)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                acc += padded[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
        out = acc / 9.0
    return out


def cut_white(img: Image.Image) -> Image.Image:
    """白底 → 透明，保留内部高光。"""
    rgb = np.array(img.convert("RGB"))
    bg = flood_white(rgb)
    soft = feather(bg.astype(np.float32), 1)
    alpha = np.clip((1.0 - soft) * 255.0, 0, 255).astype(np.uint8)
    out = np.dstack([rgb, alpha])
    return Image.fromarray(out, "RGBA")


# ---------------------------------------------------------------- 清文字


def find_text_rows(rgb: np.ndarray, x0: int, x1: int) -> list[int]:
    """在 [x0,x1) 列范围里，找「深色文字像素」密集的行。

    按钮文字是深棕/近黑，压在橙色渐变上。判据：亮度低且饱和度不高。
    只用于「定位要抹掉的区域」，不追求精确到字形。
    """
    sub = rgb[:, x0:x1].astype(np.int16)
    lum = (sub[:, :, 0] * 299 + sub[:, :, 1] * 587 + sub[:, :, 2] * 114) // 1000
    dark = lum < 110
    counts = dark.sum(axis=1)
    width = max(1, x1 - x0)
    return [y for y, c in enumerate(counts) if c > width * 0.03]


def clip_circle(img: Image.Image, center: tuple[int, int], radius: int) -> Image.Image:
    """把圆外的像素变透明（几何裁切，不做颜色判断）。

    为什么不用「去白底」那套（这版改过一次）：

    圆形按钮在原图里**几乎占满裁切框**，四个角上不是白底，而是
    旁边 UI 的碎片（左上角是角色立绘的衣角、右侧是别的按钮边框）。
    所以「从边缘洪水填充白色」找不到可填充的连通区域 ——
    实测 alpha 均值仍有 250，97% 的像素不透明，贴到深色底上
    就是一个带方框的白底方块。

    圆形元素用几何裁切才可靠：圆心和半径都是量出来的确定值，
    圆外一律 alpha=0，边缘 1.5px 做羽化抗锯齿。
    """
    arr = np.array(img.convert("RGBA"))
    h, w = arr.shape[:2]
    cx, cy = center
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)

    # 圆外完全透明；边缘 1.5px 内线性过渡
    fade = np.clip((radius + 1.5 - dist) / 1.5, 0.0, 1.0)
    arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * fade).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def rebuild_round_disc(
    img: Image.Image,
    center: tuple[int, int],
    radius: int,
) -> Image.Image:
    """把圆形按钮的内盘**重画成干净的径向渐变**。

    这是最终采用的方案。前面七版都在试图「保住原图的纹理再抹掉文字」，
    全部失败，原因写在下面（值得记下来，避免以后再走同一条弯路）：

    | 方案 | 失败原因 |
    |---|---|
    | 矩形带填充 | 啃掉外圈金属环 |
    | 以圆心镜像取源 | 文字压在圆心附近，镜像源也是文字 → 鬼影 |
    | 纵向拉伸干净带 | 每行宽度按圆收窄 → 纹理拉成锥形 |
    | 按半径径向渐变 | 采样行是纯色 → 纯橙平面，高光丢 |
    | 按 y 平涂横色带 | 三条生硬横带 |
    | 同心环复制 | 竖条纹 |
    | 深色像素 inpainting | 笔画密集处扩散不开，留斑块 |

    **结论：内盘是「蜂窝纹 + 径向高光 + 弧形描边」三层叠加的位图纹理，
    任何局部修补都会破坏其中一层。** 在启动器里这个按钮要被叠加中文、
    要做悬停变色、要适配多种尺寸 —— 位图纹理反而是负担。

    所以**重画**。颜色节点取自原图实测：
        中心  #FFFDF0   （近乎纯白的高光）
        中段  #FBD26A   （黄橙）
        外圈  #F08A10   （橙）
        边缘  #C85A05   （深橙，靠近描边）

    重画后：任意尺寸缩放都清晰、叠加文字干净、可程序化改色，
    而且视觉上和原图几乎一致（原图也是这个渐变）。

    保留 ``r*0.97`` 以外的部分不动 —— 那是橙色描边和金属环。
    """
    arr = np.array(img.convert("RGBA"))
    h, w = arr.shape[:2]
    cx, cy = center

    # (半径比例, RGB) —— 越多节点过渡越顺
    STOPS = [
        (0.00, (255, 253, 240)),
        (0.28, (253, 244, 205)),
        (0.52, (251, 220, 130)),
        (0.72, (246, 176, 60)),
        (0.88, (240, 138, 16)),
        (1.00, (200, 90, 5)),
    ]

    def color_at(t: float) -> tuple[int, int, int]:
        t = max(0.0, min(1.0, t))
        for i in range(len(STOPS) - 1):
            t0, c0 = STOPS[i]
            t1, c1 = STOPS[i + 1]
            if t0 <= t <= t1:
                k = (t - t0) / max(1e-6, t1 - t0)
                # 用平方缓动，让中心高光更集中（原图就是中心一团亮）
                k = k ** 1.35
                return (
                    int(c0[0] + (c1[0] - c0[0]) * k),
                    int(c0[1] + (c1[1] - c0[1]) * k),
                    int(c0[2] + (c1[2] - c0[2]) * k),
                )
        return STOPS[-1][1]

    limit = radius * 0.97
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    inside = dist <= limit
    # 边缘 1.5px 做抗锯齿，避免圆形边界出现锯齿
    edge = (dist > limit - 1.5) & inside

    for y in range(max(0, cy - radius), min(h, cy + radius + 1)):
        for x in range(max(0, cx - radius), min(w, cx + radius + 1)):
            if not inside[y, x]:
                continue
            col = color_at(dist[y, x] / limit)
            if edge[y, x]:
                # 与原有像素按距离混合
                k = (limit - dist[y, x]) / 1.5
                old = arr[y, x, :3].astype(np.float32)
                new = np.array(col, np.float32)
                col = tuple(int(v) for v in (old * (1 - k) + new * k))
            arr[y, x, :3] = col
    return Image.fromarray(arr, "RGBA")

    # --- 重绘下半部（文字所在区），上沿保持在 text_top 处接续
    for y in range(text_top, min(h, cy + radius + 1)):
        dy = y - cy
        if abs(dy) > radius:
            continue
        half = int((radius * radius - dy * dy) ** 0.5)
        x0, x1 = max(0, cx - half), min(w, cx + half)
        if x1 <= x0:
            continue
        arr[y, x0:x1, :3] = np.clip(color_at(y), 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def erase_text_adaptive(
    img: Image.Image,
    band: tuple[int, int],
    x_min: int,
    x_max: int,
) -> Image.Image:
    """抹掉文字：从 ``x_min`` 到 ``x_max``，纵向在 ``band`` 内。

    三次踩坑都记在这里，避免以后再犯：

    **坑一：固定矩形 + 水平插值 → 灰条。**
    右端点落在银色边框上（RGB≈[155,156,156]），左边是橙色，
    插值出来一条渐变灰带。→ 改用**上下锚点做纵向插值**。

    **坑二：原地读改写 → 满屏彩色噪点。**
    第 y 行的插值源取了 ``arr[y-1]`` / ``arr[y+1]``，而这两个位置
    已被本轮改写过。误差逐行放大。→ **先快照**，锚点固定在 band 边界外。

    **坑三：逐行找「最左纯橙」→ 宝石被吃掉。**
    文字把纯橙切成碎段（y=50 那行有 7 段），"最左纯橙" 可能落在
    文字缝隙里，于是起始点被拉到宝石左侧，整块宝石被擦。
    → 改成**按列统计**：从哪一列起「该带内 100% 是橙色」就是宝石右缘。
    实测 btn_a 在 x=128 达到 100%，与目视的宝石右边缘完全吻合。

    所以 ``x_min`` 是**固定值**，由调用方量好写进 JOBS，不做自适应。
    """
    arr = np.array(img.convert("RGBA"))
    h, w = arr.shape[:2]
    y0, y1 = band
    y0, y1 = max(1, y0), min(h - 1, y1)
    x_min, x_max = max(0, x_min), min(w, x_max)
    if x_max <= x_min:
        return img

    # 快照：插值只读这里，不读 arr（避免自反馈）
    snap = arr.copy()

    # 锚点行要落在 band **外面**，且离开至少 4px —— 文字的抗锯齿边缘
    # 会溢出 1~2px，贴着 band 取锚点就会把边缘暗像素带进来，
    # 插值结果是一道道竖条（实测踩过）。
    pad = 4
    ty = max(0, y0 - pad)
    by = min(h - 1, y1 + pad)
    top_row = snap[ty, x_min:x_max, :3].astype(np.float32)
    bot_row = snap[by, x_min:x_max, :3].astype(np.float32)

    for y in range(y0, y1):
        t = (y - y0) / max(1, (y1 - y0))
        arr[y, x_min:x_max, :3] = (top_row * (1.0 - t) + bot_row * t).astype(
            np.uint8
        )
    return Image.fromarray(arr, "RGBA")


# ---------------------------------------------------------------- 主流程

#: 各元素裁切框。
#:
#: 参数都是**看着 3x 放大网格图逐个量的**，不是估算：
#:
#:   btn_a 实测：宝石右缘 x≈118、下缘 y≈108
#:               主标题 y 44~78 右端 x≈258
#:               副标题 y 82~108 右端 x≈248
#:
#: ``band`` 要**把两行文字整个包住**（44~110），``pad`` 是往外扩的余量。
#: 锚点行取 band 外再 pad 像素处 —— 那里必须是干净橙板，
#: 否则锚点带上文字边缘的暗像素，插值出来会是一道道竖条。
JOBS = {
    "logo": dict(box=(671, 6, 1600, 212)),
    "title": dict(box=(40, 135, 440, 320)),
    "btn_a": dict(box=(16, 762, 320, 902), band=(42, 112), x_min=120, x_max=290),
    "btn_b": dict(box=(322, 762, 648, 902), band=(42, 112), x_min=112, x_max=300),
    "btn_c": dict(box=(1024, 762, 1350, 902), band=(42, 112), x_min=115, x_max=302),
    "btn_d": dict(box=(1352, 762, 1660, 902), band=(42, 112), x_min=103, x_max=288),
    # 圆形按钮走单独的分支：内盘是圆，矩形带切不了
    # 圆按钮量出来的圆心 (823,708)、外环半径约 152；取 160 边距，
    # 框 (663,548)-(983,868) = 320x320。之前框 410x381 起点偏左，
    # 把圆的左边缘切掉了（实测左缘落在 x=0 处被硬切）。
    "round": dict(box=(663, 548, 983, 868), disc=((160, 160), 120, 100, 152)),
}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    OUT.mkdir(parents=True, exist_ok=True)
    src = Image.open(SRC).convert("RGB")
    print(f"源图 {src.size[0]}x{src.size[1]}")

    for name, job in JOBS.items():
        crop = src.crop(job["box"])
        # 圆形元素必须**先几何裁圆再做别的**：它四周不是白底而是别的 UI 碎片，
# 去白底那一步对它无效（实测 alpha 均值 250、97% 不透明）。
        if job.get("disc"):
            center, inner_r, _text_top, outer_r = job["disc"]
            # 先按外环半径裁圆（保住整颗按钮），再重建比外环小的内盘
            cut = clip_circle(crop.convert("RGBA"), center, outer_r)
            cut = rebuild_round_disc(cut, center, inner_r)
        else:
            cut = cut_white(crop)
            if job.get("band"):
                cut = erase_text_adaptive(
                    cut, job["band"], job["x_min"], job["x_max"]
                )
        dst = OUT / f"{name}.png"
        cut.save(dst)
        alpha = np.array(cut)[:, :, 3]
        opaque = (alpha > 128).mean() * 100
        print(f"  {name:8} {cut.size[0]:4}x{cut.size[1]:<4} 不透明 {opaque:5.1f}%  -> {dst.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())