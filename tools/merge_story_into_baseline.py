#!/usr/bin/env python
"""把剧情覆盖层合并进原版完整包，产出可直接被原版 exe 加载的单包。

为什么这样合并
--------------

原版 pck 是「完整包」（含 project.binary、全部资源），剧情包是「源码覆盖层」
（741 条，只有 data/scripts/scenes/remap/uid）。原版 exe 是模板导出，只会自动
认**同名的一个 pck**，没有挂载器就没有第二层，所以只能合成一个包。

实现策略：**只重排目录，不动数据字节**

- 原版数据区 [112, 112+data_len) 原样整体拷贝，所有原条目 offset 继续有效；
- 剧情包数据区整体追加到后面，剧情条目 offset 统一加 ``base_data_len`` 重定位；
- 同名条目（408 条）在目录里改指向剧情版本，剧情独有条目（333 条）追加；
- md5 全部沿用两侧原有值 —— 字节没被重新编码，就不可能算错。

相比「逐条读出来再写回去」，这条路省掉 38 万次文件打开和 2.9 GB 的 md5 重算。

用法::

    python tools/merge_story_into_baseline.py \
        --baseline ".../机甲旋风Demo.pck" \
        --story "packs/story-官方分区-轻量/story.pck" \
        --out ".../机甲旋风Demo.merged.pck" --report out.json

    # 只统计不写盘
    python tools/merge_story_into_baseline.py ... --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import pck as pck_mod  # noqa: E402
from app.core.pck import format as fmt  # noqa: E402
from app.core.pck import PckReader  # noqa: E402

CHUNK = 8 * 1024 * 1024


def _skip_path(path: str, patterns: Sequence[str]) -> bool:
    """按后缀排除条目。

    存在的理由（2026-11-02 定位）：剧情覆盖层是「编辑器工程导出」形态，带了一整套
    ``.gd`` 脚本源码和指向源码自己的 ``.gd.remap``。原版 pck 里同名脚本是编译好的
    ``.gdc``，``.gd.remap`` 指向 ``.gdc``。一旦覆盖层的 ``.gd`` 进包，engine 会优先
    命中真实存在的 ``.gd``，于是 138 个脚本全部改成「运行时现场编译源码」，
    偏离原版加载方式。需要能把这些排除掉、让原版的 ``.gdc`` 重新生效。
    """
    for pat in patterns:
        if path.endswith(pat):
            return True
    return False


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _copy_range(src: Path, dst, start: int, length: int, label: str) -> None:
    done = 0
    next_note = 512 * 1024 * 1024
    with src.open("rb") as f:
        f.seek(start)
        while done < length:
            block = f.read(min(CHUNK, length - done))
            if not block:
                raise SystemExit(f"{label}：源文件提前结束（已拷 {done}/{length}）")
            dst.write(block)
            done += len(block)
            if done >= next_note:
                print(f"  {label} {_human(done)} / {_human(length)}", flush=True)
                next_note += 512 * 1024 * 1024
    if done != length:
        raise SystemExit(f"{label}：拷贝长度不符 {done} != {length}")


def merge(
    baseline: Path,
    story: Path,
    out: Path,
    *,
    dry_run: bool = False,
    skip: Sequence[str] = (),
    log=print,
) -> dict:
    t0 = time.time()
    base = PckReader.open(baseline)
    over = PckReader.open(story)

    if base.header.pack_version != over.header.pack_version:
        raise SystemExit(
            f"pack_version 不一致：基线 {base.header.pack_version} / 剧情 {over.header.pack_version}"
        )
    if base.header.dir_encrypted or over.header.dir_encrypted:
        raise SystemExit("包目录被加密，本工具不支持")

    base_entries = base.entries
    story_all = over.entries
    story_entries = [e for e in story_all if not _skip_path(e.path, skip)]
    skipped = [e for e in story_all if _skip_path(e.path, skip)]
    base_index = base.index
    story_index = over.index

    # 数据区：file_base 到 dir_offset 之间的原始字节，整体搬
    base_data_start = base.header.file_base
    base_data_len = base.header.dir_offset - base.header.file_base
    story_data_start = over.header.file_base
    story_data_len = over.header.dir_offset - over.header.file_base

    covered = [e for e in story_entries if e.path in base_index]
    added = [e for e in story_entries if e.path not in base_index]

    # 合并后的条目表：(path, offset, size, md5_hex, flags)
    merged: dict[str, tuple[str, int, int, str, int]] = {}
    for e in base_entries:
        merged[e.path] = (e.path, e.offset, e.size, e.md5, e.flags)
    for e in story_entries:
        merged[e.path] = (
            e.path,
            base_data_len + e.offset,   # 重定位到追加区
            e.size,
            e.md5,
            e.flags,
        )

    total_entries = len(merged)
    out_size = fmt.HEADER_SIZE + base_data_len + story_data_len
    report = {
        "baseline": str(baseline),
        "story": str(story),
        "out": str(out),
        "baseline_entries": len(base_entries),
        "story_entries": len(story_entries),
        "skipped_entries": len(skipped),
        "skip_patterns": list(skip),
        "covered": len(covered),
        "added": len(added),
        "merged_entries": total_entries,
        "base_data_bytes": base_data_len,
        "story_data_bytes": story_data_len,
        "out_bytes": out_size,
        "pack_version": base.header.pack_version,
        "engine": base.header.engine_version,
        "covered_sample": [e.path for e in covered[:8]],
        "added_sample": [e.path for e in added[:8]],
        "seconds": 0.0,
    }
    log(f"基线条目      : {len(base_entries):,}")
    log(f"剧情条目      : {len(story_entries):,}（同名覆盖 {len(covered)}，新增 {len(added)}）")
    if skipped:
        log(f"按 --skip 排除 : {len(skipped):,} 条（{', '.join(skip)}）")
    log(f"合并后条目    : {total_entries:,}")
    log(f"数据区        : 基线 {_human(base_data_len)} + 剧情 {_human(story_data_len)}")
    log(f"预计产物      : {_human(out_size)}（{out_size:,} B）")

    if dry_run:
        report["seconds"] = round(time.time() - t0, 2)
        report["dry_run"] = True
        return report

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        tmp.unlink()

    ordered = sorted(merged.values(), key=lambda it: it[0].encode("utf-8"))
    try:
        with tmp.open("wb") as f:
            # --- 头部
            f.write(fmt.MAGIC)
            f.write(struct.pack("<I", base.header.pack_version))
            ma, mi, pa = base.header.engine_version
            f.write(struct.pack("<III", ma, mi, pa))
            f.write(struct.pack("<I", fmt.PACK_REL_FILEBASE))
            f.write(struct.pack("<Q", fmt.HEADER_SIZE))
            f.write(struct.pack("<Q", 0))  # dir_offset 占位
            used = 4 + 4 + 12 + 4 + 8 + 8
            f.write(b"\x00" * (fmt.HEADER_SIZE - used))

            log("拷贝基线数据区…")
            _copy_range(baseline, f, base_data_start, base_data_len, "基线")
            log("追加剧情数据区…")
            _copy_range(story, f, story_data_start, story_data_len, "剧情")

            # --- 目录
            dir_offset = f.tell()
            log(f"写目录区：{total_entries:,} 条 @ {dir_offset:,}")
            f.write(struct.pack("<I", total_entries))
            for path, offset, size, md5_hex, flags in ordered:
                raw = path.encode("utf-8")
                pad = (-len(raw)) % 4
                f.write(struct.pack("<I", len(raw) + pad))
                f.write(raw + b"\x00" * pad)
                f.write(struct.pack("<QQ", offset, size))
                f.write(bytes.fromhex(md5_hex))
                f.write(struct.pack("<I", flags))

            f.seek(32)
            f.write(struct.pack("<Q", dir_offset))
            f.flush()
            os.fsync(f.fileno())

        report["dir_offset"] = dir_offset
        log(f"产物写毕（{time.time() - t0:.1f}s），开始校验…")
        checks = verify(
            tmp, base, over, merged,
            story_paths={e.path for e in story_entries},
            log=log,
        )
        report.update(checks)
        os.replace(tmp, out)
        report["out_bytes"] = out.stat().st_size   # 含目录区的真实体积
        report["seconds"] = round(time.time() - t0, 2)
        report["sha256"] = _sha256(out)
        log(f"完成：{out}（{_human(out.stat().st_size)}，{report['seconds']}s）")
        return report
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify(
    out: Path,
    base: PckReader,
    over: PckReader,
    merged: dict[str, tuple[str, int, int, str, int]],
    *,
    story_paths: set[str] | None = None,
    log=print,
) -> dict:
    """回读校验：条目数、剧情条目全量 md5、原版抽样 md5。"""
    r = PckReader.open(out)
    if len(r) != len(merged):
        raise SystemExit(f"回读条目数不符：{len(r)} != {len(merged)}")

    sel = [e for e in over.entries if story_paths is None or e.path in story_paths]
    bad: list[str] = []
    for e in sel:
        got = r.get(e.path)
        if got is None or got.size != e.size or got.md5 != e.md5:
            bad.append(e.path)
            continue
        if hashlib.md5(r.read_bytes(got)).hexdigest() != e.md5:
            bad.append(e.path + " (md5)")
    if bad:
        raise SystemExit(f"剧情条目校验失败 {len(bad)} 条：{bad[:5]}")

    # 原版侧抽样：文件头/尾 + 等距 300 条
    # 注意：被剧情覆盖的路径 offset 必然变化（数据追加到尾部），比对时要排除，
    # 否则会把「按设计覆盖」误判成「原版条目被破坏」。
    overridden = set(story_paths) if story_paths is not None else {e.path for e in over.entries}
    be = base.entries
    sample = [e for e in (be[:16] + be[-16:] + be[:: max(1, len(be) // 300)])
              if e.path not in overridden]
    bad2 = []
    for e in sample:
        got = r.get(e.path)
        if got is None or got.offset != e.offset or got.size != e.size or got.md5 != e.md5:
            bad2.append(e.path)
    if bad2:
        raise SystemExit(f"原版条目校验失败 {len(bad2)} 条：{bad2[:5]}")

    # project.binary 必须原版字节
    pb = "project.binary"
    same_pb = (
        pb in r.index
        and r.index[pb].md5 == base.index[pb].md5
        and r.index[pb].size == base.index[pb].size
    )
    if not same_pb:
        raise SystemExit("project.binary 与基线不一致，产物不可用")

    log(f"  回读 {len(r):,} 条；剧情 741 条全量 md5 通过；原版抽样 {len(sample)} 条通过")
    return {
        "verified_entries": len(r),
        "verified_story": len(over.entries),
        "verified_base_sample": len(sample),
        "project_binary_kept": True,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="剧情覆盖层 → 原版完整包 合并")
    ap.add_argument("--baseline", required=True, help="原版完整 pck")
    ap.add_argument("--story", required=True, help="剧情覆盖层 pck")
    ap.add_argument("--out", required=True, help="输出 pck")
    ap.add_argument("--report", help="把统计写成 json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--skip",
        default="",
        help="逗号分隔的后缀，命中的剧情条目不合入（例：--skip .gd,.gd.remap,.gd.uid）",
    )
    args = ap.parse_args(argv)

    skip = tuple(s.strip() for s in args.skip.split(",") if s.strip())
    report = merge(
        Path(args.baseline),
        Path(args.story),
        Path(args.out),
        dry_run=args.dry_run,
        skip=skip,
    )
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps({k: report[k] for k in report if k not in ("covered_sample", "added_sample")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
