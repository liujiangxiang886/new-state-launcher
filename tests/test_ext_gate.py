"""开发者模式的启动闸门：不通过就整组剔除，不做「以某个为准」。

这个文件只测 ``gate.plan`` 的判定本身（纯计算），以及它和
``readiness`` / ``build_for_launch`` 的结论是否一致——这三处一旦不一致，
就会出现「按钮说能开、点下去被拒」。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core import config as cfg_mod
from app.core import paths
from app.core.ext import gate, service as service_mod
from app.core.fsutil import atomic_write_json, read_json


def _registry(items: list[dict]) -> None:
    atomic_write_json(service_mod.REGISTRY, {"schema": 1, "items": items})


def _entry(ext_id: str, line: str = "4.6") -> dict:
    return {
        "id": ext_id,
        "name": ext_id,
        "enabled": True,
        "engine_line": line,
        "lines": {
            line: {"line": line, "baseline": "jjxf.v1.45", "status": "ready"},
        },
    }


def _manifest(ext_id: str, data: dict, line: str = "4.6") -> None:
    folder = paths.PACKS / ext_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"manifest-{line.replace('.', '')}.v2.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _setup(items: list[tuple[str, dict]], line: str = "4.6") -> dict:
    _registry([_entry(ext_id, line) for ext_id, _ in items])
    for ext_id, data in items:
        _manifest(ext_id, data, line)
    return {"extensions": {"extension_mode": "strict_verify"}}


def _conflict_pair() -> list[tuple[str, dict]]:
    """两个包声明互斥——这是最典型的「谁生效取决于安装顺序」。"""
    return [
        ("mod.a", {"name": "A", "conflicts": [{"id": "mod.b"}]}),
        ("mod.b", {"name": "B", "conflicts": [{"id": "mod.a"}]}),
    ]


# ---------------------------------------------------------------- 模式


def test_loose_mode_keeps_conflicting_mods() -> None:
    """宽松档不剔除：冲突只报告并降级放行，启动仍由玩家决定。"""
    cfg = _setup(_conflict_pair())
    cfg["extensions"]["extension_mode"] = "loose"

    plan = gate.plan(cfg, "4.6")

    assert plan.dropped == []
    assert plan.keep == ["mod.a", "mod.b"]
    assert plan.mode == "loose"


def test_legacy_loose_names_behave_like_loose() -> None:
    """旧配置里的 normal / strict_warn 一律当宽松档，不剔除。"""
    for legacy in ("normal", "strict_warn"):
        cfg = _setup(_conflict_pair())
        plan = gate.plan(cfg, "4.6", mode=legacy)
        assert plan.mode == "loose", legacy
        assert plan.dropped == [], legacy


def test_legacy_strict_name_resolves_to_strict() -> None:
    """旧值 strict_verify / developer 归一到 strict，行为不变（照旧剔除）。"""
    for legacy in ("strict_verify", "developer"):
        cfg = _setup(_conflict_pair())
        plan = gate.plan(cfg, "4.6", mode=legacy)
        assert plan.mode == "strict", legacy
        assert plan.dropped_ids == ["mod.a", "mod.b"], legacy


# ---------------------------------------------------------------- 剔除


def test_conflict_drops_both_sides() -> None:
    """冲突双方全剔：留任何一方都等于「以它为准」。"""
    plan = gate.plan(_setup(_conflict_pair()), "4.6")

    assert plan.ok is True, "全剔光算裸启动，不该拒绝"
    assert plan.keep == []
    assert plan.dropped_ids == ["mod.a", "mod.b"]
    assert {d.code for d in plan.dropped} == {"conflict.declared"}
    assert plan.summary() == "剔除 2 个，剩 0 个可启动"


def test_overlap_does_not_drop_under_compose() -> None:
    """两个包改同一份数据但位置不相交 → 合并层会叠加，开发者模式也不再剔除。"""
    cfg = _setup([
        ("mod.a", {"name": "A", "data_patches": [{"target": "data/equipment.json"}]}),
        ("mod.b", {"name": "B", "data_patches": [{"target": "data/equipment.json"}]}),
    ])

    plan = gate.plan(cfg, "4.6")

    assert plan.dropped == []
    assert plan.keep == ["mod.a", "mod.b"]
    assert plan.summary() == "未剔除任何扩展"


def test_overlap_drops_when_compose_off() -> None:
    """关掉合并层 → 回到旧语义：同一个文件被两方改就算未通过，整组剔除。"""
    cfg = _setup([
        ("mod.a", {"name": "A", "data_patches": [{"target": "data/equipment.json"}]}),
        ("mod.b", {"name": "B", "data_patches": [{"target": "data/equipment.json"}]}),
    ])
    cfg["extensions"]["compose"] = False

    # 先确认 resolver 只给 warn —— 否则这个测试没有意义
    from app.core.ext import resolver as resolver_mod

    resolution = resolver_mod.resolve(
        [
            ("mod.a", {"data_patches": [{"target": "data/equipment.json"}]}),
            ("mod.b", {"data_patches": [{"target": "data/equipment.json"}]}),
        ],
        compose=False,
    )
    assert resolution.ok is True
    # 合并层是默认且唯一路径：关掉 compose 只是把「改同一个文件」降级成
    # 说明（普通档 info），不再升成 warn 级问题
    assert any(p.code == "overlap.data" for p in resolution.problems)

    plan = gate.plan(cfg, "4.6")

    assert plan.dropped_ids == ["mod.a", "mod.b"]
    assert {d.code for d in plan.dropped} == {"overlap.data"}


def test_same_pointer_drops_both_even_with_compose() -> None:
    """合并层救不了「写同一处内容」：这才是真正的整组剔除。"""
    clash = [
        {"target": "data/equipment.json", "ops": [{"op": "replace", "path": "/catalog/1/power", "value": 1}]},
    ]
    cfg = _setup([
        ("mod.a", {"name": "A", "data_patches": clash}),
        ("mod.b", {"name": "B", "data_patches": clash}),
    ])

    plan = gate.plan(cfg, "4.6")

    assert plan.dropped_ids == ["mod.a", "mod.b"]
    # 位置级判定统一走 policy：写同一处的码是 conflict.location
    # （merge.pointer 是旧的文件级判定码，已不再产出）
    assert {d.code for d in plan.dropped} == {"conflict.location"}
    assert plan.keep == []


def test_dependency_closure_drops_dependents() -> None:
    """B 被剔掉后，依赖 B 的 A 也活不了，同一轮一起剔。"""
    cfg = _setup([
        ("mod.a", {"name": "A", "requires": [{"id": "mod.b"}]}),
        ("mod.b", {"name": "B", "conflicts": [{"id": "mod.c"}]}),
        ("mod.c", {"name": "C", "conflicts": [{"id": "mod.b"}]}),
    ])

    plan = gate.plan(cfg, "4.6")

    assert plan.dropped_ids == ["mod.a", "mod.b", "mod.c"]
    dependency = [d for d in plan.dropped if d.code == "gate.dependency"]
    assert [d.ids for d in dependency] == [["mod.a"]]
    assert "mod.b" in dependency[0].reason


def test_unrelated_mod_survives() -> None:
    """剔除只波及冲突组：无关的包必须留下，否则一次升级能剔掉一半。"""
    cfg = _setup([
        *_conflict_pair(),
        ("mod.ok", {"name": "OK", "data_patches": [{"target": "data/other.json"}]}),
    ])

    plan = gate.plan(cfg, "4.6")

    assert plan.keep == ["mod.ok"]
    assert plan.dropped_ids == ["mod.a", "mod.b"]


def test_clean_set_has_no_drops() -> None:
    cfg = _setup([
        ("mod.a", {"name": "A", "data_patches": [{"target": "data/a.json"}]}),
        ("mod.b", {"name": "B", "data_patches": [{"target": "data/b.json"}]}),
    ])

    plan = gate.plan(cfg, "4.6")

    assert plan.dropped == [] and plan.rounds == 1
    assert plan.keep == ["mod.a", "mod.b"]
    assert plan.summary() == "未剔除任何扩展"


def test_single_mod_skips_resolution() -> None:
    """只有一个包时不可能有组合冲突，直接放行（省一次解析）。"""
    cfg = _setup([("mod.a", {"name": "A", "conflicts": [{"id": "mod.b"}]})])

    plan = gate.plan(cfg, "4.6")

    assert plan.keep == ["mod.a"] and plan.dropped == []


# ---------------------------------------------------------------- 落地


def test_apply_to_mods_keeps_order() -> None:
    def _mod(ext_id: str) -> dict:
        return {"id": ext_id, "overlay": f"{ext_id}.pck"}

    plan = gate.plan(_setup([*_conflict_pair(), ("mod.c", {"name": "C"})]), "4.6")
    mods = [_mod("mod.c"), _mod("mod.a"), _mod("mod.b")]

    kept = gate.apply_to_mods(mods, plan)
    assert [m["id"] for m in kept] == ["mod.c"], "不在 keep 里的都要滤掉，且保序"


def test_apply_to_mods_passthrough_without_drops() -> None:
    mods = [{"id": "mod.a"}]
    plan = gate.plan(_setup([("mod.a", {"name": "A"})]), "4.6")
    assert gate.apply_to_mods(mods, plan) == mods


def test_write_report_is_json_on_disk(tmp_path: Path) -> None:
    plan = gate.plan(_setup(_conflict_pair()), "4.6")

    path = gate.write_report(plan, tmp_path)

    assert path == tmp_path / "gate.json"
    data = read_json(path)
    assert data["schema"] == 1 and data["ok"] is True
    assert data["dropped_ids"] == ["mod.a", "mod.b"]
    assert data["mode"] == "strict"


# ---------------------------------------------------------------- 与就绪度一致


def test_readiness_reports_same_drops_as_gate(monkeypatch) -> None:
    """按钮上的「剔除 N 个」必须和实际组装算出来的完全一致。"""
    from app.core import readiness

    # 这个测试只关心「剔除清单是否一致」，基线在临时目录里当然不存在
    monkeypatch.setattr(cfg_mod, "baseline_health", lambda *_a, **_k: ("ok", ""))
    cfg = _setup([
        *_conflict_pair(),
        ("mod.ok", {"name": "OK"}),
    ])

    plan = gate.plan(cfg, "4.6")
    result = readiness.evaluate(cfg, "4.6")

    assert result.ok is True, "还有可启动的组合，不该阻止启动"
    flat = sorted({pid for d in result.dropped for pid in d.get("ids") or []})
    assert flat == plan.dropped_ids
    assert result.drop_summary() == "开发者模式将剔除 2 个，剩 1 个可启动"
    assert any(p.get("code") == "gate.dropped" for p in result.problems)
    assert any("mod.a" in line for line in result.drop_lines())


def test_normal_mode_readiness_has_no_drops(monkeypatch) -> None:
    from app.core import readiness

    monkeypatch.setattr(cfg_mod, "baseline_health", lambda *_a, **_k: ("ok", ""))
    cfg = _setup(_conflict_pair())
    cfg["extensions"]["extension_mode"] = "normal"

    result = readiness.evaluate(cfg, "4.6")

    assert result.dropped == []
    assert result.drop_summary() == ""


def test_gate_line_isolation() -> None:
    """一条线的冲突不影响另一条线：各算各的。"""
    _registry([_entry("mod.a", "4.6"), _entry("mod.b", "4.6")])
    _manifest("mod.a", {"conflicts": [{"id": "mod.b"}]}, "4.6")
    _manifest("mod.b", {"conflicts": [{"id": "mod.a"}]}, "4.6")
    # 另一条线上只有 A，且没有冲突声明
    items = read_json(service_mod.REGISTRY)["items"]
    items[0]["lines"]["4.5"] = {"line": "4.5", "baseline": "jjxf.v1.44", "status": "ready"}
    atomic_write_json(service_mod.REGISTRY, {"schema": 1, "items": items})
    _manifest("mod.a", {"name": "A"}, "4.5")

    cfg = {"extensions": {"extension_mode": "strict_verify"}}
    assert gate.plan(cfg, "4.6").dropped_ids == ["mod.a", "mod.b"]
    assert gate.plan(cfg, "4.5").dropped_ids == []


def test_mode_labels_are_two_tier() -> None:
    """档位显示名定稿为两档：宽松校验 / 完全校验。

    「开发者校验」这个名字没有了 —— 完全校验就是完全校验，不区分
    作者自测还是玩家使用。
    """
    assert cfg_mod.MODE_LABELS == {"loose": "宽松校验", "strict": "完全校验"}
    assert cfg_mod.normalize_mode("developer") == "strict"
    assert cfg_mod.is_strict("developer") is True
    assert cfg_mod.is_strict("strict_warn") is False
