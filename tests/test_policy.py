"""三档策略（``policy.plan``）的阈值测试。

三档共用同一份判定，只有阈值不同：

============  ============  ============  ============
情形            normal        strict_warn   strict_verify
============  ============  ============  ============
已声明接管      override      override      override
有本地处置     按处置         按处置         按处置
未处置          degraded/warn pending/warn  pending/error
============  ============  ============  ============

铁律：``normal`` **永远不产生 error**。
"""

from __future__ import annotations

from pathlib import Path

from app.core.ext import decisions, policy


def manifest(**kwargs) -> dict:
    data = {
        "schema": 3,
        "id": "test.x",
        "name": "t",
        "version": "1.0.0",
        "data_patches": [],
        "text_patches": [],
        "files": [],
        "requires": [],
    }
    data.update(kwargs)
    return data


def op(target: str, path: str, value: object = 1) -> dict:
    return {"target": target, "operations": [{"op": "replace", "path": path, "value": value}]}


def pair() -> list[tuple[str, dict]]:
    return [
        ("a", manifest(data_patches=[op("data/e.json", "/catalog/1/power", 10)])),
        ("b", manifest(data_patches=[op("data/e.json", "/catalog/1/power", 20)])),
    ]


def make_decision(items, action: str, **kwargs) -> decisions.Decision:
    meta = {pid: data for pid, data in items}
    from app.core.ext import units as units_mod

    unit = units_mod.discover(items)[0]
    return decisions.Decision(
        line="4.6",
        target=unit.target,
        locator=unit.locator,
        parties=tuple(unit.parties),
        action=action,
        fingerprint=decisions.fingerprint("4.6", unit.target, unit.locator, unit.parties, meta),
        **kwargs,
    )


# ---------------------------------------------------------------- 三档默认行为


def test_normal_degrades_without_error() -> None:
    plan = policy.plan(pair(), ["a", "b"], mode="normal")
    assert plan.pending == []
    assert len(plan.degraded) == 1
    state = plan.degraded[0]
    assert state.severity == "warn"
    assert state.code == "conflict.location"
    assert plan.disabled == set() and plan.synthetic == [] and plan.extra_deps == []


def test_loose_degrades_without_pending() -> None:
    """宽松档：查出来但**降级放行**，不产生 pending（不要求玩家逐项处置）。"""
    plan = policy.plan(pair(), ["a", "b"], mode="loose")
    assert plan.pending == []
    assert len(plan.degraded) == 1
    assert plan.degraded[0].severity == "warn"


def test_strict_pends_as_error() -> None:
    """完全档：同一处冲突判 error，交给 gate 剔除。"""
    plan = policy.plan(pair(), ["a", "b"], mode="strict")
    assert len(plan.pending) == 1
    assert plan.pending[0].severity == "error"


def test_files_conflict_uses_files_code() -> None:
    items = [
        ("a", manifest(files=[{"source": "a", "target": "mods/x/main.gd"}])),
        ("b", manifest(files=[{"source": "b", "target": "mods/x/main.gd"}])),
    ]
    plan = policy.plan(items, ["a", "b"], mode="loose")
    assert plan.degraded[0].code == "conflict.files"


def test_no_conflict_produces_empty_plan() -> None:
    items = [
        ("a", manifest(data_patches=[op("data/e.json", "/catalog/1")])),
        ("b", manifest(data_patches=[op("data/e.json", "/catalog/2")])),
    ]
    plan = policy.plan(items, ["a", "b"], mode="loose")
    assert plan.states == [] and plan.as_dict()["degraded"] == []


# ---------------------------------------------------------------- 已声明接管


def declared_pair() -> list[tuple[str, dict]]:
    return [
        ("a", manifest(data_patches=[op("data/e.json", "/catalog/1/power", 10)])),
        (
            "b",
            manifest(
                requires=["a"],
                data_patches=[
                    {
                        "target": "data/e.json",
                        "operations": [
                            {
                                "op": "replace",
                                "path": "/catalog/1/power",
                                "expect": 10,
                                "overrides": ["a"],
                                "value": 20,
                            }
                        ],
                    }
                ],
            ),
        ),
    ]


def test_declared_override_is_info_in_every_mode() -> None:
    for mode in ("normal", "strict_warn", "strict_verify"):
        plan = policy.plan(declared_pair(), ["a", "b"], mode=mode)
        assert len(plan.states) == 1, mode
        state = plan.states[0]
        assert state.action == "override" and state.source == "declared"
        assert state.severity == "info", f"{mode} 下已声明接管不该报警"
        assert plan.pending == [] and plan.degraded == []


