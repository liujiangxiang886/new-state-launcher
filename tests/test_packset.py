"""分片打包测试：规划规则、真实往返、跨片重复检测。

运行::

    python tests/test_packset.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.pck import packset as packset_mod  # noqa: E402
from app.core.pck.reader import PckReader  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402


def test_plan_groups_and_limit() -> None:
    entries = [
        ("a.png", 100),
        ("a.png.import", 10),
        ("a.png.remap", 5),
        ("b.bin", 200),
        ("c.txt", 50),
    ]
    plans, warnings = packset_mod.plan(entries, limit=150)
    assert len(plans) == 3, [p.as_dict() for p in plans]
    # a 的三个文件必须在同一片
    first_paths = {p for p, _s in plans[0].entries}
    assert {"a.png", "a.png.import", "a.png.remap"} <= first_paths, first_paths
    # b 单组超限 → 警告 + 独占
    assert any("b.bin" in w for w in warnings), warnings
    assert [p for p, _s in plans[1].entries] == ["b.bin"]
    assert [p for p, _s in plans[2].entries] == ["c.txt"]
    print("  ✓ 组不拆开、超限单组独占并告警")


def test_plan_deterministic() -> None:
    entries = [(f"d{i:03d}/f.bin", 10) for i in range(50)]
    a, _ = packset_mod.plan(entries, limit=100)
    b, _ = packset_mod.plan(list(reversed(entries)), limit=100)
    assert [p.as_dict() for p in a] == [p.as_dict() for p in b], "输入顺序影响分片，破坏确定性"
    print("  ✓ 输入顺序不影响分片结果")


def test_build_roundtrip(tmp: Path) -> None:
    src = tmp / "src.pck"
    w = PckWriter(src, line="4.6")
    payloads = {}
    for i in range(30):
        name = f"data/f{i:02d}.bin"
        blob = bytes([i % 256]) * (1000 + i * 37)
        payloads[name] = blob
        w.add_bytes(name, blob)
    # 加一组带 .import 的贴图，验证不会跨片
    w.add_bytes("art/u.png", b"PNGDATA" * 500)
    w.add_bytes("art/u.png.import", b"[remap]\n")
    w.commit()

    out = tmp / "shards"
    reader = PckReader.open(src)
    result = packset_mod.build([reader], out, line="4.6", limit=6000, verify=True)

    assert len(result.shards) > 1, f"期望切多片，实际 {len(result.shards)}"
    assert result.entry_count == len(payloads) + 2
    packset = json.loads(result.packset.read_text(encoding="utf-8"))
    assert packset["entry_count"] == result.entry_count
    assert len(packset["packs"]) == len(result.shards)

    # 逐片回读，内容必须与源一致
    merged: dict[str, bytes] = {}
    for pack in packset["packs"]:
        r = PckReader.open(pack["path"])
        for entry in r.entries:
            merged[entry.path] = r.read_bytes(entry)
    for name, blob in payloads.items():
        assert merged.get(name) == blob, f"内容不一致：{name}"
    assert "art/u.png" in merged and "art/u.png.import" in merged
    print(f"  ✓ 切成 {len(result.shards)} 片，{result.entry_count} 条内容逐条一致")


def test_duplicate_detection(tmp: Path) -> None:
    """两个分片含同一条目时，校验必须报错。"""
    a = tmp / "a.pck"
    w = PckWriter(a, line="4.6", verify=False)
    w.add_bytes("dup.txt", b"x")
    w.commit()
    b = tmp / "b.pck"
    w = PckWriter(b, line="4.6", verify=False)
    w.add_bytes("dup.txt", b"y")
    w.commit()

    payload = {
        "schema": 1,
        "entry_count": 2,
        "total_bytes": 2,
        "packs": [
            {"path": a.as_posix(), "entries": 1, "bytes": 1, "sha256": _sha(a)},
            {"path": b.as_posix(), "entries": 1, "bytes": 1, "sha256": _sha(b)},
        ],
    }
    meta = tmp / "packset.json"
    meta.write_text(json.dumps(payload), encoding="utf-8")
    report = packset_mod.verify_packset(meta)
    assert not report["ok"]
    assert any("跨片重复" in p for p in report["problems"]), report["problems"]
    print("  ✓ 跨片重复条目被拦住")


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        print("分片打包测试：")
        test_plan_groups_and_limit()
        test_plan_deterministic()
        test_build_roundtrip(tmp)
        test_duplicate_detection(tmp)
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
