"""用真引擎验证 M0-3（G2 修复）：扩展能不能真拿到 instance / arguments。

这是本轮**唯一会改变游戏行为**的改动，必须实跑验证，不能只看生成的文本。

验证三件事：
1. 插桩后的脚本能被引擎解析（尾随逗号、self 引用都合法）
2. ``context["instance"]`` 拿到的是**被钩方法所在的宿主节点**
3. ``context["arguments"]`` 拿到的是**位置参数数组**（按下标能取对）

做法：把被测脚本 + 一个「扩展」打进 pck，让扩展在钩子里把读到的东西写进文件，
Python 侧读文件断言。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import paths as paths_mod  # noqa: E402
from app.core.ext.hooks import Hook, instrument_script  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402


def _work_dir() -> Path:
    """调用时取 ``paths.BUILDS``，让 conftest 的临时目录隔离生效。

    写死 ``ROOT/builds`` 会绕过隔离：每个 case 要拷一份 164 MB 的
    ``jjxf46.exe``，两个 case 就是 328 MB 落在真实目录里。
    """
    return paths_mod.BUILDS / "ctx-extended"

# 被钩的「游戏脚本」：宿主节点，带两个参数
GAME_SCRIPT = '''extends Node

var hit_count: int = 0

func take_hit(dmg: int, attacker: Node) -> int:
	hit_count += 1
	return dmg * 2
'''

# 「扩展」：在钩子里把读到的 instance / arguments 写进文件
EXT_SCRIPT = '''extends Node

func _after_take_hit(context: Dictionary) -> void:
	var f := FileAccess.open("user://ctx_probe.json", FileAccess.WRITE)
	if f == null:
		return
	var inst: Node = context.get("instance")
	var args: Array = context.get("arguments", [])
	f.store_string(JSON.stringify({
		"has_instance": inst != null,
		"instance_valid": is_instance_valid(inst),
		"instance_is_self": inst == get_parent().get_node("Host"),
		"instance_hit_count": inst.hit_count if is_instance_valid(inst) else -1,
		"args_size": args.size(),
		"arg0": args[0] if args.size() > 0 else null,
		"arg1_class": args[1].get_class() if args.size() > 1 else null,
		"result": context.get("result"),
	}))
	f.close()
'''


def build_and_run(extended: bool) -> dict:
    """跑一遍，返回扩展写出的探测结果。"""
    case = _work_dir() / ("on" if extended else "off")
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)

    instrumented, warns = instrument_script(
        GAME_SCRIPT,
        [Hook(script="game.gd", method="take_hit", phase="after")],
        "game.gd",
        extended_ctx=extended,
    )
    assert not warns, warns

    writer = PckWriter(case / "probe.pck", line="4.6")
    writer.add_text(
        "project.godot",
        "config_version=5\n\n[application]\n\nconfig/name=\"ctx\"\n"
        "run/main_scene=\"res://main.tscn\"\n"
        "config/features=PackedStringArray(\"4.6\", \"GL Compatibility\")\n\n"
        "[autoload]\n\n"
        "HookRuntime=\"*res://HookRuntime.gd\"\n\n"
        "[rendering]\n\nrenderer/rendering_method=\"gl_compatibility\"\n",
    )
    writer.add_text(
        "main.tscn",
        '[gd_scene format=3]\n\n'
        '[ext_resource type="Script" path="res://main.gd" id="1"]\n\n'
        '[node name="Main" type="Node"]\n'
        'script = ExtResource("1")\n',
    )
    # main.gd：建宿主节点、挂被钩脚本、调它；再跑扩展的钩子
    writer.add_text(
        "main.gd",
        "extends Node\n\n"
        "func _ready() -> void:\n"
        '\tvar host := Node.new()\n'
        '\thost.name = "Host"\n'
        '\thost.set_script(load("res://game.gd"))\n'
        "\tadd_child(host)\n"
        # load() 返回 Variant，不能用 := 推断类型（引擎会报
        # "Cannot infer the type of variable"）—— 必须显式标注。
        '\tvar ext_script: Script = load("res://ext.gd")\n'
        "\tvar ext: Node = ext_script.new()\n"
        "\tadd_child(ext)\n"
        "\tHookRuntime.register(\"game.gd\", \"take_hit\", \"after\", "
        'ext._after_take_hit, 50)\n'
        "\thost.take_hit(21, self)\n"
        "\tget_tree().quit(0)\n",
    )
    writer.add_text("game.gd", instrumented)
    writer.add_text("ext.gd", EXT_SCRIPT)
    writer.add_text(
        "HookRuntime.gd",
        "extends Node\n\n"
        "var _hooks: Dictionary = {}\n\n"
        "func register(s: String, m: String, p: String, cb: Callable, prio: int) -> bool:\n"
        '\tvar k := "%s::%s::%s" % [s, m, p]\n'
        "\tif not _hooks.has(k):\n"
        "\t\t_hooks[k] = []\n"
        "\t_hooks[k].append(cb)\n"
        "\treturn true\n\n"
        "func dispatch_after(s: String, m: String, c: Dictionary) -> void:\n"
        '\tvar k := "%s::%s::%s" % [s, m, p2()]\n'
        "\tfor cb in _hooks.get(k, []):\n"
        "\t\tif cb.is_valid():\n"
        "\t\t\tcb.call(c)\n\n"
        "func p2() -> String:\n"
        '\treturn "after"\n',
    )
    writer.commit()

    exe = case / "probe.exe"
    shutil.copyfile(ROOT / "runtime" / "host46" / "jjxf46.exe", exe)

    # user:// 指向 case 目录，便于读回探测结果
    env = {
        **dict(__import__("os").environ),
        "APPDATA": str(case / "appdata"),
        "LOCALAPPDATA": str(case / "appdata"),
    }
    (case / "appdata").mkdir(exist_ok=True)

    proc = subprocess.run(
        [str(exe), "--headless", "--quit-after", "600"],
        cwd=str(case),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    err = proc.stdout + proc.stderr
    if "Parse Error" in err or "Failed to load script" in err:
        return {"__error__": err[-1500:]}

    out = list(case.rglob("ctx_probe.json"))
    if not out:
        return {"__error__": "扩展没写出探测文件\n" + err[-1200:]}
    return json.loads(out[0].read_text(encoding="utf-8"))


def main() -> int:
    print("=== extended_ctx = False（原行为）===")
    off = build_and_run(False)
    print(json.dumps(off, ensure_ascii=False, indent=2))

    print("\n=== extended_ctx = True（G2 修复后）===")
    on = build_and_run(True)
    print(json.dumps(on, ensure_ascii=False, indent=2))

    print("\n=== 判定 ===")
    fails: list[str] = []
    if "__error__" in off:
        fails.append(f"关闭时也报错：{off['__error__']}")
    if "__error__" in on:
        fails.append(f"打开时报错：{on['__error__']}")
    else:
        if on.get("instance_valid") is not True:
            fails.append(f"instance 无效：{on}")
        if on.get("instance_is_self") is not True:
            fails.append(f"instance 不是宿主节点：{on}")
        if on.get("args_size") != 2:
            fails.append(f"arguments 长度不对（应为 2）：{on}")


# ---------------------------------------------------------------- pytest 入口


def test_extended_ctx_gives_extension_real_data() -> None:
    """扩展能真正拿到 instance / arguments —— 这是 M0-3 的核心验收。

    修之前：context 里没有这两个键 → ``is_instance_valid(null)`` 恒假 →
    回调第一行就 return → **145 处扩展代码是死代码**。
    """
    on = build_and_run(True)
    assert "__error__" not in on, on.get("__error__")
    assert on.get("instance_valid") is True, f"instance 无效：{on}"
    assert on.get("instance_is_self") is True, f"instance 不是宿主节点：{on}"
    assert on.get("instance_hit_count") == 1, f"instance 不是被钩方法的宿主：{on}"
    assert on.get("args_size") == 2, f"arguments 长度不对：{on}"
    assert on.get("arg0") == 21, f"arguments[0] 不对：{on}"
    assert on.get("arg1_class") == "Node", f"arguments[1] 不对：{on}"
    assert on.get("result") == 42, f"result 被破坏（21*2=42）：{on}"


def test_extended_ctx_off_keeps_old_behaviour() -> None:
    """关掉开关时行为与修复前一致 —— 证明这是「新增能力」而非「本来就有」。

    也保证用户能一键回退。
    """
    off = build_and_run(False)
    assert "__error__" not in off, off.get("__error__")
    assert off.get("instance_valid") is False, f"关闭时 instance 仍有效，开关没起作用：{off}"
    assert off.get("args_size") == 0, f"关闭时 arguments 仍非空：{off}"
    assert off.get("result") == 42, f"关闭时 result 被破坏：{off}"


def test_instrument_script_emits_instance_and_arguments() -> None:
    """静态层：生成的代码里必须有这两个键，且 arguments 是**数组**。

    扩展是按下标取参的（``args[0]`` / ``args[3]``），所以必须是数组，
    不是命名参数字典 —— 键名也不一样（字典那个叫 args，扩展读 arguments）。
    """
    src = "extends Node\n\nfunc pull(dmg: int, who) -> void:\n\tpass\n"
    out, warns = instrument_script(
        src, [Hook(script="x.gd", method="pull", phase="before")], "x.gd", extended_ctx=True
    )
    assert not warns, warns
    assert '"instance": self' in out, out
    assert '"arguments": [dmg, who]' in out, out

    off, _ = instrument_script(
        src, [Hook(script="x.gd", method="pull", phase="before")], "x.gd", extended_ctx=False
    )
    assert "instance" not in off, off
    # extended_ctx 关的是「注入 instance / arguments 数组」；
    # before 之后的参数回写（__hr_args）是独立能力，必须保留——
    # 否则钩子改了实参却传不回原方法，大招模型那类问题会复发。
    assert '"arguments": [dmg, who]' not in off, off
    assert "__hr_args" in off, off


if __name__ == "__main__":
    raise SystemExit(main())
