"""PCK 写入的条目来源抽象。

分片打包时内容几乎都来自「另一个 pck」，如果先解包到磁盘再打回去，会白写
一遍几 GB 的数据。这里用一个极小的协议把「字节从哪来」和「怎么写」解耦：

- :class:`BytesSource` —— 内存里的字节（小文件、生成的 remap）
- :class:`FileSource`  —— 磁盘文件（覆盖层、外部资源）
- :class:`PckSource`   —— 已有 pck 里的某个条目（流式读取，不解包）

三个实现都只暴露 ``size`` 与 ``chunks()``，``PckWriter`` 只依赖这一点。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

CHUNK = 8 * 1024 * 1024


@runtime_checkable
class EntrySource(Protocol):
    """条目内容来源。"""

    size: int

    def chunks(self) -> Iterator[bytes]:
        """按块产出内容；总和必须等于 ``size``。"""
        ...


@dataclass(frozen=True)
class BytesSource:
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    def chunks(self) -> Iterator[bytes]:
        yield self.data


@dataclass(frozen=True)
class FileSource:
    path: Path
    size: int

    @classmethod
    def of(cls, path: str | Path) -> "FileSource":
        p = Path(path)
        return cls(path=p, size=p.stat().st_size)

    def chunks(self) -> Iterator[bytes]:
        with self.path.open("rb") as f:
            while True:
                block = f.read(CHUNK)
                if not block:
                    break
                yield block


@dataclass(frozen=True)
class PckSource:
    """已有 pck 中的一个条目。``reader`` 需保持打开状态。"""

    reader: object          # PckReader（避免循环导入，用鸭子类型）
    entry: object           # PckEntry

    @property
    def size(self) -> int:
        return int(getattr(self.entry, "size"))

    def chunks(self) -> Iterator[bytes]:
        yield from self.reader.stream(self.entry, CHUNK)  # type: ignore[attr-defined]
