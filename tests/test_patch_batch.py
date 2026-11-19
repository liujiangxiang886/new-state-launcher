"""同一个目标多条 data_patch 的合并语义（完全校验导入提速的一部分）。

背景（2026-11-17）：``patch_bytes`` 原来每条 data_patch 都把整份 JSON
解析、深拷贝、再序列化一次。一个几 MB 的 ``items.json`` 上 14 条补丁，
光 deepcopy 就 11.5 秒。现在严格路径（``allow_partial=False`` 且所有补丁
都是「失败即中止」）合并成一次解析 + 一次序列化。

这里锁住两件事：
1. 合并后结果与逐条应用一致（含逐条的 applied 计数）；
2. 只要有一条补丁声明了 ``on_test_fail: skip``，就**不**合并 —— 那条路径
   依赖「失败的那条整条丢弃、前面已应用的保留」，语义不能变。
"""

from __future__ import annotations

import json

import pytest

from app.core.ext import patcher
from app.core.ext.patcher import PatchError


def _base() -> bytes:
    return json.dumps(
        {"items": {"a": {"v": 1}, "b": {"v": 2}}, "meta": {"ver": 3}},
        ensure_ascii=False,
    ).encode("utf-8")


def test_strict_path_merges_and_keeps_per_patch_counts() -> None:
    patches = [
        {"target": "data/items.json", "ops": [
            {"op": "replace", "path": "/items/a/v", "value": 10},
        ]},
        {"target": "data/items.json", "ops": [
            {"op": "replace", "path": "/items/b/v", "value": 20},
            {"op": "replace", "path": "/meta/ver", "value": 4},
        ]},
    ]
    out, reports = patcher.patch_bytes("data/items.json", _base(), [], patches)
    doc = json.loads(out.decode("utf-8"))

    assert doc["items"]["a"]["v"] == 10
    assert doc["items"]["b"]["v"] == 20
    assert doc["meta"]["ver"] == 4
    # 逐条的 applied 计数必须还在：安装结果用它判断「哪些补丁没落地」
    assert [r.applied for r in reports] == [1, 2]


def test_strict_path_aborts_on_first_bad_patch() -> None:
    patches = [
        {"target": "data/items.json", "ops": [
            {"op": "replace", "path": "/items/a/v", "value": 10},
        ]},
        {"target": "data/items.json", "ops": [
            {"op": "replace", "path": "/missing/x", "value": 1},
        ]},
    ]
    with pytest.raises(PatchError):
        patcher.patch_bytes("data/items.json", _base(), [], patches)


def test_on_test_fail_skip_keeps_per_patch_semantics() -> None:
    """带 ``on_test_fail: skip`` 时不能合并：失败那条整条丢弃，其余照旧。"""
    patches = [
        {"target": "data/items.json", "on_test_fail": "skip", "ops": [
            {"op": "test", "path": "/items/a/v", "value": 999},   # 必失败
        ]},
        {"target": "data/items.json", "ops": [
            {"op": "replace", "path": "/items/b/v", "value": 20},
        ]},
    ]
    out, reports = patcher.patch_bytes("data/items.json", _base(), [], patches)
    doc = json.loads(out.decode("utf-8"))

    assert doc["items"]["a"]["v"] == 1          # 失败那条没留下任何改动
    assert doc["items"]["b"]["v"] == 20         # 后一条照常生效
    assert reports[0].applied == 0 and reports[0].issues
    assert reports[1].applied == 1


def test_inplace_apply_json_reuses_document() -> None:
    doc = {"a": {"v": 1}}
    out, applied, _ = patcher.apply_json(
        doc, [{"op": "replace", "path": "/a/v", "value": 2}], inplace=True
    )
    assert applied == 1 and out["a"]["v"] == 2
    # 默认仍然是拷贝：调用方复用 doc 时不能被悄悄改掉
    doc2 = {"a": {"v": 1}}
    patcher.apply_json(doc2, [{"op": "replace", "path": "/a/v", "value": 9}])
    assert doc2["a"]["v"] == 1
