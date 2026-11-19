"""自检：自定义游戏线（多条、去重、边界、启动命令）。

覆盖的关键行为：
1. 支持**多条**（不是只能一条）
2. 同一 exe 不重复登记
3. 非 .exe / 不存在的文件被拒
4. 存档目录名唯一且文件系统安全
5. 移除只删登记、不删文件
6. 启动命令不带任何 `--packset`（证明不挂载扩展）
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import custom_lines as cl


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 造三个假的 exe
        exes = []
        for name in ("gameA.exe", "gameB.exe", "gameC.exe"):
            p = root / name
            p.write_bytes(b"MZ" + b"\x00" * 64)
            exes.append(str(p))
        bad = root / "not_exe.txt"
        bad.write_text("x", encoding="utf-8")

        cfg: dict = {}

        # 1. 多条
        a = cl.add_line(cfg, "改版 A", exes[0])
        b = cl.add_line(cfg, "改版 B", exes[1])
        c = cl.add_line(cfg, "改版 C", exes[2])
        lines = cl.load_all(cfg)
        results.append(("支持登记多条（3 条）", len(lines) == 3, f"实际 {len(lines)}"))
        results.append(("id 各不相同", len({x.id for x in lines}) == 3, ""))
        results.append(("id 带 custom: 前缀", all(cl.is_custom_id(x.id) for x in lines), ""))

        # 2. 去重
        dup_blocked = False
        try:
            cl.add_line(cfg, "重复", exes[0])
        except ValueError:
            dup_blocked = True
        results.append(("同一 exe 不重复登记", dup_blocked, ""))

        # 3. 格式与存在性
        bad_blocked = False
        try:
            cl.add_line(cfg, "坏文件", str(bad))
        except ValueError:
            bad_blocked = True
        results.append(("非 .exe 被拒", bad_blocked, ""))

        missing_blocked = False
        try:
            cl.add_line(cfg, "不存在", str(root / "nope.exe"))
        except ValueError:
            missing_blocked = True
        results.append(("不存在的文件被拒", missing_blocked, ""))

        # 4. 存档名安全且唯一
        profiles = [x.profile_name() for x in cl.load_all(cfg)]
        results.append(("存档目录名唯一", len(set(profiles)) == len(profiles), str(profiles)))
        weird_exe = root / "gameWeird.exe"
        weird_exe.write_bytes(b"MZ" + b"\x00" * 32)
        weird = cl.add_line(cfg, "带非法字符", str(weird_exe), profile="a/b:c*d")
        safe = weird.profile_name()
        results.append((
            "存档名过滤非法字符",
            not any(ch in safe for ch in '/\\:*?"<>|'),
            safe,
        ))
        cl.remove_line(cfg, weird.id)

        # 5. 移除不删文件
        target = Path(exes[0])
        cl.remove_line(cfg, a.id)
        results.append((
            "移除登记不删游戏文件",
            target.exists() and len(cl.load_all(cfg)) == 2,
            f"文件在={target.exists()} 剩={len(cl.load_all(cfg))}",
        ))

        # 6. 启动命令不含 packset / 不含引擎参数
        only = cl.load_all(cfg)[0]
        cmd = cl.command_for(only)
        joined = " ".join(cmd).lower()
        results.append(("命令就是 exe 本身", cmd[0] == only.exe, cmd[0]))
        results.append((
            "命令不含 --packset / --main-pack",
            "--packset" not in joined and "--main-pack" not in joined,
            joined,
        ))

        # 7. prepare_launch 只做存档隔离
        plan_root = root / "profiles"
        plan = cl.prepare_launch({"profiles": {"redirect_appdata": True}}, only, plan_root)
        results.append((
            "prepare_launch 给出 appdata 目录",
            plan.env_appdata is not None and str(plan_root) in str(plan.env_appdata),
            str(plan.env_appdata),
        ))
        results.append(("prepare_launch 不产生扩展相关警告", not plan.warnings, str(plan.warnings)))

    failed = 0
    for name, ok, detail in results:
        print(("[PASS] " if ok else "[FAIL] ") + name)
        if not ok:
            print("       ", detail)
            failed += 1
    print()
    print("全部通过" if not failed else f"失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())