"""生成引擎桩，或把底座 pck 内嵌成单文件。

用法::

    # 看内置引擎的现状（版本、是否编辑器构建、能否做导出）
    python tools/make_stub.py inspect

    # 从游戏 exe 里剥出纯 PE 桩（仅用于分析/差分，不可执行——见下）
    python tools/make_stub.py strip "D:\\...\\mchGame.exe" -o builds/stub46.exe --keep 104599040

    # 把底座 pck 内嵌进引擎 exe，得到单文件运行时
    python tools/make_stub.py embed runtime/host46/jjxf46.exe runtime/host46/jjxf46.pck -o builds/single46.exe

    # 检查一个 exe 尾部有没有内嵌 pck
    python tools/make_stub.py check builds/single46.exe

关于引擎桩的重要结论（实测，已否决）：

    把 1.45 的 mchGame.exe 截断到 104,599,040 字节（pck 起点）后，
    PE 头里的映像尺寸与文件实际大小不一致，Windows 直接拒绝加载
    （"不是此操作系统平台的有效应用程序"）。原版 exe 本身可正常运行，
    问题只在截断。**所以 4.6 一律用引擎本体，不用截断桩。**

``strip`` 保留下来是因为它还能用于「只要 PE 前缀做差异分析」的场景。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod, paths  # noqa: E402
from app.core.build import godot  # noqa: E402
from app.core.pck import embed as embed_mod  # noqa: E402
from app.core.pck.reader import locate_pck  # noqa: E402
from app.core.pck.writer import truncate_stub  # noqa: E402


def cmd_inspect(args: argparse.Namespace) -> int:
    report = godot.engine_report()
    for line, info in report.items():
        print(f"[{line}]")
        print(f"  路径      : {info['path']}")
        print(f"  存在      : {info['exists']}")
        if info.get("exists"):
            print(f"  大小      : {info.get('bytes', 0):,} 字节")
            print(f"  版本      : {info.get('version')}")
            print(f"  可导出    : {'是（编辑器构建）' if info.get('editor') else '否（导出模板）'}")
        print()

    print("结论：")
    for line, info in report.items():
        if not info.get("exists"):
            print(f"  {line}：引擎缺失，该线功能应先置灰")
        elif info.get("editor"):
            print(f"  {line}：编辑器构建，支持 --main-pack 与 --export-pack")
        else:
            print(f"  {line}：导出模板，只能靠同名 .pck 自动配对；不支持 --main-pack/--export-pack")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.target)
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    size = path.stat().st_size
    print(f"文件：{path}")
    print(f"  大小    : {size:,} 字节")
    has = embed_mod.has_pck(path)
    print(f"  内嵌 pck: {'有' if has else '没有'}")
    if has:
        inner = embed_mod.embedded_size(path)
        print(f"  内嵌大小: {inner:,} 字节")
        print(f"  pck 起点: {size - inner - embed_mod.TAIL_SIZE:,}")
        try:
            start, inner_size = locate_pck(path)
            print(f"  locate_pck: start={start:,} size={inner_size:,}")
        except Exception as exc:  # noqa: BLE001
            print(f"  locate_pck 失败：{exc}")
    return 0


def cmd_strip(args: argparse.Namespace) -> int:
    src = Path(args.source)
    if not src.is_file():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 2
    keep = args.keep
    if keep is None:
        if embed_mod.has_pck(src):
            keep = src.stat().st_size - embed_mod.embedded_size(src) - embed_mod.TAIL_SIZE
            print(f"按内嵌包自动推算截断长度：{keep:,}")
        else:
            print("文件里没有内嵌 pck，必须用 --keep 指定长度", file=sys.stderr)
            return 2
    out = Path(args.out)
    truncate_stub(src, keep, out)
    print(f"已生成 {out}（{out.stat().st_size:,} 字节）")
    print()
    print("注意：截断后的 PE 头与文件大小不一致，Windows 会拒绝执行。")
    print("      需要可运行的 4.6 运行时请直接用引擎本体（engines/godot-4.6.exe 或配置里的路径）。")
    if args.verify:
        try:
            proc = subprocess.run(
                [str(out), "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            print(f"      实测 --version：rc={proc.returncode} out={(proc.stdout or proc.stderr or '').strip()[:120]}")
        except OSError as exc:
            print(f"      实测 --version 失败：{exc}")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    exe = Path(args.exe)
    pck = Path(args.pck)
    out = Path(args.out) if args.out else exe.with_name(exe.stem + "-single.exe")
    if not exe.is_file() or not pck.is_file():
        print("exe 或 pck 不存在", file=sys.stderr)
        return 2

    size_before = exe.stat().st_size
    result = embed_mod.embed_pck(exe, pck, out)
    size_after = result.stat().st_size
    print(f"已内嵌：{result}")
    print(f"  stub {size_before:,} + pck {pck.stat().st_size:,} + 尾标 {embed_mod.TAIL_SIZE} = {size_after:,}")
    if not embed_mod.has_pck(result):
        print("  [失败] 尾部标记校验未通过", file=sys.stderr)
        return 1
    print("  尾部标记校验通过")
    if args.check:
        try:
            proc = subprocess.run(
                [str(result), "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            ok = proc.returncode == 0
            print(f"  实测 --version：{'通过' if ok else '失败'}（{(proc.stdout or '').strip()[:80]}）")
            return 0 if ok else 1
        except OSError as exc:
            print(f"  实测失败：{exc}", file=sys.stderr)
            return 1
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """列出可用的引擎与已构建的运行时，便于挑选 stub。"""
    print("=== engines/ ===")
    for path in sorted(paths.ENGINES.glob("*.exe")):
        print(f"  {path.name:28s} {path.stat().st_size:>14,} 字节")
    print("\n=== runtime/host* ===")
    for host in sorted(paths.RUNTIME.glob("host*")):
        if not host.is_dir():
            continue
        for path in sorted(host.iterdir()):
            if path.is_file():
                print(f"  {host.name}/{path.name:28s} {path.stat().st_size:>14,} 字节")
    cfg = cfg_mod.load()
    print("\n=== 配置里的引擎 ===")
    for line, meta in (cfg.get("engines") or {}).items():
        path = Path(str(meta.get("path") or ""))
        print(f"  {line}: {path}  ({'存在' if path.is_file() else '缺失'})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="引擎桩与单文件内嵌工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="查看两代引擎的版本与能力")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("check", help="检查 exe 尾部是否带内嵌 pck")
    p.add_argument("target")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("strip", help="剥出纯 PE 桩（不可执行，仅供分析）")
    p.add_argument("source")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--keep", type=int, default=None, help="保留前多少字节；省略则按内嵌包自动推算")
    p.add_argument("--verify", action="store_true", help="顺便实测能否执行（预计失败）")
    p.set_defaults(func=cmd_strip)

    p = sub.add_parser("embed", help="把 pck 内嵌进 exe 得到单文件运行时")
    p.add_argument("exe")
    p.add_argument("pck")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--check", action="store_true", help="生成后跑一次 --version")
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("list", help="列出引擎与已构建的运行时")
    p.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())