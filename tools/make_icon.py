"""由 ``app/launcher.png`` 生成多尺寸图标 ``app/launcher.ico``。

**图标源只有一份**：``app/launcher.png``（64×64，透明底）。
exe 内嵌图标、窗口标题栏、任务栏三处都从 ``app/launcher.ico`` 取，
不再各自画一套——之前 exe 是青色旋风、窗口是蓝色三角，两处对不上。

像素画放大不要用平滑插值：64→256 走整数倍最近邻，边缘保持干脆；
缩小时才用 LANCZOS，小尺寸下噪点会被平均掉，反而更清楚。

用法::

    python tools/make_icon.py            # 写 app/launcher.ico
    python tools/make_icon.py --preview  # 另存一张各尺寸并排的检查图
"""

from __future__ import annotations

import argparse
import io
import struct
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "app" / "launcher.png"
DST = ROOT / "app" / "launcher.ico"

#: Windows 会按这几个尺寸取用：16/20/24 标题栏与任务栏，32/40/48 桌面与开始菜单，
#: 64 大图标，96/128/256 资源管理器「超大图标」与属性对话框
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def render(src: Image.Image, size: int) -> Image.Image:
    """渲染指定尺寸。"""
    if size == src.width:
        return src.copy()
    if size < src.width:
        return src.resize((size, size), Image.LANCZOS)
    # 放大：先整数倍最近邻，再落到目标尺寸，保住像素边
    factor = max(1, size // src.width)
    big = src.resize((src.width * factor, src.height * factor), Image.NEAREST)
    if big.width == size:
        return big
    return big.resize((size, size), Image.LANCZOS)


def write_ico(frames: list[tuple[int, Image.Image]], dst: Path) -> int:
    """手写 ICO：Vista 之后各尺寸可以直接存 PNG 数据，体积小且支持 alpha。

    不走 PIL 的 ICO 保存，是因为它内部会把主图重缩一遍，控制不到
    「小尺寸用 LANCZOS、大尺寸用最近邻」这条规则。
    """
    blobs: list[bytes] = []
    for _size, frame in frames:
        buf = io.BytesIO()
        frame.save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())

    n = len(frames)
    header = struct.pack("<HHH", 0, 1, n)  # reserved, type=icon, count
    entry_size = 16
    offset = len(header) + entry_size * n

    directory = b""
    for (size, _frame), blob in zip(frames, blobs):
        # 256 在 ICO 目录里写 0
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)

    dst.write_bytes(header + directory + b"".join(blobs))
    return len(header) + len(directory) + sum(len(b) for b in blobs)


def preview(frames: list[tuple[int, Image.Image]], dst: Path) -> None:
    """各尺寸并排，深色底上检查小尺寸是否还认得出。"""
    pad = 12
    wid = sum(s + pad for s, _ in frames) + pad
    hei = max(s for s, _ in frames) + pad * 2
    canvas = Image.new("RGBA", (wid, hei), (22, 14, 36, 255))
    x = pad
    for size, frame in frames:
        canvas.alpha_composite(frame, (x, pad + (hei - pad * 2 - size) // 2))
        x += size + pad
    canvas.save(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="另存各尺寸并排检查图")
    args = ap.parse_args()

    if not SRC.is_file():
        print(f"[错误] 缺图标源：{SRC}")
        return 1
    src = Image.open(SRC).convert("RGBA")
    print(f"图标源 {SRC.name}：{src.width}×{src.height}")

    frames = [(size, render(src, size)) for size in SIZES]
    total = write_ico(frames, DST)
    print(f"已写出 {DST.name}：{total:,} 字节，{len(frames)} 个尺寸")
    print("  " + " / ".join(str(s) for s, _ in frames))

    if args.preview:
        out = ROOT / "logs" / "icon-preview.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        preview(frames, out)
        print(f"检查图：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
