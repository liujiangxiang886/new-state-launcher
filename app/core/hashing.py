"""哈希与指纹工具。

大文件（3 GB 级）一律分块流式计算，避免一次性读入内存。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

CHUNK = 8 * 1024 * 1024  # 8 MiB，兼顾吞吐与内存


def _blocks(fh, size: int) -> Iterator[bytes]:
    remaining = size
    while remaining > 0:
        chunk = fh.read(min(CHUNK, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        yield chunk


def sha256_file(path: str | Path, limit: int | None = None) -> str:
    """整文件 sha256；``limit`` 只算前 N 字节（用于「头部快照」指纹）。"""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        total = Path(path).stat().st_size if limit is None else min(limit, Path(path).stat().st_size)
        for block in _blocks(f, total):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def sha256_file_fast(path: str | Path) -> str:
    """优先使用大块读取的同义实现，保留语义清晰的两个入口。"""
    return sha256_file(path)


def fingerprint(path: str | Path) -> dict[str, object]:
    """轻量指纹：大小 + 头尾各 1 MiB 的 sha256。

    用于「不读全盘也能识别基线」的场景；正式校验仍走 :func:`sha256_file`。
    """
    p = Path(path)
    size = p.stat().st_size
    head_h = hashlib.sha256()
    tail_h = hashlib.sha256()
    with p.open("rb") as f:
        head_h.update(f.read(min(CHUNK, size)))
        if size > CHUNK:
            f.seek(max(0, size - CHUNK))
            tail_h.update(f.read(CHUNK))
    return {
        "size": size,
        "head_sha256": head_h.hexdigest(),
        "tail_sha256": tail_h.hexdigest(),
    }


def same_file(a: str | Path, b: str | Path) -> bool:
    """先比大小，再比 sha256；避免无谓的整文件读。"""
    pa, pb = Path(a), Path(b)
    if not pa.exists() or not pb.exists():
        return False
    if pa.stat().st_size != pb.stat().st_size:
        return False
    return sha256_file(pa) == sha256_file(pb)
