"""合并层：把多个扩展对同一份文件的改动按顺序叠加。

这一层解决的是「装了却不生效」的根因：覆盖层一包一个 ``.pck``，同一路径
只有一个赢家，后挂的整份替换先挂的。于是 5 个包都改 ``data/equipment.json``
时，只有最后一个的改动活下来——尽管它们写的 JSON 位置两两不相交。

这里锁四件事：

1. 位置不相交就叠加，各方的改动都在；
2. 叠加顺序 = 解析顺序（``resolver`` 排好的那个），不是字典序；
3. 真叠加不了的要**明确记账**（``compose.files`` / ``compose.anchor`` /
   ``compose.base``），不能悄悄产出一份半套补丁的文件；
4. 没有重叠目标时不产任何文件——免得每次启动都多挂一个空包。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.ext import compose
from app.core.pck.reader import PckReader

EQUIP = "data/equipment.json"
SCRIPT = "scripts/game.gd"

BASE_JSON = {
    "catalog": {
        "300001": {"name": "旧名", "power": 10},
        "9005115": {"name": "浮游炮"},
    }
}

BASE_GD = "extends Node\n\nvar a = 1\nvar b = 2\n"


def _manifest(ext_id: str, **kwargs) -> tuple[str, dict]:
    data = {"schema": 2, "id": ext_id, "name": ext_id}
    data.update(kwargs)
    return ext_id, data


def _ops(path: str, value: object) -> dict:
    return {"target": EQUIP, "ops": [{"op": "replace", "path": path, "value": value}]}


def _text(find: str, replace: str) -> dict:
    return {"target": SCRIPT, "mode": "literal", "find": find, "replace": replace}


def _base_reader(files: dict[str, bytes]):
    def read(target: str) -> bytes | None:
        return files.get(target)

    return read


def _overlay_reader(files: dict[tuple[str, str], bytes]):
    def read(ext_id: str, target: str) -> bytes | None:
        return files.get((ext_id, target))

    return read


def _build(tmp_path: Path, ordered: list[tuple[str, dict]], files: dict[str, bytes], overlays=None):
    return compose.build(
        ordered,
        read_base=_base_reader(files),
        read_overlay=_overlay_reader(overlays or {}),
        out_pck=tmp_path / "merged.pck",
        line="4.6",
    )


def _read_pck(res: compose.Result) -> dict[str, bytes]:
    reader = PckReader.open(res.pck)
    return {entry.path: reader.read_bytes(entry) for entry in reader.entries}


# ---------------------------------------------------------------- 收集


def test_shared_targets_needs_two_owners() -> None:
    """只有 1 个包碰的目标不必进合并层——它自己的 overlay 已经生效了。"""
    groups = compose.shared_targets(
        [
            _manifest("a", data_patches=[{"target": EQUIP, "ops": []}]),
            _manifest("b", data_patches=[{"target": "data/items.json", "ops": []}]),
        ]
    )
    assert groups == {}


def test_shared_targets_keeps_resolution_order() -> None:
    """层序必须等于传进来的顺序（resolver 已排好），不能自己再排。"""
    groups = compose.shared_targets(
        [
            _manifest("zzz", data_patches=[{"target": EQUIP, "ops": []}]),
            _manifest("aaa", data_patches=[{"target": EQUIP, "ops": []}]),
        ]
    )
    assert [layer.ext_id for layer in groups[EQUIP]] == ["zzz", "aaa"]


def test_layers_of_collects_all_four_kinds() -> None:
    layers = compose.layers_of(
        {
            "id": "x",
            "files": [{"source": "f", "target": EQUIP}],
            "data_patches": [{"target": EQUIP, "ops": []}],
            "text_patches": [_text("var a", "var z")],
            "scripts": {"members": [{"target": SCRIPT, "members": {"FOO": "1"}}]},
        }
    )
    assert layers[EQUIP].whole is True
    assert len(layers[EQUIP].data) == 1
    assert len(layers[SCRIPT].text) == 1
    assert layers[SCRIPT].members == {"FOO": "1"}


# ---------------------------------------------------------------- 叠加


def test_disjoint_json_pointers_all_survive(tmp_path: Path) -> None:
    """两个包改同一份 JSON 的不同位置 → 合并后两处改动都在。"""
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", data_patches=[_ops("/catalog/300001/power", 12)]),
            _manifest("mod.b", data_patches=[_ops("/catalog/300001/name", "新名")]),
        ],
        {EQUIP: json.dumps(BASE_JSON, ensure_ascii=False).encode("utf-8")},
    )

    assert res.ok, res.conflicts
    doc = json.loads(res.entries[EQUIP].decode("utf-8"))
    assert doc["catalog"]["300001"]["power"] == 12
    assert doc["catalog"]["300001"]["name"] == "新名"
    assert "9005115" in doc["catalog"], "没被任何补丁碰的部分必须原样保留"


def test_json_order_follows_layer_order(tmp_path: Path) -> None:
    """写同一个位置时「后者胜」——顺序就是 ordered，不是字典序。"""
    files = {EQUIP: json.dumps(BASE_JSON, ensure_ascii=False).encode("utf-8")}
    a = _manifest("mod.a", data_patches=[_ops("/catalog/300001/power", 1)])
    b = _manifest("mod.b", data_patches=[_ops("/catalog/300001/power", 2)])

    forward = _build(tmp_path / "f", [a, b], files)
    backward = _build(tmp_path / "b", [b, a], files)

    assert json.loads(forward.entries[EQUIP])["catalog"]["300001"]["power"] == 2
    assert json.loads(backward.entries[EQUIP])["catalog"]["300001"]["power"] == 1


def test_disjoint_text_anchors_all_survive(tmp_path: Path) -> None:
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", text_patches=[_text("var a = 1", "var a = 100")]),
            _manifest("mod.b", text_patches=[_text("var b = 2", "var b = 200")]),
        ],
        {SCRIPT: BASE_GD.encode("utf-8")},
    )

    assert res.ok, res.conflicts
    text = res.entries[SCRIPT].decode("utf-8")
    assert "var a = 100" in text and "var b = 200" in text
    assert res.texts()[SCRIPT] == text


def test_members_apply_after_patches(tmp_path: Path) -> None:
    """先打补丁再注入成员声明：反过来的话补丁可能把刚注入的声明改掉。"""
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", text_patches=[_text("var a = 1", "var a = 100")]),
            _manifest(
                "mod.b",
                scripts={
                    "members": [{"target": SCRIPT, "members": {"BONUS": "const BONUS = 5"}}]
                },
            ),
        ],
        {SCRIPT: BASE_GD.encode("utf-8")},
    )

    assert res.ok, res.conflicts
    text = res.entries[SCRIPT].decode("utf-8")
    assert "var a = 100" in text
    assert "const BONUS = 5" in text


def test_whole_file_becomes_the_base(tmp_path: Path) -> None:
    """一方自带整文件副本、另一方打补丁 → 副本当基底，补丁叠在它上面。"""
    own = "extends Node\n\nvar a = 1\nvar b = 2\nvar from_mod = true\n"
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", files=[{"source": "game.gd", "target": SCRIPT}]),
            _manifest("mod.b", text_patches=[_text("var a = 1", "var a = 100")]),
        ],
        {SCRIPT: BASE_GD.encode("utf-8")},
        overlays={("mod.a", SCRIPT): own.encode("utf-8")},
    )

    assert res.ok, res.conflicts
    text = res.entries[SCRIPT].decode("utf-8")
    assert "from_mod" in text, "整文件副本的内容必须保留"
    assert "var a = 100" in text, "别人的补丁要叠在副本之上"
    assert "基底取自扩展整文件" in res.notes[0]


def test_base_label_names_the_real_source(tmp_path: Path) -> None:
    """基底来源必须写在 notes 里，不能含糊成「原版」。"""
    files = {SCRIPT: BASE_GD.encode("utf-8")}
    ordered = [
        _manifest("mod.a", text_patches=[_text("var a = 1", "var a = 100")]),
        _manifest("mod.b", text_patches=[_text("var b = 2", "var b = 200")]),
    ]
    res = compose.build(
        ordered,
        read_base=_base_reader(files),
        read_overlay=_overlay_reader({}),
        out_pck=tmp_path / "merged.pck",
        line="4.6",
        origin_base=lambda _t: "source",
    )
    assert "基底取自源码" in res.notes[0]


def test_pck_is_readable_and_has_remap(tmp_path: Path) -> None:
    """产物必须能被 PckReader 读回；覆盖 .gd 要补自指 remap。"""
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", text_patches=[_text("var a = 1", "var a = 100")]),
            _manifest("mod.b", text_patches=[_text("var b = 2", "var b = 200")]),
        ],
        {SCRIPT: BASE_GD.encode("utf-8")},
    )

    assert res.pck is not None and res.pck.exists()
    assert res.size > 0 and len(res.sha256) == 64
    stored = _read_pck(res)
    assert SCRIPT in stored
    assert f"{SCRIPT}.remap" in stored


# ---------------------------------------------------------------- 拒绝合并


def test_two_whole_files_cannot_merge(tmp_path: Path) -> None:
    """两份完整副本内容完全不同源，谁也说不清该用哪份。"""
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", files=[{"source": "a.gd", "target": SCRIPT}]),
            _manifest("mod.b", files=[{"source": "b.gd", "target": SCRIPT}]),
        ],
        {SCRIPT: BASE_GD.encode("utf-8")},
        overlays={
            ("mod.a", SCRIPT): b"extends Node\nvar x = 1\n",
            ("mod.b", SCRIPT): b"extends Node\nvar x = 2\n",
        },
    )

    assert not res.ok
    assert {c["code"] for c in res.conflicts} == {"compose.files"}
    assert SCRIPT not in res.entries, "拒绝合并的目标不能写进产物"


def test_anchor_miss_is_recorded_not_swallowed(tmp_path: Path) -> None:
    """锚点失配 → 记 compose.anchor，且整个目标不写（绝不产出半套补丁）。"""
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", text_patches=[_text("var a = 1", "var a = 100")]),
            _manifest("mod.b", text_patches=[_text("var 不存在 = 9", "x")]),
        ],
        {SCRIPT: BASE_GD.encode("utf-8")},
    )

    assert not res.ok
    hits = [c for c in res.conflicts if c["code"] == "compose.anchor"]
    assert hits and hits[0]["ids"] == ["mod.b"]
    assert hits[0]["target"] == SCRIPT
    assert SCRIPT not in res.entries


def test_missing_base_is_recorded(tmp_path: Path) -> None:
    """原文取不到（源码、基线、整文件副本都没有）→ compose.base。"""
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", data_patches=[_ops("/catalog/300001/power", 1)]),
            _manifest("mod.b", data_patches=[_ops("/catalog/300001/name", "x")]),
        ],
        {},
    )

    assert not res.ok
    assert {c["code"] for c in res.conflicts} == {"compose.base"}


def test_bad_pointer_is_recorded(tmp_path: Path) -> None:
    """补丁本身写错（路径不存在）不能让整个启动崩掉。"""
    bad = {"target": EQUIP, "ops": [{"op": "replace", "path": "/catalog/404/power", "value": 1}]}
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", data_patches=[bad]),
            _manifest("mod.b", data_patches=[_ops("/catalog/300001/name", "x")]),
        ],
        {EQUIP: json.dumps(BASE_JSON, ensure_ascii=False).encode("utf-8")},
    )

    assert not res.ok
    assert {c["code"] for c in res.conflicts} == {"compose.patch"}


# ---------------------------------------------------------------- 空转


def test_no_overlap_produces_nothing(tmp_path: Path) -> None:
    """没有共同目标就不该产包——否则每次启动都多挂一个空 .pck。"""
    res = _build(
        tmp_path,
        [
            _manifest("mod.a", data_patches=[{"target": "data/a.json", "ops": []}]),
            _manifest("mod.b", data_patches=[{"target": "data/b.json", "ops": []}]),
        ],
        {},
    )

    assert res.ok and res.entries == {}
    assert res.pck is None
    assert not (tmp_path / "merged.pck").exists()


def test_signature_covers_content(tmp_path: Path) -> None:
    """签名按内容算：插桩缓存键要靠它判断基底有没有变。"""
    files = {SCRIPT: BASE_GD.encode("utf-8")}
    same = [
        _manifest("mod.a", text_patches=[_text("var a = 1", "var a = 100")]),
        _manifest("mod.b", text_patches=[_text("var b = 2", "var b = 200")]),
    ]
    other = [
        _manifest("mod.a", text_patches=[_text("var a = 1", "var a = 999")]),
        _manifest("mod.b", text_patches=[_text("var b = 2", "var b = 200")]),
    ]

    first = compose.signature(_build(tmp_path / "1", same, files))
    second = compose.signature(_build(tmp_path / "2", same, files))
    changed = compose.signature(_build(tmp_path / "3", other, files))

    assert first == second
    assert first != changed
