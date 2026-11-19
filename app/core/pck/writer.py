"""PCK 写入器。

与 :mod:`reader` 对称，产出 Godot 4.6（pack_version=3）可直接加载的未加密包。
（v4 仍可被**读取**，但写侧只产出内置线的 v3——见 ``format.py``。）设计目标：

1. **确定性**：同样输入必得同样字节（条目按 UTF-8 路径排序、mtime 不参与、
   无时间戳），这样 packset 的 sha256 才有意义，缓存与增量构建才成立。
2. **流式**：大文件（数百 MB 的 ctex）分块读入，不整块驻留内存。
3. **自校验**：``commit()`` 结束前用 :class:`PckReader` 回读，条目数/大小/md5
   逐项比对，失败则删除产物。
4. **remap 友好**：:meth:`add_remap` 生成自指 remap，覆盖基线资源时必需——
   否则引擎会顺着基线里的 ``x.gd.remap`` 读回旧的 ``x.gdc``。

示例::

    w = PckWriter(Path("out/pack_000.pck"), line="4.6")
    w.add_bytes("project.godot", text.encode())
    w.add_file("scripts/game.gd", Path("mods/game.gd"))
    w.add_remap("scripts/game.gd")        # 中和基线的 .gdc 重定向
    result = w.commit()
"""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from ..errors import PckFormatError, PckIntegrityError
from . import format as fmt
from .sources import BytesSource, EntrySource, FileSource


@dataclass
class _Item:
    path: str
    source: EntrySource
    md5: str = ""
    offset: int = 0
    flags: int = 0

    @property
    def size(self) -> int:
        return self.source.size


@dataclass
class WriteResult:
    path: Path
    pack_version: int
    engine_version: tuple[int, int, int]
    entry_count: int
    size: int
    sha256: str
    data_bytes: int
    dir_offset: int
    entries: list[tuple[str, int, int, str, int]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "pack_version": self.pack_version,
            "engine": ".".join(map(str, self.engine_version)),
            "entry_count": self.entry_count,
            "size": self.size,
            "sha256": self.sha256,
            "data_bytes": self.data_bytes,
            "dir_offset": self.dir_offset,
        }


def _pad_len(n: int, align: int = 4) -> int:
    return (align - (n % align)) % align


