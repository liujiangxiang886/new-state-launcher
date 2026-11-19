"""S6：含 ``return`` 的方法注入 ``after`` 后，返回值不变且钩子被调用。

这是插桩器最容易出错的一类：``after`` 要在原方法跑完后派发，还得允许回调改结果。
原方法里若有多处 ``return``（尤其是嵌在 ``if`` 里的提前返回），
在函数末尾追加一句是错的——那条路径根本走不到末尾。

正确做法：把每个 ``return X`` 各自改写成
``var __r := X`` + 派发 + ``return ctx.get("result", __r)``。

本测试用一份合成脚本 + 底座引擎实跑，覆盖：

1. 单点 return；
2. 多处 return（含嵌套提前返回）；
3. void 方法的提前 return；
4. 钩子的 ``result`` 能否被回调改写。

    python tests/test_hook_return.py
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

TARGET = (
    "extends Node\n"
    "\n"
    "var calls: Array = []\n"
    "var events: Array = []\n"
    "\n"
    "func single(a: int) -> int:\n"
    "\treturn a * 2\n"
    "\n"
    "func multi(a: int) -> String:\n"
    "\tif a < 0:\n"
    "\t\treturn \"negative\"\n"
    "\tif a == 0:\n"
    "\t\treturn \"zero\"\n"
    "\treturn \"positive\"\n"
    "\n"
    "func void_one(a: int) -> void:\n"
    "\tcalls.append(a)\n"
    "\tif a > 10:\n"
    "\t\treturn\n"
    "\tcalls.append(a + 1)\n"
)

#: 驱动器：跑一遍所有方法，并把事件数、返回值都打出来
DRIVER = (
    "extends Node\n"
    "\n"
    "func _ready() -> void:\n"
    "\tvar t = load(\"res://target.gd\").new()\n"
    "\tprint(\"R1=\", t.single(3))\n"
    "\tprint(\"R2=\", t.multi(-1))\n"
    "\tprint(\"R3=\", t.multi(0))\n"
    "\tprint(\"R4=\", t.multi(5))\n"
    "\tt.void_one(20)\n"
    "\tt.void_one(1)\n"
    "\tprint(\"CALLS=\", t.calls)\n"
    "\tprint(\"EVENTS=\", HookRuntime.recorded.size())\n"
    "\tget_tree().quit(0)\n"
)

#: 最小的 HookRuntime 替身：只记录调用，不依赖底座
STUB = (
    "extends Node\n"
    "\n"
    "static var recorded: Array = []\n"
    "\n"
    "static func dispatch_after(script: String, method: String, ctx: Dictionary) -> void:\n"
    "\trecorded.append(\"%s::%s\" % [script, method])\n"
    "\tif method == \"single\":\n"
    "\t\tctx[\"result\"] = 999\n"
    "\n"
    "static func dispatch_before(script: String, method: String, ctx: Dictionary) -> void:\n"
    "\trecorded.append(\"before::%s::%s\" % [script, method])\n"
)

PROJECT = (
    "config_version=5\n"
    "\n"
    "[application]\n"
    "\n"
    'config/name="s6"\n'
    'run/main_scene="res://main.tscn"\n'
    'config/features=PackedStringArray("4.6", "GL Compatibility")\n'
    "\n"
    "[autoload]\n"
    "\n"
    'HookRuntime="*res://hook_runtime.gd"\n'
    "\n"
    "[rendering]\n"
    "\n"
    'renderer/rendering_method="gl_compatibility"\n'
)

SCENE = (
    "[gd_scene load_steps=2 format=3]\n"
    "\n"
    '[ext_resource type="Script" path="res://main.gd" id="1"]\n'
    "\n"
    '[node name="M" type="Node"]\n'
    'script = ExtResource("1")\n'
)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def run_probe(files: dict[str, str], work: Path) -> tuple[bool, str]:
    from app.core.pck.writer import PckWriter

    case = work / "case"
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)
    writer = PckWriter(case / "probe.pck", line="4.6")
    for path, text in files.items():
        writer.add_text(path, text)
    writer.commit()

    exe = case / "probe.exe"
    shutil.copyfile(ROOT / "runtime" / "host46" / "jjxf46.exe", exe)
    try:
        proc = subprocess.run(
            [str(exe), "--headless", "--quit-after", "900"],
            cwd=str(case),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "超时"
    return proc.returncode == 0, (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or "")


def main() -> int:
    work = ROOT / "builds" / "s6-hooks"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    print("=== 插桩 ===")
    hooks = [
        Hook(script="target.gd", method=name, phase="after")
        for name in ("single", "multi", "void_one")
    ]
    instrumented, warnings = instrument_script(TARGET, hooks, "target.gd")
    for line in instrumented.splitlines():
        if "__hr_" in line or "dispatch_after" in line:
            print(f"    {line.rstrip()}")
    check("插桩确实改写了源码", instrumented != TARGET)
    check("无警告", not warnings, str(warnings))
    check(
        "所有 return 都被改写（5 个 return + 1 个末尾补派发）",
        instrumented.count("dispatch_after") == 6,
        f"dispatch_after 出现 {instrumented.count('dispatch_after')} 次",
    )
    check(
        "嵌套 return 的缩进被保留",
        "\t\treturn __hr_ctx_3.get" in instrumented,
        "嵌套分支的 return 未按原缩进改写",
    )
    check(
        "void 方法末尾补了派发（正常走完的路径也要触发）",
        instrumented.rstrip().endswith("dispatch_after(\"target.gd\", \"void_one\", {\"result\": null})"),
        "末尾没有补派发，正常路径不会触发钩子",
    )

    print()
    print("=== 引擎实跑 ===")
    ok, output = run_probe(
        {
            "project.godot": PROJECT,
            "main.tscn": SCENE,
            "main.gd": DRIVER,
            "hook_runtime.gd": STUB,
            "target.gd": instrumented,
        },
        work,
    )
    tail = output.strip().splitlines()
    print("\n".join(f"    {line}" for line in tail[-12:]))

    check("引擎跑通", ok and "EVENTS=" in output, "未输出 EVENTS")
    check("single 返回值被钩子改写为 999", "R1=999" in output, "")
    check("multi(-1) 走提前返回且钩子触发", "R2=negative" in output)
    check("multi(0) 走第二个提前返回", "R3=zero" in output)
    check("multi(5) 走末尾返回", "R4=positive" in output)
    check("void 的提前 return 生效", "CALLS=[20, 1, 2]" in output, "")
    check("钩子调用 6 次（3 方法 6 条路径）", "EVENTS=6" in output, "")

    shutil.rmtree(work, ignore_errors=True)
    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())