# ---------------------------------------------------------------- 三种本地处置


def test_disable_decision_disables_whole_package(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    store.put(make_decision(items, "disable", party="b"))

    plan = policy.plan(items, ["a", "b"], mode="normal", store=store)
    assert plan.disabled == {"b"}
    assert plan.degraded == []


def test_disable_earlier_package_keeps_later(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    store.put(make_decision(items, "disable", party="a"))

    plan = policy.plan(items, ["a", "b"], mode="normal", store=store)
    assert plan.disabled == {"a"}


def test_compatible_decision_yields_synthetic_value(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    store.put(make_decision(items, "compatible", value=15))

    plan = policy.plan(items, ["a", "b"], mode="normal", store=store)
    assert len(plan.synthetic) == 1
    syn = plan.synthetic[0]
    assert syn["value"] == 15
    assert syn["target"] == "data/e.json" and syn["locator"] == "/catalog/1/power"
    assert plan.degraded == []


def test_dependent_decision_injects_order_edge(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    store.put(make_decision(items, "dependent", before="a", after="b"))

    plan = policy.plan(items, ["a", "b"], mode="normal", store=store)
    assert plan.extra_deps == [("a", "b")]
    assert plan.degraded == []
    assert plan.states[0].action == "dependent"


def test_stale_decision_is_ignored(tmp_path: Path) -> None:
    """这一处的内容变了，旧处置必须失效，不能拿旧结论去改新版本。"""
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    store.put(make_decision(items, "compatible", value=15))

    # 同一位置写入的值从 10 变成 33 —— 内容真的变了
    changed = [
        ("a", manifest(version="2.0.0", data_patches=[op("data/e.json", "/catalog/1/power", 33)])),
        items[1],
    ]
    plan = policy.plan(changed, ["a", "b"], mode="normal", store=store)
    assert plan.synthetic == []
    assert len(plan.degraded) == 1, "认不出旧决定时回到默认处置"


def test_version_bump_keeps_decision(tmp_path: Path) -> None:
    """只升版本、这一处内容不变时，处置继续有效。

    2026-11-05 修订：旧口径把版本号算进指纹，升级包就让所有处置失效，
    表现为「装包报未处置冲突」。见 decisions 模块开头「指纹的口径」。
    """
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    store.put(make_decision(items, "compatible", value=15))

    # 只把 version 从 1.0.0 改成 2.0.0，写入值保持原样（a=10, b=20）
    bumped = [
        ("a", dict(items[0][1], version="2.0.0")),
        items[1],
    ]
    plan = policy.plan(bumped, ["a", "b"], mode="normal", store=store)
    assert len(plan.synthetic) == 1, "只升版本号不该让处置失效"
    assert plan.synthetic[0]["value"] == 15


def test_decision_scoped_to_other_line_does_not_apply(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    store.put(make_decision(items, "compatible", value=15))

    plan = policy.plan(items, ["a", "b"], mode="normal", store=store, line="4.7")
    assert plan.synthetic == []


# ---------------------------------------------------------------- 只算停用


def test_disabled_parties_only_counts_disable(tmp_path: Path) -> None:
    items = pair()
    store = decisions.Store.load(tmp_path / "decisions.json")
    store.put(make_decision(items, "disable", party="b"))
    assert policy.disabled_parties(items, store=store) == {"b"}

    store2 = decisions.Store.load(tmp_path / "d2.json")
    store2.put(make_decision(items, "compatible", value=15))
    assert policy.disabled_parties(items, store=store2) == set()


def test_disabled_parties_without_store_is_empty() -> None:
    assert policy.disabled_parties(pair()) == set()


# ---------------------------------------------------------------- 顺序


def test_parties_follow_resolution_order() -> None:
    """处置面板里的「前者/后者」必须按真实解析顺序，不能按字母序。"""
    items = pair()
    plan = policy.plan(items, ["b", "a"], mode="normal")
    assert plan.states[0].unit.parties == ["b", "a"]


def test_plan_dict_is_serialisable() -> None:
    import json

    plan = policy.plan(pair(), ["a", "b"], mode="normal")
    blob = json.dumps(plan.as_dict(), ensure_ascii=False)
    assert "degraded" in blob and "states" in blob
