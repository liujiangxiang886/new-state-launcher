"""schema 4 的机读契约：`schemas/extension.v4.schema.json` 必须与代码同义。

为什么单独测 schema 文件
------------------------

``manifest.py`` 是手写校验器，schema 文件是**给作者和 AI 看的契约**。两者
最容易悄悄分叉：代码加了规则、schema 忘了改，作者照着 schema 写包，装的时候
才被拒。这里把三条最容易分叉的规则钉住：

1. ``schema`` 常量是 4（不是 3）；
2. 写了 ``text_patches`` / ``method_hooks`` 却没写 ``sources`` → 必须报错
   （这是 v4 存在的唯一理由，靠 ``if/then`` 表达；``schemacheck`` 若不支持
   条件必填，这条测试会立刻失败，而不是静默放过）；
3. 纯资源包（只有 ``files``）不写 ``sources`` → 必须放行，不能一刀切。

外加一条历史遗留：v3 里 ``engine.line`` 曾只允许 ``4.6``，4.7 已是正式线，
v3 也必须接受 4.7（存量 4.7 包大量是 schema 3）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import schemacheck  # noqa: E402

SCHEMAS = ROOT / "schemas"


def _v4():
    return schemacheck.load_schema(SCHEMAS / "extension.v4.schema.json")


def _v3():
    return schemacheck.load_schema(SCHEMAS / "extension.v3.schema.json")


def _hook_manifest(**over) -> dict:
    data = {
        "schema": 4,
        "id": "demo.hooked",
        "name": "示例",
        "version": "1.0.0",
        "sources": {"name": "sources-4.7.zip", "sha256": "a" * 64, "line": "4.7"},
        "method_hooks": [
            {"target": "scripts/game.gd", "method": "combat_power", "phase": "after"}
        ],
    }
    data.update(over)
    return data


def test_v4_schema_file_exists_and_declares_schema_4() -> None:
    path = SCHEMAS / "extension.v4.schema.json"
    assert path.is_file(), "缺少 schemas/extension.v4.schema.json"
    assert _v4()["properties"]["schema"]["const"] == 4


def test_v4_accepts_hooked_manifest_with_sources() -> None:
    errs = schemacheck.validate(_hook_manifest(), _v4())
    assert not errs, schemacheck.format_errors(errs, 5)


def test_v4_rejects_hooks_without_sources() -> None:
    bad = _hook_manifest()
    bad.pop("sources")
    errs = schemacheck.validate(bad, _v4())
    assert errs, "有 method_hooks 但缺 sources，schema 4 必须拒绝"
    assert any("sources" in msg for _path, msg in errs), errs


def test_v4_rejects_text_patches_without_sources() -> None:
    bad = _hook_manifest(method_hooks=None)
    bad.pop("method_hooks")
    bad["text_patches"] = [
        {"target": "scripts/game.gd", "find": "func combat_power(", "replace": "func combat_power("}
    ]
    bad.pop("sources")
    assert schemacheck.validate(bad, _v4()), "有 text_patches 但缺 sources，schema 4 必须拒绝"


def test_v4_allows_pure_resource_package_without_sources() -> None:
    pure = {
        "schema": 4,
        "id": "demo.res",
        "name": "纯资源",
        "version": "1.0.0",
        "files": [{"source": "payload/a.json", "target": "data/a.json"}],
    }
    errs = schemacheck.validate(pure, _v4())
    assert not errs, schemacheck.format_errors(errs, 5)


def test_v4_sources_accepts_short_string_form() -> None:
    """``sources`` 允许简写成字符串（manifest.py 会补成 {"name": ...}）。"""
    errs = schemacheck.validate(_hook_manifest(sources="sources-4.7.zip"), _v4())
    assert not errs, schemacheck.format_errors(errs, 5)


def test_v3_schema_accepts_47_line() -> None:
    """4.7 是正式线；v3 里 engine.line 只写 4.6 会让存量 4.7 包校验失败。"""
    data = {
        "schema": 3,
        "id": "demo.47",
        "name": "4.7 包",
        "version": "1.0.0",
        "engine": {"line": "4.7"},
        "sources": "sources-4.7.zip",
    }
    errs = schemacheck.validate(data, _v3())
    assert not errs, schemacheck.format_errors(errs, 5)


def test_both_schemas_are_loadable() -> None:
    for name in ("extension.v2.schema.json", "extension.v3.schema.json", "extension.v4.schema.json"):
        assert isinstance(schemacheck.load_schema(SCHEMAS / name), dict), name
