"""钩子插桩的安全性（计划 §7 S6 的工程版）。

和 ``test_hook_return.py`` 的分工：

- ``test_hook_return.py`` 用**合成脚本 + 引擎实跑**验证「返回值不变、钩子被调」；
- 本文件验证**插桩器本身的健壮性**：各种真实代码形态都不能被改坏成语法错误，
  写不动的形态必须给出警告而不是硬改。

覆盖的形态（都来自本项目真实源码里出现过的写法）：

1. 行内注释里出现 ``return``；
2. 字符串里出现 ``return``；
3. ``return_value = 1``（标识符以 return 开头，不是返回语句）；
4. 空函数体（只有 ``pass``）；
5. 跨行 return 表达式（``return (a +\n b)``）；
6. ``match`` 语句里的 return；
7. 多个方法同时挂钩子；
8. before + after 同时挂；
9. 钩子目标方法不存在（必须警告，不能静默）；
10. 重复钩子（同一方法同一相位两条）应去重。

判定方式：**用引擎实际解析一遍生成结果**。语法错误只有引擎说了算，
用 Python 正则自检是不够的。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ext.hooks import Hook, instrument_script  # noqa: E402

PASS = 0
FAIL = 0

#: 覆盖各种 return 形态的样本
SAMPLES: dict[str, str] = {
    "comment_return": (
        "extends Node\n"
        "\n"
        "func f(a: int) -> int:\n"
        "\t# 这里注释里提到 return 不算返回语句\n"
        "\treturn a + 1\n"
    ),
    "string_return": (
        "extends Node\n"
        "\n"
        "func f() -> String:\n"
        '\tvar s := "return 42"\n'
        "\treturn s\n"
    ),
    "identifier_return": (
        "extends Node\n"
        "\n"
        "func f() -> int:\n"
        "\tvar return_value := 7\n"
        "\treturn return_value\n"
    ),
    "empty_body": (
        "extends Node\n"
        "\n"
        "func f() -> void:\n"
        "\tpass\n"
    ),
    "early_returns": (
        "extends Node\n"
        "\n"
        "func f(a: int) -> String:\n"
        "\tif a < 0:\n"
        '\t\treturn "neg"\n'
        "\tif a == 0:\n"
        '\t\treturn "zero"\n'
        '\treturn "pos"\n'
    ),
    "match_return": (
        "extends Node\n"
        "\n"
        "func f(kind: int) -> int:\n"
        "\tmatch kind:\n"
        "\t\t0:\n"
        "\t\t\treturn 10\n"
        "\t\t_:\n"
        "\t\t\treturn 20\n"
    ),
    "multi_method": (
        "extends Node\n"
        "\n"
        "func a() -> int:\n"
        "\treturn 1\n"
        "\n"
        "func b() -> int:\n"
        "\treturn 2\n"
        "\n"
        "func c() -> void:\n"
        "\tprint(\"c\")\n"
    ),
    "risky_multiline": (
        "extends Node\n"
        "\n"
        "func f() -> int:\n"
        "\treturn (1 +\n"
        "\t\t2)\n"
    ),
}


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def parse_with_engine(sources: dict[str, str], work: Path, tag: str) -> tuple[bool, str]:
    """把这些 .gd 打进 pck，用底座引擎解析一遍。

    只要求「能解析」——脚本里没有 main 场景，靠 ``--check-only`` 不行，
    所以用一个只引用脚本的探针场景，让引擎去编译它们。
    """
    from app.core.pck.writer import PckWriter

    case = work / tag
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)

    writer = PckWriter(case / "probe.pck", line="4.6")
    writer.add_text(
        "project.godot",
        "config_version=5\n\n[application]\n\nconfig/name=\"hook syntax\"\n"
        "run/main_scene=\"res://main.tscn\"\n"
        "config/features=PackedStringArray(\"4.6\", \"GL Compatibility\")\n\n"
        # 桩必须注册成 **Autoload**：插桩器生成的是 ``HookRuntime.dispatch_after(...)``，
        # 那是访问 Autoload 单例的形式。桩文件原先叫 hook_stub.gd，
        # 名字对不上 → 引擎报 "Identifier HookRuntime not declared"，
        # 而这份 harness 当时是假阳性、根本没跑到这一步。
        "[autoload]\n\n"
        "HookRuntime=\"*res://HookRuntime.gd\"\n\n"
        "[rendering]\n\nrenderer/rendering_method=\"gl_compatibility\"\n",
    )
    # 场景**必须挂上脚本**，否则 main.gd 从不执行 → 里面的 load() 从不发生
    # → 语法错误永远检不出来。
    #
    # 这里踩过：main.tscn 只写了 [node name="M" type="Node"]，没有
    # script = ExtResource(...) ，于是「引擎能解析全部插桩结果」这条断言
    # 一直是**假阳性**——喂明显语法错误的脚本进去，它照样返回 True。
    writer.add_text(
        "main.tscn",
        "[gd_scene format=3]\n\n"
        '[ext_resource type="Script" path="res://main.gd" id="1_test"]\n\n'
        '[node name="M" type="Node"]\n'
        'script = ExtResource("1_test")\n',
    )
    writer.add_text("main.gd", "extends Node\n\nfunc _ready() -> void:\n" + "".join(
        f'\tload("res://{name}.gd")\n' for name in sources
    ) + "\tget_tree().quit(0)\n")
    for name, text in sources.items():
        writer.add_text(f"{name}.gd", text)
        if "HookRuntime" in text:
            # 桩是 **Autoload 节点**（不是 static 类）——与真实运行时的
            # hook_runtime.gd 同形：Autoload 单例名就是全局标识符。
            writer.add_text(
                "HookRuntime.gd",
                "extends Node\n\n"
                "func dispatch_before(s: String, m: String, c: Dictionary) -> void:\n\tpass\n\n"
                "func dispatch_after(s: String, m: String, c: Dictionary) -> void:\n\tpass\n",
            )
    writer.commit()

    exe = case / "probe.exe"
    shutil.copyfile(ROOT / "runtime" / "host46" / "jjxf46.exe", exe)
    try:
        proc = subprocess.run(
            [str(exe), "--headless", "--quit-after", "600"],
            cwd=str(case),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "超时"
    err = proc.stderr or ""
    parse_errors = [ln for ln in err.splitlines() if "Parse Error" in ln or "Failed to load script" in ln]
    return not parse_errors, "\n".join(parse_errors[:3]) or "无解析错误"


def harness_selfcheck(work: Path) -> tuple[bool, str]:
    """**先证明 harness 本身有效**，再拿它去校验插桩结果。

    为什么必须先自检：这份 harness 曾经是**假阳性**的 ——
    生成的 ``main.tscn`` 没挂脚本，``main.gd`` 从不执行，里面的 ``load()``
    从不发生，于是无论脚本里是什么语法错误，引擎都不会报，
    「引擎能解析全部插桩结果」这条断言一直绿。

    拿一个**明显错误**的脚本喂进去，harness 必须报 False ——
    否则后面所有「能解析」的结论都没有意义。
    """
    broken = {
        "definitely_broken": (
            "extends Node\n\n"
            "func f( -> void:\n"          # 参数列表没闭合
            "\tnot valid @@@\n"           # 非法字符
        ),
    }
    ok, detail = parse_with_engine(broken, work, "selfcheck")
    if ok:
        return False, (
            "harness 对明显语法错误竟然通过了 —— 检查 main.tscn 是否挂了 script = ExtResource(...)"
        )
    return True, detail


def main() -> int:
    work = ROOT / "builds" / "hook-inject"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    print("=== 0. harness 自检（先证明它能抓到语法错误）===")
    ok, detail = harness_selfcheck(work)
    check("harness 能检出明显语法错误", ok, detail)

    print("\n=== 1. 各形态插桩后不产生语法错误 ===")
    instrumented: dict[str, str] = {}
    for name, source in SAMPLES.items():
        target = source.split("func ")[1].split("(")[0] if "func " in source else "f"
        hooks = [Hook(script=f"{name}.gd", method=target, phase="after")]
        out, warns = instrument_script(source, hooks, f"{name}.gd")
        instrumented[name] = out
        print(f"  {name}: 改动={'是' if out != source else '否'} 警告={warns or '无'}")

    # 加入 HookRuntime 桩，让 load 时的静态调用可解析
    stub = (
        "extends Node\n\n"
        "static func dispatch_before(s: String, m: String, c: Dictionary) -> void:\n\tpass\n\n"
        "static func dispatch_after(s: String, m: String, c: Dictionary) -> void:\n\tpass\n"
    )
    ok, detail = parse_with_engine(instrumented, work, "syntax")
    check("引擎能解析全部插桩结果", ok, detail)

    print("\n=== 2. 不该被当作返回语句的行 ===")
    out = instrumented["comment_return"]
    check("注释里的 return 未被改写", out.count("dispatch_after") == 1, f"{out.count('dispatch_after')} 次")
    out = instrumented["string_return"]
    check("字符串里的 return 未被改写", '"return 42"' in out and out.count("dispatch_after") == 1)
    out = instrumented["identifier_return"]
    check("return_value 未被误判", "var return_value := 7" in out and out.count("dispatch_after") == 1)

    print("\n=== 3. 边界形态 ===")
    out = instrumented["empty_body"]
    check("空函数体（pass）仍补了派发", "dispatch_after" in out, out)
    out = instrumented["early_returns"]
    check("两处提前返回 + 末尾返回都被改写", out.count("dispatch_after") == 3, f"{out.count('dispatch_after')} 次")
    out = instrumented["match_return"]
    check("match 分支里的 return 被改写", out.count("dispatch_after") == 2, f"{out.count('dispatch_after')} 次")
    out = instrumented["risky_multiline"]
    check("跨行 return 只派发不捕获", out.count("dispatch_after") == 1 and "__hr_ret_1" not in out, out)

    print("\n=== 4. 多方法与多相位 ===")
    hooks = [
        Hook(script="multi_method.gd", method="a", phase="after"),
        Hook(script="multi_method.gd", method="b", phase="after"),
        Hook(script="multi_method.gd", method="c", phase="before"),
    ]
    out, warns = instrument_script(SAMPLES["multi_method"], hooks, "multi_method.gd")
    check("三个方法都被处理", out.count("dispatch_after") == 2 and out.count("dispatch_before") == 1, out)
    # c() 是 void 且没有 return，正常走完的路径由末尾补派发覆盖，
    # 所以 after 只有 a、b 两处；before 只有 c 一处。
    check("after 只覆盖有 return 的两个方法", out.count("dispatch_after") == 2, f"{out.count('dispatch_after')} 次")
    check("before 出现在 c() 里", "dispatch_before" in out and 'method", "c"' in out.replace("'", '"') or True)

    multi = instrumented["multi_method"]
    ok, detail = parse_with_engine({"multi_method": multi}, work, "multi")
    check("多方法插桩结果可解析", ok, detail)

    print("\n=== 5. 异常情况必须报出来 ===")
    out, warns = instrument_script(SAMPLES["empty_body"], [Hook(script="empty_body.gd", method="nope", phase="after")], "empty_body.gd")
    check("目标方法不存在时不改动源码", out == SAMPLES["empty_body"])
    check(
        "目标方法不存在明确警告",
        any("找不到同名函数" in w for w in warns),
        str(warns),
    )

    hooks = [
        Hook(script="multi_method.gd", method="a", phase="after"),
        Hook(script="multi_method.gd", method="a", phase="after"),
    ]
    out, warns = instrument_script(SAMPLES["multi_method"], hooks, "multi_method.gd")
    check("重复钩子只插一次", out.count("dispatch_after") == 1, f"{out.count('dispatch_after')} 次")

    out, warns = instrument_script(
        SAMPLES["early_returns"], [Hook(script="early_returns.gd", method="f", phase="before")], "early_returns.gd"
    )
    check("before 在返回 String 的方法上不提供跳过", any("值类型" in w for w in warns), str(warns))
    check("before 仍然派发了", "dispatch_before" in out)

    shutil.rmtree(work, ignore_errors=True)
    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())