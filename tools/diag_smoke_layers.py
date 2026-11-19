"""4.6 冒烟对照实验：逐步加挂载层，定位 Parse Error 的来源。

用法::

    python tools/diag_smoke_layers.py

要点：
- 必须在**导入 app.core 之前**设好 ``JJXF_ROOT``，因为 ``paths.ROOT`` 是
  模块级常量，import 时就固定了（踩过：设晚了会解析成 ``D:\\d\\...``）。
- 每个用例单独跑一次引擎，只比 Parse Error 条数与冒烟结果。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "dist" / "机甲旋风兼容启动器"
os.environ["JJXF_ROOT"] = str(TARGET)

sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import runner  # noqa: E402

BASELINE_OFFSET = 104599040


def _run(cfg: dict, tag: str, packs: list[dict]) -> tuple[int, list[str], list[str]]:
    rd = TARGET / "builds" / "_diag"
    rd.mkdir(parents=True, exist_ok=True)
    ps = rd / f"{tag}.json"
    ps.write_text(json.dumps({"schema": 1, "packs": packs}, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    cmd = runner.build_command(
        cfg, "4.6", packset=ps, main_scene="res://scenes/start_page.tscn",
        smoke=True, headless=True, log_path=rd / f"{tag}_mount.json", mods_path=None,
    )
    env, _ = runner.build_env(cfg, "diag46")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=280)
    out = (proc.stdout or "") + (proc.stderr or "")
    parse = [l for l in out.splitlines() if "Parse Error" in l]
    smoke = [l for l in out.splitlines() if '"smoke"' in l]
    return proc.returncode, parse, smoke


def main() -> int:
    cfg = cfg_mod.load()
    base = cfg["baselines"]["jjxf.v1.45"]["path"]
    baseline_entry = {"path": base, "offset": BASELINE_OFFSET, "role": "baseline"}

    rd = ROOT / "builds" / "_smoke2" / "run46"
    packset_file = rd / "packset.json"
    extra: list[dict] = []
    if packset_file.exists():
        data = json.loads(packset_file.read_text(encoding="utf-8"))
        extra = [p for p in data.get("packs", [])
                 if isinstance(p, dict) and p.get("role") != "baseline"]

    cases = [("A_只有基线", [baseline_entry])]
    inst = [p for p in extra if p.get("role") == "instrumented"]
    mods = [p for p in extra if p.get("role") != "instrumented"]
    if mods:
        cases.append(("B_基线+全部overlay", [baseline_entry] + mods))
    if inst:
        cases.append(("C_基线+插桩层", [baseline_entry] + inst))
    if mods and inst:
        cases.append(("D_全部（当前实现）", [baseline_entry] + mods + inst))

    for tag, packs in cases:
        try:
            rc, parse, smoke = _run(cfg, tag, packs)
            print(f"{tag:22s} rc={rc} ParseError={len(parse):2d} smoke={smoke[:1]}")
            for line in parse[:3]:
                print(f"    {line[:110]}")
        except Exception as exc:  # noqa: BLE001 - 单用例失败不影响其它
            print(f"{tag:22s} 失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())