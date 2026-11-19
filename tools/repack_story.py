#!/usr/bin/env python
"""把补丁脚本内置进剧情覆盖层 story.pck（方案 A）。

背景
----
4.7 是两层结构：

- baseline = ``机甲旋风Demo.pck``（2.90 GB / 379,926 条目），脚本**只有 .gdc**
- story    = ``story.pck``（741 条目），带 **138 个 .gd 文本源码**

本体 138 个 ``.gdc`` 与 story 层 138 个 ``.gd`` 一一对应，引擎实际读的是 story 的
``.gd``。因此把修复写进 story.pck 就等于改运行逻辑，**无需碰 2.9 GB 本体**。

为什么不改本体：本体只有 ``.gdc``，而 ``engines/godot-4.7.2.exe`` 是
release export template（无 ``--export-*``、无脚本编译器），无法产出 ``.gdc``。

用法
----
    # 1) 生成修复包（不动原文件）
    python tools/repack_story.py \\
        --src packs/story-官方分区-轻量/story.pck \\
        --patch _perf47/iter8.pck \\
        --out _perf47/build/story_fixed.pck

    # 2) 校验产物（逐条目比对 md5 + 头部 + 注入内容）
    python tools/repack_story.py --verify \\
        --src packs/story-官方分区-轻量/story.pck \\
        --patch _perf47/iter8.pck \\
        --out _perf47/build/story_fixed.pck

    # 3) 同步 config/stories.json 的 sha256 / total_bytes / version / issues
    python tools/repack_story.py --update-meta \\
        --meta config/stories.json --out _perf47/build/story_fixed.pck

    # 4) 替换（先备份原包为 .orig）
    python tools/repack_story.py --install \\
        --src packs/story-官方分区-轻量/story.pck \\
        --out _perf47/build/story_fixed.pck

注意
----
- ``PckWriter`` 默认 ``engine_version=(4, 6, 0)``，**必须显式传原包的 engine_version**
  （本包为 4.7.2），否则头部写错。
- 条目按 UTF-8 路径排序写入，产出确定：同样输入必得同样字节。
- 用 ``PckSource`` 流式复制未改动条目，160 MB 全程不峰值内存。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.pck.reader import PckReader  # noqa: E402
from app.core.pck.sources import PckSource  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402

PATCH_SUFFIXES = (".gd",)   # 只注入脚本；.remap 原样保留（自指 remap 与内容无关）


def sha256_of(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            block = f.read(8 * 1024 * 1024)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def build(src: Path, patch: Path, out: Path) -> dict[str, object]:
    """按 src 的条目顺序重打，用 patch 里的同名脚本替换。"""
    if not src.is_file():
        raise SystemExit(f"源包不存在：{src}")
    if not patch.is_file():
        raise SystemExit(f"补丁包不存在：{patch}")
    if out.exists() and out.resolve() == src.resolve():
        raise SystemExit("--out 不能覆盖 --src，请先输出到别处再 --install")

    rs = PckReader.open(src)
    rp = PckReader.open(patch)

    inject = [k for k in rp.index if k.endswith(PATCH_SUFFIXES) and not k.endswith(".remap")]
    missing = [k for k in inject if k not in rs.index]
    if missing:
        raise SystemExit(f"补丁里有 {len(missing)} 个脚本在源包中不存在，拒绝注入：{missing[:5]}")

    out.parent.mkdir(parents=True, exist_ok=True)
    w = PckWriter(out, line="4.7", engine_version=tuple(rs.header.engine_version))

    n_copy = n_patch = 0
    for e in rs.entries:
        if e.path in inject:
            w.add_text(e.path, rp.read_text(e.path))
            n_patch += 1
        else:
            w.add_source(e.path, PckSource(reader=rs, entry=e), flags=e.flags)
            n_copy += 1
    result = w.commit()

    digest, size = sha256_of(out)
    total_bytes = sum(x.size for x in PckReader.open(out).entries)
    print(f"重打包完成：{out}")
    print(f"  条目 {result.entry_count}（原样 {n_copy} + 注入 {n_patch}）")
    print(f"  文件 {size:,} B   条目体积和 {total_bytes:,} B")
    print(f"  sha256 {digest}")
    print(f"  头部 pack_version={result.pack_version} engine={result.engine_version}")
    return {
        "entry_count": result.entry_count,
        "size": size,
        "total_bytes": total_bytes,
        "sha256": digest,
        "injected": inject,
        "copied": n_copy,
    }


def verify(src: Path, patch: Path, out: Path) -> int:
    """逐条目校验：未改动条目 md5 必须相同，注入条目内容必须等于 patch。"""
    rs = PckReader.open(src)
    rp = PckReader.open(patch)
    ro = PckReader.open(out)

    inject = {k for k in rp.index if k.endswith(PATCH_SUFFIXES) and not k.endswith(".remap")}
    ps = {e.path: e for e in rs.entries}
    problems: list[str] = []

    if len(ro.entries) != len(rs.entries):
        problems.append(f"条目数 {len(ro.entries)} != 原 {len(rs.entries)}")

    for e in ro.entries:
        src_e = ps.get(e.path)
        if src_e is None:
            problems.append(f"多出条目：{e.path}")
        elif e.path in inject:
            if ro.read_bytes(e) != rp.read_bytes(e.path):
                problems.append(f"注入内容不符：{e.path}")
        elif src_e.md5 != e.md5:
            problems.append(f"未改动条目 md5 变化：{e.path}")

    for key in ("pack_version", "engine_version", "pack_flags", "file_base"):
        a, b = getattr(rs.header, key), getattr(ro.header, key)
        if a != b:
            problems.append(f"头部 {key} 不一致：{a} -> {b}")

    print(f"校验 {out.name}：")
    print(f"  条目 {len(ro.entries)}（原 {len(rs.entries)}）")
    print(f"  注入脚本 {len(inject)} 个")
    print(f"  头部 pack_version={ro.header.pack_version} engine={ro.header.engine_version} "
          f"flags={ro.header.pack_flags}")
    if problems:
        print(f"\n  ✗ 发现 {len(problems)} 个问题：")
        for p in problems[:20]:
            print(f"    - {p}")
        return 1
    print("\n  ✓ 全部通过：未改动条目逐字节一致，注入内容等于补丁，头部一致")
    return 0


def update_meta(meta: Path, out: Path, *, version: str | None, note: str | None) -> int:
    """把产物的 sha256 / total_bytes 写回 stories.json。"""
    if not meta.is_file():
        raise SystemExit(f"元数据不存在：{meta}")
    digest, size = sha256_of(out)
    total_bytes = sum(x.size for x in PckReader.open(out).entries)
    entry_count = len(PckReader.open(out).entries)

    data = json.loads(meta.read_text(encoding="utf-8"))
    items = data.get("items") or []
    if not items:
        raise SystemExit("stories.json 里没有条目，拒绝改")

    hit = None
    for it in items:
        rel = str(it.get("overlay") or "")
        if rel and (ROOT / rel).resolve() == out.resolve():
            hit = it
            break
    if hit is None:
        # overlay 指向的不是 out（常见于先输出到临时目录），退化为改唯一那条记录
        if len(items) != 1:
            raise SystemExit("无法确定要更新哪条 story 记录（overlay 不匹配且条目不唯一）")
        hit = items[0]

    old = {k: hit.get(k) for k in ("entry_count", "total_bytes", "sha256", "version")}
    hit["entry_count"] = entry_count
    hit["total_bytes"] = total_bytes
    hit["sha256"] = digest
    if version:
        hit["version"] = version
    if note:
        issues = hit.get("issues")
        if not isinstance(issues, list):
            issues = []
        if note not in issues:
            issues.append(note)
        hit["issues"] = issues
    hit["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    meta.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已更新 {meta.name}：")
    for k in ("entry_count", "total_bytes", "sha256", "version"):
        v = hit.get(k)
        if old.get(k) != v:
            print(f"  {k}: {old.get(k)} -> {v}")
    print(f"  （文件大小 {size:,} B）")
    return 0


def install(src: Path, out: Path, keep_orig: bool) -> int:
    """备份原包，然后用 out 覆盖 src。"""
    if not out.is_file():
        raise SystemExit(f"产物不存在：{out}")
    if src.resolve() == out.resolve():
        raise SystemExit("源与产物是同一文件")

    orig = src.with_suffix(src.suffix + ".orig")
    if keep_orig and not orig.exists():
        shutil.copy2(src, orig)
        print(f"已备份原包：{orig}（{orig.stat().st_size:,} B）")
    elif orig.exists():
        print(f"原包备份已存在，保留不动：{orig}")

    tmp = src.with_suffix(src.suffix + ".new")
    shutil.copy2(out, tmp)
    tmp.replace(src)
    print(f"已替换：{src}（{src.stat().st_size:,} B）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把补丁脚本内置进 story.pck")
    ap.add_argument("--src", type=Path, help="原 story.pck")
    ap.add_argument("--patch", type=Path, help="补丁覆盖包（含要注入的 .gd）")
    ap.add_argument("--out", type=Path, required=True, help="输出/待校验/待安装的 pck")
    ap.add_argument("--meta", type=Path, help="stories.json（--update-meta 用）")
    ap.add_argument("--version", default=None, help="更新 stories.json 的 version")
    ap.add_argument("--note", default=None, help="追加到 issues 的说明")
    ap.add_argument("--verify", action="store_true", help="只做校验")
    ap.add_argument("--update-meta", action="store_true", help="只更新元数据")
    ap.add_argument("--install", action="store_true", help="备份并替换 --src")
    ap.add_argument("--no-keep-orig", action="store_true", help="安装时不保留 .orig 备份")
    args = ap.parse_args()

    if args.verify:
        if not (args.src and args.patch):
            ap.error("--verify 需要 --src 与 --patch")
        return verify(args.src, args.patch, args.out)
    if args.update_meta:
        if not args.meta:
            ap.error("--update-meta 需要 --meta")
        return update_meta(args.meta, args.out, version=args.version, note=args.note)
    if args.install:
        if not args.src:
            ap.error("--install 需要 --src")
        return install(args.src, args.out, keep_orig=not args.no_keep_orig)

    if not (args.src and args.patch):
        ap.error("生成模式需要 --src 与 --patch")
    build(args.src, args.patch, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())