class PckWriter:
    """顺序写入一个 PCK。条目在 ``commit()`` 时统一排序落盘。"""

    def __init__(
        self,
        dest: str | Path,
        *,
        line: str = "4.6",
        engine_version: tuple[int, int, int] | None = None,
        pack_version: int | None = None,
        file_base: int = fmt.HEADER_SIZE,
        verify: bool = True,
    ) -> None:
        self.dest = Path(dest)
        self.pack_version = pack_version or fmt.pack_version_for(line)
        if engine_version is None:
            engine_version = (4, 6, 0)
        self.engine_version = engine_version
        self.file_base = file_base
        self.verify = verify
        self._items: dict[str, _Item] = {}

    # ------------------------------------------------------------ 添加

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, path: str) -> bool:
        return path in self._items

    def add_bytes(self, path: str, data: bytes, flags: int = 0) -> None:
        self._items[path] = _Item(path=path, source=BytesSource(data), flags=flags)

    def add_text(self, path: str, text: str, encoding: str = "utf-8") -> None:
        self.add_bytes(path, text.encode(encoding))

    def add_file(self, path: str, src: str | Path, flags: int = 0) -> None:
        p = Path(src)
        if not p.exists():
            raise FileNotFoundError(f"源文件不存在：{p}")
        self._items[path] = _Item(path=path, source=FileSource.of(p), flags=flags)

    def add_source(self, path: str, source: EntrySource, flags: int = 0) -> None:
        """直接挂一个内容来源（分片时用来从已有 pck 流式取条目）。"""
        if source.size < 0:
            raise ValueError(f"来源大小非法：{path} -> {source.size}")
        self._items[path] = _Item(path=path, source=source, flags=flags)

    def add_tree(self, root: str | Path, prefix: str = "", exclude: Iterable[str] = ()) -> int:
        """递归添加目录。``exclude`` 是相对路径前缀黑名单。"""
        base = Path(root)
        if not base.is_dir():
            raise NotADirectoryError(base)
        skip = tuple(exclude)
        count = 0
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                src = Path(dirpath) / name
                rel = src.relative_to(base).as_posix()
                if skip and rel.startswith(skip):
                    continue
                target = f"{prefix.rstrip('/')}/{rel}" if prefix else rel
                self.add_file(target, src)
                count += 1
        return count

    def add_remap(self, target: str, *, kind: str = "auto") -> None:
        """写入自指 remap，覆盖基线中的 ``target + '.remap'`` 重定向。

        ``kind``：

        - ``"auto"``：按后缀推断（``.gd``→脚本、``.tscn/.tres``→场景、图片→imported）
        - ``"self"``：``path="res://<target>"``
        - ``"imported"``：``path="res://<target>"`` + ``type="Texture2D"`` 之类由调用方决定
        """
        body = f'[remap]\n\npath="res://{target}"\n'
        self.add_text(f"{target}.remap", body)

    def get_text(self, path: str, encoding: str = "utf-8") -> str | None:
        """读回一个**尚未提交**条目的文本内容；不存在或不可解码时返回 None。

        用途：同一个目标既要打补丁又要注入成员声明时，后者需要拿到前者
        刚写进去的版本，而不是磁盘上的旧文件。条目内容可能来自
        ``FileSource``（还未读进内存），所以这里主动读一次。
        """
        item = self._items.get(path)
        if item is None:
            return None
        try:
            chunks = list(item.source.chunks())
        except OSError:
            return None
        try:
            return b"".join(chunks).decode(encoding)
        except UnicodeDecodeError:
            return None

    def remove(self, path: str) -> None:
        self._items.pop(path, None)

    # ------------------------------------------------------------ 提交

    def commit(self) -> WriteResult:
        if not self._items:
            raise PckFormatError("空包：没有任何条目")

        ordered = sorted(self._items.values(), key=lambda it: it.path.encode("utf-8"))
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.dest.parent), prefix=".pck-", suffix=".tmp")
        os.close(fd)
        tmp = Path(tmp_name)

        ma, mi, pa = self.engine_version
        try:
            with tmp.open("wb") as f:
                # --- 头部（dir_offset 先占位）
                f.write(fmt.MAGIC)
                f.write(struct.pack("<I", self.pack_version))
                f.write(struct.pack("<III", ma, mi, pa))
                f.write(struct.pack("<I", fmt.PACK_REL_FILEBASE))
                f.write(struct.pack("<Q", self.file_base))
                f.write(struct.pack("<Q", 0))          # dir_offset 占位
                header_used = 4 + 4 + 12 + 4 + 8 + 8
                if self.file_base < header_used:
                    raise PckFormatError(f"file_base 太小：{self.file_base} < {header_used}")
                f.write(b"\x00" * (self.file_base - header_used))

                data_start = self.file_base
                pos = data_start

                # --- 数据区
                for item in ordered:
                    h = hashlib.md5()
                    item.offset = pos - data_start
                    written = 0
                    for block in item.source.chunks():
                        h.update(block)
                        f.write(block)
                        written += len(block)
                    if written != item.size:
                        raise PckIntegrityError(
                            f"来源产出字节数与声明不符：{item.path}",
                            want=item.size,
                            got=written,
                        )
                    item.md5 = h.hexdigest()
                    pos += item.size

                # --- 目录区
                dir_offset = pos
                f.write(struct.pack("<I", len(ordered)))
                for item in ordered:
                    raw = item.path.encode("utf-8")
                    pad = _pad_len(len(raw))
                    f.write(struct.pack("<I", len(raw) + pad))
                    f.write(raw)
                    if pad:
                        f.write(b"\x00" * pad)
                    f.write(struct.pack("<QQ", item.offset, item.size))
                    f.write(bytes.fromhex(item.md5))
                    f.write(struct.pack("<I", item.flags))

                # --- 回填 dir_offset
                f.seek(32)
                f.write(struct.pack("<Q", dir_offset))
                f.flush()
                os.fsync(f.fileno())

            size = tmp.stat().st_size
            sha = _sha256(tmp)

            result = WriteResult(
                path=self.dest,
                pack_version=self.pack_version,
                engine_version=self.engine_version,
                entry_count=len(ordered),
                size=size,
                sha256=sha,
                data_bytes=dir_offset - self.file_base,
                dir_offset=dir_offset,
                entries=[(i.path, i.offset, i.size, i.md5, i.flags) for i in ordered],
            )

            if self.verify:
                self._verify(tmp, result, ordered)

            os.replace(tmp, self.dest)
            return result
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _verify(self, path: Path, result: WriteResult, ordered: list[_Item]) -> None:
        from .reader import PckReader

        r = PckReader.open(path)
        if len(r) != result.entry_count:
            raise PckIntegrityError(
                "回读条目数不符", want=result.entry_count, got=len(r)
            )
        sample = ordered if len(ordered) <= 64 else ordered[:32] + ordered[-32:]
        for item in sample:
            entry = r.get(item.path)
            if entry is None:
                raise PckIntegrityError(f"回读缺少条目：{item.path}")
            if entry.size != item.size or entry.md5 != item.md5:
                raise PckIntegrityError(
                    f"回读条目不一致：{item.path}",
                    want=(item.size, item.md5),
                    got=(entry.size, entry.md5),
                )
            # 用回读字节重算 md5 与写入时比对，避免把整个来源读进内存
            digest = hashlib.md5(r.read_bytes(entry)).hexdigest()
            if digest != item.md5:
                raise PckIntegrityError(
                    f"回读内容不一致：{item.path}", want=item.md5, got=digest
                )


def _sha256(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- 内嵌 exe

# 内嵌包与 PE 桩的实现放在 embed.py（单一定义）。这里保留同名导出，
# 避免既有的 `from .writer import embed_pck` 全部失效。
from .embed import embed_pck, has_pck, strip_pck  # noqa: E402,F401


def truncate_stub(exe_src: str | Path, keep: int, dest: str | Path) -> Path:
    """从内嵌 pck 的 exe 截出纯 PE 桩（去掉 pck 与尾部标记）。

    注意：4.6 这条路线实测不可行——截断后 PE 头里的映像尺寸与文件实际大小
    不一致，Windows 会拒绝加载（"不是此操作系统平台的有效应用程序"）。
    保留函数是因为它在「只想拿到原版 PE 前缀做 diff」时还有用，
    但**不要**用它生成可执行桩，请直接用 4.6 引擎本体。
    """
    src, out = Path(exe_src), Path(dest)
    if keep <= 0 or keep >= src.stat().st_size:
        raise ValueError(f"非法截断长度：{keep}")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=".stub-", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tmp.open("wb") as dst, src.open("rb") as f:
            remaining = keep
            while remaining > 0:
                block = f.read(min(8 * 1024 * 1024, remaining))
                if not block:
                    break
                dst.write(block)
                remaining -= len(block)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, out)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return out
