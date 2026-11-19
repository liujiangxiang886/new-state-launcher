"""1.45（4.6 线）启动故障诊断。

用法：
    python tools/diag_145.py base            # 只挂基线，不装任何扩展
    python tools/diag_145.py ID1,ID2         # 只启用指定扩展
    python tools/diag_145.py all             # 启用所有 4.6 线扩展（复现故障）

实现：用 monkeypatch 让 ExtensionService.installed 只返回指定子集，
**不修改磁盘上的真实注册表**，安全隔离。

输出：mount_report / smoke_report / 退出码 / stderr 尾部，据此判断能否启动。
所有运行走独立 profile（profiles/diag145_*），不会动真实存档。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as cfg_mod, runner, readiness as readiness_mod, paths
from app.core.ext import enable as ext_enable

LINE = "4.6"


def _patch_installed(subset: set[str]) -> None:
    registry = json.loads((paths.CONFIG_DIR / "extensions.json").read_text(encoding="utf-8"))
    items = [
        it
        for it in registry.get("items", [])
        if it["id"] in subset and it.get("enabled", True)
    ]
    def _fake_enabled_mods(cfg, line):  # noqa: ANN001 - monkeypatch
        return items
    ext_enable.enabled_mods = _fake_enabled_mods


def smoke(tag: str, enabled_ids: set[str]) -> dict:
    _patch_installed(enabled_ids)
    cfg = cfg_mod.load()  # 仅用于基线/引擎配置，不含扩展
    rd = runner.run_dir_for(f"diag46-{tag}")
    mods = ext_enable.build_for_launch(cfg, LINE, rd)
    from app.core import story as story_mod

    bid = readiness_mod.baseline_id(LINE)
    packs = [
        *runner.baseline_packs(cfg, bid),
        *mods.packs,
        *story_mod.enabled_overlays(LINE),
    ]
    packset = runner.make_packset(
        rd / "packset.json",
        packset_id=f"diag-{tag}",
        line=LINE,
        packs=packs,
        baseline_id=bid,
    )
    result = runner.launch(
        cfg,
        LINE,
        packset=packset,
        smoke=True,
        headless=True,
        profile=f"diag145_{tag}",
        run_dir=rd,
        timeout=240,
        mods_path=mods.mods_json,
    )
    stderr = result.stderr or ""
    has_err = "SCRIPT ERROR" in stderr or "ERROR:" in stderr
    return {
        "tag": tag,
        "enabled": sorted(enabled_ids),
        "ok": result.ok,
        "returncode": result.returncode,
        "seconds": round(result.seconds, 1),
        "mount_ok": (result.mount_report or {}).get("ok"),
        "mount_errors": (result.mount_report or {}).get("errors", [])[:6],
        "mount_mounted_count": len((result.mount_report or {}).get("mounted", [])),
        "smoke": (result.smoke_report or {}).get("smoke"),
        "has_script_error": has_err,
        "error_lines": [ln.strip() for ln in stderr.splitlines() if ("SCRIPT ERROR" in ln or "ERROR:" in ln)][:8],
        "notes": result.notes,
    }


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "base").strip()
    registry = json.loads((paths.CONFIG_DIR / "extensions.json").read_text(encoding="utf-8"))
    all_ids = {
        it["id"]
        for it in registry.get("items", [])
        if it.get("enabled", True)
        and isinstance((it.get("lines") or {}).get(LINE), dict)
    }
    if arg == "base":
        enabled: set[str] = set()
        tag = "base"
    elif arg == "all":
        enabled = all_ids
        tag = "all"
    elif arg == "each":
        # 逐个启用，隔离故障扩展
        results = []
        for ext_id in sorted(all_ids):
            out = smoke(f"one_{ext_id}", {ext_id})
            results.append(out)
            flag = "ERR" if out.get("has_script_error") else "ok "
            print(f"  {flag} {ext_id:<28} rc={out['returncode']} smoke={out['smoke']} mount_ok={out['mount_ok']}")
            for ln in out.get("error_lines", [])[:4]:
                print(f"        {ln}")
        bad = [r["enabled"][0] for r in results if r.get("has_script_error")]
        print(f"[each] 共 {len(results)} 个，引入脚本错误的扩展：{bad or '无'}")
        return 0
    else:
        enabled = {x for x in arg.split(",") if x}
        tag = "sel"
    print(f"[diag] line={LINE} tag={tag} enabled({len(enabled)})={sorted(enabled)}")
    out = smoke(tag, enabled)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
