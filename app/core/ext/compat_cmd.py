"""扩展兼容性命令行。

    python -m app.cli compat <包.zip> --line 4.6
    python -m app.cli compat <合集.zip> --line 4.6 --json

对每个扩展做静态试装，给出「兼容 / 部分兼容 / 不兼容」的结论与具体原因。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core import config as cfg_mod


def cmd_compat(args) -> int:
    """检查一个扩展包（或合集包）对某条引擎线的兼容性。"""
    from app.core.ext import compat

    cfg = cfg_mod.load()
    line = args.line
    results = compat.probe_bundle(args.package, line, cfg)

    if args.json:
        payload = {
            "package": str(args.package),
            "line": line,
            "results": [r.as_dict() for r in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"包：{args.package}")
    print(f"目标引擎线：{line}\n")

    width = max((len(r.ext_id) for r in results), default=10)
    counts = {"compatible": 0, "partial": 0, "incompatible": 0, "unknown": 0}

    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
        mark = {"compatible": "OK  ", "partial": "部分", "incompatible": "不兼容", "unknown": "待查"}[r.verdict]
        print(
            f"  [{mark}] {r.ext_id:<{width}}  v{r.version:<8} "
            f"补丁 {r.patch_ok}/{r.patch_total}  覆盖 {r.override_hit} 命中 / {r.override_new} 新增"
        )
        for msg in r.failures[:3]:
            print(f"           - {msg}")

    print(
        f"\n合计：兼容 {counts['compatible']}，部分兼容 {counts['partial']}，"
        f"不兼容 {counts['incompatible']}，待查 {counts['unknown']}"
    )
    if counts["partial"] or counts["incompatible"]:
        print(
            "\n提示：部分兼容的包可以用 "
            "`ext <包> --install --line <线> --allow-partial` 装，"
            "没命中的补丁会被跳过（安装时会强制冒烟验证）。"
        )
    return 0
