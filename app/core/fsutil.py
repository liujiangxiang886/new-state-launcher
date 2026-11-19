"""文件系统小工具：原子写、安全解压、磁盘空间估算、Windows 长路径。

原则：
- 任何「覆盖已有文件」的动作都走 :func:`atomic_write_bytes`，先写临时文件再 ``os.replace``；
- 解压一律拒绝绝对路径、``..`` 与符号链接（防 zip slip）；
- Windows 下解压与目录清理一律走扩展长度路径（``\\\\?\\`` 前缀），绕开
  ``MAX_PATH``(260)。扩展包里 ``.godot/imported/*.ctex`` 这类带 32 位哈希的
  长文件名，在深安装目录里会把总路径顶到 260，普通写法会收到
  ``WinError 206`` 并被 Python 包成 ``FileNotFoundError``；
- 不在系统目录留任何临时文件。
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .errors import LauncherError


class UnsafeArchiveError(LauncherError):
    code = "E_UNSAFE_ARCHIVE"


class ArchivePathTooLongError(UnsafeArchiveError):
    """解压目标路径超过 Windows 限制，或路径形态不被扩展长度前缀接受。

    与「包损坏」分开报：换更浅的安装目录或升级启动器就能解决，
    不该让玩家去重新下载一个其实完好的包。
    """

    code = "E_ARCHIVE_PATH_TOO_LONG"


# ---------------------------------------------------------------- 长路径
#
# 为什么自己实现而不只用进程 manifest：
# PyInstaller 6.12 打出来的 exe 默认 manifest 已带 longPathAware（2026-11-11
# 从产物 exe 里提取确认），但那只保证「进程允许长路径」，不保证 Python 侧
# 的路径拼接形态正确；而且开发环境、旧构建、子进程都不一定带这个声明。
# 显式加前缀是双保险，也是下午修着色器缓存时验证过的做法。


def _expand_dos_short_names(path: str) -> str:
    """把 8.3 短名（``LIJIXI~1``）展开成长名。

    ``\\\\?\\`` 路径不做任何规范化，里面残留短名会直接报 ``WinError 123``。
    pytest 的临时目录、部分安装盘符都可能出现短名，所以入口必须展开。
    """
    if os.name != "nt":
        return path
    try:
        import ctypes
        from ctypes import wintypes

        fn = ctypes.windll.kernel32.GetLongPathNameW
        fn.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        fn.restype = wintypes.DWORD
        size = fn(path, None, 0)
        if not size:
            return path
        buf = ctypes.create_unicode_buffer(size)
        if fn(path, buf, size):
            return buf.value or path
    except Exception:  # noqa: BLE001 - 展开失败就按原路径试，不该先崩在这
        pass
    return path


def win_long_path(path: str | Path) -> str:
    """把路径转成 Windows 扩展长度形式，绕开 260 字符上限。

    约束（缺一不可）：绝对路径、全反斜杠、无 DOS 短名、无 ``..``。
    调用方 :func:`extract_zip_safe` 已由 :func:`_safe_member` 保证成员名安全。
    非 Windows 原样返回绝对路径，方便跨平台跑测试。
    """
    raw = str(path)
    if os.name != "nt":
        return os.path.abspath(os.path.expanduser(raw))
    # 已经是扩展路径就别再 abspath：ntpath.abspath 会破坏 \\?\ 前缀
    if raw.startswith("\\\\?\\"):
        return raw
    s = os.path.abspath(os.path.expanduser(raw))
    s = _expand_dos_short_names(s)
    if s.startswith("\\\\"):  # UNC 要换成 \\?\UNC\server\share
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def is_longpath_error(exc: BaseException) -> bool:
    """把「路径过长 / 路径形态非法」从一堆 OSError 里认出来。

    ``WinError 206`` = 文件名或扩展名太长；``WinError 123`` = 名称语法不正确
    （短名、正斜杠混入 ``\\\\?\\`` 路径时）；POSIX 上是 ``ENAMETOOLONG``。
    """
    if isinstance(exc, ArchivePathTooLongError):
        return True
    if not isinstance(exc, OSError):
        return False
    return (
        getattr(exc, "winerror", None) in (206, 123)
        or exc.errno == errno.ENAMETOOLONG
    )


def remove_tree_long(path: str | Path) -> bool:
    """删掉整棵目录（走长路径），返回是否确实不存在了。

    不用 ``shutil.rmtree(ignore_errors=True)``：超长路径下它会一个字节都不删
    还不抛错（着色器缓存那次「日志写已清理、实际没动」的老坑），而且某些
    环境会把 rmtree 重定向到回收站，``\\\\?\\`` 路径在那里会被判成网络路径。
    自己用 scandir/unlink/rmdir 走一遍，删没删掉由 ``os.path.exists`` 复核。
    """
    target = win_long_path(path)
    if not os.path.exists(target):
        return True
    if not os.path.isdir(target):
        try:
            os.unlink(target)
        except OSError:
            pass
        return not os.path.exists(target)

    stack = [target]
    visited: list[str] = []
    while stack:
        cur = stack.pop()
        visited.append(cur)
        try:
            with os.scandir(cur) as it:
                children = list(it)
        except OSError:
            continue
        for entry in children:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    os.unlink(entry.path)
            except OSError:
                pass
    for cur in reversed(visited):
        try:
            os.rmdir(cur)
        except OSError:
            pass
    return not os.path.exists(target)


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=p.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: str | Path, obj: Any, indent: int = 2) -> Path:
    payload = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=False)
    return atomic_write_text(path, payload + "\n")


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def quarantine_file(path: str | Path, *, suffix: str = "corrupt") -> Path | None:
    """把坏文件改名留档，返回新路径；失败返回 ``None``。

    为什么是「改名」而不是「删除」：坏掉的那份往往正是排障需要的现场
    （半截写入、磁盘坏道、被杀软截断都在里面留了痕）。删掉就再也查不出
    它当初是被谁写坏的。改成一个不会被程序再读到的名字即可。

    为什么带时间戳：同一个文件反复坏掉时，前一次的现场不能被后一次覆盖。
    """
    src = Path(path)
    if not src.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = src.with_name(f"{src.name}.{suffix}-{stamp}")
    index = 1
    while target.exists():
        target = src.with_name(f"{src.name}.{suffix}-{stamp}-{index}")
        index += 1
    try:
        src.rename(target)
    except OSError:
        return None
    return target


def copy_stream(src: str | Path, dst: str | Path, chunk: int = 8 * 1024 * 1024) -> int:
    """流式复制，返回字节数。目标先写临时文件再原子替换。"""
    sp, dp = Path(src), Path(dst)
    dp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dp.parent), prefix=".tmp-")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out, sp.open("rb") as inp:
            while True:
                block = inp.read(chunk)
                if not block:
                    break
                out.write(block)
                total += len(block)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return total


def _safe_member(name: str) -> PurePosixPath:
    """把压缩包内路径规范化，拒绝越界与绝对路径。"""
    clean = name.replace("\\", "/")
    if clean.startswith("/") or (len(clean) > 1 and clean[1] == ":"):
        raise UnsafeArchiveError(f"压缩包含绝对路径：{name!r}", member=name)
    parts = [p for p in PurePosixPath(clean).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchiveError(f"压缩包含路径穿越：{name!r}", member=name)
    if not parts:
        raise UnsafeArchiveError(f"压缩含空成员名：{name!r}", member=name)
    return PurePosixPath(*parts)


def iter_zip_members(zf: zipfile.ZipFile) -> Iterator[tuple[zipfile.ZipInfo, PurePosixPath]]:
    for info in zf.infolist():
        if info.is_dir():
            continue
        # 符号链接在 zip 里表现为外部属性高位
        mode = (info.external_attr >> 16) & 0xF000
        if mode == 0xA000:
            raise UnsafeArchiveError(f"压缩包含符号链接：{info.filename!r}", member=info.filename)
        yield info, _safe_member(info.filename)


def extract_zip_safe(zip_path: str | Path, dest: str | Path) -> int:
    """安全解压，返回文件数。已存在同名文件时覆盖。

    与标准库 ``extractall`` 的三点不同：

    1. 拒绝路径穿越 / 绝对路径 / 符号链接（zip slip 防护）；
    2. 逐文件走 ``\\\\?\\`` 扩展长度路径，支持总路径 > 260 字符；
    3. 路径类 OSError 转成 :class:`ArchivePathTooLongError`，让调用方
       能把「路径过长」和「包损坏」分开报。

    ``zipfile.BadZipFile`` 原样抛出——那是真的包坏了，别吞。
    """
    root = win_long_path(dest)
    os.makedirs(root, exist_ok=True)
    sep = "\\" if os.name == "nt" else os.sep
    count = 0
    # zip 包自身也可能躺在深目录里；打开时同样走长路径
    with zipfile.ZipFile(win_long_path(zip_path)) as zf:
        for info, rel in iter_zip_members(zf):
            # zip 内部用正斜杠；\\?\ 路径必须全反斜杠，否则 WinError 123
            target = root.rstrip(sep) + sep + sep.join(rel.parts)
            try:
                os.makedirs(target.rsplit(sep, 1)[0], exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=8 * 1024 * 1024)
            except OSError as exc:
                if is_longpath_error(exc):
                    raise ArchivePathTooLongError(
                        f"解压目标路径过长（{len(target)} 字符）：{info.filename!r}",
                        member=info.filename,
                        length=len(target),
                    ) from exc
                raise
            count += 1
    return count


def dir_size(path: str | Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def free_space(path: str | Path) -> int:
    return shutil.disk_usage(str(path)).free


def require_space(path: str | Path, need: int, factor: float = 1.0) -> None:
    """空间不足时抛错，避免构建到一半炸掉。"""
    need = int(need * factor)
    free = free_space(path)
    if free < need:
        raise LauncherError(
            f"磁盘空间不足：需要约 {need / 2**30:.1f} GiB，可用 {free / 2**30:.1f} GiB",
            need=need,
            free=free,
        )
