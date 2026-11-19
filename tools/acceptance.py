"""发布目录全链路验收：整体校验 + 扩展就绪状态。

为什么单独写这个：开发态（``JJXF_ROOT`` 指向仓库）能跑通不代表发布态能跑。
两者唯一但致命的差别是 ``paths.TOOLS`` —— 发布包里没有 ``tools/``，
任何按路径加载命令行脚本的写法都会当场崩。

用法::

    python tools/acceptance.py                    # 验仓库目录
    python tools/acceptance.py --root dist/...    # 验发布目录
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="要验收的启动器根目录；空=仓库本目录")
    ap.add_argument("--lines", default="4.6")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)

    if args.root:
        os.environ["JJXF_ROOT"] = str(Path(args.root).resolve())

    from app.core import config as cfgm
    from app.core import paths
    from app.core.ext.verify import verify_line

    print(f"验收根目录 : {paths.ROOT}")
    print(f"tools/ 存在: {paths.TOOLS.exists()}  (发布目录应为 False)")
    print("")

    cfg = cfgm.load()
    bad = 0
    for line in [x.strip() for x in args.lines.split(",") if x.strip()]:
        out = verify_line(cfg, line, args.timeout)
        print(f"=== {line} ===")
        print(f"  已启用   : {len(out['enabled'])}")
        print(f"  声明入口 : {len(out['expected'])}")
        print(f"  实际装载 : {len(out['loaded'])}")
        print(f"  装载失败 : {len(out['failed'])}")
        print(f"  游戏事件 : {out['game_events'] or '（无）'}")
        print(f"  耗时     : {out['seconds']} 秒")
        print(f"  结论     : {'通过' if out['ok'] else '未通过'}")
        if not out["ok"]:
            for p in out["problems"]:
                print(f"    - {p}")
            bad += 1
        print("")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())