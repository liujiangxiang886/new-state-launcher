"""分类模块：增删改、回落、默认归属。

这些断言盯的都是**会让玩家丢数据**的地方：
- 删分类后条目必须还在（回落到「其他」），不能指向空分类
- 改名不能让人重复
- 旧配置没有 ``categories`` 时必须能正常显示默认分类
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import categories as cat  # noqa: E402


def _cfg() -> dict:
    return {
        "baselines": {
            "jjxf.v1.45": {"engine": "4.6", "path": "x.exe"},
            "jjxf.custom": {"engine": "4.6", "path": "y.pck"},
        },
        "custom_lines": [
            {"id": "custom:aaa", "name": "A", "path": "a.exe", "category": ""},
        ],
    }


def test_defaults_without_config() -> None:
    """旧配置没有 categories 段 → 内置四个分类，且不写盘。"""
    cfg: dict = {}
    cats = cat.load(cfg)
    assert [c.id for c in cats] == [cat.OFFICIAL_ID, cat.STORY_ID, cat.MOD_ID, cat.OTHER_ID]
    assert "categories" not in cfg, "只读操作不该改配置"


def test_baseline_default_category() -> None:
    """1.45 默认归「原版」，其它本体默认归「改版」。"""
    cfg = _cfg()
    assert cat.category_of_baseline(cfg, "jjxf.v1.45") == cat.OFFICIAL_ID
    assert cat.category_of_baseline(cfg, "jjxf.custom") == cat.MOD_ID


def test_baseline_category_override() -> None:
    cfg = _cfg()
    assert cat.set_baseline_category(cfg, "jjxf.v1.45", cat.MOD_ID)
    assert cat.category_of_baseline(cfg, "jjxf.v1.45") == cat.MOD_ID


def test_add_rename_and_duplicate() -> None:
    cfg = _cfg()
    made = cat.add(cfg, "联机版")
    assert made.id.startswith("cat_")
    assert cat.name_of(cfg, made.id) == "联机版"

    with pytest.raises(ValueError, match="同名"):
        cat.add(cfg, "联机版")
    with pytest.raises(ValueError, match="同名"):
        cat.add(cfg, "  原版  ")  # 大小写/空格归一后撞名

    assert cat.rename(cfg, made.id, "多人联机")
    assert cat.name_of(cfg, made.id) == "多人联机"


def test_name_limits() -> None:
    cfg = _cfg()
    with pytest.raises(ValueError, match="不能为空"):
        cat.add(cfg, "   ")
    with pytest.raises(ValueError, match="最多"):
        cat.add(cfg, "x" * (cat.MAX_NAME + 1))


def test_remove_falls_back_to_other() -> None:
    """删掉分类后，归在它下面的条目必须改到「其他」——不能丢。"""
    cfg = _cfg()
    made = cat.add(cfg, "临时")
    cat.set_baseline_category(cfg, "jjxf.v1.45", made.id)
    cfg["custom_lines"][0]["category"] = made.id

    assert cat.remove(cfg, made.id)

    assert cat.category_of_baseline(cfg, "jjxf.v1.45") == cat.OTHER_ID
    assert cfg["custom_lines"][0]["category"] == cat.OTHER_ID
    assert made.id not in cat.sorted_ids(cfg)


def test_cannot_remove_protected() -> None:
    cfg = _cfg()
    with pytest.raises(ValueError, match="回落目标"):
        cat.remove(cfg, cat.OTHER_ID)


def test_move_reorders() -> None:
    cfg = _cfg()
    before = cat.sorted_ids(cfg)
    assert cat.move(cfg, cat.STORY_ID, -1)
    after = cat.sorted_ids(cfg)
    assert after.index(cat.STORY_ID) < before.index(cat.STORY_ID)
    # 上移后 story 到了首位，再上移就该越界返回 False（不抛）
    assert after[0] == cat.STORY_ID
    assert not cat.move(cfg, cat.STORY_ID, -1)


def test_reassign_counts() -> None:
    cfg = _cfg()
    cfg["custom_lines"].append(
        {"id": "custom:bbb", "name": "B", "path": "b.exe", "category": cat.STORY_ID}
    )
    moved = cat.reassign(cfg, cat.STORY_ID, cat.MOD_ID)
    assert moved == 1
    assert cfg["custom_lines"][1]["category"] == cat.MOD_ID


def test_broken_config_is_tolerated() -> None:
    """配置被手改坏时跳过坏项，不能让界面整块崩掉。"""
    cfg = {
        "categories": [
            {"id": "BAD ID!", "name": "非法"},
            {"id": "official", "name": "原版"},
            {"name": "没有 id"},
        ]
    }
    ids = cat.sorted_ids(cfg)
    assert cat.OFFICIAL_ID in ids
    assert "BAD ID!" not in ids
    assert cat.OTHER_ID in ids
