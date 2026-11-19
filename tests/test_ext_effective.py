"""生效视图：gate 剔了谁，界面就必须显示谁。

这里测两件事，都是「说法不一致」类缺陷的根：

1. ``order_bundle_members``：合集安装必须**被依赖的先装**。P01 要改次元
   裂缝的脚本，按字母序打包时 P01 排在前面，装它时次元裂缝还没落盘，
   取原文必然失败（某个 mod.zip 实测 19 个只成功 18 个）。
2. ``effective.of``：开发者模式剔除后的生效集合与状态，三处界面
   （扩展管理、游戏库右栏、就绪度）读的都必须是这一份。
"""

from __future__ import annotations

import json

from app.core.ext import effective, service as service_mod
from app.core.fsutil import atomic_write_json


def _registry(items: list[dict]) -> None:
    atomic_write_json(service_mod.REGISTRY, {"schema": 1, "items": items})


def _entry(ext_id: str, line: str = "4.6") -> dict:
    return {
        "id": ext_id,
        "name": ext_id,
        "enabled": True,
        "engine_line": line,
        "lines": {line: {"line": line, "baseline": "jjxf.v1.45", "status": "ready"}},
    }


def _manifest(ext_id: str, data: dict, line: str = "4.6") -> None:
    from app.core import paths

    folder = paths.PACKS / ext_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"manifest-{line.replace('.', '')}.v2.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _setup(items: list[tuple[str, dict]], line: str = "4.6", mode: str = "strict_verify") -> dict:
    _registry([_entry(ext_id, line) for ext_id, _ in items])
    for ext_id, data in items:
        _manifest(ext_id, data, line)
    effective.invalidate()
    return {"extensions": {"extension_mode": mode}}


def _data(target: str, path: str = "") -> dict:
    """一条数据补丁。给了 ``path`` 才有 ``ops``——合并层能叠加的前提是位置不相交。"""
    patch = {"target": target, "ops": []}
    if path:
        patch["ops"] = [{"op": "replace", "path": path, "value": 1}]
    return {"name": target, "data_patches": [patch]}


def _text(target: str) -> dict:
    return {
        "name": target,
        "text_patches": [{"target": target, "find": f"x_{target}", "replace": "y"}],
    }


# ---------------------------------------------------------------- 合集顺序


def test_bundle_order_puts_dependency_first() -> None:
    """A 依赖 B → B 必须先装，否则 A 读不到 B 改过的文件。"""
    metas = {"a.zip": ("mod.a", ["mod.b"]), "b.zip": ("mod.b", [])}

    order = service_mod.order_bundle_members(["a.zip", "b.zip"], metas)

    assert order == ["b.zip", "a.zip"]


def test_bundle_order_survives_cycle() -> None:
    """互相依赖（作者写错）不能丢包，也不能死循环。"""
    metas = {"a.zip": ("mod.a", ["mod.b"]), "b.zip": ("mod.b", ["mod.a"])}

    order = service_mod.order_bundle_members(["a.zip", "b.zip"], metas)

    assert sorted(order) == ["a.zip", "b.zip"]


def test_bundle_order_ignores_requirement_outside_bundle() -> None:
    """依赖不在合集里（用户只挑了一部分）时保持原序，不报错也不丢。"""
    metas = {"a.zip": ("mod.a", ["mod.not_in_bundle"]), "b.zip": ("mod.b", [])}

    order = service_mod.order_bundle_members(["a.zip", "b.zip"], metas)

    assert order == ["a.zip", "b.zip"]


def test_bundle_order_keeps_unknown_members() -> None:
    """清单读不出来的成员（metas 里没有）照样保留。"""
    order = service_mod.order_bundle_members(["x.zip", "y.zip"], {})
    assert order == ["x.zip", "y.zip"]


# ---------------------------------------------------------------- 生效视图


def _two_packages_same_pointer() -> list[tuple[str, dict]]:
    """两个包写同一个 JSON 位置——合并层救不了，这才是真冲突。"""
    return [
        ("mod.a", _data("data/items.json", "/catalog/1/power")),
        ("mod.b", _data("data/items.json", "/catalog/1/power")),
    ]


