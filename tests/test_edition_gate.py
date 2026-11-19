"""发行版本门控 + 档位语义。

1.6 的事故背景：两份包用同一份源码构建，但构建期没把 ``EDITION`` 烧成
``player``，结果玩家版也带三档校验和开发者工具。

**1.7 起档位从三档收敛为两档**（``loose`` / ``strict``）：

- 两档跑的是**同一套检查**（锚点 / units / 钩子 / 成员），
  差别只在处置：宽松只提示并照装，完全按验证标志与检查结果剔除。
- 4.7 线固定 ``strict``；4.6 线两档自选。
- 旧名（normal / strict_warn / strict_verify / developer）由
  ``MODE_ALIASES`` 单向转换，不再参与任何判定。

这组测试守住：

1. 两条线可选的档位集合正确；
2. 旧配置名能被正确转换，且转换后不再出现旧名；
3. 玩家版/开发者版的默认档符合预期；
4. 「挂不上」的包在两档下都从生效集合剔除（宽松不放行坏包）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.edition as edition
from app.core import config as cfg_mod
from app.core import paths
from app.core.ext import enable as enable_mod
from app.core.ext import service as service_mod
from app.ui.pyside import features


@pytest.fixture
def as_player(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(edition, "EDITION", edition.PLAYER)


@pytest.fixture
def as_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(edition, "EDITION", edition.DEV)


def _cfg(mode: str = "loose") -> dict:
    return {"extensions": {"extension_mode": mode}}


# ---------------------------------------------------------------- 档位集合


def test_46_line_offers_both_modes() -> None:
    """4.6 线：玩家自选宽松 / 完全。"""
    assert features.available_modes("4.6") == ("loose", "strict")


def test_47_line_is_strict_only() -> None:
    """4.7 线：只有完全校验。

    剧情随版本更新，代码层一变旧 mod 就不再适配；给 4.7 开放宽松档
    等于让玩家装一堆注定坏的东西。
    """
    assert features.available_modes("4.7") == ("strict",)


def test_unknown_line_offers_both() -> None:
    """线和档位无关的调用点（例如自定义线）不该被限制。"""
    assert features.available_modes("") == ("loose", "strict")


# ---------------------------------------------------------------- 旧名转换


def test_legacy_names_map_to_new_ones() -> None:
    """旧档位名单向映射，且映射是幂等的。"""
    assert cfg_mod.normalize_mode("normal") == "loose"
    assert cfg_mod.normalize_mode("strict_warn") == "loose"
    assert cfg_mod.normalize_mode("strict_verify") == "strict"
    assert cfg_mod.normalize_mode("developer") == "strict"
    # 新名原样返回
    assert cfg_mod.normalize_mode("loose") == "loose"
    assert cfg_mod.normalize_mode("strict") == "strict"


def test_unknown_mode_falls_back_to_loose() -> None:
    """配置写错一个词不该让启动器起不来，回落宽松。"""
    assert cfg_mod.normalize_mode("乱写的") == "loose"
    assert cfg_mod.normalize_mode(None) == "loose"
    assert cfg_mod.normalize_mode("") == "loose"


def test_legacy_config_is_migrated_on_load(tmp_path: Path) -> None:
    """老配置读进来就转成新名，内部不再出现旧值。"""
    p = tmp_path / "launcher.json"
    p.write_text(
        json.dumps({"extensions": {"extension_mode": "normal"}}), encoding="utf-8"
    )
    assert cfg_mod.extension_mode(cfg_mod.load(p)) == "loose"

    p.write_text(
        json.dumps({"extensions": {"extension_mode": "strict_verify"}}),
        encoding="utf-8",
    )
    assert cfg_mod.extension_mode(cfg_mod.load(p)) == "strict"


def test_mode_round_trip_keeps_new_name(tmp_path: Path) -> None:
    """存进去再读出来还是新名，不会退回旧名。"""
    p = tmp_path / "launcher.json"
    cfg = cfg_mod.load(p)
    cfg["extensions"]["extension_mode"] = "strict"
    cfg_mod.save(cfg, p)
    assert cfg_mod.extension_mode(cfg_mod.load(p)) == "strict"


# ---------------------------------------------------------------- 默认档


def test_default_mode_without_config_is_edition_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没写档位时的默认值。

    开发者版：完全校验（导入即准入，确定性优先）；
    玩家版 / 发布者版：宽松校验（装包不被技术细节打断）。
    """
    monkeypatch.setattr(edition, "EDITION", edition.DEV)
    assert cfg_mod.default_extension_mode() == "strict"

    monkeypatch.setattr(edition, "EDITION", edition.PLAYER)
    assert cfg_mod.default_extension_mode() == "loose"

    monkeypatch.setattr(edition, "EDITION", edition.PUBLISHER)
    assert cfg_mod.default_extension_mode() == "loose"


