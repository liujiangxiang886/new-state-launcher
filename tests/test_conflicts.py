"""位置级判定：同一处 / 不同处 / 三档分级。

1.6 起不再有「后挂的盖掉先挂的」，判定下沉到位置。这一组测试锁四件事：

1. **同一份文件、不同位置**不是冲突——这是 19 个真实包能共存的前提，
   永远不能退回到「同一文件就判 error」；
2. **同一处**（同指针 / 父子指针 / 同锚点 / 双整文件）会生成一个 ``Unit``，
   由 ``policy`` 按档位分成 ``degraded`` / ``pending``：
   - ``normal``        → ``degraded``，warn，**不阻断启动**；
   - ``strict_warn``   → ``pending``，warn，要人去处置，但取消也能启动；
   - ``strict_verify`` → ``pending``，error，阻断启动。
3. **目录前缀重叠**是合并层也拼不出来的，单独判 ``conflict.files``；
4. ``derive=False`` 是逃生门，回到「只比 claims」的老行为。
"""

from __future__ import annotations

from app.core.ext import resolver


def manifest(**kwargs) -> dict:
    """一份最小可用清单（schema 3 的字段全给默认值）。"""
    data = {
        "schema": 3,
        "id": "test.x",
        "name": "测试扩展",
        "version": "1.0.0",
        "kind": "extension",
        "priority": 50,
        "requires": [],
        "conflicts": [],
        "load_after": [],
        "load_before": [],
        "claims": [],
        "files": [],
        "data_patches": [],
        "text_patches": [],
        "method_hooks": [],
        "scripts": {},
    }
    data.update(kwargs)
    return data


def by_code(res: resolver.Resolution, code: str) -> list[resolver.Problem]:
    return [p for p in res.problems if p.code == code]


def states_of(res: resolver.Resolution, action: str) -> list:
    return [s for s in (res.plan.states if res.plan else []) if s.action == action]


def ops(target: str, path: str, value: object = 1) -> dict:
    return {
        "target": target,
        "operations": [{"op": "replace", "path": path, "value": value}],
    }


# ---------------------------------------------------------------- 同一份文件、不同位置


def test_disjoint_pointers_in_same_file_is_info_only() -> None:
    """同一文件、不同位置 → 只是说明，不产生任何处置。

    这条是 19 个真实包能原样共存的地基：mch.curse_badge 与
    qt6663.exp_equipment 都改 data/equipment.json，位置互不相交。
    """
    res = resolver.resolve(
        [
            ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/9005115")])),
            ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/attributes")])),
        ]
    )
    assert res.ok is True
    assert states_of(res, "degraded") == [] and states_of(res, "pending") == []
    hits = by_code(res, "overlap.data")
    assert hits and hits[0].level == "info"
    assert "自动合并" in hits[0].message


def test_disjoint_pointers_stays_info_in_both_modes() -> None:
    """「同一文件、位置不重叠」在**两档**下都只是 info。

    旧的三档制里 ``strict_warn`` 会把它升成 warn，那是为了给作者做灰度
    体检、列全量影响面。那个档已经删了，而宽松档现在是**玩家的默认档** ——
    把「两个包各改一个字段」这种正常情况当提示刷给玩家，只会淹没真正
    需要看的东西。作者要的影响面报告改由 ``ext.inspect`` 出。
    """
    payload = [
        ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/9005115")])),
        ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/attributes")])),
    ]
    for mode in ("loose", "strict"):
        res = resolver.resolve(payload, mode=mode)
        assert res.ok is True, mode
        hits = by_code(res, "overlap.data")
        assert hits and hits[0].level == "info", f"{mode} 不该把它当提示"
        assert states_of(res, "degraded") == [] and states_of(res, "pending") == []


