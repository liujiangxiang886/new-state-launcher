"""PCK 只读解析器。

用法::

    r = PckReader.open(r"D:\\...\\机甲旋风Demo.pck")
    print(r.header.summary())
    for e in r.iter_entries(prefix="scripts/"):
        print(e.path, e.size)

    data = r.read_bytes("scripts/game.gd")   # 精确路径
    r.extract_all(Path("D:/out"), match=lambda p: p.endswith(".json"))

设计要点：
- 目录区一次性读入（约 35 MiB），后续全内存查找，380k 条解析约 1 秒；
- 只读、不修改源文件；打开即校验头部，非法包立刻给出可读错误；
- 支持独立 ``.pck`` 与内嵌在 exe 尾部的包（``[PE][pck][u64 size]["GDPC"]``）。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from ..errors import PckEncryptedError, PckFormatError, PckIntegrityError, PckNotFoundError
from . import format as fmt


# ---------------------------------------------------------------- 数据类


@dataclass(frozen=True, slots=True)
class PckHeader:
    pack_version: int
    engine_version: tuple[int, int, int]
    pack_flags: int
    file_base: int
    dir_offset: int
    pck_start: int
    pck_size: int
    header_size: int

    @property
    def dir_encrypted(self) -> bool:
        return bool(self.pack_flags & fmt.PACK_DIR_ENCRYPTED)

    @property
    def rel_filebase(self) -> bool:
        return bool(self.pack_flags & fmt.PACK_REL_FILEBASE)

    @property
    def data_start(self) -> int:
        """数据区绝对起点（相对文件）。"""
        base = self.file_base or self.header_size
        return self.pck_start + base

    @property
    def engine_line(self) -> str | None:
        return fmt.engine_line_for(self.pack_version)

    def summary(self) -> dict[str, object]:
        ma, mi, pa = self.engine_version
        return {
            "pack_version": self.pack_version,
            "engine": f"{ma}.{mi}.{pa}",
            "engine_line": self.engine_line,
            "pack_flags": self.pack_flags,
            "dir_encrypted": self.dir_encrypted,
            "file_base": self.file_base,
            "dir_offset": self.dir_offset,
            "pck_start": self.pck_start,
            "pck_size": self.pck_size,
            "header_size": self.header_size,
        }


@dataclass(frozen=True, slots=True)
class PckEntry:
    path: str
    offset: int          # 相对数据区起点
    size: int
    md5: str             # 32 位十六进制
    flags: int

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & fmt.FILE_ENCRYPTED)

    @property
    def suffix(self) -> str:
        i = self.path.rfind(".")
        return self.path[i:].lower() if i >= 0 else ""

    def as_row(self) -> list[object]:
        """紧凑索引行，用于落盘 JSON。"""
        return [self.path, self.offset, self.size, self.md5, self.flags]


# ---------------------------------------------------------------- 定位


def locate_pck(path: str | Path) -> tuple[int, int]:
    """返回 ``(pck_start, pck_size)``。

    先看文件头是不是 ``GDPC``（独立 pck），再看尾部 12 字节标记（内嵌 exe）。
    """
    p = Path(path)
    if not p.exists():
        raise PckNotFoundError(f"文件不存在：{p}", path=str(p))
    total = p.stat().st_size
    if total < 16:
        raise PckFormatError(f"文件太小，不可能是 PCK/PE：{p}", size=total)

    with p.open("rb") as f:
        if f.read(4) == fmt.MAGIC:
            return 0, total
        if total > fmt.EMBED_TAIL_SIZE:
            f.seek(total - fmt.EMBED_TAIL_SIZE)
            tail = f.read(fmt.EMBED_TAIL_SIZE)
            size, magic = struct.unpack("<Q4s", tail)
            if magic == fmt.MAGIC and 0 < size <= total - fmt.EMBED_TAIL_SIZE:
                return total - fmt.EMBED_TAIL_SIZE - size, size

    raise PckNotFoundError(
        f"未找到 PCK：头部不是 GDPC，尾部也没有内嵌标记：{p}",
        path=str(p),
        size=total,
    )


# ---------------------------------------------------------------- 读取器


class PckReader:
    """只读 PCK。实例化即解析头部，``entries`` 惰性加载。"""

    def __init__(self, path: str | Path, pck_start: int | None = None, pck_size: int | None = None) -> None:
        self.path = Path(path)
        if pck_start is None or pck_size is None:
            pck_start, pck_size = locate_pck(self.path)
        self.header = self._read_header(pck_start, pck_size)
        self._entries: list[PckEntry] | None = None
        self._index: dict[str, PckEntry] | None = None

    # ------------------------------------------------------------ 构造

    @classmethod
    def open(cls, path: str | Path) -> "PckReader":
        return cls(path)

    @classmethod
    def from_exe(cls, path: str | Path) -> "PckReader":
        """显式按内嵌包打开；找不到就报错（不做独立 pck 回退）。"""
        start, size = locate_pck(path)
        if start == 0:
            raise PckNotFoundError(f"该文件是独立 pck，不是内嵌 exe：{path}", path=str(path))
        return cls(path, start, size)

    # ------------------------------------------------------------ 头部

    def _read_header(self, pck_start: int, pck_size: int) -> PckHeader:
        with self.path.open("rb") as f:
            f.seek(pck_start)
            head = f.read(fmt.HEADER_SIZE)
        if len(head) < 40:
            raise PckFormatError(f"头部不足 40 字节：{self.path}", read=len(head))
        if head[:4] != fmt.MAGIC:
            raise PckFormatError(f"magic 不是 GDPC：{head[:4]!r}", path=str(self.path))

        pack_version, ma, mi, pa, pack_flags = struct.unpack_from("<IIIII", head, 4)
        file_base, dir_offset = struct.unpack_from("<QQ", head, 24)

        if pack_version < 3:
            raise PckFormatError(
                f"pack_version={pack_version} 暂不支持（目标基线为 v3/v4，v1/v2 的目录区定位方式不同）",
                pack_version=pack_version,
                engine=f"{ma}.{mi}.{pa}",
            )
        if pack_flags & fmt.PACK_DIR_ENCRYPTED:
            raise PckEncryptedError(
                "目录区被加密（pack_flags bit0=1），无法列举条目",
                path=str(self.path),
                pack_flags=pack_flags,
            )
        if not (0 <= dir_offset < pck_size):
            raise PckFormatError(
                f"dir_offset 越界：{dir_offset} 不在 [0, {pck_size})",
                dir_offset=dir_offset,
                pck_size=pck_size,
            )

        header_size = file_base if file_base else fmt.HEADER_SIZE
        return PckHeader(
            pack_version=pack_version,
            engine_version=(ma, mi, pa),
            pack_flags=pack_flags,
            file_base=file_base,
            dir_offset=dir_offset,
            pck_start=pck_start,
            pck_size=pck_size,
            header_size=header_size,
        )

    # ------------------------------------------------------------ 目录

    def _load_entries(self) -> list[PckEntry]:
        if self._entries is not None:
            return self._entries

        h = self.header
        dir_pos = h.pck_start + h.dir_offset
        end = h.pck_start + h.pck_size
        length = end - dir_pos
        if length < 4:
            raise PckFormatError("目录区长度不足 4 字节", dir_pos=dir_pos, length=length)

        with self.path.open("rb") as f:
            f.seek(dir_pos)
            raw = f.read(length)
        if len(raw) != length:
            raise PckFormatError("目录区读取不完整（文件被截断？）", want=length, got=len(raw))

        count = struct.unpack_from("<I", raw, 0)[0]
        if count > 5_000_000:
            raise PckFormatError(f"条目数异常：{count}（疑似目录区错位）", count=count)

        entries: list[PckEntry] = []
        pos = 4
        total = len(raw)
        unpack_u32 = struct.Struct("<I").unpack_from
        unpack_fixed = fmt.ENTRY_FIXED.unpack_from

        for i in range(count):
            if pos + 4 > total:
                raise PckFormatError(f"目录在第 {i} 条处截断（缺 path_len）", count=count, index=i)
            (path_len,) = unpack_u32(raw, pos)
            pos += 4
            if path_len > 4096 or pos + path_len + fmt.ENTRY_FIXED.size > total:
                raise PckFormatError(
                    f"目录在第 {i} 条处结构非法：path_len={path_len}",
                    count=count,
                    index=i,
                    pos=pos,
                )
            raw_path = raw[pos : pos + path_len]
            pos += path_len
            offset, size, md5_raw, flags = unpack_fixed(raw, pos)
            pos += fmt.ENTRY_FIXED.size
            path = raw_path.rstrip(b"\x00").decode("utf-8", "replace")
            entries.append(PckEntry(path, offset, size, md5_raw.hex(), flags))

        self._entries = entries
        self._index = {e.path: e for e in entries}
        return entries

    @property
    def entries(self) -> list[PckEntry]:
        return self._load_entries()

    @property
    def index(self) -> dict[str, PckEntry]:
        self._load_entries()
        assert self._index is not None
        return self._index

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[PckEntry]:
        return iter(self.entries)

    def __contains__(self, path: str) -> bool:
        return path in self.index

    # ------------------------------------------------------------ 查找

    def get(self, path: str) -> PckEntry | None:
        return self.index.get(path)

    def iter_entries(
        self,
        prefix: str | None = None,
        suffix: str | None = None,
        contains: str | None = None,
    ) -> Iterator[PckEntry]:
        for e in self.entries:
            if prefix is not None and not e.path.startswith(prefix):
                continue
            if suffix is not None and not e.path.endswith(suffix):
                continue
            if contains is not None and contains not in e.path:
                continue
            yield e

    def search(self, pattern: str, limit: int = 50) -> list[PckEntry]:
        """子串匹配（大小写不敏感），用于诊断。"""
        low = pattern.lower()
        out: list[PckEntry] = []
        for e in self.entries:
            if low in e.path.lower():
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    def suffixes(self, top: int = 40) -> list[tuple[str, int]]:
        """按后缀统计条目数，快速看清包内组成。"""
        from collections import Counter

        c = Counter(e.suffix for e in self.entries)
        return c.most_common(top)

    # ------------------------------------------------------------ 读取

    def entry_abs_pos(self, entry: PckEntry) -> int:
        return self.header.data_start + entry.offset

    def read_bytes(self, target: str | PckEntry, verify: bool = False) -> bytes:
        entry = target if isinstance(target, PckEntry) else self.get(target)
        if entry is None:
            raise KeyError(f"包内不存在：{target!r}")
        if entry.encrypted:
            raise PckEncryptedError(f"条目被加密：{entry.path}", path=entry.path)
        with self.path.open("rb") as f:
            f.seek(self.entry_abs_pos(entry))
            data = f.read(entry.size)
        if len(data) != entry.size:
            raise PckIntegrityError(
                f"读取长度不符：{entry.path}", want=entry.size, got=len(data)
            )
        if verify and hashlib.md5(data).hexdigest() != entry.md5:
            raise PckIntegrityError(f"md5 不符：{entry.path}", path=entry.path)
        return data

    def read_text(self, target: str | PckEntry, encoding: str = "utf-8") -> str:
        return self.read_bytes(target).decode(encoding, "replace")

    def stream(self, entry: PckEntry, chunk: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        with self.path.open("rb") as f:
            f.seek(self.entry_abs_pos(entry))
            remaining = entry.size
            while remaining > 0:
                block = f.read(min(chunk, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    def verify(self, sample: int | None = None) -> dict[str, object]:
        """随机/抽样校验 md5；``sample=None`` 表示全量（3 GB 级会很慢）。"""
        targets = self.entries if sample is None else self.entries[:sample]
        bad: list[str] = []
        for e in targets:
            if e.encrypted:
                continue
            if hashlib.md5(self.read_bytes(e)).hexdigest() != e.md5:
                bad.append(e.path)
        return {"checked": len(targets), "bad": bad, "ok": not bad}

    def extract_all(
        self,
        dest: str | Path,
        match: Callable[[str], bool] | None = None,
        verify: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """按需解包到目录，保留包内相对路径。返回写出的文件数。"""
        root = Path(dest)
        chosen = [e for e in self.entries if (match is None or match(e.path))]
        written = 0
        for i, e in enumerate(chosen, 1):
            if e.encrypted:
                continue
            target = root / e.path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                for block in self.stream(e):
                    out.write(block)
            if verify and hashlib.md5(self.read_bytes(e)).hexdigest() != e.md5:
                raise PckIntegrityError(f"解包后 md5 不符：{e.path}", path=e.path)
            written += 1
            if on_progress is not None and (i % 500 == 0 or i == len(chosen)):
                on_progress(i, len(chosen))
        return written

    # ------------------------------------------------------------ 导出

    def to_index(self) -> dict[str, object]:
        """紧凑索引（供 baseline/index-*.json.gz 使用）。"""
        return {
            "schema": 1,
            "source": str(self.path),
            "header": self.header.summary(),
            "entry_count": len(self.entries),
            "entries": [e.as_row() for e in self.entries],
        }

    def to_json_bytes(self, indent: int | None = None) -> bytes:
        import json

        payload = self.to_index()
        return json.dumps(payload, ensure_ascii=False, indent=indent).encode("utf-8")

    def save_index(self, dest: str | Path) -> Path:
        """写 gzip 压缩索引；380k 条约 8–12 MiB。"""
        import gzip

        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with gzip.open(tmp, "wb", compresslevel=6) as f:
            f.write(self.to_json_bytes())
        tmp.replace(p)
        return p

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict[str, object]:
        entries = self.entries
        total = sum(e.size for e in entries)
        return {
            **self.header.summary(),
            "entry_count": len(entries),
            "total_bytes": total,
            "data_region_bytes": self.header.dir_offset - self.header.file_base
            if self.header.dir_offset > self.header.file_base
            else None,
            "encrypted_entries": sum(1 for e in entries if e.encrypted),
            "top_suffixes": self.suffixes(15),
        }
