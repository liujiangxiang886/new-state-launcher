"""共同装载校验：把某条线上**全部已启用**扩展一起挂起来跑一次。

本文件只做参数解析与输出排版，真正的逻辑在
:func:`app.core.ext.verify.verify_line`——因为启动器界面也要调它，
而界面不能反向依赖 ``tools/`` 这个命令行目录（打包后 ``tools/`` 根本
不在发布包里，界面按路径加载会直接 ``FileNotFoundError``）。

用法::

    python tools/verify_mods.py 4.6
    python tools/verify_mods.py 4.6 --json builds/verify46.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core.ext.verify import verify_line  # noqa: E402


def print_report(data: dict) -> None:
    line = data["line"]
    print(f"\n=== {line} ===")
    print(f"已启用 {len(data['enabled'])} 个：{', '.join(data['enabled']) or '（无）'}")
    print(f"覆盖层/插桩层 {len(data['packs'])} 个：{', '.join(data['packs']) or '（无）'}")
    print(f"声明的运行期入口 {len(data['expected'])} 个：{', '.join(data['expected']) or '（无）'}")
    print(f"实际装载 {len(data['loaded'])} 个：{', '.join(data['loaded']) or '（无）'}")
    if data["missing"]:
        print("!! 未装载：" + "、".join(data["missing"]))
    if data["unexpected"]:
        print("!! 未声明却被装载：" + "、".join(data["unexpected"]))
    for item in data["failed"]:
        print(f"!! 装载失败：{item.get('id')} — {item.get('error')}")
    for warn in data["warnings"]:
        print(f"   · {warn}")
    print(f"耗时 {data['seconds']} 秒，节点数 {data.get('nodes', '-')}")
    print(("通过：" if data["ok"] else "未通过：") + str(data.get("reason") or ""))
    for problem in data.get("problems") or []:
        print("   - " + problem)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验某条线上全部已启用扩展能否共同装载")
    parser.add_argument("lines", nargs="+", choices=["4.6"], help="引擎线")
    parser.add_argument("--timeout", type=float, default=300.0, help="单次冒烟超时（秒）")
    parser.add_argument("--json", default="", help="把结果写成 JSON")
    args = parser.parse_args(argv)

    cfg = cfg_mod.load()
    reports = []
    for line in args.lines:
        data = verify_line(cfg, line, args.timeout)
        print_report(data)
        reports.append(data)

    if args.json:
        target = Path(args.json)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"schema": 1, "reports": reports}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {target}")

    return 0 if all(r["ok"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())