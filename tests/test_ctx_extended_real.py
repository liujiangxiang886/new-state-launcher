"""用真实游戏脚本（反解工程的 player.gd）验证 M0-3 端到端。

前面的 test_ctx_extended.py 用的是合成脚本。这里用**玩家真正会跑的**那个
``scripts/player.gd``（内置线反解工程里的原版脚本）跑一遍：

1. 插桩后的 player.gd 仍能被引擎解析（真实脚本比合成脚本复杂得多，
   有静态方法、有嵌套类、有各种边角）
2. ``context["instance"]`` 拿到的是 player.gd 的宿主节点
3. ``context["arguments"]`` 拿到的是 ``_physics_process(delta)`` 的实参
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ext.hooks import Hook, instrument_script  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402

#: 真实脚本的来源（反解工程）。找不到就跳过（那是用户机器上的可选路径）。
#: 用环境变量 ``JJXF_REAL_SRC_DIR`` 指定反解工程根目录；未设置时整组用例跳过。
_REAL_SRC = os.environ.get("JJXF_REAL_SRC_DIR", "").strip()
REAL_SCRIPT_CANDIDATES = (
    (Path(_REAL_SRC) / "scripts" / "player.gd"),
) if _REAL_SRC else ()

#: 跑真实 player.gd 需要它脚本里 ``$Visual`` 那一整套角色外观节点。
#: 这些节点由游戏场景提供（visual.gd 等），本仓库不复刻——桩起来要连带实现
#: configure / advance_action_motion / control_action_timeline 等一整套接口，
#: 那已经超出「验证插桩没破坏脚本」的范围了。
#: 反解工程里没有 visual.gd 时，本文件的两条端到端用例直接跳过。
VISUAL_SCRIPT = (
    Path(_REAL_SRC) / "scripts" / "visual.gd"
) if _REAL_SRC else Path("__none__")

#: 扩展侧：读 instance / arguments，写进文件
EXT_SCRIPT = '''extends Node

func _after_physics(context: Dictionary) -> void:
	var f := FileAccess.open("user://real_probe.json", FileAccess.WRITE)
	if f == null:
		return
	var inst: Node = context.get("instance")
	var args: Array = context.get("arguments", [])
	f.store_string(JSON.stringify({
		"instance_valid": is_instance_valid(inst),
		"instance_class": inst.get_class() if is_instance_valid(inst) else null,
		"instance_name": inst.name if is_instance_valid(inst) else null,
		"args_size": args.size(),
		"arg0_is_float": args[0] is float if args.size() > 0 else false,
	}))
	f.close()
'''


def _real_script() -> Path | None:
    for cand in REAL_SCRIPT_CANDIDATES:
        if cand.is_file():
            return cand
    return None


def _write_stubs(writer) -> None:
    """补齐 player.gd 依赖的东西。

    真实脚本引用了游戏本体的一堆东西，孤立环境里都不存在：
    ``Game`` / ``Audio`` 两个 autoload、几个 preload 的脚本。
    这里给最小 stub —— 目的是让**引擎去解析 player.gd**，
    验证插桩没有破坏它，不关心 stub 的行为。
    """
    writer.add_text("Game.gd", "extends Node\n\n"
                          "func character_attributes() -> Dictionary:\n\treturn {}\n\n"
                          "func get_node_or_null_x(_n: String) -> Node:\n\treturn null\n")
    writer.add_text("Audio.gd", "extends Node\n")
    writer.add_text("Controls.gd", "extends Node\n")
    # preload 的脚本：内容不重要，能解析就行。
    # 但**静态方法的签名必须对得上** —— 调用方按位置传参，
    # 签名少一个参数就会报 "Too many arguments"。
    writer.add_text(
        "scripts/slice_effect.gd",
        "extends Node\n\n"
        "static func spawn(_a = null, _b = null, _c = null) -> Node:\n"
        "\treturn Node.new()\n",
    )
    writer.add_text(
        "scripts/foot_shadow.gd",
        "extends Node\n\n"
        "static func shadow_texture() -> Texture2D:\n\treturn null\n",
    )
    for rel in (
        "scripts/flash/motion_script_player.gd",
        "scripts/flash/vkey_match.gd",
    ):
        writer.add_text(rel, "extends Node\n")


def _run(instrumented: str, case: Path) -> dict:
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)

    writer = PckWriter(case / "probe.pck", line="4.6")
    writer.add_text(
        "project.godot",
        "config_version=5\n\n[application]\n\nconfig/name=\"real\"\n"
        "run/main_scene=\"res://main.tscn\"\n"
        "config/features=PackedStringArray(\"4.6\", \"GL Compatibility\")\n\n"
        "[autoload]\n\n"
        "HookRuntime=\"*res://HookRuntime.gd\"\n"
        "Game=\"*res://Game.gd\"\n"
        "Audio=\"*res://Audio.gd\"\n"
        "Controls=\"*res://Controls.gd\"\n\n"
        "[rendering]\n\nrenderer/rendering_method=\"gl_compatibility\"\n",
    )
    writer.add_text(
        "main.tscn",
        '[gd_scene format=3]\n\n'
        '[ext_resource type="Script" path="res://main.gd" id="1"]\n\n'
        '[node name="Main" type="Node"]\n'
        'script = ExtResource("1")\n',
    )
    writer.add_text(
        "main.gd",
        "extends Node\n\n"
        "func _ready() -> void:\n"
        # player.gd 继承 CharacterBody2D —— 宿主必须是同类型的节点，
        # 否则 set_script() 会报「不能赋给 Node」。
        # 子节点必须在 set_script 之前挂好：赋值脚本会立刻触发 _ready，
        # 而 player.gd 的 _ready 第一件事就是 get_node("Visual"/"Shadow")。
        "\tvar host := CharacterBody2D.new()\n"
        '\thost.name = "Player"\n'
        '\tvar visual := Node2D.new()\n'
        '\tvisual.name = "Visual"\n'
        "\thost.add_child(visual)\n"
        '\tvar name_node := Node2D.new()\n'
        '\tname_node.name = "Name"\n'
        "\thost.add_child(name_node)\n"
        # 内置线的 player.gd 还要求一个 Shadow 节点（_ready 里直接 get_node）。
        # 它是原版脚本的固有依赖，与本测试要验证的东西无关，补个空的即可。
        '\tvar shadow := Node2D.new()\n'
        '\tshadow.name = "Shadow"\n'
        "\thost.add_child(shadow)\n"
        # 玩家脚本继承 CharacterBody2D —— 宿主必须是同类型节点，
        # 否则 set_script() 会报「不能赋给 Node」。
        '\thost.set_script(load("res://player.gd"))\n'
        "\tadd_child(host)\n"
        '\tvar ext_script: Script = load("res://ext.gd")\n'
        "\tvar ext: Node = ext_script.new()\n"
        "\tadd_child(ext)\n"
        "\tHookRuntime.register(\"scripts/player.gd\", \"_physics_process\", \"after\", "
        'ext._after_physics, 50)\n'
        "\thost._physics_process(0.016)\n"
        "\tget_tree().quit(0)\n",
    )
    writer.add_text("player.gd", instrumented)
    writer.add_text("ext.gd", EXT_SCRIPT)
    _write_stubs(writer)
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
        '\tvar k := "%s::%s::after" % [s, m]\n'
        "\tfor cb in _hooks.get(k, []):\n"
        "\t\tif cb.is_valid():\n"
        "\t\t\tcb.call(c)\n",
    )
    writer.commit()

    exe = case / "probe.exe"
    shutil.copyfile(ROOT / "runtime" / "host46" / "jjxf46.exe", exe)
    env = {
        **dict(os.environ),
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
        return {"__error__": err[-1800:]}
    found = list(case.rglob("real_probe.json"))
    if not found:
        return {"__error__": "扩展没写出探测文件\n" + err[-1500:]}
    return json.loads(found[0].read_text(encoding="utf-8"))


def test_real_player_gd_instrumentation_works() -> None:
    """真实 player.gd 插桩后仍能被引擎解析，且 instance/arguments 正确。"""
    real = _real_script()
    if real is None:
        print("  （跳过：反解工程不在本机）")
        return
    if not VISUAL_SCRIPT.is_file():
        print("  （跳过：缺少角色外观脚本，跑不起来完整 player.gd 场景）")
        return

    source = real.read_text(encoding="utf-8", errors="replace")
    # 只钩 _physics_process —— 它有 delta 参数，能验证 arguments 是位置参数数组
    out, warns = instrument_script(
        source,
        [Hook(script="scripts/player.gd", method="_physics_process", phase="after")],
        "scripts/player.gd",
        extended_ctx=True,
    )
    assert '"instance": self' in out, "真实脚本里没插进 instance"
    assert '"arguments": [delta]' in out, "arguments 表达式不对"

    case = ROOT / "builds" / "ctx-real"
    result = _run(out, case)
    assert "__error__" not in result, result.get("__error__")
    assert result.get("instance_valid") is True, f"instance 无效：{result}"
    assert result.get("instance_name") == "Player", f"instance 不是宿主：{result}"
    assert result.get("args_size") == 1, f"arguments 长度不对：{result}"
    assert result.get("arg0_is_float") is True, f"arguments[0] 不是 delta：{result}"


def test_real_player_gd_without_extended_keeps_working() -> None:
    """关掉开关时，真实脚本插桩后也必须能解析（不能破坏原行为）。"""
    real = _real_script()
    if real is None:
        print("  （跳过：反解工程不在本机）")
        return
    if not VISUAL_SCRIPT.is_file():
        print("  （跳过：缺少角色外观脚本，跑不起来完整 player.gd 场景）")
        return
    source = real.read_text(encoding="utf-8", errors="replace")
    out, warns = instrument_script(
        source,
        [Hook(script="scripts/player.gd", method="_physics_process", phase="after")],
        "scripts/player.gd",
        extended_ctx=False,
    )
    case = ROOT / "builds" / "ctx-real-off"
    result = _run(out, case)
    assert "__error__" not in result, result.get("__error__")
    assert result.get("instance_valid") is False, f"关闭时 instance 仍有效：{result}"


if __name__ == "__main__":
    test_real_player_gd_instrumentation_works()
    test_real_player_gd_without_extended_keeps_working()
    print("真实 player.gd 端到端验证通过")
