"""扫描工程里引用了、但打包产物里没有的资源（计划 §4.4 路线 C 的验收手段）。

用法::

    # 拿着「工程目录」对「基线包」扫
    python tools/scan_missing_res.py "D:\\gobot工具\\demo" --baseline 4.6

    # 对着某个覆盖层 + 基线一起扫
    python tools/scan_missing_res.py "D:\\gobot工具\\demo" --baseline 4.6 --overlay packs/foo/overlay.pck

判据是「缺失清单为空」。误报来源有两类，都已处理：

1. **源文件 → 编译产物**：引用 ``assets/ui/x.png``，但包里只有
   ``.godot/imported/x.png-<md5>.ctex``；由 ``x.png.import`` 描述。
   只要 ``.import`` 在，就视为不缺。
2. **脚本类**：引用 ``scripts/x.gd``，包里是 ``x.gdc`` + ``x.gd.remap``。
   只要 ``.remap`` 在，就视为不缺。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod, paths  # noqa: E402
from app.core.baseline import path_set  # noqa: E402
from app.core.build import godot  # noqa: E402
from app.core.pck import PckReader  # noqa: E402


def _collect(packset: Path | None, line: str) -> set[str]:
    """合并「基线 + 覆盖层」的全部条目路径。"""
    out: set[str] = set()
    if packset is None:
        try:
            out |= set(path_set(line))
        except FileNotFoundError:
            print(f"[警告] 缺少 {line} 的基线索引，改用直接读 pck", file=sys.stderr)
            cfg = cfg_mod.load()
            bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
            meta = (cfg.get("baselines") or {}).get(bid) or {}
            p = Path(str(meta.get("path") or ""))
            if p.is_file():
                out |= {e.path for e in PckReader.open(p).entries}
        return out
    for pck in sorted(packset.parent.glob("*.pck")):
        out |= {e.path for e in PckReader.open(pck).entries}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫描工程里缺失的资源")
    ap.add_argument("project", help="Godot 工程目录（含 project.godot）")
    ap.add_argument("--baseline", default="4.6", choices=["4.6"])
    ap.add_argument("--overlay", default=None, help="额外叠加的覆盖层 pck 或目录")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--report", default=None, help="把结果写到指定 JSON")
    args = ap.parse_args(argv)

    project = Path(args.project)
    if not (project / "project.godot").is_file():
        print(f"不是 Godot 工程：{project}", file=sys.stderr)
        return 2

    print(f"工程：{project}")
    packed = _collect(None, args.baseline)
    print(f"基线 {args.baseline}：{len(packed):,} 条目")

    if args.overlay:
        overlay = Path(args.overlay)
        if overlay.is_file():
            packed |= {e.path for e in PckReader.open(overlay).entries}
        elif overlay.is_dir():
            for pck in overlay.glob("*.pck"):
                packed |= {e.path for e in PckReader.open(pck).entries}
        print(f"叠加覆盖层后：{len(packed):,} 条目")

    result = godot.scan_missing_resources(project, frozenset(packed))
    print(f"\n扫描 res:// 引用 {result['total_refs']:,} 处")
    missing = result["missing"]

    if missing:
        print(f"\n=== 缺失 {len(missing)} 项 ===")
        for target, users in list(missing.items())[: args.limit]:
            print(f"  {target}")
            for user in users[:3]:
                print(f"      ← {user}")
        if len(missing) > args.limit:
            print(f"  …还有 {len(missing) - args.limit} 项")
    else:
        print("\n缺失清单为空（路线 C 的验收判据达成）")

    payload = {
        "project": str(project),
        "baseline": args.baseline,
        "overlay": args.overlay,
        "total_refs": result["total_refs"],
        "missing_count": len(missing),
        "missing": {k: v for k, v in list(missing.items())[:500]},
    }
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {target}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())