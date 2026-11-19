"""白边检测：对截图四边做像素扫描，量化判定是否残留浅色边缘。

不靠肉眼判断，因为浅色边缘有时只有 1–2px、或在缩放后才出现。
判定标准：
- 取上下左右四条边各 N 像素宽的带状区域；
- 统计「明显偏亮」像素（亮度 > 阈值）占比；
- 任何一条边占比超过容差即视为失败，并打印最亮像素位置。

用法::

    python tools/check_edges.py _build_tools/shots/game.png
    python tools/check_edges.py shots/*.png --band 3 --max 0.02
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - 依赖缺失时给出可执行提示
    print("需要 Pillow：pip install pillow", file=sys.stderr)
    raise SystemExit(2)

#: 深色主题下，「偏亮」按平均亮度判断。页面底色 #130c1f 亮度约 24，
#: 卡片最亮处不到 70；超过 110 只可能是浅色残留（白边、滚动条轨道等）。
BRIGHT = 110


def luminance(px: tuple[int, int, int]) -> float:
    r, g, b = px[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def scan(path: Path, band: int, bright: int) -> dict:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()

    results = {}
    for side in ("top", "bottom", "left", "right"):
        coords = []
        if side == "top":
            coords = [(x, y) for y in range(band) for x in range(w)]
        elif side == "bottom":
            coords = [(x, y) for y in range(h - band, h) for x in range(w)]
        elif side == "left":
            coords = [(x, y) for x in range(band) for y in range(h)]
        else:
            coords = [(x, y) for x in range(w - band, w) for y in range(h)]

        total = len(coords)
        hits = 0
        peak = 0.0
        peak_at = None
        for x, y in coords:
            lum = luminance(px[x, y])
            if lum > peak:
                peak = lum
                peak_at = (x, y)
            if lum >= bright:
                hits += 1
        results[side] = {
            "ratio": round(hits / total, 5) if total else 0.0,
            "hits": hits,
            "total": total,
            "peak_lum": round(peak, 1),
            "peak_at": peak_at,
        }

    # 四角单独看（圆角/阴影最容易在这露出底色差）
    corners = {}
    c = max(2, band)
    for name, box in {
        "tl": (0, 0, c, c),
        "tr": (w - c, 0, w, c),
        "bl": (0, h - c, c, h),
        "br": (w - c, h - c, w, h),
    }.items():
        region = im.crop(box)
        vals = [luminance(p) for p in region.getdata()]
        corners[name] = {
            "max": round(max(vals), 1) if vals else 0,
            "avg": round(sum(vals) / len(vals), 1) if vals else 0,
        }

    return {"file": path.name, "size": [w, h], "edges": results, "corners": corners}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--band", type=int, default=3, help="边缘采样带宽（像素）")
    ap.add_argument("--bright", type=int, default=BRIGHT, help="判定为偏亮的亮度阈值")
    ap.add_argument("--max", type=float, default=0.005, help="允许的偏亮像素占比上限")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    failed = 0
    reports = []
    for item in args.images:
        path = Path(item)
        if not path.is_file():
            print(f"跳过（不存在）：{path}")
            continue
        rep = scan(path, args.band, args.bright)
        reports.append(rep)
        worst = max(rep["edges"].items(), key=lambda kv: kv[1]["ratio"])
        bad = [k for k, v in rep["edges"].items() if v["ratio"] > args.max]
        corner_bad = [k for k, v in rep["corners"].items() if v["max"] >= args.bright]
        ok = not bad and not corner_bad
        if not ok:
            failed += 1

        if args.json:
            continue
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {rep['file']} {rep['size'][0]}x{rep['size'][1]}")
        print(
            f"       最差边={worst[0]} 偏亮占比={worst[1]['ratio']*100:.3f}% "
            f"峰值亮度={worst[1]['peak_lum']:.0f}@{worst[1]['peak_at']}"
        )
        if bad:
            print(f"       超限的边：{', '.join(bad)}")
        if corner_bad:
            detail = ", ".join(f"{k}(max={rep['corners'][k]['max']:.0f})" for k in corner_bad)
            print(f"       过亮的角：{detail}")

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())