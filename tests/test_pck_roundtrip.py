"""PCK 写入/读取往返测试（不依赖引擎）。

运行::

    python -m pytest tests -q          # 若装了 pytest
    python tests/test_pck_roundtrip.py # 直接跑
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.pck import PckReader  # noqa: E402
from app.core.pck.writer import PckWriter, embed_pck, truncate_stub  # noqa: E402


def test_roundtrip(tmp: Path) -> None:
    dest = tmp / "pack_000.pck"
    w = PckWriter(dest, line="4.6")
    w.add_bytes("project.godot", b'config_version=5\n[application]\n\nconfig/name="t"\n')
    w.add_text("_compat/entry.tscn", "[gd_scene]\n")
    blob = bytes(range(256)) * 400  # 102 KB
    w.add_bytes("data/blob.bin", blob)
    w.add_remap("scripts/game.gd")
    res = w.commit()

    assert res.entry_count == 4, res.entry_count
    assert res.pack_version == 3
    assert dest.stat().st_size == res.size

    r = PckReader.open(dest)
    assert r.header.pack_version == 3
    assert r.header.engine_version == (4, 6, 0)
    assert r.header.file_base == 112
    assert len(r) == 4
    assert r.read_bytes("data/blob.bin") == blob
    assert r.get("data/blob.bin").md5 == hashlib.md5(blob).hexdigest()
    assert r.read_text("scripts/game.gd.remap") == '[remap]\n\npath="res://scripts/game.gd"\n'
    # 排序保证确定性
    paths = [e.path for e in r.entries]
    assert paths == sorted(paths, key=lambda s: s.encode()), paths
    print("  ✓ 往返一致，条目 4，偏移/排序/md5 全部匹配")


def test_roundtrip_46(tmp: Path) -> None:
    dest = tmp / "pack46.pck"
    w = PckWriter(dest, line="4.6")
    w.add_bytes("a.txt", b"hello")
    res = w.commit()
    r = PckReader.open(dest)
    assert res.pack_version == 3
    assert r.header.engine_version == (4, 6, 0)
    assert r.read_bytes("a.txt") == b"hello"
    print("  ✓ 4.6 往返一致（pack_version=3）")


def test_determinism(tmp: Path) -> None:
    a, b = tmp / "a.pck", tmp / "b.pck"
    for dest in (a, b):
        w = PckWriter(dest, line="4.6", verify=False)
        w.add_bytes("z.txt", b"1")
        w.add_bytes("a.txt", b"2")
        w.commit()
    assert a.read_bytes() == b.read_bytes(), "同输入未产出相同字节"
    print("  ✓ 确定性：两次构建字节完全一致")


def test_embed_and_truncate(tmp: Path) -> None:
    exe = tmp / "stub.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 4094)
    pck = tmp / "x.pck"
    w = PckWriter(pck, line="4.6")
    w.add_bytes("f.txt", b"payload" * 100)
    w.commit()

    embedded = tmp / "game.exe"
    embed_pck(exe, pck, embedded)
    r = PckReader.open(embedded)
    assert r.read_bytes("f.txt") == b"payload" * 100
    print(f"  ✓ 内嵌 exe 可解析（pck_start={r.header.pck_start}）")

    stub = tmp / "cut.exe"
    truncate_stub(exe, 2048, stub)
    assert stub.stat().st_size == 2048
    print("  ✓ 截断桩生成正常")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        print("PCK 往返测试：")
        test_roundtrip(tmp)
        test_roundtrip_46(tmp)
        test_determinism(tmp)
        test_embed_and_truncate(tmp)
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
