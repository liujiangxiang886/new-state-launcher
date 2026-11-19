"""自定义基线导入验收（B0–B5）与登记失效机制。

用 monkeypatch 替掉「读 2.9GB 本体索引」这一步，单元测试只验规则；
真实本体的端到端验收由 `_custom_lab/demo_baseline/` 的实测脚本覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import custom_baseline as cb

HASH = "a" * 64
SCRIPT = "extends Node\n\nfunc combat_power():\n\treturn 1\n\nsignal equipment_changed\n"


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    root = tmp_path / "baseline"
    src = root / "sources" / "scripts"
    src.mkdir(parents=True)
    (src / "game.gd").write_text(SCRIPT, encoding="utf-8")
    (root / "index.json").write_text(json.dumps({
        "files": ["scripts/game.gd"],
        "symbols": {"scripts/game.gd": ["combat_power", "equipment_changed"]},
        "anchors": {"scripts/game.gd": ["func combat_power("]},
    }, ensure_ascii=False), encoding="utf-8")
    (root / "baseline.json").write_text(json.dumps({
        "schema": 1, "identity_hash": HASH, "engine_line": "4.7",
        "source_dir": "sources", "index": "index.json",
    }, ensure_ascii=False), encoding="utf-8")

    verdict = SimpleNamespace(identity_hash=HASH, engine_line="4.7")
    monkeypatch.setattr(cb, "_body_paths",
                        lambda p: ({"scripts/game.gd", "scripts/game.gdc"}, ""))
    monkeypatch.setattr(cb.paths, "CONFIG_DIR", tmp_path / "config")
    return SimpleNamespace(
        root=root, src=src, verdict=verdict, tmp=tmp_path, monkeypatch=monkeypatch,
    )


def _verify(w, **kw):
    return cb.verify(w.root, w.verdict, w.tmp / "game.pck", **kw)


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- B0–B5

def test_good_baseline_passes(world) -> None:
    r = _verify(world, required_scripts={"scripts/game.gd"})
    assert r.ok and not r.errors, r.errors
    assert r.body_match == 1.0
    assert r.scripts == ["scripts/game.gd"]
    assert r.digest


def test_wrong_identity_hash_rejected(world) -> None:
    meta = json.loads((world.root / "baseline.json").read_text(encoding="utf-8"))
    meta["identity_hash"] = "b" * 64
    _write_json(world.root / "baseline.json", meta)
    r = _verify(world)
    assert not r.ok and any("identity_hash" in e for e in r.errors)


def test_wrong_engine_line_rejected(world) -> None:
    meta = json.loads((world.root / "baseline.json").read_text(encoding="utf-8"))
    meta["engine_line"] = "4.6"
    _write_json(world.root / "baseline.json", meta)
    r = _verify(world)
    assert not r.ok and any("engine_line" in e for e in r.errors)


def test_missing_source_script_rejected(world) -> None:
    (world.src / "game.gd").unlink()
    r = _verify(world)
    assert not r.ok
    assert any("sources/" in e or "没有" in e for e in r.errors)


def test_hand_written_symbol_rejected(world) -> None:
    idx = json.loads((world.root / "index.json").read_text(encoding="utf-8"))
    idx["symbols"]["scripts/game.gd"].append("not_in_source")
    _write_json(world.root / "index.json", idx)
    r = _verify(world)
    assert not r.ok and any("not_in_source" in e for e in r.errors)


def test_hand_written_anchor_rejected(world) -> None:
    idx = json.loads((world.root / "index.json").read_text(encoding="utf-8"))
    idx["anchors"]["scripts/game.gd"] = ["func totally_absent("]
    _write_json(world.root / "index.json", idx)
    r = _verify(world)
    assert not r.ok and any("锚点" in e for e in r.errors)


def test_source_not_matching_body_rejected(world) -> None:
    world.monkeypatch.setattr(cb, "_body_paths", lambda p: ({"other/x.gdc"}, ""))
    r = _verify(world)
    assert not r.ok
    assert any("不是该改版" in e for e in r.errors)


def test_required_script_missing_rejected(world) -> None:
    r = _verify(world, required_scripts={"scripts/not_provided.gd"})
    assert not r.ok and any("没有交源码" in e for e in r.errors)


def test_network_protocol_in_source_rejected(world) -> None:
    (world.src / "game.gd").write_text(
        SCRIPT + "\nvar peer = ENetMultiplayerPeer.new()\n", encoding="utf-8")
    r = _verify(world)
    assert not r.ok and any("联机协议" in e for e in r.errors)


def test_bad_baseline_json_rejected(world) -> None:
    (world.root / "baseline.json").write_text("{ broken", encoding="utf-8")
    r = _verify(world)
    assert not r.ok and any("baseline.json" in e for e in r.errors)


# ---------------------------------------------------------------- 登记与失效

def test_register_then_status_ready(world) -> None:
    r = _verify(world)
    cb.register("custom:demo", world.root, r)
    st = cb.status("custom:demo", HASH)
    assert st["ready"] and st["scripts"] == 1
    assert cb.source_root_for("custom:demo", HASH) == (world.root / "sources").resolve()


def test_source_change_invalidates_registration(world) -> None:
    r = _verify(world)
    cb.register("custom:demo", world.root, r)
    (world.src / "game.gd").write_text(SCRIPT + "\n# touched\n", encoding="utf-8")
    st = cb.status("custom:demo", HASH)
    assert not st["ready"] and "源码内容已变" in st["reason"]


def test_body_change_invalidates_registration(world) -> None:
    r = _verify(world)
    cb.register("custom:demo", world.root, r)
    assert not cb.status("custom:demo", "f" * 64)["ready"]
    assert cb.source_root_for("custom:demo", "f" * 64) is None


def test_unregistered_line_is_not_ready(world) -> None:
    assert not cb.status("custom:never", HASH)["ready"]


# ---------------------------------------------------------------- CLI 注册

def test_cli_subcommand_registered() -> None:
    from app.cli import build_parser

    args = build_parser().parse_args(
        ["custom-baseline", "verify", "some/dir", "--line", "custom:x"])
    assert args.action == "verify" and args.path == "some/dir"
    assert args.line == "custom:x"
