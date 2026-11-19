"""schema 3 清单的解析与归一。

三件事必须锁住：

1. `operations` 是正式名、`ops` 是别名，两种都归一到 ``ops``；
2. `expect` 的两种写法（值 / 断言对象）语义不同，不能被合并；
3. schema 1 / 2 的老包**一个字节不改**也要能读——这是向后兼容的底线。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.core.ext import manifest


def make_pkg(tmp_path: Path, data: dict, files: dict[str, bytes] | None = None, name: str = "pkg.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("extension.json", json.dumps(data, ensure_ascii=False, indent=2))
        for key, blob in (files or {}).items():
            zf.writestr(key, blob)
    return path


def base(**kwargs) -> dict:
    data = {
        "schema": 3,
        "id": "test.ext",
        "name": "测试扩展",
        "version": "1.0.0",
        "kind": "extension",
        "baseline": {"id": "jjxf.v1.45", "engine": {"line": "4.6"}},
        "files": [],
        "data_patches": [],
        "text_patches": [],
    }
    data.update(kwargs)
    return data


def codes(m: manifest.Manifest) -> set[str]:
    return {i.code for i in m.issues}


def errors(m: manifest.Manifest) -> list:
    return [i for i in m.issues if i.level == "error"]


# ---------------------------------------------------------------- 版本


def test_schema3_roundtrip(tmp_path: Path) -> None:
    m = manifest.load(make_pkg(tmp_path, base()))
    assert m.data["schema"] == 3
    assert not errors(m), [i.as_dict() for i in m.issues]


def test_schema1_is_upgraded_not_rejected(tmp_path: Path) -> None:
    raw = {
        "schema": 1,
        "id": "test.old",
        "name": "老包",
        "version": "1.0.0",
        "files": [{"source": "a.txt", "target": "mods/x/a.txt"}],
    }
    m = manifest.load(make_pkg(tmp_path, raw, {"a.txt": b"hi"}))
    assert m.data["schema"] == 2, "schema 1 升格到 2（带 x_origin 记录来源）"
    assert m.data["x_origin"]["schema"] == 1
    assert not errors(m)


def test_schema2_still_reads(tmp_path: Path) -> None:
    raw = base(schema=2)
    raw["data_patches"] = [{"target": "data/e.json", "ops": [{"op": "add", "path": "/a", "value": 1}]}]
    m = manifest.load(make_pkg(tmp_path, raw))
    assert m.data["schema"] == 2
    assert m.data["data_patches"][0]["ops"][0]["path"] == "/a"
    assert not errors(m)


def test_unknown_schema_is_error(tmp_path: Path) -> None:
    m = manifest.load(make_pkg(tmp_path, base(schema=9)))
    assert "schema.unsupported" in codes(m)


# ---------------------------------------------------------------- operations / ops


def test_operations_is_normalised_to_ops(tmp_path: Path) -> None:
    raw = base(
        data_patches=[
            {"target": "data/e.json", "operations": [{"op": "replace", "path": "/a", "value": 2}]}
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert m.data["data_patches"][0]["ops"][0]["path"] == "/a"


def test_ops_alias_still_works(tmp_path: Path) -> None:
    raw = base(data_patches=[{"target": "data/e.json", "ops": [{"op": "add", "path": "/a", "value": 1}]}])
    m = manifest.load(make_pkg(tmp_path, raw))
    assert m.data["data_patches"][0]["ops"][0]["path"] == "/a"


def test_set_is_alias_of_replace(tmp_path: Path) -> None:
    raw = base(
        data_patches=[{"target": "data/e.json", "operations": [{"op": "set", "path": "/a", "value": 1}]}]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert m.data["data_patches"][0]["ops"][0]["op"] == "replace"


def test_unknown_op_is_error(tmp_path: Path) -> None:
    raw = base(
        data_patches=[{"target": "data/e.json", "operations": [{"op": "frobnicate", "path": "/a"}]}]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert "op.unknown" in codes(m)


def test_pointer_must_start_with_slash(tmp_path: Path) -> None:
    raw = base(data_patches=[{"target": "data/e.json", "operations": [{"op": "add", "path": "a/b"}]}])
    m = manifest.load(make_pkg(tmp_path, raw))
    assert "pointer.invalid" in codes(m)


def test_ops_of_reads_both_names() -> None:
    assert manifest.ops_of({"operations": [{"op": "add"}]}) == [{"op": "add"}]
    assert manifest.ops_of({"ops": [{"op": "add"}]}) == [{"op": "add"}]
    assert manifest.ops_of({}) == []


# ---------------------------------------------------------------- expect


def test_scalar_expect_becomes_eq(tmp_path: Path) -> None:
    raw = base(
        data_patches=[
            {"target": "data/e.json", "operations": [{"op": "replace", "path": "/a", "expect": "recovery_1"}]}
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert m.data["data_patches"][0]["ops"][0]["expect"] == {"eq": "recovery_1"}


def test_structured_expect_is_kept(tmp_path: Path) -> None:
    raw = base(
        data_patches=[
            {
                "target": "data/e.json",
                "operations": [{"op": "replace", "path": "/a", "expect": {"exists": True}}],
            }
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert m.data["data_patches"][0]["ops"][0]["expect"] == {"exists": True}


def test_object_without_assertion_keys_is_a_whole_value(tmp_path: Path) -> None:
    """``expect: {"level": 31}`` 是「整对象期望值」，不是断言。"""
    raw = base(
        data_patches=[
            {
                "target": "data/e.json",
                "operations": [{"op": "replace", "path": "/a", "expect": {"level": 31}}],
            }
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert m.data["data_patches"][0]["ops"][0]["expect"] == {"eq": {"level": 31}}


def test_mixed_expect_warns(tmp_path: Path) -> None:
    raw = base(
        data_patches=[
            {
                "target": "data/e.json",
                "operations": [
                    {"op": "replace", "path": "/a", "expect": {"eq": 1, "whatever": 2}}
                ],
            }
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert "expect.bad_shape" in codes(m)


# ---------------------------------------------------------------- overrides / on_miss


def test_overrides_and_on_miss_are_kept(tmp_path: Path) -> None:
    raw = base(
        requires=["other.ext"],
        data_patches=[
            {
                "target": "data/e.json",
                "operations": [
                    {
                        "op": "replace",
                        "path": "/a",
                        "expect": 1,
                        "overrides": ["other.ext"],
                        "on_miss": "skip",
                    }
                ],
            }
        ],
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    op = m.data["data_patches"][0]["ops"][0]
    assert op["overrides"] == ["other.ext"]
    assert op["on_miss"] == "skip"


def test_overrides_bad_shape_is_error(tmp_path: Path) -> None:
    raw = base(
        data_patches=[
            {"target": "data/e.json", "operations": [{"op": "replace", "path": "/a", "overrides": "other"}]}
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert "override.bad_shape" in codes(m)


def test_unknown_on_miss_warns(tmp_path: Path) -> None:
    raw = base(
        data_patches=[
            {"target": "data/e.json", "operations": [{"op": "replace", "path": "/a", "on_miss": "explode"}]}
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert "expect.bad_shape" in codes(m)


def test_text_patch_overrides_and_expect_sha(tmp_path: Path) -> None:
    raw = base(
        text_patches=[
            {
                "target": "scripts/game.gd",
                "find": "var a = 1",
                "replace": "var a = 2",
                "expect_sha256": "0" * 64,
                "overrides": ["other.ext"],
            }
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw, {"scripts/game.gd": b"var a = 1\n"}))
    patch = m.data["text_patches"][0]
    assert patch.get("overrides") == ["other.ext"]
    assert patch.get("expect_sha256") == "0" * 64


def test_files_expect_is_kept(tmp_path: Path) -> None:
    raw = base(
        files=[
            {
                "source": "a.txt",
                "target": "mods/x/a.txt",
                "expect": {"sha256": "1" * 64, "size": 3},
            }
        ]
    )
    m = manifest.load(make_pkg(tmp_path, raw, {"a.txt": b"abc"}))
    rec = m.data["files"][0]
    assert rec["expect"]["sha256"] == "1" * 64
    assert rec["expect"]["size"] == 3


# ---------------------------------------------------------------- 字段白名单


def test_unknown_top_level_field_warns_not_errors(tmp_path: Path) -> None:
    m = manifest.load(make_pkg(tmp_path, base(mystery=1)))
    assert "field.unknown" in codes(m)
    assert not errors(m)
    assert "mystery" in m.data["extra"]


def test_x_prefixed_field_is_silent(tmp_path: Path) -> None:
    m = manifest.load(make_pkg(tmp_path, base(x_my_note="作者备注")))
    assert "field.unknown" not in codes(m)
    assert "x_my_note" not in (m.data.get("extra") or {})


# ---------------------------------------------------------------- 真实包形状


def test_real_package_shape_qingliang(tmp_path: Path) -> None:
    """真实包 ``qt6663.exp_potion_drops`` 用 ``"expect": "recovery_1"``。

    初版把它当 ``expect.bad_shape`` 判 error，导致这个包整包解析失败。
    """
    raw = base(
        id="qt6663.exp_potion_drops",
        data_patches=[
            {
                "target": "data/dungeon_loot.json",
                "operations": [
                    {
                        "op": "replace",
                        "path": "/routes/90031/potion_pool",
                        "expect": "recovery_1",
                        "value": "recovery_2",
                    }
                ],
            }
        ],
    )
    m = manifest.load(make_pkg(tmp_path, raw))
    assert not errors(m), [i.as_dict() for i in m.issues]
    assert m.data["data_patches"][0]["ops"][0]["expect"] == {"eq": "recovery_1"}
