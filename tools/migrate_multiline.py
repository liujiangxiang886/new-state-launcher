"""把旧格式的扩展安装迁移到多线格式。

旧格式：``packs/<id>/overlay.pck`` + 顶层 ``engine_line``
新格式：``packs/<id>/overlay-<线>.pck`` + ``lines`` 映射

迁移只做重命名与注册表补写，不重新构建覆盖层（内容不变）。
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


def main() -> int:
    cfg = cfg_mod.load()
    svc = ExtensionService(cfg)
    items = svc.installed()

    migrated = 0
    for item in items:
        ext_id = str(item.get("id", ""))
        line = str(item.get("engine_line", ""))
        if not ext_id or not line:
            continue

        # 已经迁过
        lines = item.get("lines")
        if isinstance(lines, dict) and lines.get(line):
            continue

        tag = line.replace(".", "")
        root = paths.PACKS / ext_id
        old_overlay = root / "overlay.pck"
        new_overlay = root / f"overlay-{tag}.pck"
        if old_overlay.exists() and not new_overlay.exists():
            shutil.copyfile(old_overlay, new_overlay)
            print(f"  复制 {old_overlay.name} -> {new_overlay.name}")

        old_manifest = root / "manifest.v2.json"
        new_manifest = root / f"manifest-{tag}.v2.json"
        if old_manifest.exists() and not new_manifest.exists():
            shutil.copyfile(old_manifest, new_manifest)
            print(f"  复制 {old_manifest.name} -> {new_manifest.name}")

        line_entry = {
            "line": line,
            "baseline": item.get("baseline", ""),
            "status": item.get("status", ""),
            "overlay": new_overlay.as_posix(),
            "entry_count": item.get("entry_count", 0),
            "sha256": item.get("sha256", ""),
            "verified": item.get("verified", False),
            "runtime_hooks": item.get("runtime_hooks") or [],
            "patches": item.get("patches") or [],
            "installed_at": item.get("installed_at", ""),
            "hooks_pending": item.get("hooks_pending", 0),
        }
        item["lines"] = {line: line_entry}
        item["overlay"] = new_overlay.as_posix()
        migrated += 1
        print(f"  {ext_id} -> 线 {line}")

    if migrated:
        svc._save_registry(items)
    print(f"\n迁移完成：{migrated} 个扩展")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
