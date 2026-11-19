"""比对两个 pck / 两个 packset 的条目差异。

用法::

    # 两个包之间比（A 是"旧"，B 是"新"）
    python tools/diff_packs.py 旧.pck 新.pck
    python tools/diff_packs.py --a D:\\gobot工具\\demo  --b packs/story-x/story.pck

    # 两个分片目录之间比
    python tools/diff_packs.py --packset packs/old/packset.json packs/new/packset.json

输出四类：

- ``仅旧包有``：被删掉的条目
- ``仅新包有``：新增的条目
- ``大小变了``：同路径但字节数不同（最需要看的一类）
- ``内容变了``：大小相同但 md5 不同

差异大时只打印前 N 条，并给总数——用来判断「这个覆盖层到底改了什么」。
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


def load_entries(target: Path) -> dict[str, tuple[int, str]]:
    """``{路径: (大小, md5)}``。target 可以是 pck、目录或 packset。"""
    path = Path(target)
    if path.is_dir():
        candidate = path / "packset.json"
        if candidate.is_file():
            path = candidate
        else:
            out: dict[str, tuple[int, str]] = {}
            for pck in sorted(path.glob("pack_*.pck")):
                out.update(load_entries(pck))
            return out
    if path.name == "packset.json":
        data = json.loads(path.read_text(encoding="utf-8"))
        out = {}
        for entry in data.get("packs") or []:
            raw = Path(str(entry.get("path") or ""))
            resolved = raw if raw.is_absolute() else (path.parent / raw.name)
            if resolved.is_file():
                out.update(load_entries(resolved))
        return out
    reader = PckReader.open(path)
    return {e.path: (e.size, e.md5) for e in reader.entries}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="比对两个包/分片目录的条目差异")
    ap.add_argument("paths", nargs="*", help="两个位置：旧 新")
    ap.add_argument("--a", dest="a", default=None, help="旧包（也可用位置参数）")
    ap.add_argument("--b", dest="b", default=None, help="新包（也可用位置参数）")
    ap.add_argument("--limit", type=int, default=40, help="每类最多打印多少条")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--filter", default=None, help="只看路径包含该子串的条目")
    args = ap.parse_args(argv)

    left = Path(args.a) if args.a else (Path(args.paths[0]) if len(args.paths) >= 1 else None)
    right = Path(args.b) if args.b else (Path(args.paths[1]) if len(args.paths) >= 2 else None)
    if left is None or right is None:
        ap.print_help()
        return 2

    print(f"A: {left}")
    print(f"B: {right}")
    a = load_entries(left)
    b = load_entries(right)
    print(f"   A 有 {len(a):,} 条目，B 有 {len(b):,} 条目")

    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    size_changed = sorted(k for k in set(a) & set(b) if a[k][0] != b[k][0])
    content_changed = sorted(
        k for k in set(a) & set(b) if a[k][0] == b[k][0] and a[k][1] != b[k][1]
    )

    if args.filter:
        needle = args.filter
        only_a = [k for k in only_a if needle in k]
        only_b = [k for k in only_b if needle in k]
        size_changed = [k for k in size_changed if needle in k]
        content_changed = [k for k in content_changed if needle in k]

    def dump(title: str, keys: list[str]) -> None:
        if not keys:
            return
        print(f"\n=== {title}（{len(keys)}）===")
        for key in keys[: args.limit]:
            if title == "大小变了":
                print(f"  {key}：{a[key][0]:,} → {b[key][0]:,}")
            else:
                print(f"  {key}")
        if len(keys) > args.limit:
            print(f"  …还有 {len(keys) - args.limit} 条")

    dump("仅 A 有", only_a)
    dump("仅 B 有", only_b)
    dump("大小变了", size_changed)
    dump("内容变了", content_changed)

    total = len(only_a) + len(only_b) + len(size_changed) + len(content_changed)
    print("\n=== 汇总 ===")
    print(f"  删除 {len(only_a)}，新增 {len(only_b)}，大小变化 {len(size_changed)}，内容变化 {len(content_changed)}")
    print(f"  差异条目合计 {total}（占 A 的 {total * 100 // max(1, len(a))}%）")

    if args.json:
        print(json.dumps(
            {
                "a": str(left), "b": str(right),
                "counts": {"a": len(a), "b": len(b), "removed": len(only_a),
                           "added": len(only_b), "resized": len(size_changed),
                           "changed": len(content_changed)},
                "removed": only_a[:200], "added": only_b[:200],
                "resized": size_changed[:200], "changed": content_changed[:200],
            },
            ensure_ascii=False, indent=2,
        ))
    return 0