def test_disjoint_pointers_stays_info_in_both_modes() -> None:
    """「同一文件、位置不重叠」在**两档**下都只是 info。

    旧的三档制里 ``strict_warn`` 会把它升成 warn，那是为了给作者做灰度
    体检、列全量影响面。那个档已经删了，而宽松档现在是**玩家的默认档** ——
    把「两个包各改一个字段」这种正常情况当提示刷给玩家，只会淹没真正
    需要看的东西。作者要的影响面报告改由 ``ext.inspect`` 出。
    """
    payload = [
        ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/9005115")])),
        ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/attributes")])),
    ]
    for mode in ("loose", "strict"):
        res = resolver.resolve(payload, mode=mode)
        assert res.ok is True, mode
        hits = by_code(res, "overlap.data")
        assert hits and hits[0].level == "info", f"{mode} 不该把它当提示"
        assert states_of(res, "degraded") == [] and states_of(res, "pending") == []


def test_disjoint_pointers_stays_info_in_both_modes() -> None:
    """「同一文件、位置不重叠」在**两档**下都只是 info。

    旧的三档制里 ``strict_warn`` 会把它升成 warn，那是给作者做灰度体检、
    看全量影响面用的。那个档已经删了，而宽松档现在是**玩家的默认档** ——
    把「两个包各改一个字段」这种正常情况当提示刷给玩家，只会淹没真正
    需要看的东西。作者要的影响面报告改由 ``ext.inspect`` 出。
    """
    payload = [
        ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/9005115")])),
        ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/attributes")])),
    ]
    for mode in ("loose", "strict"):
        res = resolver.resolve(payload, mode=mode)
        assert res.ok is True, mode
        hits = by_code(res, "overlap.data")
        assert hits and hits[0].level == "info", f"{mode} 不该把它当提示"
        assert states_of(res, "degraded") == [] and states_of(res, "pending") == []


def test_data_overlap_dedup_same_pack() -> None:
    """同一个包内两条补丁指向同一文件是正常写法，不能自己和自己冲突。

    实测 mch.curse_badge 就有两条 data_patches 都指向 data/equipment.json。
    """
    res = resolver.resolve(
        [
            (
                "a",
                manifest(
                    data_patches=[
                        {"target": "data/equipment.json", "operations": []},
                        {"target": "data/equipment.json", "operations": []},
                    ]
                ),
            ),
        ]
    )
    assert by_code(res, "overlap.data") == []


def test_data_overlap_merges_targets_per_owner_set() -> None:
    """同一组包撞了多个文件时合成一条，避免提示被重复信息占满。"""
    res = resolver.resolve(
        [
            (
                "a",
                manifest(
                    data_patches=[
                        {"target": "data/a.json", "operations": []},
                        {"target": "data/b.json", "operations": []},
                    ]
                ),
            ),
            (
                "b",
                manifest(
                    data_patches=[
                        {"target": "data/a.json", "operations": []},
                        {"target": "data/b.json", "operations": []},
                    ]
                ),
            ),
        ]
    )
    hits = by_code(res, "overlap.data")
    assert len(hits) == 1
    assert "data/a.json" in hits[0].message and "data/b.json" in hits[0].message


# ---------------------------------------------------------------- 同一处：数据指针


def test_same_pointer_is_degraded_in_normal() -> None:
    """普通档：后者生效 + 标降级，**不阻断启动**。"""
    res = resolver.resolve(
        [
            ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/power")])),
            ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/power", 2)])),
        ]
    )
    assert res.ok is True, "普通档不允许因为位置重叠就拒绝启动"
    degraded = states_of(res, "degraded")
    assert len(degraded) == 1
    assert degraded[0].severity == "warn"
    hits = by_code(res, "conflict.location")
    assert hits and hits[0].level == "warn"
    assert hits[0].target == "data/equipment.json"
    assert hits[0].locator == "/catalog/300001/power"


def test_same_pointer_is_degraded_in_loose_mode() -> None:
    """宽松档：同一处冲突**照常按顺序取舍**，不阻断、不 pending。

    新语义下宽松档是「查出来 + 告诉玩家 + 放行」，不是「让玩家逐项处置」。
    后者是旧 ``strict_warn`` 的行为，已由完全校验接管（那边的处置是剔除）。
    """
    res = resolver.resolve(
        [
            ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/power")])),
            ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/power", 2)])),
        ],
        mode="loose",
    )
    assert res.ok is True, "宽松档不允许因为位置重叠就拒绝启动"
    assert states_of(res, "pending") == []
    degraded = states_of(res, "degraded")
    assert len(degraded) == 1
    assert degraded[0].severity == "warn"


