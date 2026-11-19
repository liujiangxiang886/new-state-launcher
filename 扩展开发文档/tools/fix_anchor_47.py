#!/usr/bin/env python3
"""按 4.7 源码修正 `text_patches` 的锚点。

`text_patches` 靠字面匹配定位原代码。4.7 剧情版改了不少函数体，
旧锚点匹配不上，补丁就静默失效。

本工具做三件事：

1. **定位**：拿锚点里的函数名/变量名去 4.7 源码里找位置
2. **对齐**：在附近取出等价的代码块，逐行比对差异
3. **重写**：把锚点替换成 4.7 里真实存在的版本

**只改写锚点，不改 `replace` 的语义。** 如果 4.7 里的目标代码结构变了
（比如某行被删、参数增加），工具会把差异打出来交给人工确认 ——
自动猜「该改成什么」风险太大，那等于替作者决定功能行为。

用法::

    python tools/fix_anchor_47.py 包.zip --src "D:/1107/完整反解(含资源)"
    python tools/fix_anchor_47.py 包.zip --src ... --dry-run
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import zipfile
from pathlib import Path


def read_source(src_root: Path, target: str) -> str | None:
    rel = str(target).replace("res://", "").replace("\\", "/").lstrip("/")
    p = src_root / rel
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def func_name_of(snippet: str) -> str:
    m = re.search(r"\bfunc\s+([A-Za-z_]\w*)", snippet)
    return m.group(1) if m else ""


def extract_func(text: str, name: str) -> tuple[int, int] | None:
    """取出某个函数的 [起, 止) 行区间。"""
    lines = text.splitlines(keepends=True)
    start = None
    pat = re.compile(rf"^func\s+{re.escape(name)}\s*\(")
    for i, line in enumerate(lines):
        if pat.match(line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        # 顶格的 func / class / var / const 视为下一个定义开始
        if re.match(r"^(func|class|class_name|static func)\s", lines[j]):
            end = j
            break
        if lines[j].startswith("###") or lines[j].startswith("## "):
            continue
    return start, end


def try_fix_anchor(old_find: str, source: str) -> tuple[str | None, list[str]]:
    """尝试在 4.7 源码里找到与旧锚点等价的片段。

    返回 ``(新锚点 或 None, 差异说明行)``。
    """
    notes: list[str] = []
    name = func_name_of(old_find)
    if not name:
        notes.append("锚点里没有 func 声明，无法按函数定位")
        return None, notes

    span = extract_func(source, name)
    if span is None:
        notes.append(f"4.7 源码里找不到函数 {name}()")
        return None, notes

    lines = source.splitlines(keepends=True)
    actual = "".join(lines[span[0]:span[1]])

    # 完全一致（可能只是行尾/空白差异）
    if _norm(actual) == _norm(old_find):
        notes.append("规范化后内容一致，可直接替换")
        return actual, notes

    # 逐行 diff，把差异列出来给人看
    old_lines = old_find.splitlines()
    new_lines = actual.splitlines()
    sm = difflib.SequenceMatcher(None, [_norm(x) for x in old_lines],
                                 [_norm(x) for x in new_lines])
    changed = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_part = " / ".join(x.strip() for x in old_lines[i1:i2][:2])
        new_part = " / ".join(x.strip() for x in new_lines[j1:j2][:2])
        changed.append(f"    [{tag}] 旧: {old_part[:100]}")
        if new_part:
            changed.append(f"           新: {new_part[:100]}")

    notes.append(f"函数 {name}() 在 4.7 里有 {len(changed)//2 if changed else 0} 处差异")
    notes.extend(changed[:16])

    # 只有当差异只是「新增行」时才自动采用完整新函数体
    only_insert = all(
        tag == "insert"
        for tag, *_ in sm.get_opcodes()
        if tag != "equal"
    )
    if only_insert:
        notes.append("    -> 差异均为新增行，可安全采用 4.7 版本")
        return actual, notes

    notes.append("    -> 存在替换/删除，需人工确认后再改")
    return None, notes


def _norm(s: str) -> str:
    """归一化：去掉行尾空白与制表/空格差异，只比语义。"""
    return "\n".join(x.rstrip().replace("\t", "    ") for x in s.splitlines())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按 4.7 源码修正 text_patches 锚点")
    ap.add_argument("package", help="扩展 zip")
    ap.add_argument("--src", required=True, help="4.7 反解工程根目录")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写回")
    ap.add_argument("--apply", action="store_true",
                    help="把可自动修的锚点写回（生成 -fixed.zip）")
    args = ap.parse_args(argv)

    src_root = Path(args.src)
    pkg = Path(args.package)
    if not src_root.is_dir():
        print(f"× 反解工程不存在：{src_root}")
        return 1

    z = zipfile.ZipFile(pkg)
    man_name = "manifest-47.v2.json" if "manifest-47.v2.json" in z.namelist() else "extension.json"
    manifest = json.loads(z.read(man_name).decode("utf-8"))
    patches = manifest.get("text_patches") or []
    if not patches:
        print(f"{manifest.get('id')}：没有 text_patches，无需处理")
        return 0

    print(f"扩展 {manifest.get('id')}  共 {len(patches)} 条 text_patches\n")

    fixed = 0
    for i, patch in enumerate(patches):
        target = str(patch.get("target", ""))
        find = str(patch.get("find", ""))
        if not target or not find:
            continue
        source = read_source(src_root, target)
        if source is None:
            print(f"[{i}] × 4.7 里没有 {target}")
            continue
        if find in source:
            print(f"[{i}] ✓ 已命中 {target}")
            continue

        print(f"[{i}] ✗ 未命中 {target}")
        new, notes = try_fix_anchor(find, source)
        for line in notes:
            print("    " + line if not line.startswith("    ") else line)
        if new:
            patch["_fixed_find"] = new
            fixed += 1
        print()

    print(f"可自动修复 {fixed} / {len(patches)}")
    if fixed:
        print("（加 --apply 写回；存在替换/删除的仍需人工处理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())