"""联机闸门的回归测试。

锁两件事：
1. 协议层规则真的能拦住 P2P，但不会因为"联机"两个字误杀正常扩展；
2. `tools/jjm_pack.py` 的规则表与运行时 `app/core/ext/netguard.py` 不漂移。
"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from app.core.ext import netguard

ROOT = Path(__file__).resolve().parents[1]


def _make_pkg(tmp_path: Path, files: dict[str, str], manifest: dict | None = None) -> Path:
    """按给定文件构造一个扩展 zip。"""
    data = {
        "schema": 2,
        "id": "test.pkg",
        "name": "测试扩展",
        "version": "1.0.0",
        "kind": "extension",
        "baseline": {"id": "jjxf.v1.45", "engine": {"line": "4.6"}},
    }
    data.update(manifest or {})
    pkg = tmp_path / "pkg.zip"
    with zipfile.ZipFile(pkg, "w") as zf:
        zf.writestr("extension.json", json.dumps(data, ensure_ascii=False))
        for name, text in files.items():
            zf.writestr(name, text)
    return pkg


def test_blocks_enet_p2p(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        {"mods/x/main.gd": "var peer = ENetMultiplayerPeer.new()\npeer.create_server(7777)\n"},
    )
    report = netguard.scan_package(pkg)
    assert report.blocked
    assert any("ENet" in hit.reason for hit in report.blocks)
    # 给玩家的话里不能出现 Python 异常或内部类名
    assert "BaseContent" not in report.message()
    assert "联机" in report.message()


def test_blocks_udp_and_address(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, {"mods/x/net.gd": "var p = PacketPeerUDP.new()\n# 1.2.3.4:9000\n"})
    assert netguard.scan_package(pkg).blocked


def test_blocks_declared_capability(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, {}, {"capabilities": ["p2p"]})
    assert netguard.scan_package(pkg).blocked


def test_text_only_warning_not_blocked(tmp_path: Path) -> None:
    """README 里写"联机"不算能力。"""
    pkg = _make_pkg(tmp_path, {"README.md": "本扩展不支持联机，也不提供房间功能。\n"})
    report = netguard.scan_package(pkg)
    assert not report.blocked
    assert report.warnings


def test_rpc_annotation_not_blocked(tmp_path: Path) -> None:
    """Godot 里没有对等端时 RPC 不生效，单机脚本可能预留注解。"""
    pkg = _make_pkg(tmp_path, {"mods/x/main.gd": "@rpc\nfunc sync_state() -> void:\n\tpass\n"})
    report = netguard.scan_package(pkg)
    assert not report.blocked
    assert report.warnings


def test_scans_patch_payload(tmp_path: Path) -> None:
    """联机代码藏在 text_patches 的 replace 里，也要拦。"""
    pkg = _make_pkg(
        tmp_path,
        {},
        {
            "text_patches": [
                {
                    "target": "scripts/game.gd",
                    "find": "pass",
                    "replace": "var peer = WebSocketMultiplayerPeer.new()",
                }
            ]
        },
    )
    assert netguard.scan_package(pkg).blocked


def _load_tool():
    spec = importlib.util.spec_from_file_location("jjm_pack", ROOT / "tools" / "jjm_pack.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_tool_rules_match_runtime() -> None:
    """工具发给作者、闸门跑在启动器里，两份规则必须一致。"""
    tool = _load_tool()
    assert tuple(tool.P2P_BLOCK_RULES) == tuple(netguard.P2P_BLOCK_RULES)
    assert tuple(tool.P2P_WARN_RULES) == tuple(netguard.P2P_WARN_RULES)
    assert tuple(tool.DECLARED_CAPS) == tuple(netguard.DECLARED_CAPS)