def test_same_pointer_blocks_in_strict_mode() -> None:
    res = resolver.resolve(
        [
            ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/power")])),
            ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/power", 2)])),
        ],
        mode="strict",
    )
    assert res.ok is False
    hits = by_code(res, "conflict.location")
    assert hits and hits[0].level == "error"


def test_parent_child_pointer_is_same_place() -> None:
    """``/a`` 与 ``/a/b`` 算同一处：一方整体替换对象，另一方改其中一个字段。"""
    res = resolver.resolve(
        [
            ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001", {})])),
            ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/300001/power", 2)])),
        ],
        mode="strict",
    )
    assert res.ok is False
    hits = by_code(res, "conflict.location")
    assert hits and "/catalog/300001" in hits[0].locator


def test_array_append_under_same_parent_is_same_place() -> None:
    """同一父数组里各加一项，下标会互相挤——算同一处。"""
    patch_op = {"op": "add", "path": "/routes/-", "value": {"level": 31}}
    res = resolver.resolve(
        [
            ("a", manifest(data_patches=[{"target": "data/dungeon_loot.json", "operations": [patch_op]}])),
            ("b", manifest(data_patches=[{"target": "data/dungeon_loot.json", "operations": [patch_op]}])),
        ]
    )
    assert len(states_of(res, "degraded")) == 1


def test_numeric_segment_is_not_array_index() -> None:
    """``/catalog/9005115`` 里的 9005115 是字典键，不是数组下标。

    初版把数字段一律当数组下标，在 19 个真实包上误报了 4 个假冲突。
    """
    res = resolver.resolve(
        [
            ("a", manifest(data_patches=[ops("data/equipment.json", "/catalog/9005115")])),
            ("b", manifest(data_patches=[ops("data/equipment.json", "/catalog/9005116")])),
        ]
    )
    assert res.plan.states == []


# ---------------------------------------------------------------- 同一处：文本锚点


def test_same_find_is_same_place() -> None:
    patch = {"target": "scripts/game.gd", "mode": "literal", "find": "old", "replace": "new"}
    res = resolver.resolve(
        [("a", manifest(text_patches=[patch])), ("b", manifest(text_patches=[patch]))]
    )
    assert len(states_of(res, "degraded")) == 1
    hits = by_code(res, "conflict.location")
    assert hits and hits[0].target == "scripts/game.gd"


def test_nested_anchor_is_same_place() -> None:
    """B 的 find 落在 A 的 find 里面 → A 改完 B 必然命中 0 处。"""
    res = resolver.resolve(
        [
            (
                "a",
                manifest(
                    text_patches=[
                        {
                            "target": "scripts/game.gd",
                            "mode": "literal",
                            "find": "var x = 1\nvar y = 2",
                            "replace": "pass",
                        }
                    ]
                ),
            ),
            (
                "b",
                manifest(
                    text_patches=[
                        {"target": "scripts/game.gd", "mode": "literal", "find": "var y = 2", "replace": "pass"}
                    ]
                ),
            ),
        ]
    )
    assert len(states_of(res, "degraded")) == 1


def test_different_find_in_same_script_is_fine() -> None:
    """同一脚本、锚点互不相交 → 各改各的，不是冲突。"""
    res = resolver.resolve(
        [
            (
                "a",
                manifest(
                    text_patches=[
                        {"target": "scripts/game.gd", "mode": "literal", "find": "func a():", "replace": "x"}
                    ]
                ),
            ),
            (
                "b",
                manifest(
                    text_patches=[
                        {"target": "scripts/game.gd", "mode": "literal", "find": "func b():", "replace": "y"}
                    ]
                ),
            ),
        ]
    )
    assert res.plan.states == []
    hits = by_code(res, "overlap.text")
    assert hits and hits[0].level == "info"


# ---------------------------------------------------------------- 同一处：整文件


