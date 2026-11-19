#!/usr/bin/env python
"""生成基线索引与身份文件。

产物（写入 ``baseline/``）：
- ``index-<short>.json.gz``   条目清单（path/offset/size/md5/flags），后续所有路径以此为准
- ``identity-<short>.json``   基线身份：大小、sha256、头部字段、条目数、索引指纹

示例::

    python tools/make_index.py            # 全部基线
    python tools/make_index.py jjxf.v1.45 # 只做 1.45
"""

from __future__ import annotations

import argparse
import re
import gzip
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod, paths  # noqa: E402
from app.core.pck import PckReader  # noqa: E402
from app.core.hashing import sha256_file  # noqa: E402


def short_id(baseline_id: str) -> str:
    """"jjxf.v1.45" -> "1.45"；用于文件名。"""
    m = re.match(r"^jjxf\.v(.+)$", baseline_id)
    return m.group(1) if m else baseline_id.replace(".", "-")


def write_index(reader: PckReader, dest: Path) -> str:
    """分块写 gzip JSON，避免一次性构造巨大字符串。返回文件 sha256。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    h = hashlib.sha256()
    header = {
        "schema": 1,
        "source": str(reader.path),
        "header": reader.header.summary(),
        "entry_count": len(reader.entries),
    }
    with gzip.open(tmp, "wb", compresslevel=6) as gz:

        def emit(text: str) -> None:
            data = text.encode("utf-8")
            h.update(data)
            gz.write(data)

        head_json = json.dumps(header, ensure_ascii=False)
        assert head_json.endswith("}")
        emit(head_json[:-1])          # 去掉尾部 }，继续拼 entries
        emit(',"entries":[')

        buf: list[str] = []
        first = True

        def flush() -> None:
            nonlocal first
            if not buf:
                return
            chunk = ",".join(buf)
            emit(chunk if first else "," + chunk)
            buf.clear()
            first = False

        for e in reader.entries:
            buf.append(json.dumps(e.as_row(), ensure_ascii=False, separators=(",", ":")))
            if len(buf) >= 5000:
                flush()
        flush()
        emit("]}")
    tmp.replace(dest)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="生成基线索引")
    ap.add_argument("baseline_ids", nargs="*", help="缺省处理全部基线")
    ap.add_argument("--no-hash", action="store_true", help="跳过整文件 sha256（很慢）")
    args = ap.parse_args(argv)

    cfg = cfg_mod.load()
    ids = args.baseline_ids or list(cfg["baselines"].keys())
    paths.ensure_dirs()

    for baseline_id in ids:
        meta = cfg_mod.baseline(cfg, baseline_id)
        src = Path(meta["path"])
        print(f"\n=== {baseline_id} ===\n  {src}")
        if not src.exists():
            print("  [跳过] 文件不存在")
            continue

        t0 = time.time()
        size = src.stat().st_size
        expect_size = meta.get("size")
        if expect_size and size != expect_size:
            print(f"  [警告] 大小不符：实际 {size:,}，期望 {expect_size:,}")

        reader = PckReader.open(src)
        print(f"  解析完成：{len(reader):,} 条，用时 {time.time() - t0:.1f}s")
        print(f"  头部：v{reader.header.pack_version} engine "
              f"{'.'.join(map(str, reader.header.engine_version))} "
              f"file_base={reader.header.file_base} dir_offset={reader.header.dir_offset}")

        if meta.get("entry_count") and meta["entry_count"] != len(reader):
            print(f"  [警告] 条目数不符：实际 {len(reader):,}，期望 {meta['entry_count']:,}")

        suffix = short_id(baseline_id)
        index_path = paths.BASELINE / f"index-{suffix}.json.gz"
        t1 = time.time()
        index_sha = write_index(reader, index_path)
        print(f"  索引：{index_path.name} ({index_path.stat().st_size:,} 字节，{time.time() - t1:.1f}s)")

        t2 = time.time()
        if args.no_hash:
            file_sha = meta.get("sha256", "")
            print("  [跳过] 整文件 sha256")
        else:
            file_sha = sha256_file(src)
            ok = (file_sha == meta.get("sha256"))
            print(f"  sha256：{file_sha} {'✓ 与配置一致' if ok else '✗ 与配置不一致'} "
                  f"({time.time() - t2:.1f}s)")

        identity = {
            "schema": 1,
            "id": baseline_id,
            "label": meta.get("label", ""),
            "kind": meta.get("kind", ""),
            "engine_line": meta.get("engine", ""),
            "path": str(src),
            "size": size,
            "sha256": file_sha,
            "header": reader.header.summary(),
            "entry_count": len(reader),
            "total_entry_bytes": sum(e.size for e in reader.entries),
            "encrypted_entries": sum(1 for e in reader.entries if e.encrypted),
            "top_suffixes": reader.suffixes(12),
            "index_file": index_path.name,
            "index_sha256": index_sha,
            "produced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        out = paths.BASELINE / f"identity-{suffix}.json"
        out.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  身份：{out.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
