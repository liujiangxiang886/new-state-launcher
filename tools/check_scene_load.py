"""验证 begin() 的目标场景能否正常加载。

思路比「点按钮看画面变没变」确定得多：``start_page.gd::begin()`` 的最后一步是
``change_scene_to_file("res://scenes/role_select.tscn")``。这个场景以及它依赖的
``Game`` autoload 只要能加载成功，就说明点击后能进去。

用法::

    python tools/check_scene_load.py                 # 依次测 start_page / role_select / character_create
    python tools/check_scene_load.py --scene role_select

判定：rc == 0 且 smoke 报告为 ok 就算通过；出现 SCRIPT ERROR 一律算失败。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["JJXF_ROOT"] = str(ROOT / "dist" / "机甲旋风兼容启动器")
sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import runner  # noqa: E402

RUN_DIR = ROOT / "builds" / "_smoke4" / "run46"

#: 场景名 → res:// 路径
SCENES = {
    "start_page": "res://scenes/start_page.tscn",
    "role_select": "res://scenes/role_select.tscn",
    "character_create": "res://scenes/character_create.tscn",
}


def probe(cfg: dict, name: str) -> dict[str, object]:
    scene = SCENES[name]
    rd = RUN_DIR
    log = rd / f"scene_{name}.json"
    cmd = runner.build_command(
        cfg, "4.6", packset=rd / "packset.json", main_scene=scene,
        smoke=True, headless=True, log_path=log, mods_path=rd / "mods.json",
    )
    env, _ = runner.build_env(cfg, f"scene_{name}")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=300)
    out = (proc.stdout or "") + (proc.stderr or "")
    parse = [l for l in out.splitlines() if "Parse Error" in l]
    script = [l for l in out.splitlines() if "SCRIPT ERROR" in l and "Parse Error" not in l]
    smoke_line = next((l for l in out.splitlines() if '"smoke"' in l), "")
    smoke = {}
    try:
        smoke = json.loads(smoke_line) if smoke_line else {}
    except json.JSONDecodeError:
        smoke = {}
    return {
        "scene": scene,
        "rc": proc.returncode,
        "nodes": smoke.get("nodes"),
        "smoke": smoke.get("smoke"),
        "parse_errors": len(parse),
        "script_errors": len(script),
        "first_error": (parse + script)[0][:140] if (parse or script) else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", default="", help="只测某个场景（默认全测）")
    args = parser.parse_args()

    cfg = cfg_mod.load()
    names = [args.scene] if args.scene else list(SCENES)
    results: list[dict[str, object]] = []
    for name in names:
        try:
            results.append({"name": name, **probe(cfg, name)})
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "error": str(exc)})

    failed = 0
    for r in results:
        if "error" in r:
            print(f"[FAIL] {r['name']:18s} 异常：{r['error']}")
            failed += 1
            continue
        ok = (r["rc"] == 0 and r["smoke"] == "ok"
              and r["parse_errors"] == 0 and r["script_errors"] == 0)
        tag = "[PASS] " if ok else "[FAIL] "
        print(f"{tag}{r['name']:18s} rc={r['rc']} nodes={r['nodes']} "
              f"parse={r['parse_errors']} script={r['script_errors']}")
        if not ok:
            failed += 1
            if r["first_error"]:
                print(f"        {r['first_error']}")
    print()
    print("全部通过" if not failed else f"失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())