def test_player_without_config_block_gets_loose(as_player: None) -> None:
    cfg = cfg_mod.load(Path("__不存在的配置__.json"))
    assert cfg_mod.extension_mode(cfg) == "loose"


# ---------------------------------------------------------------- 判定入口


def test_is_strict_only_true_for_strict() -> None:
    """``is_strict`` 是「要不要剔除」的唯一入口。"""
    assert cfg_mod.is_strict("strict") is True
    assert cfg_mod.is_strict("strict_verify") is True  # 旧名同义
    assert cfg_mod.is_strict("loose") is False
    assert cfg_mod.is_strict("normal") is False
    assert cfg_mod.is_strict("strict_warn") is False


def test_is_verify_and_is_developer_are_aliases() -> None:
    """两个旧函数名保留，语义与 is_strict 完全一致。"""
    for mode in ("strict", "loose", "normal", "strict_warn", "strict_verify"):
        assert cfg_mod.is_verify(mode) == cfg_mod.is_strict(mode)
        assert cfg_mod.is_developer(mode) == cfg_mod.is_strict(mode)


def test_loose_mode_helper_follows_strict_flag() -> None:
    """``_loose_mode`` 只用于「要不要多跑一次冒烟」这类耗时决策。"""
    assert service_mod._loose_mode(_cfg("loose")) is True
    assert service_mod._loose_mode(_cfg("normal")) is True   # 旧名→宽松
    assert service_mod._loose_mode(_cfg("strict")) is False
    assert service_mod._loose_mode(_cfg("strict_verify")) is False


def test_developer_only_follows_setting(as_dev: None) -> None:
    """开发者工具由**设置项**决定，不再看校验档。

    2026-11-08 单包化：原来按 ``edition`` + ``is_strict`` 判断，
    单包之后 edition 恒为同一个值，「按 edition 分档」等于写死 ——
    要么所有玩家都看到反解工程，要么开发者永远看不到。

    改成 ``ui.developer_mode``，默认关闭。这和「展示模式」
    （``ui.show_detail``）是同一个思路：能力边界靠开关，不靠构建期常量。
    """
    # 默认关闭
    assert features.developer_only(_cfg("strict")) is False
    assert features.developer_only(_cfg("loose")) is False
    # 显式开启
    cfg = _cfg("strict")
    cfg.setdefault("ui", {})["developer_mode"] = True
    assert features.developer_only(cfg) is True
    # 显式关闭（即使档位是 strict）
    cfg2 = _cfg("strict")
    cfg2.setdefault("ui", {})["developer_mode"] = False
    assert features.developer_only(cfg2) is False


# ---------------------------------------------------------------- 生效集合


def _write_mod(ext_id: str, *, schema: int | None, overlay: bool) -> dict:
    root = paths.PACKS / ext_id
    root.mkdir(parents=True, exist_ok=True)
    overlay_path = root / "overlay-46.pck"
    if overlay:
        overlay_path.write_bytes(b"GDPC" + b"\0" * 32)
    if schema is not None:
        (root / "manifest-46.v2.json").write_text(
            json.dumps({"schema": schema, "id": ext_id, "name": ext_id, "version": "1.0.0"}),
            encoding="utf-8",
        )
    return {
        "id": ext_id,
        "enabled": True,
        "engine_line": "4.6",
        "overlay": str(overlay_path),
    }


def test_both_modes_drop_unloadable_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    """「挂不上」的包两档都剔除。

    宽松档放行的是「内容有冲突」，不是「根本挂不上」。清单 schema 不认识、
    覆盖层文件缺失这类问题，硬挂进去的结果就是黑屏 —— 那不是宽松，那是坏。
    """
    good = _write_mod("good.mod", schema=2, overlay=True)
    bad_schema = _write_mod("bad.schema", schema=4, overlay=True)
    no_manifest = _write_mod("no.manifest", schema=None, overlay=True)
    no_overlay = _write_mod("no.overlay", schema=2, overlay=False)
    monkeypatch.setattr(
        service_mod.ExtensionService,
        "installed",
        lambda self: [good, bad_schema, no_manifest, no_overlay],
    )

    for mode in ("loose", "strict"):
        ids = [m["id"] for m in enable_mod.enabled_mods(_cfg(mode), "4.6")]
        assert ids == ["good.mod"], f"{mode} 档应当只留下可装载的包"