"""自定义版本描述 line.json 的一致性校验。

红线：只信声明不验证就会绕回「硬挂只会崩」。所以描述里每一项 method /
signal / hook / anchor，都必须在创作者提供的反解工程索引里查得到；
identity_hash 也必须等于闸门对文件算出的指纹。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import custom_lines as cl
from app.core import identity_gate as gate
from app.core import line_desc
from app.core.pck.writer import PckWriter

VERDICT = SimpleNamespace(
    identity_hash="a" * 64,
    engine_line="4.7",
    baseline=gate.BASELINE["id"],
    accepted=True,
)


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_body(dest: Path) -> Path:
    """造一个能通过闸门的最小改版本体（与专项测试同款）。"""
    def kv(key: str, value: str) -> bytes:
        vb = value.encode("utf-8")
        return key.encode() + b"\x20\x00\x00\x00" + __import__("struct").pack("<II", 4, len(vb)) + vb

    pb = kv("application/config/name", "旋风改版计划") + kv(
        "application/run/main_scene", "res://scenes/start_page.tscn")
    w = PckWriter(dest, line="4.7", engine_version=(4, 7, 2))
    w.add_bytes("project.binary", pb)
    for p in gate.CORE_SCRIPTS[:80]:
        w.add_text(p, "GDSC")
    for p in gate.CORE_DATA[:95]:
        w.add_text(p, "{}")
    for p in gate.LINE_MARKERS["4.7"]:
        w.add_text(p, "GDSC")
    w.add_text("scenes/start_page.tscn", "[gd_scene]\n")
    for i in range(1000):
        w.add_text(f"assets/fill_{i:05d}.txt", "x")
    w.commit()
    return dest


def _good_desc() -> dict:
    return {
        "schema": 1,
        "name": "旋风改版计划",
        "version": "1.0.0",
        "publisher": "改版作者",
        "identity": {
            "identity_hash": VERDICT.identity_hash,
            "engine_line": "4.7",
            "baseline": gate.BASELINE["id"],
        },
        "source_index": "sources/index.json",
        "capabilities": [
            {"id": "Game.combat_power", "kind": "method",
             "target": "scripts/game.gd", "symbol": "combat_power", "phase": "after"},
            {"id": "game.equipment_changed", "kind": "signal",
             "target": "scripts/game.gd", "symbol": "equipment_changed"},
            {"id": "overlay.mount", "kind": "resource"},
        ],
        "anchors": [
            {"id": "patch.power", "file": "scripts/game.gd", "anchor": "func combat_power("},
        ],
    }


def _good_index() -> dict:
    return {
        "files": ["scripts/game.gd", "scripts/equipment_panel.gd"],
        "symbols": {"scripts/game.gd": ["combat_power", "equipment_changed", "character_attributes"]},
        "anchors": {"scripts/game.gd": ["func combat_power(character):"]},
    }


def _seed(tmp_path: Path, desc: dict | None = None, index: dict | None = None):
    idx_dir = tmp_path / "sources"
    idx_dir.mkdir(parents=True, exist_ok=True)
    _write_json(idx_dir / "index.json", index if index is not None else _good_index())
    if desc is not None:
        _write_json(tmp_path / line_desc.DESC_FILENAME, desc)
    return _make_body(tmp_path / "body.pck")


def test_missing_desc_stays_probe_only(tmp_path: Path) -> None:
    body = _seed(tmp_path, None)
    tier, note = line_desc.try_upgrade(body, VERDICT)
    assert tier == line_desc.TIER_PROBE_ONLY
    assert "未提供" in note


def test_consistent_desc_upgrades_to_full(tmp_path: Path) -> None:
    body = _seed(tmp_path, _good_desc())
    report = line_desc.validate_desc(line_desc.load_desc(tmp_path / "line.json"),
                                    VERDICT, tmp_path)
    assert report.ok and report.tier == line_desc.TIER_FULL
    assert len(report.capabilities) == 3
    tier, note = line_desc.try_upgrade(body, VERDICT)
    assert tier == line_desc.TIER_FULL and "校验通过" in note


def test_wrong_identity_hash_is_rejected(tmp_path: Path) -> None:
    d = _good_desc()
    d["identity"]["identity_hash"] = "b" * 64
    _seed(tmp_path, d)
    r = line_desc.validate_desc(d, VERDICT, tmp_path)
    assert not r.ok
    assert any("身份指纹" in e for e in r.errors)


def test_declared_but_missing_symbol_is_rejected(tmp_path: Path) -> None:
    """填了漂亮的能力表、反解索引里却没有 → 必须降级并报因。"""
    d = _good_desc()
    d["capabilities"][0]["symbol"] = "not_existing_method"
    _seed(tmp_path, d)
    r = line_desc.validate_desc(d, VERDICT, tmp_path)
    assert not r.ok
    assert any("反解符号表" in e for e in r.errors)


def test_missing_target_file_is_rejected(tmp_path: Path) -> None:
    d = _good_desc()
    d["capabilities"][1]["target"] = "scripts/not_here.gd"
    _seed(tmp_path, d)
    r = line_desc.validate_desc(d, VERDICT, tmp_path)
    assert not r.ok
    assert any("不在反解索引的 files" in e for e in r.errors)


def test_missing_anchor_text_is_rejected(tmp_path: Path) -> None:
    d = _good_desc()
    d["anchors"][0]["anchor"] = "func totally_absent("
    _seed(tmp_path, d)
    r = line_desc.validate_desc(d, VERDICT, tmp_path)
    assert not r.ok
    assert any("锚点表" in e for e in r.errors)


def test_broken_desc_degrades_not_crashes(tmp_path: Path) -> None:
    body = _seed(tmp_path, None)
    (tmp_path / "line.json").write_text("{ not json", encoding="utf-8")
    tier, note = line_desc.try_upgrade(body, VERDICT)
    assert tier == line_desc.TIER_PROBE_ONLY and "描述不可用" in note


def test_absolute_source_index_is_refused(tmp_path: Path) -> None:
    d = _good_desc()
    d["source_index"] = "D:/whatever/index.json"
    _seed(tmp_path, d)
    r = line_desc.validate_desc(d, VERDICT, tmp_path)
    assert not r.ok
    assert any("相对路径" in e for e in r.errors)


def test_add_line_upgrades_when_desc_is_valid(tmp_path: Path, monkeypatch) -> None:
    """端到端：同目录有合格 line.json → 登记直接落在 full 档。"""
    body = _seed(tmp_path, _good_desc())
    verdict = gate.probe(body)
    d = _good_desc()
    d["identity"]["identity_hash"] = verdict.identity_hash
    _write_json(tmp_path / "line.json", d)

    cfg = {"engines": {"4.6": {"kind": "editor"}, "4.7": {"kind": "template"}}}
    line = cl.add_line(cfg, "旋风改版计划", str(body), kind=cl.KIND_PCK)
    assert line.access_tier == cl.TIER_FULL
    assert line.access_note.startswith("描述校验通过")
    assert line.identity_hash == verdict.identity_hash
    assert cfg["custom_lines"][0]["access_note"] == line.access_note


def test_add_line_stays_probe_only_when_desc_lies(tmp_path: Path) -> None:
    body = _seed(tmp_path, _good_desc())
    d = _good_desc()
    d["identity"]["identity_hash"] = "c" * 64  # 故意绑错
    _write_json(tmp_path / "line.json", d)

    cfg = {"engines": {"4.6": {"kind": "editor"}, "4.7": {"kind": "template"}}}
    line = cl.add_line(cfg, "冒名描述", str(body), kind=cl.KIND_PCK)
    assert line.access_tier == cl.TIER_PROBE_ONLY
    assert "身份指纹" in line.access_note
