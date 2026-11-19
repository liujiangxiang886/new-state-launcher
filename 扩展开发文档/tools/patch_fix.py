#!/usr/bin/env python3
"""对转换后的 4.7 包做「锚点 + 替换体」的双向修正。

为什么需要这个：`text_patches` 的 `find` 失配时，光改 `find` 是不够的 ——
`replace` 里往往也带着旧的函数签名。只改一边会导致补丁「命中但写错」。

本脚本接受一份显式的修正表（旧串 -> 新串），同时对 `find` 和 `replace`
做替换，保证两边同步。

用法::

    python tools/patch_fix.py 包.zip 修正表.json
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path


def apply_fixes(manifest: dict, rules: list[dict]) -> tuple[int, list[str]]:
    """在 manifest 的 text_patches 上应用修正规则。

    每条规则形如::

        {"target": "scripts/item_cell.gd",
         "old": "旧的片段",
         "new": "新的片段",
         "note": "为什么这么改"}

    `old`/`new` 会同时作用于 `find` 和 `replace`。
    """
    patches = manifest.get("text_patches") or []
    changed = 0
    log: list[str] = []

    for rule in rules:
        target = rule["target"]
        old = rule["old"]
        new = rule["new"]
        note = rule.get("note", "")
        hit_any = False
        for i, patch in enumerate(patches):
            if str(patch.get("target", "")) != target:
                continue
            for field in ("find", "replace"):
                val = patch.get(field)
                if not isinstance(val, str) or old not in val:
                    continue
                patch[field] = val.replace(old, new)
                hit_any = True
                log.append(f"  [{i}].{field} 已更新")
        if hit_any:
            changed += 1
            log.append(f"✓ {target}：{note}")
        else:
            log.append(f"○ {target}：规则未命中（{note}）")
    return changed, log


def rewrite_zip(src: Path, dst: Path, manifest: dict) -> None:
    """重写 zip。

    注意 ``src == dst`` 的情况（原地改）：必须**先把内容全读进内存**再开写，
    否则 ``ZipFile(dst, "w")`` 会在读取之前把自己截断 —— 报
    ``BadZipFile: Truncated file header``。
    """
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    # 先把要保留的条目全部读出来
    payload: list[tuple[zipfile.ZipInfo, bytes]] = []
    with zipfile.ZipFile(src) as zin:
        for item in zin.infolist():
            if item.filename in ("extension.json", "manifest-47.v2.json"):
                continue
            payload.append((item, zin.read(item.filename)))

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item, data in payload:
            zout.writestr(item, data)
        zout.writestr("extension.json", text)
        zout.writestr("manifest-47.v2.json", text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按修正表修 text_patches")
    ap.add_argument("package")
    ap.add_argument("rules")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    pkg = Path(args.package)
    rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
    if isinstance(rules, dict):
        rules = rules.get("rules") or []

    z = zipfile.ZipFile(pkg)
    name = "manifest-47.v2.json" if "manifest-47.v2.json" in z.namelist() else "extension.json"
    manifest = json.loads(z.read(name).decode("utf-8"))

    changed, log = apply_fixes(manifest, rules)
    print(f"{manifest.get('id')}  应用 {changed} 条规则")
    for line in log:
        print(line)

    out = Path(args.out) if args.out else pkg.with_name(pkg.stem + "-fixed.zip")
    rewrite_zip(pkg, out, manifest)
    print(f"\n已写出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())