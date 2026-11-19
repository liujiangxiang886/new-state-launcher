r"""长路径安装回归（2026-11-11 跨会话交接问题）。

背景：扩展包里 ``payload/.godot/imported/*.ctex`` 带 32 位哈希，单文件名
99 字符；在 116 字符的深安装根下会把总路径顶到 Windows MAX_PATH(260)，
启动器误报「扩展包解压失败，文件可能损坏」。包本身 CRC 完好。

覆盖：
- 解压链走 ``\\?\`` 扩展长度路径，>300 字符也能完整落盘；
- 真损坏（BadZipFile）仍被识别，不被路径分支吞掉；
- zip slip 防护不因改造退化；
- ``extract_payload`` 清旧残留不能静默失败（着色器缓存同款坑）；
- 错误分类只把 WinError 206/123 / ENAMETOOLONG 归到路径问题。

注意：本机 venv 的 python.exe 不带 longPathAware，连 ``Path.mkdir()`` 造深目录
都会 WinError 206；所以测试自身也要用 ``win_long_path``，不能用普通 pathlib。
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from app.core.ext.apply import extract_payload
from app.core.fsutil import (
    ArchivePathTooLongError,
    UnsafeArchiveError,
    extract_zip_safe,
    is_longpath_error,
    remove_tree_long,
    win_long_path,
)

LONG_NAME = (
    "payload/.godot/imported/floating_cannon_unicorn_mall_252.png-"
    "406db931a8491bf1ab96fdb2b72e763c.ctex"
)
PAYLOAD = b"data" * 50


def _make_pkg(name: str = LONG_NAME, data: bytes = PAYLOAD) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


def _write(path: Path, data: bytes) -> None:
    long = win_long_path(path)
    os.makedirs(os.path.dirname(long), exist_ok=True)
    with open(long, "wb") as f:
        f.write(data)


def _read(path: Path) -> bytes:
    with open(win_long_path(path), "rb") as f:
        return f.read()


def _exists(path: Path) -> bool:
    return os.path.exists(win_long_path(path))


def _deep_root(tmp_path: Path) -> Path:
    """造一个足够深的根目录，保证加上长文件名后 >300 字符。"""
    root = tmp_path
    for chunk in ("a" * 60, "b" * 60, "c" * 60):
        root = root / chunk
    os.makedirs(win_long_path(root), exist_ok=True)
    return root


def _target(out: Path, name: str = LONG_NAME) -> Path:
    return out.joinpath(*PurePosixPath(name).parts)


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH 专属回归")
def test_longpath_install_ok(tmp_path: Path) -> None:
    deep = _deep_root(tmp_path)
    pkg = deep / "pkg.zip"
    _write(pkg, _make_pkg())
    out = deep / "out"
    assert len(str(_target(out))) > 300, "测试目录不够深，没复现长路径场景"
    try:
        assert extract_zip_safe(pkg, out) == 1
        assert _read(_target(out)) == PAYLOAD
    finally:
        remove_tree_long(deep)


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH 专属回归")
def test_extract_payload_clears_stale_deep_src(tmp_path: Path) -> None:
    """同目录重装必须清掉上一版残留，不能 rmtree 静默失败。"""
    deep = _deep_root(tmp_path)
    src = deep / "src"
    first = deep / "first.zip"
    second = deep / "second.zip"
    _write(first, _make_pkg("payload/old.gd", b"old"))
    _write(second, _make_pkg("payload/new.gd", b"new"))
    try:
        extract_payload(first, src)
        assert _read(src / "payload" / "old.gd") == b"old"
        extract_payload(second, src)
        assert _read(src / "payload" / "new.gd") == b"new"
        assert not _exists(src / "payload" / "old.gd"), "旧残留没清掉"
    finally:
        remove_tree_long(deep)


def test_truncated_zip_reported_as_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
    with pytest.raises(zipfile.BadZipFile):
        extract_zip_safe(bad, tmp_path / "out")


@pytest.mark.parametrize("name", ["../evil.gd", "/abs/evil.gd", "C:/evil.gd"])
def test_zip_slip_still_rejected(tmp_path: Path, name: str) -> None:
    pkg = tmp_path / "slip.zip"
    pkg.write_bytes(_make_pkg(name))
    with pytest.raises(UnsafeArchiveError):
        extract_zip_safe(pkg, tmp_path / "out")


def test_longpath_error_classification() -> None:
    too_long = OSError("filename too long")
    too_long.winerror = 206
    assert is_longpath_error(too_long)
    assert is_longpath_error(ArchivePathTooLongError("x"))

    bad_syntax = OSError("invalid name")
    bad_syntax.winerror = 123
    assert is_longpath_error(bad_syntax)

    assert not is_longpath_error(OSError("disk full"))
    assert not is_longpath_error(zipfile.BadZipFile("bad"))
    assert ArchivePathTooLongError("x").code == "E_ARCHIVE_PATH_TOO_LONG"


def test_win_long_path_is_idempotent() -> None:
    """已经带前缀的路径不能被 abspath 二次加工破坏。"""
    p = win_long_path(Path("."))
    assert win_long_path(p) == p
    if os.name == "nt":
        assert p.startswith("\\\\?\\")
