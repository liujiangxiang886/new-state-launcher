"""扩展隔离模式：配置、优先级、以及「模式传到游戏进程」的链路。

三条链路要分别验：

1. **配置层** —— ``extension_mode()`` 的解析与回落
2. **CLI 层** —— 全局 ``--ext-mode`` 与子命令 ``--ext-mode`` 的优先级
3. **传递层** —— mode 要写进 ``mods.json``（游戏侧 ModHost 读它）

第 3 条最要紧：模式如果没传到游戏进程，界面上的开关就是摆设。

**注意**：``tests/conftest.py`` **没有隔离 ``launcher.json``**（只隔离了
``extensions.json`` 和 ``packs/builds/cache/logs``）。涉及配置的测试
**必须显式传路径**，不能碰真实的 ``config/launcher.json``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402


# ---------------------------------------------------------------- 配置层


def test_default_mode_is_edition_specific(monkeypatch) -> None:
    """没写 extension_mode 时，按 edition 给默认档。

    开发者版开箱即确定性（strict 完全校验），玩家版与发布者版宽松
    （loose，装包不被技术细节打断，作者整合包要能直接启动）。
    """
    from app import edition

    monkeypatch.setattr(edition, "EDITION", edition.DEV)
    cfg = cfg_mod.load(Path("__不存在的配置__.json"))
    assert cfg_mod.extension_mode(cfg) == "strict"

    monkeypatch.setattr(edition, "EDITION", edition.PLAYER)
    cfg = cfg_mod.load(Path("__不存在的配置__.json"))
    assert cfg_mod.extension_mode(cfg) == "loose"

    monkeypatch.setattr(edition, "EDITION", edition.PUBLISHER)
    cfg = cfg_mod.load(Path("__不存在的配置__.json"))
    assert cfg_mod.extension_mode(cfg) == "loose"


def test_legacy_config_without_extensions_block() -> None:
    """旧配置文件（没有 extensions 块）必须能拿到默认值。"""
    import tempfile

    p = Path(tempfile.gettempdir()) / "legacy_launcher_cfg.json"
    p.write_text(json.dumps({"schema": 1, "log_level": "INFO"}), encoding="utf-8")
    try:
        cfg = cfg_mod.load(p)
        # 源码态按开发者版跑：没显式写档位时默认 strict（完全校验）
        assert cfg_mod.extension_mode(cfg) == "strict"
        assert cfg["extensions"]["hook_ctx_extended"] is True
        assert cfg["extensions"]["strict_exempt"] == []
    finally:
        p.unlink(missing_ok=True)


def test_invalid_mode_falls_back() -> None:
    """玩家手改配置写错一个词，不该让启动器起不来 → 回落宽松。"""
    cfg = {"extensions": {"extension_mode": "乱写的"}}
    assert cfg_mod.extension_mode(cfg) == "loose"


def test_override_priority() -> None:
    """override 优先于配置；多个 override 取先到的那个。"""
    cfg = {"extensions": {"extension_mode": "loose"}}
    assert cfg_mod.extension_mode(cfg, "strict") == "strict"
    assert cfg_mod.extension_mode(cfg, "strict", "loose") == "strict"
    assert cfg_mod.extension_mode(cfg, None, "strict") == "strict"


def test_is_strict_and_is_verify() -> None:
    """两档制：is_strict 只对 strict 为真；is_verify 是它的旧名同义词。"""
    assert not cfg_mod.is_strict("loose")
    assert cfg_mod.is_strict("strict")
    # 旧名仍按语义映射，不是按字面比较
    assert not cfg_mod.is_strict("normal")
    assert not cfg_mod.is_strict("strict_warn")
    assert cfg_mod.is_strict("strict_verify")
    assert cfg_mod.is_verify("strict") == cfg_mod.is_strict("strict")
    assert cfg_mod.is_verify("loose") == cfg_mod.is_strict("loose")


def test_mode_survives_save_load(tmp_path: Path) -> None:
    """配置往返：存进去再读出来，值不能丢。"""
    p = tmp_path / "launcher.json"
    cfg = cfg_mod.load(p)
    cfg["extensions"]["extension_mode"] = "strict"
    cfg_mod.save(cfg, p)
    again = cfg_mod.load(p)
    assert cfg_mod.extension_mode(again) == "strict"


# ---------------------------------------------------------------- CLI 层


def test_cli_global_and_subcommand_mode() -> None:
    """全局 --ext-mode 与子命令 --ext-mode 的优先级。

    argparse 里子命令的同名选项会**整体覆盖**全局的 namespace，
    所以子命令级必须用 ``default=argparse.SUPPRESS`` ——
    用 ``default=None`` 会把全局的值盖掉（实测踩过）。
    """
    from app.cli import build_parser

    ap = build_parser()
    assert ap.parse_args(["--ext-mode", "loose", "smoke", "4.6"]).ext_mode == "loose"
    assert (
        ap.parse_args(
            ["--ext-mode", "loose", "smoke", "4.6", "--ext-mode", "strict"]
        ).ext_mode
        == "strict"
    )
    assert ap.parse_args(["smoke", "4.6"]).ext_mode is None
    assert ap.parse_args(["smoke", "4.6", "--ext-mode", "strict"]).ext_mode == "strict"
    assert ap.parse_args(["run", "4.6", "--ext-mode", "strict"]).ext_mode == "strict"


def test_cli_rejects_unknown_mode() -> None:
    """非法档位要被 argparse 挡掉（而不是悄悄回落）。"""
    from app.cli import build_parser

    ap = build_parser()
    with pytest.raises(SystemExit):
        ap.parse_args(["--ext-mode", "乱写的", "smoke", "4.6"])


# ---------------------------------------------------------------- 传递层


def test_mods_json_carries_mode(tmp_path: Path, monkeypatch) -> None:
    """mode 必须写进 mods.json —— 游戏侧 ModHost 读它。

    没有这一步，界面上的开关就是摆设：游戏里跑的还是老逻辑。
    """
    import app.core.paths as paths
    from app.core.ext import enable as enable_mod

    monkeypatch.setattr(paths, "BUILDS", tmp_path / "builds")
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(paths, "PACKS", tmp_path / "packs")
    for d in ("builds", "cache", "packs"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    cfg = cfg_mod.load(tmp_path / "launcher.json")
    cfg["extensions"]["extension_mode"] = "loose"
    cfg_mod.save(cfg, tmp_path / "launcher.json")

    # 没有扩展时 build_for_launch 会提前返回 —— 用一个假的已启用扩展试
    monkeypatch.setattr(
        enable_mod, "enabled_mods", lambda cfg, line: [{"id": "mch.fake"}]
    )
    monkeypatch.setattr(
        enable_mod,
        "resolve_order",
        lambda cfg, line, mods, rd: (mods, {}, [], None),
    )
    monkeypatch.setattr(enable_mod, "_collect_hooks", lambda cfg, mods, line: ([], [], {}))
    monkeypatch.setattr(
        enable_mod,
        "_build_instrumented",
        lambda cfg, line, rd, hooks, targets, merged=None: (None, [], set()),
    )

    result = enable_mod.build_for_launch(cfg, "4.6", tmp_path / "run")
    assert result.mods_json is not None
    payload = json.loads(result.mods_json.read_text(encoding="utf-8"))
    assert payload.get("mode") == "loose", payload
    assert payload.get("schema") == 2, payload
