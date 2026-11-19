"""位置级冲突发现（``units.discover``）的规则测试。

这一层只回答「哪一处被两个包同时写」，不做取舍。规则一共四条：

1. JSON 指针相同 / 父子相交；
2. 同一父数组里增删（下标会位移）；
3. 文本锚点完全相同，或长锚点互相包含；
4. 两份整文件写同一目标。

同文件、位置不相交**不产生** ``Unit``——那是 19 个真实包能共存的前提。
"""

from __future__ import annotations

from app.core.ext import units


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


def op(target: str, path: str, kind: str = "replace", value: object = 1) -> dict:
    return {"target": target, "operations": [{"op": kind, "path": path, "value": value}]}


def text(target: str, find: str, replace: str = "x") -> dict:
    return {"target": target, "find": find, "replace": replace, "mode": "literal"}


def find(items) -> list[units.Unit]:
    return units.discover(items)


# ---------------------------------------------------------------- 数据指针


def test_same_pointer_is_one_unit() -> None:
    found = find(
        [
            ("a", manifest(data_patches=[op("data/e.json", "/catalog/1/power", value=1)])),
            ("b", manifest(data_patches=[op("data/e.json", "/catalog/1/power", value=2)])),
        ]
    )
    assert len(found) == 1
    assert found[0].kind == "data"
    assert found[0].locator == "/catalog/1/power"
    assert found[0].parties == ["a", "b"]


def test_parent_child_pointers_collapse_to_shorter() -> None:
    found = find(
        [
            ("a", manifest(data_patches=[op("data/e.json", "/catalog/1", value={})])),
            ("b", manifest(data_patches=[op("data/e.json", "/catalog/1/power")])),
        ]
    )
    assert len(found) == 1
    assert found[0].locator == "/catalog/1", "位置取更靠上的那个，才盖得住两边"


def test_disjoint_pointers_in_same_file_produce_nothing() -> None:
    assert (
        find(
            [
                ("a", manifest(data_patches=[op("data/e.json", "/catalog/9005115")])),
                ("b", manifest(data_patches=[op("data/e.json", "/catalog/300001/power")])),
            ]
        )
        == []
    )


def test_numeric_segment_is_a_key_not_an_index() -> None:
    """``/catalog/9005115`` 的 9005115 是字典键。按数字猜数组会误报。"""
    assert (
        find(
            [
                ("a", manifest(data_patches=[op("data/e.json", "/catalog/9005115/icon")])),
                ("b", manifest(data_patches=[op("data/e.json", "/catalog/9005116/icon")])),
            ]
        )
        == []
    )


def test_same_package_two_patches_no_self_conflict() -> None:
    """一个包自己写两次同一处是作者的事，不是冲突。"""
    assert (
        find(
            [
                (
                    "a",
                    manifest(
                        data_patches=[
                            op("data/e.json", "/catalog/1/power", value=1),
                            op("data/e.json", "/catalog/1/power", value=2),
                        ]
                    ),
                )
            ]
        )
        == []
    )


# ---------------------------------------------------------------- 数组位移


def test_append_to_same_array_is_one_unit() -> None:
    """两边都 ``add /routes/-``：既撞指针又会挤下标，只报一条。"""
    found = find(
        [
            ("a", manifest(data_patches=[op("data/loot.json", "/routes/-", "add", {"l": 1})])),
            ("b", manifest(data_patches=[op("data/loot.json", "/routes/-", "add", {"l": 2})])),
        ]
    )
    assert len(found) == 1, "同一个问题不能出两行，否则处置面板要人点两次"
    assert found[0].locator == "/routes/-"


def test_append_vs_edit_index_is_parent_array_unit() -> None:
    """一边加一项、一边改某一项：下标会移，整个父数组都不稳。"""
    found = find(
        [
            ("a", manifest(data_patches=[op("data/loot.json", "/routes/-", "add", {"l": 1})])),
            ("b", manifest(data_patches=[op("data/loot.json", "/routes/3/level", "replace", 9)])),
        ]
    )
    assert len(found) == 1
    assert found[0].locator == "/routes"
    assert found[0].payload["reason"] == "index-shift"


def test_append_and_edit_elsewhere_is_not_conflict() -> None:
    found = find(
        [
            ("a", manifest(data_patches=[op("data/loot.json", "/routes/-", "add", {"l": 1})])),
            ("b", manifest(data_patches=[op("data/pets.json", "/routes/-", "add", {"l": 2})])),
        ]
    )
    assert found == []


def test_is_array_callback_widens_detection() -> None:
    """给了类型探测回调时，数字下标也按数组处理。"""

    def is_array(target: str, parent: str) -> bool:
        return target == "data/loot.json" and parent == "/routes"

    found = units.discover(
        [
            ("a", manifest(data_patches=[op("data/loot.json", "/routes/0", "add", {"l": 1})])),
            ("b", manifest(data_patches=[op("data/loot.json", "/routes/1/level")])),
        ],
        is_array=is_array,
    )
    assert found and found[0].locator == "/routes"


