"""清掉指定引擎线上的扩展安装（只删该线的覆盖层与登记）。

用途：跨版本安装实验前把某条线复位，保证结果可复现。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import paths  # noqa: E402
from app.core.ext.service import ExtensionService  # noqa: E402


def main(argv: list[str]) -> int:
    line = argv[0] if argv else "4.6"
    cfg = cfg_mod.load()
    svc = ExtensionService(cfg)
    items = svc.installed()

    tag = line.replace(".", "")
    kept: list[dict] = []
    removed: list[str] = []

    for item in items:
        ext_id = str(item.get("id", ""))
        lines = item.get("lines")
        if not isinstance(lines, dict) or line not in lines:
            kept.append(item)
            continue

        # 删该线的覆盖层与清单
        root = paths.PACKS / ext_id
        for name in (f"overlay-{tag}.pck", f"manifest-{tag}.v2.json"):
            p = root / name
            if p.exists():
                p.unlink()
                print(f"  删除 {ext_id}/{name}")

        remaining = {k: v for k, v in lines.items() if k != line}
        if remaining:
            # 还有别的线 → 保留条目，只去掉这一线
            item["lines"] = remaining
            last = remaining[sorted(remaining)[-1]]
            item.update(
                {
                    "engine_line": last.get("line", ""),
                    "baseline": last.get("baseline", ""),
                    "status": last.get("status", ""),
                    "overlay": last.get("overlay", ""),
                    "entry_count": last.get("entry_count", 0),
                    "sha256": last.get("sha256", ""),
                    "verified": last.get("verified", False),
                    "runtime_hooks": last.get("runtime_hooks") or [],
                    "patches": last.get("patches") or [],
                    "hooks_pending": last.get("hooks_pending", 0),
                }
            )
            kept.append(item)
        else:
            removed.append(ext_id)
            # 整个扩展都没了 → 删目录
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
                print(f"  删除目录 {root.name}")

    svc._save_registry(kept)
    print(f"\n线 {line}：移除 {len(removed)} 个扩展，保留 {len(kept)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
