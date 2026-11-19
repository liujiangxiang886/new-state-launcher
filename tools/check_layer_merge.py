"""自检：多层脚本合并（_merge_script_layers）的三种冲突语义。

这三种情形都是实测踩过的，必须锁死：

1. 同位置「替换」→ 后者获胜（否则重复声明）
2. 同位置「纯插入」→ 全部保留（否则丢常量）
3. 不同位置 → 各自保留
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.ext.enable import _merge_script_layers


def check(name: str, got: str, want_cond, detail: str = "") -> tuple[str, bool]:
    ok = want_cond(got)
    return (name, ok)


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    # 1. 同位置替换：后者覆盖
    base = "extends Node\nvar x = 1\nvar y = 2\n"
    l1 = "extends Node\nvar x = 100\nvar y = 2\n"
    l2 = "extends Node\nvar x = 200\nvar y = 2\n"
    got = _merge_script_layers(base, [l1, l2])
    results.append((
        "同位置替换：后者获胜且不重复",
        got.count("var x") == 1 and "200" in got,
        repr(got),
    ))

    # 2. 同位置插入：两层都保留（这是关键回归点）
    base2 = "extends Node\n\nfunc f():\n\tpass\n"
    a = "extends Node\nconst A = 1\n\nfunc f():\n\tpass\n"
    b = "extends Node\nconst B = 2\n\nfunc f():\n\tpass\n"
    got2 = _merge_script_layers(base2, [a, b])
    results.append((
        "同位置插入：两层都保留",
        "const A = 1" in got2 and "const B = 2" in got2,
        repr(got2),
    ))

    # 3. 不同位置：各自保留
    base3 = "a\nb\nc\nd\n"
    got3 = _merge_script_layers(base3, ["a\nb1\nc\nd\n", "a\nb\nc\nd2\n"])
    results.append((
        "不同位置：两者都保留",
        "b1" in got3 and "d2" in got3,
        repr(got3),
    ))

    # 4. 替换 + 插入混合，且插入点在替换点之前
    base4 = "extends Node\nvar x = 1\nvar y = 2\n"
    l4a = "extends Node\nconst C = 9\nvar x = 1\nvar y = 2\n"
    l4b = "extends Node\nvar x = 7\nvar y = 2\n"
    got4 = _merge_script_layers(base4, [l4a, l4b])
    results.append((
        "插入+替换混合：两者都生效",
        "const C = 9" in got4 and "var x = 7" in got4 and got4.count("var x") == 1,
        repr(got4),
    ))

    # 5. 单层原样返回
    got5 = _merge_script_layers(base3, [])
    results.append(("无层：原样返回", got5 == base3, repr(got5)))

    failed = 0
    for name, ok, detail in results:
        print(("[PASS] " if ok else "[FAIL] ") + name)
        if not ok:
            print("       got:", detail)
            failed += 1
    print()
    print("全部通过" if not failed else f"失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())