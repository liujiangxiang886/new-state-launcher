"""分片打包：把「原版包 + 覆盖层」重新打成一组带校验的 pck。

为什么要分片：单个 pck 超过 2 GiB 时，某些分发渠道 / 文件系统 / 上传工具会
出问题；切成 ≤1.8 GiB 后每片都能独立校验、独立重传。运行时按 ``packset.json``
的顺序挂载，后挂的覆盖先挂的，语义与单片完全一致。

**不可拆组**：导出包里一张贴图由三个文件共同描述——``x.png``、``x.png.import``、
``.godot/imported/x.png-<md5>.ctex``。把其中任意一个切到下一片，就会出现
「贴图存在但导入信息在另一片」的悬空引用。因此规划时以「去掉 .import/.remap
后缀的主干」为组，整组一起进同一片。

用法::

    r = PckReader.open(baseline)
    build([r], Path("packs/v46"), line="4.6", limit=1.8 * 2**30)
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..errors import PckFormatError, PckIntegrityError
from ..fsutil import atomic_write_json
from .reader import PckEntry, PckReader
from .sources import PckSource
from .writer import PckWriter

#: 默认分片上限 1.8 GiB（给文件系统与上传留余量）
DEFAULT_LIMIT = 1932735283

Progress = Callable[[int, int, str], None]

#: 依附于主资源的后缀——必须与主资源同片
DEPENDENT_SUFFIXES = (".import", ".remap")


def group_key(path: str) -> str:
    """同组的主干路径。``a/b.png.import`` 与 ``a/b.png`` 归为 ``a/b.png``。"""
    for suffix in DEPENDENT_SUFFIXES:
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return path


@dataclass
class ShardPlan:
    index: int
    entries: list[tuple[str, int]] = field(default_factory=list)

    @property
    def byte_size(self) -> int:
        return sum(size for _p, size in self.entries)

    def as_dict(self) -> dict[str, object]:
        return {"index": self.index, "entries": len(self.entries), "bytes": self.byte_size}


def plan(entries: Iterable[tuple[str, int]], limit: int) -> tuple[list[ShardPlan], list[str]]:
    """按「组」切分。返回 (分片计划, 警告)。

    规则：
    - 条目按路径排序，保证同样输入得到同样分片（sha256 才有意义）；
    - 组内文件永远同片；
    - 单组超过上限时独占一片并记警告（总比拆散导致资源悬空好）。
    """
    if limit <= 0:
        raise ValueError(f"分片上限必须为正数：{limit}")

    ordered = sorted(entries, key=lambda item: item[0].encode("utf-8"))
    groups: dict[str, list[tuple[str, int]]] = {}
    for path, size in ordered:
        groups.setdefault(group_key(path), []).append((path, size))

    shards: list[ShardPlan] = []
    warnings: list[str] = []
    current = ShardPlan(index=0)
    current_bytes = 0

    for key, members in groups.items():
        group_bytes = sum(size for _p, size in members)
        if group_bytes > limit:
            warnings.append(
                f"组 {key} 共 {group_bytes:,} 字节，超过上限 {limit:,}，独占一片"
            )
        if current.entries and current_bytes + group_bytes > limit:
            shards.append(current)
            current = ShardPlan(index=len(shards))
            current_bytes = 0
        current.entries.extend(members)
        current_bytes += group_bytes

    if current.entries:
        shards.append(current)
    return shards, warnings


@dataclass
class ShardResult:
    index: int
    path: Path
    entries: int
    bytes: int
    sha256: str
    size: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path.as_posix(),
            "offset": 0,
            "entries": self.entries,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass
class PacksetResult:
    packset: Path
    shards: list[ShardResult] = field(default_factory=list)
    entry_count: int = 0
    total_bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "packset": self.packset.as_posix(),
            "shards": [s.as_dict() for s in self.shards],
            "entry_count": self.entry_count,
            "total_bytes": self.total_bytes,
            "warnings": self.warnings,
            "seconds": round(self.seconds, 1),
        }


def _merged_entries(
    readers: Sequence[PckReader], match: str | None
) -> dict[str, tuple[PckReader, PckEntry]]:
    """把多个包的条目合并成一个视图，靠后的覆盖靠前的（与挂载语义一致）。"""
    merged: dict[str, tuple[PckReader, PckEntry]] = {}
    for reader in readers:
        for entry in reader.entries:
            if match and match not in entry.path:
                continue
            merged[entry.path] = (reader, entry)
    return merged


def build(
    readers: Sequence[PckReader],
    out_dir: str | Path,
    *,
    line: str,
    limit: int = DEFAULT_LIMIT,
    packset_id: str | None = None,
    match: str | None = None,
    verify: bool = True,
    progress: Progress | None = None,
) -> PacksetResult:
    """执行分片打包。``readers`` 靠后的覆盖靠前的。"""
    if not readers:
        raise ValueError("至少需要一个来源包")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    started = time.time()
    merged = _merged_entries(readers, match)
    if not merged:
        raise PckFormatError("过滤后没有条目可打包", match=match)

    plans, warnings = plan(((p, e.size) for p, (_r, e) in merged.items()), limit)
    total_entries = sum(len(p.entries) for p in plans)
    total_bytes = sum(p.byte_size for p in plans)

    results: list[ShardResult] = []
    for plan_item in plans:
        target = out / f"pack_{plan_item.index:03d}.pck"
        writer = PckWriter(target, line=line, verify=False)
        for path, _size in plan_item.entries:
            reader, entry = merged[path]
            writer.add_source(path, PckSource(reader, entry))
        written = writer.commit()
        results.append(
            ShardResult(
                index=plan_item.index,
                path=target,
                entries=written.entry_count,
                bytes=written.data_bytes,
                sha256=written.sha256,
                size=written.size,
            )
        )
        if progress:
            progress(plan_item.index + 1, len(plans), target.name)

    payload = {
        "schema": 1,
        "id": packset_id or f"{line}-shards",
        "line": line,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "shard_limit": limit,
        "entry_count": total_entries,
        "total_bytes": total_bytes,
        "packs": [s.as_dict() for s in results],
    }
    packset_path = atomic_write_json(out / "packset.json", payload)

    result = PacksetResult(
        packset=packset_path,
        shards=results,
        entry_count=total_entries,
        total_bytes=total_bytes,
        warnings=warnings,
        seconds=time.time() - started,
    )
    if verify:
        report = verify_packset(packset_path, merged=merged)
        result.warnings.extend(report["problems"])
        if report["problems"]:
            raise PckIntegrityError(
                "分片校验未通过：" + "；".join(report["problems"][:3]),
                packset=str(packset_path),
            )
    return result


def verify_packset(
    packset_path: str | Path,
    merged: dict[str, tuple[PckReader, PckEntry]] | None = None,
) -> dict[str, object]:
    """校验分片：条目数、字节数、跨片重复、以及（可选）与源条目 md5 一致。"""
    data = json.loads(Path(packset_path).read_text(encoding="utf-8"))
    problems: list[str] = []
    seen: dict[str, str] = {}
    total_entries = 0
    total_bytes = 0

    for pack in data.get("packs", []):
        path = Path(pack["path"])
        if not path.exists():
            problems.append(f"分片缺失：{path.name}")
            continue
        reader = PckReader.open(path)
        expected_entries = int(pack.get("entries", -1))
        if len(reader) != expected_entries:
            problems.append(f"{path.name} 条目数不符：{len(reader)} != {expected_entries}")
        actual_bytes = sum(e.size for e in reader.entries)
        if actual_bytes != int(pack.get("bytes", -1)):
            problems.append(f"{path.name} 字节数不符：{actual_bytes} != {pack.get('bytes')}")
        sha = _sha256_file(path)
        if sha != pack.get("sha256"):
            problems.append(f"{path.name} sha256 与记录不符")
        for entry in reader.entries:
            if entry.path in seen:
                problems.append(f"跨片重复条目：{entry.path}（{seen[entry.path]} / {path.name}）")
            seen[entry.path] = path.name
        total_entries += len(reader)
        total_bytes += actual_bytes

        if merged is not None:
            for entry in reader.entries[:50]:
                source = merged.get(entry.path)
                if source is None:
                    problems.append(f"{path.name} 出现源里没有的条目：{entry.path}")
                elif source[1].md5 != entry.md5:
                    problems.append(f"{path.name} 条目 md5 与源不符：{entry.path}")

    if total_entries != int(data.get("entry_count", -1)):
        problems.append(f"总条目数不符：{total_entries} != {data.get('entry_count')}")
    if total_bytes != int(data.get("total_bytes", -1)):
        problems.append(f"总字节数不符：{total_bytes} != {data.get('total_bytes')}")

    return {
        "ok": not problems,
        "problems": problems,
        "entry_count": total_entries,
        "total_bytes": total_bytes,
        "shards": len(data.get("packs", [])),
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(8 * 1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
