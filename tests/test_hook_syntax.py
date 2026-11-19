"""插桩产物的**语法合法性**回归：注入的代码必须能被 GDScript 编译。

背景（2026-11-03 玩家版实测事故）
---------------------------------
``scripts/slice_run.gd`` 里有一处 lambda：

.. code-block:: gdscript

    order.sort_custom( func(a, b):
        if int(packs[a].rank) == int(packs[b].rank):
            return a < b
        return int(packs[a].rank) > int(packs[b].rank))   # ← 末尾 ')' 是 sort_custom 的

插桩器把第二处 ``return`` 改写成 ``var __hr_ret_2 := <表达式>`` 时，把那个属于
``sort_custom(`` 的右括号一起吞进了表达式，产出一行

.. code-block:: gdscript

    var __hr_ret_2 := int(packs[a].rank) > int(packs[b].rank))

GDScript 直接报 ``Expected statement, found "Indent"``，整份 ``slice_run.gd``
编译失败 → ``training_run.gd``（继承它）跟着失败 → ``opening_run.gd`` 也失败
→ ``game_world.slice_run`` 是 null → ``player.gd`` 每帧在
``if get_parent().slice_run.active:`` 抛错、走不到 ``move_and_slide()``。
玩家看到的就是「角色不能动、不能交互」。

同一轮还暴露了第二个隐患：``var x := <null 或 Variant>`` 在 GDScript 4 里
无法类型推断（``Cannot infer the type of ...``），所以返回值变量必须用 ``=``。

两个用例都用底座引擎实跑：能编译、能跑出正确结果才算通过。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ext.hooks import Hook, instrument_script  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402

HOST = ROOT / "runtime" / "host46" / "jjxf46.exe"

PROJECT = (
    "config_version=5\n"
    "\n"
    "[application]\n"
    "\n"
    'config/name="syntax"\n'
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

STUB = (
    "extends Node\n"
    "\n"
    "static var events: Array = []\n"
    "\n"
    "static func dispatch_after(script: String, method: String, ctx: Dictionary) -> void:\n"
    "\tevents.append(\"%s::%s\" % [script, method])\n"
    "\n"
    "static func dispatch_before(script: String, method: String, ctx: Dictionary) -> void:\n"
    "\tevents.append(\"before::%s::%s\" % [script, method])\n"
)


def _run(files: dict[str, str], work: Path) -> tuple[bool, str]:
    case = work / "case"
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)
    writer = PckWriter(case / "probe.pck", line="4.6")
    for path, text in files.items():
        writer.add_text(path, text)
    writer.commit()

    exe = case / "probe.exe"
    shutil.copyfile(HOST, exe)
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


def _instrument(source: str, method: str) -> str:
    hooks = [
        Hook(script="target.gd", method=method, phase="before"),
        Hook(script="target.gd", method=method, phase="after"),
    ]
    out, _warns = instrument_script(source, hooks, "target.gd")
    return out


@pytest.mark.skipif(not HOST.is_file(), reason="缺少底座引擎")
def test_return_inside_lambda_keeps_enclosing_paren(tmp_path: Path) -> None:
    """lambda 里借用外层收尾括号的 return，插桩后仍然能编译并返回正确结果。"""
    source = (
        "extends Node\n"
        "\n"
        "func ordering(packs: Array) -> Array:\n"
        "\tvar order: Array = range(packs.size())\n"
        "\torder.sort_custom( func(a, b):\n"
        "\t\tif int(packs[a].rank) == int(packs[b].rank):\n"
        "\t\t\treturn a < b\n"
        "\t\treturn int(packs[a].rank) > int(packs[b].rank))\n"
        "\treturn order\n"
    )
    instrumented = _instrument(source, "ordering")
    assert "rank))" in instrumented, "原文的收尾括号必须原样保留"
    assert "__hr_ret_2 := " not in instrumented, "括号不配平的 return 不该做捕获"

    driver = (
        "extends Node\n"
        "\n"
        "func _ready() -> void:\n"
        "\tvar t = load(\"res://target.gd\").new()\n"
        "\tvar packs = [{\"rank\": 1}, {\"rank\": 3}, {\"rank\": 1}]\n"
        "\tvar order: Array = t.ordering(packs)\n"
        "\tvar ranks: Array = []\n"
        "\tfor i in order:\n"
        "\t\tranks.append(int(packs[i].rank))\n"
        "\tprint(\"RANKS=\", ranks)\n"
        "\tprint(\"EVENTS=\", HookRuntime.events.size())\n"
        "\tget_tree().quit(0)\n"
    )
    ok, output = _run(
        {
            "project.godot": PROJECT,
            "main.tscn": SCENE,
            "main.gd": driver,
            "hook_runtime.gd": STUB,
            "target.gd": instrumented,
        },
        tmp_path,
    )
    assert "Expected statement" not in output, output[-2000:]
    assert ok, output[-2000:]
    assert "RANKS=[3, 1, 1]" in output, output[-2000:]
    assert "EVENTS=0" not in output, output[-2000:]


@pytest.mark.skipif(not HOST.is_file(), reason="缺少底座引擎")
def test_return_null_and_untyped_value_compile(tmp_path: Path) -> None:
    """``return null`` / 无类型 Variant 不能生成 ``:=`` 声明（GDScript 无法推断）。"""
    source = (
        "extends Node\n"
        "\n"
        "func pick(data: Dictionary) -> Variant:\n"
        "\tif data.is_empty():\n"
        "\t\treturn null\n"
        "\treturn data.get(\"value\")\n"
    )
    instrumented = _instrument(source, "pick")
    assert "__hr_ret_" in instrumented
    assert ":= null" not in instrumented
    assert ":= data.get" not in instrumented

    driver = (
        "extends Node\n"
        "\n"
        "func _ready() -> void:\n"
        "\tvar t = load(\"res://target.gd\").new()\n"
        "\tprint(\"NIL=\", t.pick({}))\n"
        "\tprint(\"VAL=\", t.pick({\"value\": 7}))\n"
        "\tprint(\"EVENTS=\", HookRuntime.events.size())\n"
        "\tget_tree().quit(0)\n"
    )
    ok, output = _run(
        {
            "project.godot": PROJECT,
            "main.tscn": SCENE,
            "main.gd": driver,
            "hook_runtime.gd": STUB,
            "target.gd": instrumented,
        },
        tmp_path,
    )
    assert "Cannot infer the type" not in output, output[-2000:]
    assert ok, output[-2000:]
    assert "NIL=<null>" in output, output[-2000:]
    assert "VAL=7" in output, output[-2000:]
