"""自检：scripts.members 注入的换行、幂等与去重。

用真实字符串（不经过 shell 转义）验证，避免把测试环境问题当成代码问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.ext.patcher import inject_members

SRC = "extends Node\n\nsignal save_loaded\n\nconst MAX_ROLES = 40\nvar roles: Array = []\n"

MEMBERS = {
    "WhfInstructorsBaolong": 'const WhfInstructorsBaolong = preload("res://mods/x/baolong.gd")',
    "whf_instructors_is_baolong": "func whf_instructors_is_baolong() -> bool:\n\treturn true",
}


def main() -> int:
    out, issues = inject_members(SRC, MEMBERS, target="scripts/game.gd")
    checks: list[tuple[str, bool]] = [
        ("无告警", not issues),
        ("extends 仍在最前", out.lstrip().startswith("extends Node")),
        ("const 已注入", 'const WhfInstructorsBaolong = preload("res://mods/x/baolong.gd")' in out),
        ("func 体缩进保留", "func whf_instructors_is_baolong() -> bool:\n\treturn true" in out),
        ("原内容未丢", "const MAX_ROLES = 40" in out and "var roles: Array = []" in out),
        ("注入在 signal 之前", out.index("WhfInstructorsBaolong = preload") < out.index("signal save_loaded")),
    ]

    again, _ = inject_members(out, MEMBERS, target="scripts/game.gd")
    checks.append(("幂等（重复注入无变化）", again == out))

    # 已有同名声明时应跳过，避免 "already declared"
    pre = SRC + '\nconst WhfInstructorsBaolong = preload("res://x.gd")\n'
    kept, _ = inject_members(pre, MEMBERS, target="scripts/game.gd")
    checks.append(("同名声明不重复插入", kept.count("const WhfInstructorsBaolong") == 1))

    # 空成员表必须原样返回
    same, _ = inject_members(SRC, {}, target="scripts/game.gd")
    checks.append(("空成员表不改动", same == SRC))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("[PASS] " if ok else "[FAIL] ") + name)
    print()
    if failed:
        print("失败：", failed)
        print("--- 实际输出 ---")
        print(repr(out))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())