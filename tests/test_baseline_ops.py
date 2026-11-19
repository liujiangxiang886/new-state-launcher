"""基线导入链：探测 → 按线校验 → 落配置 → 重建索引。

这里最容易出的事故是「导入成功下一秒就判文件缺失」——因为落配置只写了
``path``，而存在性判定看的是 ``size``。所以每条断言都盯住「写入的字段够不够
下游用」，而不是只测函数返回 True。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from app.core import baseline_ops, paths
from app.core.pck import PckReader
from app.core.pck.writer import PckWriter


def _pck(dest: Path, line: str = "4.6", entries: int = 2) -> Path:
    w = PckWriter(dest, line=line)
    for index in range(entries):
        w.add_text(f"data/f{index}.json", "{}")
    w.commit()
    return dest


def _probe(**kwargs):
    """造一个探测结果，只测 validate 时不碰磁盘。"""
    base = dict(
        path=Path("x.pck"),
        kind="pck",
        size=1024,
        pck_size=1024,
        pack_version=4,
        engine_version=(4, 7, 2),
        entry_count=100,
        sha256="",
    )
    base.update(kwargs)
    return baseline_ops.BaselineProbe(**base)


# ---------------------------------------------------------------- 探测


def test_probe_reads_header(tmp_path: Path) -> None:
    src = _pck(tmp_path / "pack.pck", line="4.6", entries=3)
    info = baseline_ops.probe(src)

    assert info.kind == "pck"
    assert info.pack_version == 3
    assert info.engine_line == "4.6"
    assert info.engine_version == (4, 6, 0)
    assert info.entry_count == 3
    assert info.size == src.stat().st_size
    assert info.sha256 == "", "默认不算哈希（3 GB 要几十秒）"


def test_probe_reads_v3_header(tmp_path: Path) -> None:
    info = baseline_ops.probe(_pck(tmp_path / "pack46.pck", line="4.6"))
    assert info.pack_version == 3
    assert info.engine_line == "4.6"


def test_probe_can_skip_entry_count(tmp_path: Path) -> None:
    """先「看看是什么」时只读头部，毫秒级返回。"""
    info = baseline_ops.probe(_pck(tmp_path / "pack.pck"), count_entries=False)
    assert info.entry_count == 0
    assert info.pack_version == 3


def test_probe_hashes_on_demand(tmp_path: Path) -> None:
    src = _pck(tmp_path / "pack.pck")
    info = baseline_ops.probe(src, sha256=True)
    assert len(info.sha256) == 64
    assert info.sha256 == baseline_ops.sha256_file(src)


def test_probe_rejects_unknown_file(tmp_path: Path) -> None:
    junk = tmp_path / "not-a-pck.exe"
    junk.write_bytes(b"MZ" + b"\x00" * 400)
    with pytest.raises(ValueError, match="既不是"):
        baseline_ops.probe(junk)


def test_probe_rejects_tiny_file(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.pck"
    tiny.write_bytes(b"GDPC")
    with pytest.raises(ValueError, match="太小"):
        baseline_ops.probe(tiny)


def test_probe_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        baseline_ops.probe(tmp_path / "nope.pck")


def test_probe_reports_progress(tmp_path: Path) -> None:
    seen: list[tuple[int, str]] = []
    baseline_ops.probe(_pck(tmp_path / "pack.pck"), progress=lambda p, m: seen.append((p, m)))
    assert seen and seen[0][0] == 60
    assert "已识别" in seen[0][1]


# ---------------------------------------------------------------- 校验 4.6


def test_validate_46_accepts_v3_pck_with_warning() -> None:
    issues = baseline_ops.validate(_probe(pack_version=3, kind="pck"), "4.6")

    assert baseline_ops.blocking(issues) == []
    assert [i.code for i in issues] == ["baseline.needs_exe"]
    assert issues[0].level == "warn"
    assert baseline_ops.can_adopt(_probe(pack_version=3), "4.6") is True


def test_validate_46_rejects_v4() -> None:
    issues = baseline_ops.validate(_probe(pack_version=4), "4.6")
    assert [i.code for i in baseline_ops.blocking(issues)] == ["baseline.pack_version"]
    assert "1.45" in baseline_ops.blocking(issues)[0].message
    assert baseline_ops.can_adopt(_probe(pack_version=4), "4.6") is False


# ---------------------------------------------------------------- 通用校验


def test_validate_rejects_empty_pack() -> None:
    # _probe() 默认 pack_version=3、kind=pck —— 对内置线只是个「需要配套 exe」的提示，
    # 真正阻断的是 0 条目。
    issues = baseline_ops.validate(_probe(entry_count=0), "4.6")
    assert "baseline.empty" in [i.code for i in baseline_ops.blocking(issues)]


def test_validate_unknown_line() -> None:
    issues = baseline_ops.validate(_probe(), "custom:1")
    assert issues[0].code == "baseline.line" and issues[0].level == "error"


# ---------------------------------------------------------------- 落配置


def _slot() -> dict:
    return {}


def test_adopt_writes_every_field_downstream_needs() -> None:
    p = _probe(path=Path("D:/g/pack.pck"), size=300, pck_size=280, entry_count=9, sha256="a" * 64)
    cfg = {"baselines": {}}

    slot = baseline_ops.adopt(cfg, "jjxf.v1.45", p)

    assert slot["kind"] == "pck"
    assert slot["size"] == 300
    assert slot["pck_size"] == 280
    assert slot["entry_count"] == 9
    assert slot["pack_version"] == _probe().pack_version
    assert slot["engine"] == "4.6"
    assert slot["sha256"] == "a" * 64
    assert "sha256_stale" not in slot
    assert slot["path"].endswith("pack.pck")
    # 同一个字典被就地改写，调用方 save 才有效
    assert cfg["baselines"]["jjxf.v1.45"] is slot


def test_adopt_keeps_old_hash_when_size_unchanged() -> None:
    """3 GB 重算一次 10–30 秒；大小没变就沿用旧哈希。"""
    cfg = {"baselines": {"jjxf.v1.45": {"size": 300, "sha256": "old"}}}
    slot = baseline_ops.adopt(cfg, "jjxf.v1.45", _probe(size=300, sha256=""))
    assert slot["sha256"] == "old"
    assert "sha256_stale" not in slot


def test_adopt_marks_hash_stale_when_file_changed() -> None:
    """换了文件还留着旧哈希，等于拿错指纹去校验。"""
    cfg = {"baselines": {"jjxf.v1.45": {"size": 100, "sha256": "old"}}}
    slot = baseline_ops.adopt(cfg, "jjxf.v1.45", _probe(size=300, sha256=""))
    assert slot["sha256"] == ""
    assert slot["sha256_stale"] is True


def test_adopt_records_sibling_exe_for_pck(tmp_path: Path) -> None:
    pck = _pck(tmp_path / "mchGame.pck")
    exe = tmp_path / "mchGame.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 64)
    cfg = {"baselines": {}}

    slot = baseline_ops.adopt(cfg, "jjxf.custom", baseline_ops.probe(pck))

    assert slot.get("exe_path"), "同目录同名 exe 要记下来，启动时不用再猜"
    assert slot["exe_size"] == exe.stat().st_size


def test_adopt_drops_exe_fields_for_exe_kind() -> None:
    cfg = {"baselines": {"jjxf.v1.45": {"exe_path": "old.exe", "exe_size": 1}}}
    slot = baseline_ops.adopt(cfg, "jjxf.v1.45", _probe(kind="exe", pack_version=3))
    assert "exe_path" not in slot and "exe_size" not in slot


def test_adopt_uses_explicit_sibling_exe(tmp_path: Path) -> None:
    pck = _pck(tmp_path / "a.pck")
    alt = tmp_path / "other.exe"
    alt.write_bytes(b"MZ" + b"\x00" * 32)

    slot = baseline_ops.adopt({"baselines": {}}, "jjxf.custom", baseline_ops.probe(pck), sibling_exe=alt)

    assert slot["exe_path"].endswith("other.exe")


# ---------------------------------------------------------------- 索引


def test_short_id() -> None:
    assert baseline_ops.short_id("jjxf.v1.45") == "1.45"
    assert baseline_ops.short_id("jjxf.v2.0") == "2.0"
    assert baseline_ops.short_id("custom:1") == "custom-1"


def test_write_index_is_readable_json(tmp_path: Path) -> None:
    src = _pck(tmp_path / "pack.pck", entries=5)
    dest = tmp_path / "index-1.45.json.gz"

    digest = baseline_ops.write_index(PckReader.open(src), dest)

    assert len(digest) == 64
    assert not dest.with_suffix(dest.suffix + ".tmp").exists(), "临时文件必须被 replace 掉"
    with gzip.open(dest, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["entry_count"] == 5
    assert len(data["entries"]) == 5
    assert data["header"]["pack_version"] == 3


def test_rebuild_index_writes_identity(tmp_path: Path, monkeypatch) -> None:
    src = _pck(tmp_path / "pack.pck", entries=2)
    target = tmp_path / "baseline"
    monkeypatch.setattr(paths, "BASELINE", target)
    monkeypatch.setattr(
        baseline_ops.cfg_mod,
        "load",
        lambda: {
            "baselines": {
                "jjxf.v1.45": {"path": str(src), "engine": "4.6", "label": "1.45"}
            }
        },
    )

    reports = baseline_ops.rebuild_index("4.6")

    assert len(reports) == 1
    report = reports[0]
    assert report.ok is True, report.errors
    assert report.entry_count == 2
    assert report.index_path.exists() and report.identity_path.exists()
    identity = json.loads(report.identity_path.read_text(encoding="utf-8"))
    assert identity["id"] == "jjxf.v1.45"
    assert identity["entry_count"] == 2
    assert identity["index_sha256"]
    assert identity["path"] == str(src)


def test_rebuild_index_reports_missing_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "BASELINE", tmp_path / "baseline")
    monkeypatch.setattr(
        baseline_ops.cfg_mod,
        "load",
        lambda: {"baselines": {"jjxf.v1.45": {"path": str(tmp_path / "gone.exe"), "engine": "4.6"}}},
    )

    reports = baseline_ops.rebuild_index("4.6")

    assert reports[0].ok is False
    assert "文件不存在" in reports[0].errors[0]


def test_line_of_baseline_matches_readiness() -> None:
    """映射表与 readiness 必须互逆，否则按基线反查线会错位。"""
    from app.core import config as cfg_mod
    from app.core import readiness

    assert baseline_ops.LINE_OF_BASELINE == {
        bid: line for line, bid in readiness.BASELINE_OF.items()
    }
    # 发布者版双线：4.6 仍是默认线，4.7 是可选的剧情线
    assert baseline_ops.LINE_OF_BASELINE == {
        bid: line for line, bid in cfg_mod.BASELINE_OF.items()
    }
    assert cfg_mod.BUILTIN_LINE == "4.6"
    assert baseline_ops.LINE_OF_BASELINE["jjxf.v4.7"] == "4.7"
