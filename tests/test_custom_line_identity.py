r"""自定义线接入闸门：登记必须按内容自证「是 Godot 的机甲旋风改版」。

红线：创作者填的名字、能力清单都不参与身份判定。判定依据只有内置
``family_signature.py`` 的 1.45 正典特征库 + 包内实际路径。
所以「冒名机甲旋风」必须被拒，「改了工程名的真改版」必须放行。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from app.core import custom_lines as cl
from app.core import identity_gate as gate
from app.core.pck.writer import PckWriter


def _project_binary(name: str, scene: str = "res://scenes/start_page.tscn") -> bytes:
    """造最小 Godot 二进制 ConfigFile 片段（1.45 / 4.7 正典逐字节核对过）。"""

    def kv(key: str, value: str) -> bytes:
        vb = value.encode("utf-8")
        return key.encode() + b"\x20\x00\x00\x00" + struct.pack("<II", 4, len(vb)) + vb

    return kv("application/config/name", name) + kv("application/run/main_scene", scene)


def _make_body(dest: Path, *, name: str, line: str, engine: tuple,
               real: bool = True, filler: int = 1000) -> Path:
    w = PckWriter(dest, line=line, engine_version=engine)
    w.add_bytes("project.binary", _project_binary(name))
    if real:
        for p in gate.CORE_SCRIPTS[:80]:
            w.add_text(p, "GDSC")
        for p in gate.CORE_DATA[:95]:
            w.add_text(p, "{}")
        for p in gate.LINE_MARKERS[line]:
            w.add_text(p, "GDSC")
        w.add_text("scenes/start_page.tscn", "[gd_scene]\n")
    for i in range(filler):
        w.add_text(f"assets/fill_{i:05d}.txt", "x")
    w.commit()
    return dest


@pytest.fixture()
def cfg() -> dict:
    return {"engines": {"4.6": {"kind": "editor"}, "4.7": {"kind": "template"}}}


def test_real_rebrand_is_registered_with_identity(tmp_path: Path, cfg: dict) -> None:
    """内容对、名字改了 → 靠 1.45 核心指纹兜底放行，并落下身份指纹。"""
    pck = _make_body(tmp_path / "rebrand.pck", name="旋风改版计划",
                     line="4.7", engine=(4, 7, 2))
    v = gate.probe(pck)
    assert v.accepted and v.engine_line == "4.7"
    assert v.identity_hash

    line = cl.add_line(cfg, "旋风改版计划", str(pck), kind=cl.KIND_PCK)
    assert line.identity_hash == v.identity_hash
    assert line.identity_baseline == v.baseline
    assert line.access_tier == cl.TIER_PROBE_ONLY
    # 描述文件还没接，默认只给「只读探测」档
    stored = cfg["custom_lines"][0]
    assert stored["identity_hash"] == v.identity_hash
    assert stored["access_tier"] == cl.TIER_PROBE_ONLY


def test_spoofed_name_is_refused_and_not_registered(tmp_path: Path, cfg: dict) -> None:
    """只把工程名改成「机甲旋风」，内容全是别的 → 拒绝，且不写登记簿。"""
    pck = _make_body(tmp_path / "spoof.pck", name="机甲旋风 · 伪装版",
                     line="4.7", engine=(4, 7, 2), real=False)
    assert not gate.probe(pck).accepted
    with pytest.raises(ValueError, match="不能接入"):
        cl.add_line(cfg, "冒名混入", str(pck), kind=cl.KIND_PCK)
    assert not cfg.get("custom_lines")


def test_other_godot_game_is_refused(tmp_path: Path, cfg: dict) -> None:
    pck = tmp_path / "other.pck"
    w = PckWriter(pck, line="4.6", engine_version=(4, 6, 0))
    w.add_bytes("project.binary", _project_binary("别的游戏 · Demo"))
    for i in range(1200):
        w.add_text(f"other/fill_{i:05d}.txt", "x")
    w.commit()
    assert not gate.probe(pck).accepted
    with pytest.raises(ValueError):
        cl.add_line(cfg, "别的游戏", str(pck), kind=cl.KIND_PCK)


def test_overlay_is_refused_as_not_a_body(tmp_path: Path, cfg: dict) -> None:
    """扩展覆盖层是合法 Godot 包，但只有几条，不是本体。"""
    pck = tmp_path / "overlay.pck"
    w = PckWriter(pck, line="4.7", engine_version=(4, 7, 2))
    w.add_bytes("project.binary", _project_binary("机甲旋风 · 单机"))
    w.add_text("mods/demo/main.gd", "extends Node\n")
    w.commit()
    assert gate.probe(pck).kind == "overlay-not-body"
    with pytest.raises(ValueError):
        cl.add_line(cfg, "误当本体", str(pck), kind=cl.KIND_PCK)


def test_switching_file_refreshes_identity(tmp_path: Path, cfg: dict) -> None:
    a = _make_body(tmp_path / "a.pck", name="改版 A", line="4.6", engine=(4, 6, 0))
    b = _make_body(tmp_path / "b.pck", name="改版 B", line="4.6", engine=(4, 6, 0))
    line = cl.add_line(cfg, "A", str(a), kind=cl.KIND_PCK)
    old = line.identity_hash
    assert cl.update_line(cfg, line.id, path=str(b))
    refreshed = cl.get_line(cfg, line.id)
    assert refreshed is not None
    assert refreshed.identity_hash == gate.probe(b).identity_hash
    assert refreshed.identity_hash != old


def test_legacy_config_without_identity_fields_still_loads() -> None:
    """旧配置的 custom_lines 没有新字段，不能因此整条丢掉。"""
    cfg = {"custom_lines": [{
        "id": "custom:legacy1", "name": "老改版", "path": "D:/games/x.exe",
        "kind": "exe", "args": "--foo",
    }]}
    line = cl.load_all(cfg)[0]
    assert line.identity_hash == ""
    assert line.access_tier == ""
    assert "identity_hash" in line.as_dict()