def test_developer_mode_drops_whole_group() -> None:
    """两个包写同一处内容 → 整组剔除，不是「保留后一个」。"""
    cfg = _setup(_two_packages_same_pointer())

    view = effective.of(cfg, "4.6", refresh=True)

    assert view.developer
    assert view.keep == []
    assert sorted(view.dropped) == ["mod.a", "mod.b"]
    assert view.status_of("mod.a") == effective.DROPPED
    assert view.status_of("mod.b") == effective.DROPPED


def test_overlap_alone_is_not_dropped() -> None:
    """改同一份文件但位置不相交 → 合并层会叠加，视图里两个都是「生效」。"""
    cfg = _setup(
        [
            ("mod.a", _data("data/items.json", "/catalog/1/a")),
            ("mod.b", _data("data/items.json", "/catalog/1/b")),
        ]
    )

    view = effective.of(cfg, "4.6", refresh=True)

    assert view.dropped == []
    assert view.status_of("mod.a") == effective.ACTIVE
    assert view.status_of("mod.b") == effective.ACTIVE


def test_normal_mode_keeps_everything_active() -> None:
    """普通模式只在启动前提示覆盖，不剔除。"""
    cfg = _setup(_two_packages_same_pointer(), mode="normal")

    view = effective.of(cfg, "4.6", refresh=True)

    assert view.dropped == []
    assert view.status_of("mod.a") == effective.ACTIVE
    assert view.developer is False


def test_disabled_package_reads_as_disabled_not_dropped() -> None:
    """已停用的包说「被剔除」只会让人困惑，停用优先。"""
    cfg = _setup(_two_packages_same_pointer())

    view = effective.of(cfg, "4.6", refresh=True)

    assert view.status_of("mod.a", enabled=False) == effective.DISABLED


def test_dropped_package_carries_reason() -> None:
    """一个包撞了几处，就列几处，且不重复。"""
    cfg = _setup(
        [
            ("mod.a", {**_data("data/items.json", "/catalog/1/power"), **_text("scripts/game.gd")}),
            ("mod.b", _data("data/items.json", "/catalog/1/power")),
            ("mod.c", _text("scripts/game.gd")),
        ]
    )

    view = effective.of(cfg, "4.6", refresh=True)
    reasons = view.reasons_of("mod.a")

    assert "data/items.json" in reasons
    assert "scripts/game.gd" in reasons
    assert len(reasons) == len(set(reasons))
    assert "整组剔除" in view.text_of("mod.a")


def test_drop_lines_one_per_package() -> None:
    """13 个包要出 13 行；按下标逐个念 id 会得到 26 个重复名字。"""
    cfg = _setup(_two_packages_same_pointer())

    view = effective.of(cfg, "4.6", refresh=True)
    lines = view.plan.drop_lines()

    assert lines, "这两个包必须被剔，否则这条测试什么都没测"
    assert len(lines) == len(view.dropped)
    # 每行以包 id 开头，且整份清单里每个包只出现一次「开头」
    heads = [line.split(" —— ")[0] for line in lines]
    assert sorted(heads) == sorted(view.dropped)


def test_cache_reuses_result_and_invalidate_clears() -> None:
    """同一份注册表连调两次只算一次；换注册表后结论要跟着变。"""
    cfg = _setup(_two_packages_same_pointer())

    first = effective.of(cfg, "4.6")
    second = effective.of(cfg, "4.6")
    assert first is second          # 命中缓存

    # 再装一个不冲突的包：注册表变了，视图必须重算
    atomic_write_json(
        service_mod.REGISTRY,
        {
            "schema": 1,
            "items": [_entry("mod.a"), _entry("mod.b"), _entry("mod.d")],
        },
    )
    _manifest("mod.d", {"name": "d"})
    third = effective.of(cfg, "4.6")
    assert third is not first
    assert "mod.d" in third.keep
