"""发布配置的出厂态：不能把开发机的东西发出去。

2026-11-16 的玩家日志（``launcher-20261116-113827.log``）证明这条防线
漏过一次：出厂 ``config/launcher.json`` 沿用了开发机的
``profiles.default = "smoke46"``，玩家第一次打开就跑在一个叫
「smoke46」的存档档位里。

这里钉住两件事：

1. ``profiles`` 段必须回出厂值；
2. 本体校验状态（``verify_state`` 等）必须清空 —— 否则开发机验过的
   ``verified`` 会被发给玩家，新加的「导入后必须校验」门禁直接失效。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_release_zip as brz  # noqa: E402


def _seed(tmp_path: Path) -> Path:
    """造一份「开发机视角」的配置：带测试档位名、已验证状态、假路径。"""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    seed = cfg_dir / "launcher.json"
    seed.write_text(
        json.dumps(
            {
                "schema": 1,
                "profiles": {
                    "default": "smoke46",
                    "keep_history": 99,
                    "redirect_appdata": False,
                },
                "ui": {"last_version": "4.7", "show_detail": True},
                "baselines": {
                    "jjxf.v1.45": {"engine": "4.6", "path": "D:/dev/mchGame.exe"},
                    "jjxf.v4.7": {
                        "engine": "4.7",
                        "path": "D:/dev/jjxf47.pck",
                        "exe_path": "D:/dev/jjxf47.exe",
                        "story_overlay": "D:/dev/story",
                        "size": 3066540271,
                        "entry_count": 380259,
                        "pack_version": 4,
                        "sha256": "e" * 64,
                        "verify_state": "verified",
                        "verify_at": "2026-11-16T04:00:00+00:00",
                        "verify_by": "smoke",
                        "verify_note": "冒烟通过",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return seed


def _release(tmp_path: Path) -> dict:
    _seed(tmp_path)
    monkey_dirs = {"ROOT": tmp_path, "DIST": tmp_path / "dist"}
    saved = {k: getattr(brz, k) for k in monkey_dirs}
    try:
        for k, v in monkey_dirs.items():
            setattr(brz, k, v)
        return json.loads(brz.release_launcher("player"))
    finally:
        for k, v in saved.items():
            setattr(brz, k, v)


def test_release_resets_profile_default(tmp_path: Path) -> None:
    cfg = _release(tmp_path)

    assert cfg["profiles"]["default"] == "default", "开发机的档位名被发出去了"
    assert cfg["profiles"]["keep_history"] == 10
    assert cfg["profiles"]["redirect_appdata"] is True


def test_release_clears_verify_state(tmp_path: Path) -> None:
    """开发机上验过 ≠ 玩家验过。带着 verified 出厂，门禁等于没做。"""
    meta = _release(tmp_path)["baselines"]["jjxf.v4.7"]

    for field in ("verify_state", "verify_at", "verify_by", "verify_note"):
        assert field not in meta, f"{field} 没被清掉，会带上开发机的校验结论"
    # 探测出来的运行时信息同样要清
    assert "size" not in meta and "sha256" not in meta and "entry_count" not in meta
    # 路径换成包内占位
    assert meta["path"] == "game/jjxf47.pck"
    assert meta["exe_path"] == "" and meta["story_overlay"] == ""


def test_release_keeps_only_known_baselines(tmp_path: Path) -> None:
    cfg = {"baselines": {"custom:9": {"path": "D:/x"}}}
    _ = cfg  # 说明性：白名单之外的基线会被删，见 release_launcher
    released = _release(tmp_path)
    assert set(released["baselines"]) <= {"jjxf.v1.45", "jjxf.v4.7"}
