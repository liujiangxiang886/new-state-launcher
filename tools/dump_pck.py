#!/usr/bin/env python
"""PCK 诊断工具（只读）。

示例::

    python tools/dump_pck.py "D:\\...\\机甲旋风Demo.pck" --stats
    python tools/dump_pck.py "D:\\...\\mchGame.exe" --header
    python tools/dump_pck.py "D:\\...\\机甲旋风Demo.pck" --search "scripts/game"
    python tools/dump_pck.py "D:\\...\\机甲旋风Demo.pck" --index baseline/index-1.45.json.gz
    python tools/dump_pck.py "D:\\...\\机甲旋风Demo.pck" --extract D:\\tmp\\out --match ".json"
    python tools/dump_pck.py "D:\\...\\机甲旋风Demo.pck" --verify-sample 200
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.pck import PckReader  # noqa: E402


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="PCK 只读诊断")
    ap.add_argument("path", help="pck 文件，或内嵌了 pck 的 exe")
    ap.add_argument("--header", action="store_true", help="只打印头部")
    ap.add_argument("--stats", action="store_true", help="打印统计（条目数、后缀分布）")
    ap.add_argument("--search", metavar="SUBSTR", help="按子串搜索路径")
    ap.add_argument("--limit", type=int, default=50, help="搜索/列表条数上限")
    ap.add_argument("--prefix", metavar="PREFIX", help="按前缀列出路径")
    ap.add_argument("--index", metavar="OUT", help="导出 gzip 索引到指定文件")
    ap.add_argument("--extract", metavar="DIR", help="解包到目录")
    ap.add_argument("--match", metavar="SUBSTR", help="配合 --extract：只解路径含该子串的条目")
    ap.add_argument("--verify-sample", type=int, metavar="N", help="抽样校验 N 条 md5")
    args = ap.parse_args(argv)

    reader = PckReader.open(args.path)

    if args.header:
        _print(reader.header.summary())
        return 0

    if args.stats:
        _print(reader.stats())
        return 0

    if args.search:
        hits = reader.search(args.search, limit=args.limit)
        print(f"命中 {len(hits)} 条（上限 {args.limit}）：")
        for e in hits:
            print(f"  {e.size:>12,}  {e.md5}  {e.path}")
        return 0

    if args.prefix:
        n = 0
        for e in reader.iter_entries(prefix=args.prefix):
            print(f"  {e.size:>12,}  {e.path}")
            n += 1
            if n >= args.limit:
                break
        print(f"（显示 {n} 条，上限 {args.limit}）")
        return 0

    if args.index:
        out = reader.save_index(args.index)
        print(f"索引已写出：{out}（{out.stat().st_size:,} 字节，{len(reader)} 条）")
        return 0

    if args.extract:
        match = (lambda p: args.match in p) if args.match else None
        count = reader.extract_all(
            args.extract,
            match=match,
            on_progress=lambda i, total: print(f"\r  解包 {i}/{total}", end="", flush=True),
        )
        print(f"\n解包完成：{count} 个文件 -> {args.extract}")
        return 0

    if args.verify_sample is not None:
        _print(reader.verify(sample=args.verify_sample))
        return 0

    # 默认：概览
    _print(reader.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
