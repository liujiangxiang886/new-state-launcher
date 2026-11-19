"""四种处置的合成产物：处置之后必须真的写进游戏，而且只影响那一处。

这一组是「判定 → 处置 → 产物」的最后一段。判定说「这一处撞了」只是一句话，
玩家点了「兼容：填 15」，最终 pck 里就必须是 15；点了「停用 B」，
B 的全部改动都不能漏进来，A 的其余位置也不能受影响。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.ext import compose, decisions, policy

EQUIP = "data/equipment.json"

BASE_JSON = {
    "catalog": {
        "300001": {"name": "旧名", "power": 10},
        "9005115": {"name": "浮游炮"},
    }
}


def manifest(ext_id: str, **kwargs) -> tuple[str, dict]:
    data = {
        "schema": 3,
        "id": ext_id,
        "name": ext_id,
        "version": "1.0.0",
        "data_patches": [],
        "text_patches": [],
        "files": [],
        "requires": [],
    }
    data.update(kwargs)
    return ext_id, data


def ops(path: str, value: object) -> dict:
    return {"target": EQUIP, "operations": [{"op": "replace", "path": path, "value": value}]}


def base_reader(files: dict[str, bytes]):
    return lambda target: files.get(target)


def overlay_reader(files: dict[tuple[str, str], bytes]):
    return lambda ext_id, target: files.get((ext_id, target))


def build(tmp_path: Path, ordered, plan=None, files=None):
    return compose.build(
        ordered,
        read_base=base_reader(files or {EQUIP: json.dumps(BASE_JSON, ensure_ascii=False).encode()}),
        read_overlay=overlay_reader({}),
        out_pck=tmp_path / "merged.pck",
        line="4.6",
        plan=plan,
    )


def value_of(res: compose.Result, path: str = "/catalog/300001/power"):
    doc = json.loads(res.entries[EQUIP].decode("utf-8"))
    node = doc
    for token in path.strip("/").split("/"):
        node = node[token]
    return node


def pair():
    """A 写 power=10，B 写 power=20，A 另外改一处无关的位置。"""
    return [
        manifest(
            "mod.a",
            data_patches=[ops("/catalog/300001/power", 10), ops("/catalog/9005115/name", "A 的名字")],
        ),
        manifest("mod.b", data_patches=[ops("/catalog/300001/power", 20)]),
    ]


def plan_with(items, order, mode="normal", store=None):
    return policy.plan(items, order, mode=mode, store=store)


def decide(store: decisions.Store, items, action: str, **kwargs) -> None:
    meta = {pid: data for pid, data in items}
    from app.core.ext import units as units_mod

    unit = units_mod.discover(items)[0]
    store.put(
        decisions.Decision(
            line="4.6",
            target=unit.target,
            locator=unit.locator,
            parties=tuple(unit.parties),
            action=action,
            fingerprint=decisions.fingerprint("4.6", unit.target, unit.locator, unit.parties, meta),
            **kwargs,
        )
    )


# ---------------------------------------------------------------- 默认（未处置）


def test_unhandled_later_wins_and_is_recorded(tmp_path: Path) -> None:
    items = pair()
    plan = plan_with(items, ["mod.a", "mod.b"])
    res = build(tmp_path, items, plan=plan)

    assert res.ok, res.conflicts
    assert value_of(res) == 20, "普通档按顺序取舍：后者生效"
    assert len(res.degraded) == 1
    assert res.degraded[0]["target"] == EQUIP
    assert "/catalog/300001/power" in json.dumps(res.degraded[0], ensure_ascii=False)


def test_degraded_does_not_touch_other_locations(tmp_path: Path) -> None:
    """降级只影响那一处：A 改的另一处必须还在。"""
    items = pair()
    res = build(tmp_path, items, plan=plan_with(items, ["mod.a", "mod.b"]))
    assert value_of(res, "/catalog/9005115/name") == "A 的名字"


def test_dispositions_are_listed_per_target(tmp_path: Path) -> None:
    items = pair()
    res = build(tmp_path, items, plan=plan_with(items, ["mod.a", "mod.b"]))
    assert res.dispositions, "处置结果要能逐项查，否则玩家不知道为什么是这个值"
    assert res.dispositions[0]["target"] == EQUIP


# ---------------------------------------------------------------- 兼容


def test_compatible_writes_the_negotiated_value(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    decide(store, items, "compatible", value=15)

    res = build(tmp_path, items, plan=plan_with(items, ["mod.a", "mod.b"], store=store))
    assert res.ok, res.conflicts
    assert value_of(res) == 15, "填的兼容值必须是最终值，不能被后面的补丁改回去"
    assert res.degraded == []


def test_compatible_only_affects_that_location(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    decide(store, items, "compatible", value=15)

    res = build(tmp_path, items, plan=plan_with(items, ["mod.a", "mod.b"], store=store))
    assert value_of(res, "/catalog/9005115/name") == "A 的名字"


# ---------------------------------------------------------------- 停用


def test_disable_second_package_keeps_first(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    decide(store, items, "disable", party="mod.b")

    plan = plan_with(items, ["mod.a", "mod.b"], store=store)
    assert plan.disabled == {"mod.b"}
    res = build(tmp_path, items, plan=plan)

    # 只剩一方 → 不必合成，它自己的扩展文件已经生效
    assert res.entries == {}, "停用后只剩一个提供者时不该再产一份合成结果"


def test_disable_first_package_keeps_second(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    decide(store, items, "disable", party="mod.a")

    plan = plan_with(items, ["mod.a", "mod.b"], store=store)
    assert plan.disabled == {"mod.a"}
    res = build(tmp_path, items, plan=plan)
    assert res.entries == {}


# ---------------------------------------------------------------- 依赖


def test_dependent_injects_order_edge(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    decide(store, items, "dependent", before="mod.a", after="mod.b")

    plan = plan_with(items, ["mod.a", "mod.b"], store=store)
    assert plan.extra_deps == [("mod.a", "mod.b")]
    res = build(tmp_path, items, plan=plan)
    assert value_of(res) == 20, "依赖处置由后者接管这一处"
    assert res.degraded == []


def test_dependent_reverses_effective_order(tmp_path: Path) -> None:
    """把顺序反过来声明：A 排在 B 之后，这一处就写 A 的值。"""
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    decide(store, items, "dependent", before="mod.b", after="mod.a")

    plan = plan_with(items, ["mod.a", "mod.b"], store=store)
    reordered = [items[1], items[0]]
    res = build(tmp_path, reordered, plan=plan)
    assert value_of(res) == 10


# ---------------------------------------------------------------- 已声明接管


def test_declared_override_takes_the_location(tmp_path: Path) -> None:
    items = [
        manifest("mod.a", data_patches=[ops("/catalog/300001/power", 10)]),
        manifest(
            "mod.b",
            requires=["mod.a"],
            data_patches=[
                {
                    "target": EQUIP,
                    "operations": [
                        {
                            "op": "replace",
                            "path": "/catalog/300001/power",
                            "expect": 10,
                            "overrides": ["mod.a"],
                            "value": 20,
                        }
                    ],
                }
            ],
        ),
    ]
    plan = plan_with(items, ["mod.a", "mod.b"])
    assert plan.states[0].action == "override"

    res = build(tmp_path, items, plan=plan)
    assert value_of(res) == 20
    assert res.degraded == [], "已声明接管不该被标成降级"


# ---------------------------------------------------------------- 双整文件


def test_two_whole_files_keep_first_when_planned(tmp_path: Path) -> None:
    """普通档：保留第一个提供者，其余记降级（整份文件没法互相改）。"""
    items = [
        manifest("mod.a", files=[{"source": "a.gd", "target": "mods/x/main.gd"}]),
        manifest("mod.b", files=[{"source": "b.gd", "target": "mods/x/main.gd"}]),
    ]
    plan = plan_with(items, ["mod.a", "mod.b"])
    res = compose.build(
        items,
        read_base=base_reader({}),
        read_overlay=overlay_reader({("mod.a", "mods/x/main.gd"): b"AAA"}),
        out_pck=tmp_path / "merged.pck",
        line="4.6",
        plan=plan,
    )
    assert res.ok
    # 保留的那份就是 A 自己的扩展文件，合成结果与它一致 → 不必再产一份
    assert res.entries == {}
    kept = [x for x in res.degraded if "kept" in x]
    assert kept, res.degraded
    assert kept[0]["kept"] == "mod.a"
    assert kept[0]["dropped"] == ["mod.b"]
