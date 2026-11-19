r"""自定义线能力协商：按 line.json 契约决定 mod 挂不挂，且不支持联机接入。

覆盖：
- 能力需求推断（纯覆盖层 / scripts.entry / method_hooks / 显式 requires）；
- 只有 full 档 + pck 形态才协商，exe 形态一律仅启动；
- 协商通过的 overlay 真的进 packset、mods.json 真的传给底座；
- 方法钩子无插桩层必须跳过并报因（不能「装载成功、钩子没人调用」）；
- 联机禁入三层：描述声明、扩展清单自述、overlay 正文里的协议痕迹。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import config as cfg_mod
from app.core import custom_lines as cl
from app.core import identity_gate as gate
from app.core import line_desc
from app.core import paths
from app.core import runner
from app.core.ext import enable as enable_mod
from app.core.ext import negotiate as neg
from app.core.pck.writer import PckWriter


def _project_binary(name: str) -> bytes:
    def kv(key: str, value: str) -> bytes:
        vb = value.encode("utf-8")
        return key.encode() + b"\x20\x00\x00\x00" + struct.pack("<II", 4, len(vb)) + vb
    return kv("application/config/name", name) + kv(
        "application/run/main_scene", "res://scenes/start_page.tscn")


def _make_body(dest: Path) -> Path:
    w = PckWriter(dest, line="4.6", engine_version=(4, 6, 0))
    w.add_bytes("project.binary", _project_binary("旋风改版计划"))
    for p in gate.CORE_SCRIPTS[:80]:
        w.add_text(p, "GDSC")
    for p in gate.CORE_DATA[:95]:
        w.add_text(p, "{}")
    for p in gate.LINE_MARKERS["4.6"]:
        w.add_text(p, "GDSC")
    w.add_text("scenes/start_page.tscn", "[gd_scene]\n")
    for i in range(1000):
        w.add_text(f"assets/fill_{i:05d}.txt", "x")
    w.commit()
    return dest


def _index() -> dict:
    return {
        "files": ["scripts/game.gd"],
        "symbols": {"scripts/game.gd": ["combat_power", "equipment_changed"]},
        "anchors": {"scripts/game.gd": ["func combat_power("]},
    }


def _desc(identity_hash: str, extra_caps: list | None = None) -> dict:
    return {
        "schema": 1,
        "name": "旋风改版计划",
        "version": "1.0.0",
        "identity": {
            "identity_hash": identity_hash,
            "engine_line": "4.6",
            "baseline": gate.BASELINE["id"],
        },
        "source_index": "sources/index.json",
        "capabilities": [
            {"id": "Game.combat_power", "kind": "method",
             "target": "scripts/game.gd", "symbol": "combat_power"},
            {"id": "overlay.mount", "kind": "resource"},
            *(extra_caps or []),
        ],
        "anchors": [],
    }


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


class _FakeService:
    items: list[dict] = []

    def __init__(self, cfg) -> None:
        pass

    def installed(self) -> list[dict]:
        return list(self.items)


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    body = _make_body(tmp_path / "body.pck")
    verdict = gate.probe(body)
    _write(tmp_path / "line.json", _desc(verdict.identity_hash))
    _write(tmp_path / "sources" / "index.json", _index())
    packs = tmp_path / "packs"
    # pytest 会保留失败轮次的 tmp_path，重跑时编号复用，目录可能已存在
    packs.mkdir(exist_ok=True)

    line = SimpleNamespace(
        id="custom:test1", name="旋风改版计划", kind=cl.KIND_PCK,
        path=str(body), access_tier=cl.TIER_FULL,
        identity_hash=verdict.identity_hash,
        identity_engine_line=verdict.engine_line,
        identity_baseline=verdict.baseline,
        engine_line="4.6", profile="unit_t", main_scene="", args="",
        exists=True,
        profile_name=lambda: "unit_t",
        main_scene_or_default=lambda: "res://scenes/start_page.tscn",
    )
    cfg = {"engines": {"4.6": {"kind": "template"}}, "profiles": {"redirect_appdata": True}}

    monkeypatch.setattr(paths, "PACKS", packs)
    monkeypatch.setattr(cfg_mod, "resolve_path", lambda p: Path(p) if p else None)
    monkeypatch.setattr(cfg_mod, "extension_mode", lambda c: "loose")
    monkeypatch.setattr(enable_mod, "ExtensionService", _FakeService)
    _FakeService.items = []

    def add_ext(ext_id: str, manifest: dict, script: str = "extends Node\n") -> None:
        d = packs / ext_id
        d.mkdir(parents=True, exist_ok=True)
        overlay = d / "overlay-46.pck"
        w = PckWriter(overlay, line="4.6", engine_version=(4, 6, 0))
        w.add_text("mods/x/main.gd", script)
        w.commit()
        _write(d / "manifest-46.v2.json", manifest)
        _FakeService.items.append({
            "id": ext_id, "enabled": True, "engine_line": "4.6",
            "overlay": str(overlay),
        })

    return SimpleNamespace(
        tmp=tmp_path, body=body, line=line, cfg=cfg, packs=packs,
        verdict=verdict, add_ext=add_ext,
    )


# ---------------------------------------------------------------- 能力推断

def test_needs_of_pure_overlay() -> None:
    assert neg.needs_of({"files": [{"source": "a.gd"}]}) == ["overlay.mount"]


def test_needs_of_script_entry_and_hooks() -> None:
    assert neg.needs_of({"scripts": {"entry": "res://mods/x/main.gd"}}) == [
        "overlay.mount", "modhost", "mods.json"]
    needs = neg.needs_of({"method_hooks": [
        {"target": "scripts/game.gd", "method": "combat_power"}]})
    assert "instrument:scripts/game.gd:combat_power" in needs


def test_explicit_requires_wins() -> None:
    assert neg.needs_of({"requires": ["Game.combat_power"], "scripts": {"entry": "x"}}) == [
        "Game.combat_power"]


def test_declared_capabilities() -> None:
    caps = neg.declared_capabilities({"capabilities": [
        {"id": "Game.combat_power", "kind": "method",
         "target": "scripts/game.gd", "symbol": "combat_power"},
        {"id": "overlay.mount", "kind": "resource"},
    ]})
    assert "method:scripts/game.gd:combat_power" in caps
    assert "overlay.mount" in caps
    assert "Game.combat_power" in caps


# ---------------------------------------------------------------- 分档边界

def test_probe_only_negotiates_nothing(world, monkeypatch) -> None:
    world.line.access_tier = cl.TIER_PROBE_ONLY
    r = neg.negotiate(world.cfg, world.line)
    assert not r.mounted_ids and not r.packs
    assert "仅启动" in r.warnings[0]


def test_exe_kind_negotiates_nothing(world) -> None:
    world.line.kind = cl.KIND_EXE
    r = neg.negotiate(world.cfg, world.line)
    assert not r.mounted_ids and not r.packs
    assert "exe 形态" in r.warnings[0]


# ---------------------------------------------------------------- 真实挂载

def test_pure_overlay_is_mounted(world) -> None:
    world.add_ext("demo.overlay", {"files": [{"source": "a.gd"}]})
    r = neg.negotiate(world.cfg, world.line)
    assert r.mounted_ids == ["demo.overlay"]
    assert len(r.packs) == 1 and r.packs[0]["role"] == "mod"
    assert r.mods == []
    mods_path, report_path = r.write_artifacts(world.tmp / "out")
    payload = json.loads(mods_path.read_text(encoding="utf-8"))
    assert payload["mods"] == [] and payload["hooks"] == []
    assert json.loads(report_path.read_text(encoding="utf-8"))["mounted"] == ["demo.overlay"]


def test_script_entry_is_mounted_and_exposed(world) -> None:
    world.add_ext("demo.script", {"scripts": {"entry": "res://mods/demo.script/main.gd"}})
    r = neg.negotiate(world.cfg, world.line)
    assert r.mounted_ids == ["demo.script"]
    assert r.mods == [{"id": "demo.script", "entry": "res://mods/demo.script/main.gd"}]
    mods_path, _ = r.write_artifacts(world.tmp / "out")
    assert json.loads(mods_path.read_text(encoding="utf-8"))["mods"] == r.mods


def test_method_hooks_are_skipped_with_reason(world) -> None:
    world.add_ext("demo.hook", {"method_hooks": [
        {"target": "scripts/game.gd", "method": "combat_power"}]})
    r = neg.negotiate(world.cfg, world.line)
    assert r.skipped_ids == ["demo.hook"] and not r.packs
    assert "插桩层" in r.decisions[0].reason


def test_missing_capability_is_skipped(world) -> None:
    world.add_ext("demo.needs", {"requires": ["Game.not_provided"]})
    r = neg.negotiate(world.cfg, world.line)
    assert r.skipped_ids == ["demo.needs"]
    assert "Game.not_provided" in r.decisions[0].reason


# ---------------------------------------------------------------- 联机禁入

@pytest.mark.parametrize("cap", [
    {"id": "net.p2p_connect", "kind": "method", "target": "scripts/game.gd", "symbol": "connect"},
    {"id": "ENetMultiplayerPeer", "kind": "resource"},
    {"id": "WebSocketMultiplayerPeer", "kind": "resource"},
    {"id": "联机大厅", "kind": "resource"},
])
def test_network_capability_in_desc_blocks_upgrade(world, cap) -> None:
    d = _desc(world.verdict.identity_hash, extra_caps=[cap])
    r = line_desc.validate_desc(d, world.verdict, world.tmp)
    assert not r.ok
    assert any("不支持联机接入" in e for e in r.errors)
    _write(world.tmp / "line.json", d)
    assert line_desc.try_upgrade(world.body, world.verdict)[0] == line_desc.TIER_PROBE_ONLY


def test_network_true_in_desc_is_rejected(world) -> None:
    d = _desc(world.verdict.identity_hash)
    d["network"] = True
    _write(world.tmp / "line.json", d)
    with pytest.raises(line_desc.LineDescError, match="不支持联机接入"):
        line_desc.load_desc(world.tmp / "line.json")
    assert line_desc.try_upgrade(world.body, world.verdict)[0] == line_desc.TIER_PROBE_ONLY


def test_manifest_declaring_online_is_skipped(world) -> None:
    world.add_ext("demo.online", {"capabilities": ["netplay"]})
    r = neg.negotiate(world.cfg, world.line)
    assert r.skipped_ids == ["demo.online"] and not r.packs
    assert "不支持联机" in r.decisions[0].reason or "联机模块" in r.decisions[0].reason


def test_overlay_protocol_code_is_skipped(world) -> None:
    world.add_ext("demo.proto", {"files": []},
                  script="var peer = ENetMultiplayerPeer.new()\npeer.create_server(7777)\n")
    r = neg.negotiate(world.cfg, world.line)
    assert r.skipped_ids == ["demo.proto"] and not r.packs
    assert "联机协议痕迹" in r.decisions[0].reason


def test_clean_desc_still_upgrades_after_network_rules(world) -> None:
    """加了联机规则后，正常描述不能被误杀。"""
    tier, note = line_desc.try_upgrade(world.body, world.verdict)
    assert tier == line_desc.TIER_FULL and "校验通过" in note


# ---------------------------------------------------------------- 启动链路

def test_prepare_launch_wires_packset_and_mods(world, monkeypatch) -> None:
    world.add_ext("demo.script", {"scripts": {"entry": "res://mods/demo.script/main.gd"}})
    monkeypatch.setattr(runner, "host_paths",
                        lambda engine: (world.tmp / "fake.exe", world.tmp / "fake.pck"))
    plan = cl.prepare_launch(world.cfg, world.line, world.tmp / "profiles")
    cmd = " ".join(plan.command)
    assert "--mods=" in cmd
    packset = json.loads(Path(plan.packset).read_text(encoding="utf-8"))
    entries = packset.get("packs") or []
    roles = [e.get("role") for e in entries]
    assert "baseline" in roles and "mod" in roles, f"packset 结构: {list(packset)}"
    assert any(e.get("id") == "demo.script" for e in entries)
    assert "能力协商" in " ".join(plan.warnings)
    out = world.tmp / "profiles" / "unit_t" / "custom"
    assert (out / "negotiation.json").is_file()
    assert (out / "mods.json").is_file()


def test_prepare_launch_exe_never_passes_mods(world) -> None:
    world.line.kind = cl.KIND_EXE
    plan = cl.prepare_launch(world.cfg, world.line, world.tmp / "profiles")
    assert "--mods=" not in " ".join(plan.command)
    assert plan.packset is None
