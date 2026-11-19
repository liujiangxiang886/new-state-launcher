"""生成 Windows 快捷方式（.lnk）。

为什么用 pylnk3 而不是自带实现：

1. 本机安全策略禁止 PowerShell 实例化 COM（``New-Object -ComObject WScript.Shell``
   直接被拦），`mklink` 只能建符号链接、不是快捷方式；
2. **手写最小 .lnk（只写 LinkInfo、不写 LinkTargetIDList）会被资源管理器拒绝**，
   双击报「该文件没有与之关联的应用来执行该操作」——已实测踩过这个坑；
3. ``pylnk3`` 是纯 Python 实现（不走 COM），会同时写入 IDList / LinkInfo /
   StringData，实测双击可正常启动。

依赖：

    pip install pylnk3

用法：

    python tools/make_shortcut.py \
        --target "dist\\机甲旋风兼容启动器\\机甲旋风兼容启动器.exe" \
        --name "机甲旋风兼容启动器"

    python tools/make_shortcut.py --target ... --out "D:\\somewhere\\x.lnk"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _load_pylnk3():
    try:
        import pylnk3  # 延迟导入：缺依赖时给出可执行的修复命令
    except ImportError:
        print(
            "缺少依赖 pylnk3，请先执行：\n"
            "    python -m pip install pylnk3",
            file=sys.stderr,
        )
        raise SystemExit(3)
    return pylnk3


def create_shortcut(
    target: Path,
    out: Path,
    *,
    workdir: Path | None = None,
    description: str = "",
) -> Path:
    """在 ``out`` 处生成指向 ``target`` 的快捷方式，返回写入路径。"""
    pylnk3 = _load_pylnk3()

    if not target.exists():
        raise FileNotFoundError(f"目标不存在：{target}")

    out.parent.mkdir(parents=True, exist_ok=True)

    obj = pylnk3.for_file(
        str(target),
        lnk_name=str(out),
        description=description or target.stem,
        icon_file=str(target),
        work_dir=str(workdir or target.parent),
        window_mode="Normal",
    )
    # pylnk3 的 write 接受文件对象，不接受路径
    with out.open("wb") as handle:
        obj.write(handle)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 .lnk 快捷方式（纯 Python，不走 COM）")
    parser.add_argument("--target", required=True, help="快捷方式指向的 exe / 文件")
    parser.add_argument("--name", default=None, help="快捷方式名（放到桌面）")
    parser.add_argument("--out", default=None, help="完整输出路径（与 --name 二选一）")
    parser.add_argument("--workdir", default=None, help="工作目录（默认取 target 所在目录）")
    parser.add_argument("--desc", default="", help="备注（鼠标悬停时显示）")
    args = parser.parse_args(argv)

    target = Path(args.target).expanduser().resolve()
    if not target.exists():
        print(f"[错误] 目标不存在：{target}", file=sys.stderr)
        return 1

    if args.out:
        out = Path(args.out).expanduser()
    elif args.name:
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            print(f"[错误] 找不到桌面目录：{desktop}", file=sys.stderr)
            return 1
        out = desktop / f"{args.name}.lnk"
    else:
        print("[错误] 需要 --name 或 --out 指定输出位置", file=sys.stderr)
        return 2

    workdir = Path(args.workdir).expanduser() if args.workdir else None
    written = create_shortcut(target, out, workdir=workdir, description=args.desc)

    # 回读校验：确认写进去的目标与预期一致
    pylnk3 = _load_pylnk3()
    parsed = pylnk3.parse(str(written))
    print(f"已生成：{written}")
    print(f"  字节数   ：{written.stat().st_size}")
    print(f"  指向     ：{parsed.path}")
    print(f"  工作目录 ：{parsed.work_dir}")
    if Path(str(parsed.path)) != target:
        print("[警告] 回读目标与输入不一致", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
