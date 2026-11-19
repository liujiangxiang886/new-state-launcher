"""校验一组分片（packset.json + pack_*.pck）。

用法::

    python tools/verify_packset.py packs/jjxf.v1.45-shards
    python tools/verify_packset.py packs/jjxf.v1.45-shards/packset.json --deep

``--deep`` 会把每个分片完整读一遍并重算 md5（慢，但能发现位翻转）。
默认只做结构校验：文件存在、条目数、字节数、sha256（若记录过）、跨片重复。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.pck import PckReader  # noqa: E402
from app.core.hashing import sha256_file  # noqa: E402

OK = 0
BAD = 0


def say(ok: bool, text: str) -> None:
    global OK, BAD
    if ok:
        OK += 1
        print(f"  OK   {text}")
    else:
        BAD += 1
        print(f"  FAIL {text}")


def resolve(packset_path: Path, entry: dict) -> Path:
    raw = Path(str(entry.get("path") or ""))
    if raw.is_absolute():
        return raw
    # 相对路径按 packset 所在目录解析（发布包里就是这种形态）
    return (packset_path.parent.parent / raw).resolve() if raw.parts[:1] == ("packs",) else (packset_path.parent / raw).resolve()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验分片完整性")
    ap.add_argument("target", help="packset.json 或包含它的目录")
    ap.add_argument("--deep", action="store_true", help="逐条重算 md5（慢）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    target = Path(args.target)
    packset_path = target / "packset.json" if target.is_dir() else target
    if not packset_path.is_file():
        print(f"找不到 packset：{packset_path}", file=sys.stderr)
        return 2

    data = json.loads(packset_path.read_text(encoding="utf-8"))
    packs = data.get("packs") or []
    report: dict = {"packset": str(packset_path), "packs": [], "ok": True}

    print(f"packset: {packset_path}")
    print(f"  声明 {len(packs)} 片，entry_count={data.get('entry_count')}，"
          f"total_bytes={data.get('total_bytes')}，pack_version={data.get('pack_version')}")

    seen_paths: dict[str, str] = {}
    total_entries = 0
    total_bytes = 0

    for index, entry in enumerate(packs):
        path = resolve(packset_path, entry)
        name = path.name
        item: dict = {"index": index, "path": str(path), "ok": True}
        print(f"\n[{index}] {name}")
        if not path.is_file():
            say(False, "文件存在")
            item["ok"] = False
            report["packs"].append(item)
            continue
        size = path.stat().st_size
        total_bytes += size
        print(f"     {size:,} 字节（数据区 {entry.get('bytes', 0):,}）")

        # packset 里有两个字节字段，别搞混：
        #   bytes = 数据区字节数（不含头与目录）
        #   size  = 整个文件大小
        # 校验文件大小要用 size；旧产物只有 bytes 时才退回比 bytes + 头尾开销。
        declared_size = int(entry.get("size") or 0)
        if declared_size:
            same = declared_size == size
            say(same, f"文件大小一致（声明 {declared_size:,}，实际 {size:,}）")
            item["ok"] = item["ok"] and same

        if entry.get("sha256"):
            actual = sha256_file(path)
            same = actual == entry["sha256"]
            say(same, f"sha256 一致（{actual[:16]}…）")
            item["sha256"] = actual
            item["ok"] = item["ok"] and same

        try:
            reader = PckReader.open(path)
        except Exception as exc:  # noqa: BLE001
            say(False, f"可解析（{exc}）")
            item["ok"] = False
            report["packs"].append(item)
            continue

        count = len(reader)
        total_entries += count
        item["entries"] = count
        print(f"     {count:,} 条目，pack_version={reader.header.pack_version}")
        if entry.get("entries") and int(entry["entries"]) != count:
            say(False, f"条目数一致（声明 {entry['entries']:,}，实际 {count:,}）")
            item["ok"] = False
        elif entry.get("entries"):
            say(True, "条目数一致")

        if entry.get("pack_version") and int(entry["pack_version"]) != reader.header.pack_version:
            say(False, f"pack_version 一致（声明 {entry['pack_version']}，实际 {reader.header.pack_version}）")
            item["ok"] = False

        # 跨片重复
        dup = [p for p in (e.path for e in reader.entries) if p in seen_paths]
        if dup:
            say(False, f"跨片无重复（{len(dup)} 个重复，例：{dup[0]}，也出现在 {seen_paths[dup[0]]}）")
            item["ok"] = False
        else:
            say(True, "跨片无重复")
        for entry_path in (e.path for e in reader.entries):
            seen_paths[entry_path] = name

        if args.deep:
            bad = 0
            for pck_entry in reader.entries:
                try:
                    if not reader.read_bytes(pck_entry, verify=True):
                        bad += 1
                except Exception:  # noqa: BLE001
                    bad += 1
            say(bad == 0, f"逐条 md5 校验（{count:,} 条，失败 {bad}）")
            item["ok"] = item["ok"] and bad == 0

        report["packs"].append(item)

    print("\n=== 汇总 ===")
    print(f"  分片 {len(packs)}，条目 {total_entries:,}，字节 {total_bytes:,}")
    if data.get("entry_count") and int(data["entry_count"]) != total_entries:
        say(False, f"总条目数一致（声明 {data['entry_count']:,}，实际 {total_entries:,}）")
    elif data.get("entry_count"):
        say(True, "总条目数一致")

    # 顶层 total_bytes 是各片数据区之和；与「各片 size 之和」不是一回事，
    # 所以这里比的是各片 bytes 之和。
    declared_data = sum(int(p.get("bytes") or 0) for p in packs)
    if data.get("total_bytes") and int(data["total_bytes"]) != declared_data:
        say(False, f"总数据区字节一致（声明 {data['total_bytes']:,}，累加 {declared_data:,}）")
    elif data.get("total_bytes"):
        say(True, f"总数据区字节一致（{declared_data:,}）")
    print(f"  文件总大小 {total_bytes:,} 字节")

    report["ok"] = BAD == 0
    report["summary"] = {"packs": len(packs), "entries": total_entries, "bytes": total_bytes}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\n通过 {OK}，失败 {BAD}")
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())