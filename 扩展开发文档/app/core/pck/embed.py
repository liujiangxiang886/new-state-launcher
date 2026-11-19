"""内嵌 PCK 的读写：``[PE stub][pck][u64 pck_size]["GDPC"]``。

Godot 的「单文件导出」就是这个形态：exe 尾部追加 pck，最后 12 字节是
``u64 size + "GDPC"``，引擎启动时按这个尾标反推 pck 起点。

这里只做两件事：

- :func:`embed_pck` 把一个 pck 追加到 exe 尾部（可原地，也可另存）；
- :func:`strip_pck` / :func:`has_pck` 判断并剥掉内嵌包。

实际读取内嵌包请用 :func:`app.core.pck.reader.locate_pck`——它已经处理了
「尾部标记对不上就退回全文件搜索」的边界情况。
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

from ..errors import PckFormatError

#: 尾部标记：u64 pck_size + b"GDPC"
TAIL = struct.Struct("<Q4s")
MAGIC = b"GDPC"
TAIL_SIZE = TAIL.size      # 12
HEADER_PROBE = 128


def has_pck(exe: str | Path) -> bool:
    """exe 尾部是否带内嵌 pck。"""
    path = Path(exe)
    if not path.is_file() or path.stat().st_size < HEADER_PROBE + TAIL_SIZE:
        return False
    with path.open("rb") as handle:
        handle.seek(-TAIL_SIZE, 2)
        size, magic = TAIL.unpack(handle.read(TAIL_SIZE))
    return magic == MAGIC and 0 < size <= path.stat().st_size - TAIL_SIZE


def embedded_size(exe: str | Path) -> int:
    """内嵌 pck 的字节数；没有则 0。"""
    path = Path(exe)
    if not has_pck(path):
        return 0
    with path.open("rb") as handle:
        handle.seek(-TAIL_SIZE, 2)
        size, _ = TAIL.unpack(handle.read(TAIL_SIZE))
    return int(size)


def embed_pck(exe: str | Path, pck: str | Path, out: str | Path | None = None) -> Path:
    """把 ``pck`` 追加到 ``exe`` 尾部，返回产物路径。

    ``out`` 省略时原地改写（先写临时文件再替换，避免中途失败留下半个 exe）。
    已有内嵌包时先剥掉，保证不会叠两层——叠了引擎只会读到最外层。
    """
    exe_path = Path(exe)
    pck_path = Path(pck)
    if not exe_path.is_file():
        raise PckFormatError(f"stub 不存在：{exe_path}", path=str(exe_path))
    if not pck_path.is_file():
        raise PckFormatError(f"pck 不存在：{pck_path}", path=str(pck_path))
    with pck_path.open("rb") as handle:
        if handle.read(4) != MAGIC:
            raise PckFormatError(f"不是 pck：{pck_path}", path=str(pck_path))

    target = Path(out) if out else exe_path
    if target.resolve() == exe_path.resolve():
        tmp = exe_path.with_suffix(exe_path.suffix + ".embedding")
    else:
        tmp = target.with_suffix(target.suffix + ".embedding")
    tmp.parent.mkdir(parents=True, exist_ok=True)

    base = strip_pck(exe_path) if has_pck(exe_path) else exe_path
    size = pck_path.stat().st_size
    with tmp.open("wb") as dst, base.open("rb") as stub, pck_path.open("rb") as payload:
        shutil.copyfileobj(stub, dst, 8 * 1024 * 1024)
        shutil.copyfileobj(payload, dst, 8 * 1024 * 1024)
        dst.write(TAIL.pack(size, MAGIC))

    # 校验：尾部标记与文件长度必须自洽，否则 Windows 会拒绝加载
    actual = tmp.stat().st_size
    expected = base.stat().st_size + size + TAIL_SIZE
    if actual != expected:
        tmp.unlink(missing_ok=True)
        raise PckFormatError(
            f"内嵌结果长度不符：{actual} != {expected}", path=str(tmp)
        )
    tmp.replace(target)
    return target


def strip_pck(exe: str | Path, out: str | Path | None = None) -> Path:
    """剥掉内嵌 pck，返回只有 stub 的文件。

    没有内嵌包时直接返回原文件（不复制），便于调用方统一处理。
    """
    path = Path(exe)
    if not has_pck(path):
        return path
    size = embedded_size(path)
    keep = path.stat().st_size - size - TAIL_SIZE
    target = Path(out) if out else path
    tmp = target.with_suffix(target.suffix + ".striping")
    with tmp.open("wb") as dst, path.open("rb") as src:
        remaining = keep
        while remaining > 0:
            block = src.read(min(8 * 1024 * 1024, remaining))
            if not block:
                break
            dst.write(block)
            remaining -= len(block)
    tmp.replace(target)
    return target