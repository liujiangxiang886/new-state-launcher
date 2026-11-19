"""``expect`` 断言的边界：``exists: false`` 必须能通过。

`add` 型数据补丁要防「别人已经占了这个键」，写法就是
``expect: {"exists": false}``。原实现里 exists 比对通过后仍无条件抛
「引用的位置不存在」，导致这条断言永远不可能成立——等于该写法不可用。
2026-11-05 修掉，这里把四种组合钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ext import patcher  # noqa: E402
from app.core.ext.patcher import PatchError  # noqa: E402


def test_exists_false_passes_when_absent():
    doc = {"routes": {}}
    out, applied, _ = patcher.apply_json(
        doc,
        [{"op": "add", "path": "/routes/90031", "expect": {"exists": False}, "value": {"a": 1}}],
    )
    assert applied == 1
    assert out["routes"]["90031"] == {"a": 1}


def test_exists_false_blocks_when_taken():
    doc = {"routes": {"90031": {"a": 0}}}
    with pytest.raises(PatchError) as exc:
        patcher.apply_json(
            doc,
            [{"op": "add", "path": "/routes/90031", "expect": {"exists": False}, "value": {"a": 1}}],
        )
    assert "期望 不存在" in str(exc.value)


def test_other_expect_keys_still_assert():
    """exists:false 的放行不能把 eq / type 这些断言一起放过。"""
    with pytest.raises(PatchError):
        patcher.apply_json({"a": 1}, [{"op": "replace", "path": "/a", "expect": {"eq": 2}, "value": 3}])
    with pytest.raises(PatchError):
        patcher.apply_json({"a": 1}, [{"op": "replace", "path": "/a", "expect": {"type": "string"}, "value": 3}])


def test_missing_position_without_exists_false_still_errors():
    """没写 exists:false 却引用不存在的位置，仍然要报错，不能静默。"""
    with pytest.raises(PatchError) as exc:
        patcher.apply_json({"a": 1}, [{"op": "replace", "path": "/b", "expect": {"eq": 2}, "value": 3}])
    assert "不存在" in str(exc.value)