# ---------------------------------------------------------------- 文本锚点


def test_same_find_is_one_unit() -> None:
    found = find(
        [
            ("a", manifest(text_patches=[text("scripts/game.gd", "var x = 1")])),
            ("b", manifest(text_patches=[text("scripts/game.gd", "var x = 1")])),
        ]
    )
    assert len(found) == 1
    assert found[0].kind == "text"
    assert found[0].locator.startswith("text:literal:")


def test_contained_find_is_one_unit() -> None:
    found = find(
        [
            (
                "a",
                manifest(text_patches=[text("scripts/game.gd", "var x = 1\nvar y = 2")]),
            ),
            ("b", manifest(text_patches=[text("scripts/game.gd", "var y = 2")])),
        ]
    )
    assert len(found) == 1
    assert found[0].payload["find"] == "var y = 2", "取更短的那个当展示位置"


def test_short_anchor_does_not_trigger_containment() -> None:
    """短锚点不参与包含判定，否则通用词会把所有补丁都判成冲突。"""
    assert (
        find(
            [
                ("a", manifest(text_patches=[text("scripts/game.gd", "value")])),
                ("b", manifest(text_patches=[text("scripts/game.gd", "var v = value + 1")])),
            ]
        )
        == []
    )


def test_different_functions_in_same_script_produce_nothing() -> None:
    assert (
        find(
            [
                ("a", manifest(text_patches=[text("scripts/game.gd", "func alpha() -> void:")])),
                ("b", manifest(text_patches=[text("scripts/game.gd", "func beta() -> void:")])),
            ]
        )
        == []
    )


# ---------------------------------------------------------------- 整文件


def test_two_providers_of_same_file() -> None:
    found = find(
        [
            ("a", manifest(files=[{"source": "a", "target": "mods/x/main.gd"}])),
            ("b", manifest(files=[{"source": "b", "target": "mods/x/main.gd"}])),
        ]
    )
    assert len(found) == 1
    assert found[0].kind == "files"
    assert found[0].scope == "file"


def test_three_providers_report_once() -> None:
    found = find(
        [
            ("a", manifest(files=[{"source": "a", "target": "mods/x/main.gd"}])),
            ("b", manifest(files=[{"source": "b", "target": "mods/x/main.gd"}])),
            ("c", manifest(files=[{"source": "c", "target": "mods/x/main.gd"}])),
        ]
    )
    assert len(found) == 1
    assert found[0].parties == ["a", "b", "c"]


# ---------------------------------------------------------------- 接管授权


def test_authorized_needs_three_things() -> None:
    base = dict(
        data_patches=[
            {
                "target": "data/e.json",
                "operations": [
                    {"op": "replace", "path": "/a/b", "value": 2, "expect": 1, "overrides": ["a"]}
                ],
            }
        ],
        requires=["a"],
    )
    pair = [
        ("a", manifest(data_patches=[op("data/e.json", "/a/b", value=1)])),
        ("b", manifest(**base)),
    ]
    found = find(pair)
    meta = {pid: data for pid, data in pair}
    ok, reason = units.authorized(found[0], meta)
    assert ok, reason

    # 去掉 requires
    no_req = dict(base, requires=[])
    meta2 = {"a": pair[0][1], "b": manifest(**no_req)}
    ok, reason = units.authorized(found[0], meta2)
    assert not ok and "requires" in reason

    # 去掉 expect
    no_exp = dict(base)
    no_exp["data_patches"] = [
        {"target": "data/e.json", "operations": [{"op": "replace", "path": "/a/b", "overrides": ["a"]}]}
    ]
    meta3 = {"a": pair[0][1], "b": manifest(**no_exp)}
    ok, reason = units.authorized(found[0], meta3)
    assert not ok and "expect" in reason

    # 没点名
    no_name = dict(base)
    no_name["data_patches"] = [
        {"target": "data/e.json", "operations": [{"op": "replace", "path": "/a/b", "expect": 1}]}
    ]
    meta4 = {"a": pair[0][1], "b": manifest(**no_name)}
    ok, reason = units.authorized(found[0], meta4)
    assert not ok and "接管" in reason


# ---------------------------------------------------------------- 人话


def test_describe_has_no_jargon() -> None:
    """普通模式给玩家看的位置描述，不许出现内部术语。"""
    for unit in (
        units.Unit("data/e.json", "/catalog/1", "data", ["a", "b"]),
        units.Unit("scripts/game.gd", "text:literal:abc", "text", ["a", "b"]),
        units.Unit("mods/x/main.gd", "@file", "files", ["a", "b"]),
    ):
        text_out = units.describe(unit)
        for word in ("指针", "锚点", "覆盖", "挂载", "合并层", "堆栈", "overlay"):
            assert word not in text_out, f"{text_out!r} 里出现了内部术语 {word}"
