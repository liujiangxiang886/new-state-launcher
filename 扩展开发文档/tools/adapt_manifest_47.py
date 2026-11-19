#!/usr/bin/env python3
"""把 4.6 扩展包的清单转换成 4.7 格式（`manifest-47.v2.json`）。

**只做清单转换，不碰补丁内容。**

这一步解决的是「结构性」问题：

- 老包只有 `extension.json`，schema 1/2，`baseline` 是字符串或
  `jjxf.v1.45.original`
- 4.7 需要 `manifest-47.v2.json`，schema 3，`baseline.id = jjxf.v4.7`
- 老字段名要迁移：`entry` → `scripts.entry`、`script_members` →
  `scripts.members`

**`text_patches` 的锚点不在本脚本职责内** —— 那是另一层工作，必须
对照 4.7 源码逐条核对（见 `docs/4.7-mod打包规范.md` §4.3）。
本脚本只负责把锚点原样搬过去，命中与否交给后续 verify 判定。

用法::

    python tools/adapt_manifest_47.py 输入.zip --author "竹知夜"
    python tools/adapt_manifest_47.py 目录/ --author "竹知夜" --out 输出.zip
"""
from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path

#: 4.7 清单的标准字段（顺序即输出顺序，便于人工比对）
FIELDS = (
    "schema", "id", "name", "version", "kind", "author", "license",
    "framework", "priority", "requires", "conflicts", "load_after",
    "load_before", "claims", "capabilities", "files", "packs",
    "data_patches", "text_patches", "method_hooks", "scripts", "scenes",
    "settings", "inputs", "story", "minigames", "baseline", "peer_access",
    "peer_expose",
)

DEFAULT_FRAMEWORK = ">=0.1.3,<0.2.0"


def _norm_baseline(raw) -> dict:
    """把各种老式 baseline 统一成 4.7 的结构。"""
    return {"id": "jjxf.v4.7", "engine": {"line": "4.7"}}


def _norm_files(raw) -> list[dict]:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("source", ""))
        dst = str(item.get("target", "")).replace("\\", "/").lstrip("/")
        if not src or not dst:
            continue
        rec = {"source": src, "target": dst}
        if item.get("replace") is not None:
            rec["replace"] = bool(item["replace"])
        out.append(rec)
    return out


def _norm_scripts(raw_entry, raw_members) -> dict:
    """把 schema 1 的 `entry` / `script_members` 迁到 `scripts`。"""
    scripts: dict = {}
    entry = str(raw_entry or "").strip()
    if entry:
        scripts["entry"] = entry
    members = raw_members or []
    if isinstance(members, list) and members:
        cleaned = []
        for item in members:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target", "")).replace("\\", "/").lstrip("/")
            body = item.get("members")
            if target and isinstance(body, dict) and body:
                cleaned.append({"target": target, "members": body})
        if cleaned:
            scripts["members"] = cleaned
    return scripts


def convert(raw: dict, *, author: str = "", priority: int | None = None) -> dict:
    """把一份旧清单转成 4.7 清单。"""
    out: dict = {
        "schema": 3,
        "id": raw.get("id"),
        "name": raw.get("name") or raw.get("id"),
        "version": raw.get("version"),
        "kind": raw.get("kind") or "extension",
        "author": author or str(raw.get("author") or ""),
        "license": str(raw.get("license") or ""),
        "framework": str(raw.get("framework") or DEFAULT_FRAMEWORK),
        "priority": int(priority if priority is not None else (raw.get("priority") or 50)),
        "requires": raw.get("requires") or [],
        "conflicts": raw.get("conflicts") or [],
        "load_after": raw.get("load_after") or [],
        "load_before": raw.get("load_before") or [],
        "claims": raw.get("claims") or [],
        "capabilities": raw.get("capabilities") or [],
        "files": _norm_files(raw.get("files")),
        "packs": raw.get("packs") or [],
        "data_patches": raw.get("data_patches") or [],
        "text_patches": raw.get("text_patches") or [],
        "method_hooks": raw.get("method_hooks") or [],
        "scripts": _norm_scripts(
            raw.get("entry") or (raw.get("scripts") or {}).get("entry"),
            raw.get("script_members") or (raw.get("scripts") or {}).get("members"),
        ),
        "baseline": _norm_baseline(raw.get("baseline")),
    }

    # requires 可能写成 dict（老格式），转成 id 列表
    if isinstance(out["requires"], dict):
        out["requires"] = [k for k, v in out["requires"].items() if v]

    for key in ("scenes", "settings", "inputs", "story", "minigames"):
        val = raw.get(key)
        if val:
            out[key] = val
    for key in ("peer_access", "peer_expose"):
        val = raw.get(key)
        if val:
            out[key] = val

    # 只保留认识的字段，且去掉空值（保持清单干净）
    return {k: out[k] for k in FIELDS if k in out and out[k] not in (None, "", [], {})}


