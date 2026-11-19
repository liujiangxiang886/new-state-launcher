"""目录投影 × 分类：自定义线要进目录，分类只作用于「能启动的游戏」。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import catalog  # noqa: E402
from app.core import categories as cat  # noqa: E402


def _cfg(tmp_path: Path) -> dict:
    exe = tmp_path / "mod.exe"
    exe.write_bytes(b"MZ")
    pck = tmp_path / "pack.pck"
    pck.write_bytes(b"GDPC")
    return {
        "engines": {"4.6": {"kind": "editor"}},
        "baselines": {
            "jjxf.v1.45": {"engine": "4.6", "path": str(exe)},
            # 第二条基线用自定义 id 模拟「玩家自己登记的另一个本体」
            "jjxf.custom": {"engine": "4.6", "path": str(pck)},
        },
        "custom_lines": [
            {
                "id": "custom:abc",
                "name": "某改版",
                "kind": "pck",
                "path": str(pck),
                "engine_line": "4.6",
                "category": cat.MOD_ID,
            }
        ],
    }


def test_custom_lines_appear_in_catalog(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rows = catalog.entries(cfg)
    kinds = {r.kind for r in rows}
    assert "custom" in kinds, "自定义线必须进目录，否则界面列不全"
    custom = next(r for r in rows if r.kind == "custom")
    assert custom.name == "某改版"
    assert custom.category == cat.MOD_ID
    assert custom.detail == "pck 挂载"


def test_games_excludes_story_and_extension(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    games = catalog.games(cfg)
    assert games, "至少要有内置基线"
    assert all(g.kind in ("baseline", "custom") for g in games)


def test_baseline_categories(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    games = {g.id: g for g in catalog.games(cfg)}
    assert games["jjxf.v1.45"].category == cat.OFFICIAL_ID
    # 非 1.45 的基线默认归「改版」
    assert games["jjxf.custom"].category == cat.MOD_ID


def test_filter_games_by_category(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    mods = catalog.games(cfg, category=cat.MOD_ID)
    # 「改版」分类下既有登记的非原版本体，也有玩家自定义的改版线
    assert sorted(g.id for g in mods) == ["custom:abc", "jjxf.custom"]


def test_summary_counts_by_category(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    data = catalog.summary(cfg)
    by_cat = data["by_category"]
    assert by_cat[cat.OFFICIAL_ID] == 1
    assert by_cat[cat.MOD_ID] == 2


def test_entry_as_dict_has_category(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    row = catalog.games(cfg)[0]
    assert "category" in row.as_dict()
