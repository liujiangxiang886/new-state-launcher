#!/usr/bin/env python
"""处理用户提供的 5 张高清素材，去掉白底，输出到 assets/ui/mecha/v2/。

与 ``extract_mecha_ui.py`` 的区别：

* 那版是从**官网截图**里连抠带清文字，素材本身带噪点、按钮上有字；
* 这版是用户给的高清成品图，**文字已经清空**，只需要去白底。

去白底用「连通到边缘的近白区域」判据，不用简单的颜色阈值：
圆形按钮中心有一团**近白的高光**，全局阈值会把它一起打透明，
按钮中间就破个洞。洪水填充只认连通到四边的白，内部高光安全。

用法::

    python tools/extract_mecha_v2.py
"""
from __future__ import annotations

import os
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "ui" / "mecha" / "v2"

#: 源图由使用者自备，**不入库**。默认放到 tools/mecha-src/ 下，
#: 文件名与下表一致；也可用环境变量 ``JJXF_MECHA_SRC_DIR`` 指定其他目录。
_src_dir = os.environ.get("JJXF_MECHA_SRC_DIR", "").strip()
SRC_DIR = Path(_src_dir) if _src_dir else ROOT / "tools" / "mecha-src"

SOURCES = [
    ("logo", SRC_DIR / "logo.jpg"),
    ("btn_bar", SRC_DIR / "btn_bar.jpg"),
    ("round", SRC_DIR / "round.jpg"),
    ("panel", SRC_DIR / "panel.jpg"),
    # 背景图不处理，原样复制
    ("background", SRC_DIR / "background.jpg"),
]


def flood_white(a: np.ndarray, tol: int = 228) -> np.ndarray:
    """连通到图像四边的近白像素。四连通，避免斜角渗透啃掉边缘。"""
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


def feather(mask: np.ndarray) -> np.ndarray:
    """3x3 方框模糊，让 alpha 边缘抗锯齿。"""
    out = mask.astype(np.float32)
    h, w = out.shape
    padded = np.pad(out, 1, mode="edge")
    acc = np.zeros_like(out)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            acc += padded[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
    return acc / 9.0


def dewhite(img: Image.Image) -> Image.Image:
    """白底转透明，同时把边缘的浅灰溢色按比例半透明化。

    JPEG 压缩会让白底与主体之间留 1~2px 的浅灰过渡。只做洪水填充
    会在主体轮廓外挂一圈白边（深色底上特别明显）。这里对**紧贴
    背景的非纯白像素**按「离白色多远」给部分 alpha，把白边吃掉。
    """
    rgb = np.array(img.convert("RGB"))
    bg = flood_white(rgb)
    alpha = (1.0 - feather(bg)) * 255.0

    # 只处理「外部白区边界附近」的像素，且颜色确实偏白
    near_bg = feather(bg) > 0.01
    whiteness = rgb.min(axis=2).astype(np.float32)
    pale = np.clip((250.0 - whiteness) / 45.0, 0.0, 1.0)
    soften = near_bg & (whiteness > 205)
    alpha[soften] = np.minimum(alpha[soften], pale[soften] * 255.0)

    out = np.dstack([rgb, np.clip(alpha, 0, 255).astype(np.uint8)])
    return Image.fromarray(out, "RGBA")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    OUT.mkdir(parents=True, exist_ok=True)
    for name, path in SOURCES:
        p = Path(path)
        if not p.is_file():
            print(f"  [跳过] {name}: 找不到 {p}")
            continue
        img = Image.open(p).convert("RGB")
        if name == "background":
            result = img.copy()
            dst = OUT / f"{name}.jpg"
            result.save(dst, quality=92)
        else:
            result = dewhite(img)
            dst = OUT / f"{name}.png"
            result.save(dst)
        a = np.array(result)
        if a.ndim == 3 and a.shape[2] == 4:
            transparent = (a[:, :, 3] < 8).mean() * 100
            print(f"  {name:12} {result.size[0]:5}x{result.size[1]:<5} 透明 {transparent:5.1f}%  -> {dst.name}")
        else:
            print(f"  {name:12} {result.size[0]:5}x{result.size[1]:<5}  -> {dst.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())