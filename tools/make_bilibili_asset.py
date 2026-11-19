#!/usr/bin/env python
"""把用户给的 bilibili 粉色字标抠成透明 PNG，并做高清化重建。

原图问题：790x471 的缩略图，白底 + JPEG/压缩噪点 + 粉色边缘发虚。
如果直接缩到 22px 用，字会糊成一团粉色。

做法：
  1. 按颜色距离判定「粉色」，白底转 alpha=0
  2. 边缘按「粉色程度」给部分 alpha（抗锯齿，去掉白边）
  3. **矢量式重建**：把 alpha 通道放大 4 倍后用 Lanczos 重采样，
     再按原色重新上色 —— 相当于把位图边缘重新锐化成干净的曲线
  4. 输出 512px 宽的母版，实际使用时再缩到目标尺寸

输出 ``assets/ui/mecha/v2/bilibili.png``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
#: 源图自备，不入库。默认放 tools/mecha-src/bilibili.png，
#: 或用环境变量 ``JJXF_BILIBILI_SRC`` 指定。
_src_env = os.environ.get("JJXF_BILIBILI_SRC", "").strip()
SRC = Path(_src_env) if _src_env else ROOT / "tools" / "mecha-src" / "bilibili.png"
OUT = ROOT / "assets" / "ui" / "mecha" / "v2" / "bilibili.png"

# 原图实测中位色
PINK = np.array([247, 129, 167], dtype=np.float32)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if not SRC.is_file():
        print(f"找不到源图：{SRC}")
        return 2

    rgb = np.array(Image.open(SRC).convert("RGB")).astype(np.float32)
    h, w = rgb.shape[:2]

    # --- 1 计算「有多粉」：到主粉色的距离，越近 alpha 越高
    dist = np.sqrt(((rgb - PINK) ** 2).sum(axis=2))
    # 主色附近 dist 很小；白色 (255,255,255) 到 PINK 的距离约 148
    alpha = np.clip((135.0 - dist) / 60.0, 0.0, 1.0)
    # 压掉低置信的压缩噪点（距离 > 100 的基本是白底或灰边）
    alpha[dist > 105] = 0.0
    alpha = (alpha ** 1.15) * 255.0

    # --- 2 裁到有效范围，四周留 8px
    ys, xs = np.where(alpha > 12)
    x0, x1 = max(0, xs.min() - 8), min(w, xs.max() + 9)
    y0, y1 = max(0, ys.min() - 8), min(h, ys.max() + 9)
    alpha = alpha[y0:y1, x0:x1]

    # --- 3 重建：alpha 放大 4 倍 → 轻微模糊去台阶 → 再缩回目标宽度
    # 这样边缘从「压缩噪点的锯齿」变成「干净的亚像素曲线」
    a_img = Image.fromarray(alpha.astype(np.uint8), "L")
    big = a_img.resize((a_img.width * 4, a_img.height * 4), Image.LANCZOS)
    big = big.filter(ImageFilter.GaussianBlur(0.8))
    # 锐化对比：让实心部分更实、边缘更干净
    arr = np.array(big).astype(np.float32)
    arr = np.clip((arr - 20) * 1.28, 0, 255)
    big = Image.fromarray(arr.astype(np.uint8), "L")

    target_w = 512
    target_h = max(1, int(big.height * target_w / big.width))
    final_a = big.resize((target_w, target_h), Image.LANCZOS)

    # --- 4 用统一粉色重新上色（原图有色偏和压缩色块）
    out = np.zeros((target_h, target_w, 4), np.uint8)
    out[:, :, 0] = int(PINK[0])
    out[:, :, 1] = int(PINK[1])
    out[:, :, 2] = int(PINK[2])
    out[:, :, 3] = np.array(final_a)

    Image.fromarray(out, "RGBA").save(OUT)
    print(f"已输出：{OUT}  {target_w}x{target_h}")
    print(f"有效区域裁剪自原图 ({x0},{y0})-({x1},{y1})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())