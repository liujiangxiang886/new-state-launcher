"""按线卸载：游戏本体没了，挂在上面的扩展也要跟着走。

规则是「一条线只装自己的 mod」，所以卸载某条线的本体时：扩展只删该线那一份，
**另一条线的同名扩展必须原样留着**，最后清掉该线的插桩缓存与安装中间目录。

1.4 起只剩一条内置线，但这条保护逻辑仍然必须成立——老玩家升级后，
注册表里可能还留着多线安装的历史数据，卸载不能一把全删。
测试里用 ``OTHER`` 作为「另一条线」的替身。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core import config as cfg_mod
from app.core import paths
from app.core import uninstall
from app.core.ext import service as service_mod
from app.core.fsutil import atomic_write_json, read_json


def _write_registry(items: list[dict]) -> None:
    atomic_write_json(service_mod.REGISTRY, {"schema": 1, "items": items})


#: 「另一条线」的替身。真实项目里已经只剩 4.6，但卸载逻辑必须能处理
#: 老数据中的多线记录，所以测试仍用两条线的形态验证。
OTHER = "4.5"


def _dual(ext_id: str, name: str) -> dict:
    return {
        "id": ext_id,
        "name": name,
        "engine_line": cfg_mod.BUILTIN_LINE,
        "lines": {
            cfg_mod.BUILTIN_LINE: {
                "line": cfg_mod.BUILTIN_LINE,
                "baseline": cfg_mod.BUILTIN_BASELINE,
                "status": "ready",
            },
            OTHER: {"line": OTHER, "baseline": "jjxf.v1.44", "status": "ready"},
        },
    }


def _only46(ext_id: str, name: str) -> dict:
    return {
        "id": ext_id,
        "name": name,
        "engine_line": cfg_mod.BUILTIN_LINE,
        "lines": {
            cfg_mod.BUILTIN_LINE: {
                "line": cfg_mod.BUILTIN_LINE,
                "baseline": cfg_mod.BUILTIN_BASELINE,
                "status": "ready",
            }
        },
    }


def _overlay(ext_id: str, tag: str, size: int) -> Path:
    folder = paths.PACKS / ext_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"overlay-{tag}.pck"
    path.write_bytes(b"x" * size)
    (folder / f"manifest-{tag}.v2.json").write_text("{}", encoding="utf-8")
    return path


def _mkdir(path: Path, age_days: float = 0.0) -> Path:
    import os
    import time

    path.mkdir(parents=True, exist_ok=True)
    (path / "packset.json").write_text("{}", encoding="utf-8")
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


# ---------------------------------------------------------------- 统计


def test_line_assets_counts_each_line_separately() -> None:
    """每条线只统计自己那一份；同一条线里的同一文件不能被算两次。"""
    tag_other = OTHER.replace(".", "")
    overlay_other = _overlay("mod.a", tag_other, 50)
    overlay_main = _overlay("mod.a", "46", 100)
    _overlay("mod.b", "46", 10)
    _write_registry([_dual("mod.a", "A"), _only46("mod.b", "B")])

    main = uninstall.line_assets({}, "4.6")
    assert [e.id for e in main.extensions] == ["mod.a", "mod.b"]
    assert main.extensions[0].bytes >= overlay_main.stat().st_size
    assert main.extra_bytes >= 110

    other = uninstall.line_assets({}, OTHER)
    assert [e.id for e in other.extensions] == ["mod.a"]
    # 同一份文件既在 packs/<id>/ 下、又登记在 lines.<line>.overlay，
    # 只能算一次；重复计算会把体积翻倍。
    manifest_other = paths.PACKS / "mod.a" / f"manifest-{tag_other}.v2.json"
    assert (
        other.extensions[0].bytes
        == overlay_other.stat().st_size + manifest_other.stat().st_size
    ), "体积被重复计算了"
    assert other.extensions[0].name == "A"


def test_line_assets_empty_when_nothing_installed() -> None:
    _write_registry([])
    assets = uninstall.line_assets({}, "4.6")
    assert assets.extensions == [] and assets.stories == []
    assert assets.summary() == ""
    assert assets.extra_bytes == 0


# ---------------------------------------------------------------- 扩展


def test_uninstall_line_keeps_the_other_line() -> None:
    _overlay("mod.a", "46", 100)
    _overlay("mod.a", OTHER.replace(".", ""), 50)
    _write_registry([_dual("mod.a", "A")])

    report = service_mod.service({}).uninstall_line(["mod.a"], OTHER)

    assert report.removed == ["mod.a"]
    folder = paths.PACKS / "mod.a"
    assert not (folder / f"overlay-{OTHER.replace('.', '')}.pck").exists(), "被卸载那条线的覆盖层该删掉"
    assert (folder / "overlay-46.pck").exists(), "剩下那条线的覆盖层不能被牵连"
    items = read_json(service_mod.REGISTRY)["items"]
    assert list(items[0]["lines"]) == ["4.6"]


def test_uninstall_line_removes_package_when_last_line_goes() -> None:
    _overlay("mod.a", "46", 10)
    _write_registry([_only46("mod.a", "A")])

    service_mod.service({}).uninstall_line(["mod.a"], "4.6")

    assert not (paths.PACKS / "mod.a").exists(), "没有其它线时整包删掉，不留空壳"
    assert read_json(service_mod.REGISTRY)["items"] == []


def test_uninstall_line_on_clean_line_is_noop() -> None:
    _overlay("mod.a", "46", 10)
    _write_registry([_only46("mod.a", "A")])

    # 这条包装在内置线上，对「另一条线」卸载应当什么都不做
    report = service_mod.service({}).uninstall_line(["mod.a"], OTHER)

    assert report.removed == []
    assert (paths.PACKS / "mod.a" / "overlay-46.pck").exists()


# ---------------------------------------------------------------- 工作目录


def test_line_workdirs_reads_launcher_json() -> None:
    dev = paths.BUILDS / "ctx-real"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "launcher.json").write_text(json.dumps({"line": "4.6"}), encoding="utf-8")

    assert dev in uninstall.line_workdirs("4.6")
    assert uninstall.line_workdirs(OTHER) == []


def test_purge_line_workdirs_keeps_release_package() -> None:
    install = _mkdir(paths.BUILDS / "_install_mod.a_46")
    release = _mkdir(paths.BUILDS / "release-46-demo")

    count, freed, failures = uninstall.purge_line_workdirs("4.6")

    assert not install.exists(), "安装中间目录属于工作期缓存"
    assert release.exists(), "用户打出来的发布包不能顺手删"
    assert count >= 1 and freed >= 0 and failures == []


def test_purge_line_workdirs_reports_cache_removed() -> None:
    cache = paths.CACHE / "instrumented"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "a.pck").write_bytes(b"x" * 32)
    (cache / "a.json").write_text(json.dumps({"line": "4.6"}), encoding="utf-8")
    (cache / "b.pck").write_bytes(b"x" * 32)
    (cache / "b.json").write_text(json.dumps({"line": OTHER}), encoding="utf-8")

    count, freed, _failures = uninstall.purge_line_workdirs("4.6")

    assert not (cache / "a.pck").exists()
    assert (cache / "b.pck").exists(), "另一条线的缓存不能动"
    assert count >= 1 and freed >= 32


def test_body_line_mapping_matches_config_alias() -> None:
    """卸载页靠这个映射把「本体 id」翻成「引擎线」，两边必须一致。

    **不能断言「只有一条映射」** —— 那是 1.6 的单线假设。1.7 有两条线，
    写死成 ``{1.45: 4.6}`` 的后果是卸载 4.7 本体时拿到空线名，
    ``line_assets("")`` 查不到资产，留下一堆该删没删的东西。

    这里断言的是**一致性**：映射必须覆盖配置里所有带 ``engine`` 的基线，
    且值与该基线登记的那条线相同。
    """
    # 内置那条永远在（配置读不出来时的兜底）
    assert uninstall.LINE_OF_BODY["jjxf.v1.45"] == "4.6"

    cfg = cfg_mod.load()
    mapping = uninstall.line_of_body(cfg)
    for bid, meta in (cfg.get("baselines") or {}).items():
        expect = str((meta or {}).get("engine") or "").strip()
        if expect:
            assert mapping.get(bid) == expect, (
                f"基线 {bid} 登记的引擎线是 {expect}，但卸载页映射成 {mapping.get(bid)}"
            )

    # 4.7 必须能翻出来（原实现缺这一条）
    assert mapping.get("jjxf.v4.7") == "4.7"
