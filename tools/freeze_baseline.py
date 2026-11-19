"""冻结基线身份：给一条引擎线生成 ``baseline/identity-<line>.json``。

什么时候用
==========

1. **首次给一条线建立身份**（4.7 合并包做完之后、4.6 想启用验证标志时）；
2. **官方发布新本体后**重建身份（旧身份的所有验证记录会因此失效，
   这是**预期行为** —— 代码层变了，旧记录本来就不该继续算数）。

不做什么
========

- **不校验**当前机器上的文件对不对（那是 ``baseline_identity.verify``）；
- **不改配置**里的基线路径（那是导入流程的事）。

用法
====

    # 4.7：从 pcK + exe 生成
    python tools/freeze_baseline.py --line 4.7 \\
        --body "D:/1107/合并包/jjxf47.pck" \\
        --exe  "D:/1107/合并包/jjxf47.exe"

    # 4.6：exe 内嵌 pck（用 locate_pck 定位）
    python tools/freeze_baseline.py --line 4.6 --body ".../mchGame.exe" --kind exe

    # 只预览，不写文件
    python tools/freeze_baseline.py --line 4.7 --body ... --exe ... --dry-run

产物结构见 ``baseline/identity-4.7.json``：``layers``（整包 sha256）+
``code_layer``（逐文件 sha256 + 汇总 digest）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import paths  # noqa: E402
from app.core.pck.reader import PckReader, locate_pck  # noqa: E402

#: 参与代码层指纹的条目后缀。与 ``baseline_identity.CODE_SUFFIXES`` 一致。
CODE_SUFFIXES = (".gd", ".tscn")

#: 逐文件哈希块大小。
CHUNK = 8 * 1024 * 1024

#: 各线的基础信息（写进 identity 的 label / engine_line）。
LINE_META = {
    "4.6": {
        "id": "jjxf.v1.45",
        "label": "机甲旋风 1.45（原版 · Godot 4.6）",
        "engine_version": "4.6.0",
    },
    "4.7": {
        "id": "jjxf.v4.7",
        "label": "机甲旋风 4.7（剧情版 · Godot 4.7.2）",
        "engine_version": "4.7.2",
    },
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def identity_path(line: str) -> Path:
    """与 ``baseline_identity.identity_path`` 同一套命名。"""
    bid = LINE_META.get(line, {}).get("id") or f"jjxf.v{line}"
    slug = bid
    for prefix in ("jjxf.v", "jjxf."):
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
            break
    return paths.BASELINE / f"identity-{slug}.json"


def scan_code(pck: Path) -> tuple[str, dict[str, dict]]:
    """扫包内的脚本与场景，返回 ``(digest, 逐文件表)``。"""
    reader = PckReader.open(pck)
    files: dict[str, dict] = {}
    for entry in reader.entries:
        if not entry.path.endswith(CODE_SUFFIXES):
            continue
        data = reader.read_bytes(entry)
        files[entry.path] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    blob = "".join(f"{k}:{v['sha256']};" for k, v in sorted(files.items()))
    return hashlib.sha256(blob.encode()).hexdigest(), files


def build_identity(
    line: str,
    body: Path,
    exe: Path | None,
    *,
    kind: str = "",
    label: str = "",
) -> dict:
    meta = LINE_META.get(line) or {
        "id": f"jjxf.v{line}",
        "label": f"引擎线 {line}",
        "engine_version": "",
    }
    if not body.is_file():
        raise SystemExit(f"本体文件不存在：{body}")

    # 内嵌 pck 的 exe 需要 offset；独立 pck 为 0
    inferred_kind = kind or ("exe" if body.suffix.lower() == ".exe" else "pck")
    pck_start = 0
    if inferred_kind == "exe":
        pck_start, _size = locate_pck(body)

    print(f"[{line}] 读本体：{body}  ({body.stat().st_size:,} B, kind={inferred_kind})")
    reader = PckReader.open(body)
    header = reader.header.summary()

    print(f"[{line}] 代码层扫描…")
    code_digest, code_files = scan_code(body)
    print(f"[{line}] 代码层：{len(code_files)} 个文件，digest={code_digest[:16]}")

    layers: dict[str, dict] = {
        "body": {
            "path": body.name,
            "size": body.stat().st_size,
            "sha256": sha256_file(body),
        }
    }
    if exe is not None:
        if not exe.is_file():
            raise SystemExit(f"引擎文件不存在：{exe}")
        print(f"[{line}] 读引擎：{exe}  ({exe.stat().st_size:,} B)")
        layers["exe"] = {
            "path": exe.name,
            "size": exe.stat().st_size,
            "sha256": sha256_file(exe),
        }

    return {
        "schema": 1,
        "id": meta["id"],
        "label": label or meta["label"],
        "kind": inferred_kind,
        "engine_line": line,
        "layers": layers,
        "header": header,
        "entry_count": len(reader),
        "total_entry_bytes": sum(e.size for e in reader.entries),
        "top_suffixes": reader.suffixes(12),
        "code_layer": {
            "source": "body",
            "entry_count": len(code_files),
            "digest": code_digest,
            "files": code_files,
        },
        "produced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="冻结基线身份指纹")
    ap.add_argument("--line", required=True, choices=sorted(LINE_META), help="引擎线")
    ap.add_argument("--body", required=True, help="本体文件（.pck 或内嵌 pck 的 .exe）")
    ap.add_argument("--exe", default="", help="引擎 exe（4.7 需要；4.6 内嵌则不用）")
    ap.add_argument("--kind", default="", choices=["", "exe", "pck"], help="本体形态，默认按扩展名推断")
    ap.add_argument("--label", default="", help="覆盖显示名")
    ap.add_argument("--out", default="", help="输出路径，默认 baseline/identity-<line>.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    body = Path(args.body)
    exe = Path(args.exe) if args.exe else None
    ident = build_identity(args.line, body, exe, kind=args.kind, label=args.label)

    target = Path(args.out) if args.out else identity_path(args.line)
    payload = json.dumps(ident, ensure_ascii=False, indent=2) + "\n"

    print()
    print(f"  基线 id     : {ident['id']}")
    print(f"  条目数      : {ident['entry_count']:,}")
    print(f"  代码层文件  : {ident['code_layer']['entry_count']}")
    print(f"  代码层 digest: {ident['code_layer']['digest']}")
    for name, slot in ident["layers"].items():
        print(f"  {name:12}: {slot['sha256']}")
    print()

    if args.dry_run:
        print(f"[dry-run] 未写文件。目标路径：{target}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    print(f"已写入：{target}  ({target.stat().st_size:,} B)")
    print()
    print("提示：代码层 digest 变了会让所有旧验证记录失效（这是预期行为）。")
    print("      扩展需要重新 seal 或由玩家在本机「验证全部」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())