def read_manifest(container: Path) -> tuple[dict, str]:
    """从 zip 或目录里读旧清单，返回 (内容, 源文件名)。"""
    if container.is_dir():
        for name in ("extension.json", "manifest-46.v2.json"):
            p = container / name
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8")), name
        raise SystemExit(f"目录里找不到 extension.json：{container}")

    with zipfile.ZipFile(container) as z:
        for name in ("extension.json", "manifest-46.v2.json"):
            if name in z.namelist():
                return json.loads(z.read(name).decode("utf-8")), name
    raise SystemExit(f"包里找不到 extension.json：{container}")


def write_zip(src_zip: Path, dst_zip: Path, manifest: dict, ext_id: str) -> None:
    """把源 zip 复制一份，换成 4.7 清单（保留全部载荷）。"""
    with zipfile.ZipFile(src_zip) as src, zipfile.ZipFile(
        dst_zip, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            name = item.filename
            # 老清单不要带进 4.7 包
            if name in ("extension.json", "manifest-46.v2.json", "manifest-47.v2.json"):
                continue
            dst.writestr(item, src.read(name))
        dst.writestr(
            "extension.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        dst.writestr(
            "manifest-47.v2.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )


def write_dir(src_dir: Path, dst_dir: Path, manifest: dict) -> None:
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)
    for stale in ("manifest-46.v2.json", "manifest-47.v2.json"):
        p = dst_dir / stale
        if p.is_file():
            p.unlink()
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (dst_dir / "extension.json").write_text(text, encoding="utf-8")
    (dst_dir / "manifest-47.v2.json").write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把扩展清单转成 4.7 格式")
    ap.add_argument("source", help="源 zip 或目录")
    ap.add_argument("--author", default="", help="作者名（写进清单的 author 字段）")
    ap.add_argument("--priority", type=int, default=None, help="覆盖 priority")
    ap.add_argument("--out", default=None, help="输出路径（默认 <源名>-47.zip）")
    ap.add_argument("--dry-run", action="store_true", help="只打印转换结果，不写文件")
    args = ap.parse_args(argv)

    container = Path(args.source)
    raw, src_name = read_manifest(container)
    ext_id = str(raw.get("id") or "")
    if not ext_id:
        print(f"× 清单里没有 id：{container}")
        return 1

    manifest = convert(raw, author=args.author, priority=args.priority)

    print(f"源清单：{src_name}")
    print(f"id = {ext_id}   版本 = {raw.get('version')}")
    print(f"schema {raw.get('schema')} -> 3    baseline -> jjxf.v4.7")
    print(f"  files         = {len(manifest.get('files') or [])}")
    print(f"  data_patches  = {len(manifest.get('data_patches') or [])}")
    print(f"  text_patches  = {len(manifest.get('text_patches') or [])}")
    print(f"  method_hooks  = {len(manifest.get('method_hooks') or [])}")
    print(f"  scripts       = {'entry' if manifest.get('scripts', {}).get('entry') else ''}"
          f"{'+members' if manifest.get('scripts', {}).get('members') else ''}" or "（无）")

    if args.dry_run:
        print("\n--- 转换后的清单 ---")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    if container.is_dir():
        out_dir = Path(args.out) if args.out else container.with_name(container.name + "-47")
        write_dir(container, out_dir, manifest)
        print(f"\n已写出目录：{out_dir}")
    else:
        out_zip = Path(args.out) if args.out else container.with_name(
            container.stem + "-47.zip"
        )
        write_zip(container, out_zip, manifest, ext_id)
        print(f"\n已写出：{out_zip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())