def test_two_whole_files_is_same_place() -> None:
    res = resolver.resolve(
        [
            ("a", manifest(files=[{"source": "a.txt", "target": "mods/x/a.txt"}])),
            ("b", manifest(files=[{"source": "b.txt", "target": "mods/x/a.txt"}])),
        ]
    )
    assert res.ok is True
    assert len(states_of(res, "degraded")) == 1
    hits = by_code(res, "conflict.files")
    assert hits and hits[0].ids == ["a", "b"]


def test_two_whole_files_blocks_in_strict_verify() -> None:
    res = resolver.resolve(
        [
            ("a", manifest(files=[{"source": "a.txt", "target": "mods/x/a.txt"}])),
            ("b", manifest(files=[{"source": "b.txt", "target": "mods/x/a.txt"}])),
        ],
        mode="strict",
    )
    assert res.ok is False
    assert by_code(res, "conflict.files")


def test_directory_prefix_overlap_is_conflict() -> None:
    """目录与目录下的文件：合并层拼不出来，单独判。"""
    items = [
        ("a", manifest(files=[{"source": "x", "target": "mods/x"}])),
        ("b", manifest(files=[{"source": "y", "target": "mods/x/a.txt"}])),
    ]
    loose = resolver.resolve(items)
    assert loose.ok is True
    assert by_code(loose, "conflict.files")

    strict = resolver.resolve(items, mode="strict")
    assert strict.ok is False


# ---------------------------------------------------------------- 钩子 / 依赖


def test_replace_hook_is_error() -> None:
    hook = {"target": "scripts/game.gd", "method": "tick", "phase": "replace"}
    res = resolver.resolve(
        [("a", manifest(method_hooks=[hook])), ("b", manifest(method_hooks=[hook]))]
    )
    assert res.ok is False
    assert by_code(res, "conflict.replace_hook")


def test_before_after_same_method_is_ok() -> None:
    """before/after 同时钩一个方法是设计允许的，只有 replace 才互斥。"""
    res = resolver.resolve(
        [
            (
                "a",
                manifest(
                    method_hooks=[{"target": "scripts/game.gd", "method": "tick", "phase": "before"}]
                ),
            ),
            (
                "b",
                manifest(
                    method_hooks=[{"target": "scripts/game.gd", "method": "tick", "phase": "after"}]
                ),
            ),
        ]
    )
    assert res.ok is True
    assert by_code(res, "conflict.replace_hook") == []


def test_requires_missing_still_error() -> None:
    res = resolver.resolve([("a", manifest(requires=[{"id": "ghost"}])), ("b", manifest())])
    assert res.ok is False
    assert by_code(res, "requires.missing")


# ---------------------------------------------------------------- 兼容与回归


def test_derive_false_disables_new_rules() -> None:
    """逃生门：derive=False 回到「只比 claims」的旧行为。"""
    res = resolver.resolve(
        [
            ("a", manifest(data_patches=[{"target": "data/equipment.json", "operations": []}])),
            ("b", manifest(data_patches=[{"target": "data/equipment.json", "operations": []}])),
        ],
        derive=False,
    )
    assert res.ok is True
    assert res.problems == []


def test_real_enabled_set_has_no_error() -> None:
    """用现网的形状造 13 个包：3 个共改 equipment、3 个共改 dungeon_loot。

    断言必须是 ok=True 且**没有任何处置**——这些包位置互不相交。
    """
    shared_equipment = ["mch.curse_badge", "mch.dragon_appearance", "qt6663.exp_equipment"]
    shared_loot = ["qt6663.exp_equipment", "qt6663.exp_potion_drops", "qt6663.exp_system"]
    items = []
    for index in range(7):
        items.append(
            (
                f"plain.{index}",
                manifest(files=[{"source": f"{index}.txt", "target": f"mods/p{index}/x.txt"}]),
            )
        )
    for ext_id in shared_equipment:
        items.append((ext_id, manifest(data_patches=[{"target": "data/equipment.json", "operations": []}])))
    for ext_id in shared_loot:
        items.append((ext_id, manifest(data_patches=[{"target": "data/dungeon_loot.json", "operations": []}])))

    res = resolver.resolve(items)
    assert res.ok is True
    assert res.plan.states == []
    assert len(by_code(res, "overlap.data")) == 2
