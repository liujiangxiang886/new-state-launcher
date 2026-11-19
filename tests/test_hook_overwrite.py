"""钩子覆盖记账：让「玩家配的倍率被静默吃掉」变得可见。

背景（现网已发生）：三个扩展挤在同一个钩子点上，后执行的直接改掉
``context["result"]``，前一个写的值就没了。构建期的 ``overlap.text``
警告看不到它 —— 因为它们改的不是文件，是运行期的值。

本测试用**真引擎**跑一遍 HookRuntime，验证：
1. 同一钩子点上两个扩展都改了 ``result`` 时，记账能记下**是谁**改的；
2. 只有一个回调时**不记账**（快路径，不该有额外开销）；
3. 就地修改（同一个 Dictionary 引用）也能被检测到 —— 这正是
   ``rose`` 那个 bug 的形态，用对象比较是检测不到的。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import paths as paths_mod  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402


def _work_dir() -> Path:
    """调用时取 ``paths.BUILDS``，让 conftest 的临时目录隔离生效。

    写死 ``ROOT/builds`` 会绕过隔离：每个 case 要拷一份 164 MB 的
    ``jjxf46.exe``，两个 case 就是 328 MB 落在真实目录里。
    """
    return paths_mod.BUILDS / "hook-overwrite"

PROJECT = (
    "config_version=5\n\n[application]\n\nconfig/name=\"hook overwrite\"\n"
    "run/main_scene=\"res://main.tscn\"\n"
    'config/features=PackedStringArray("4.6", "GL Compatibility")\n\n'
    "[autoload]\n\n"
    'HookRuntime="*res://_compat/hook_runtime.gd"\n\n'
    "[rendering]\n\nrenderer/rendering_method=\"gl_compatibility\"\n"
)

MAIN_SCENE = (
    "[gd_scene format=3]\n\n"
    '[ext_resource type="Script" path="res://main.gd" id="1"]\n\n'
    '[node name="Main" type="Node"]\nscript = ExtResource("1")\n'
)

#: 两个「扩展」分别改同一个 result；第一个先写 200，第二个后写 300。
MULTI = """extends Node

func _ready() -> void:
\tHookRuntime.declare_points([{"script": "x.gd", "method": "f", "phase": "after"}])
\tHookRuntime.register("x.gd", "f", "after", Callable(self, "_a"), 50, "mod.a")
\tHookRuntime.register("x.gd", "f", "after", Callable(self, "_b"), 60, "mod.b")
\tvar ctx := {"result": {"hp": 100}}
\tHookRuntime.dispatch_after("x.gd", "f", ctx)
\tvar f := FileAccess.open("user://probe.json", FileAccess.WRITE)
\tf.store_string(JSON.stringify({
\t\t"result": ctx["result"],
\t\t"records": HookRuntime.overwrite_records(),
\t}))
\tf.close()
\tget_tree().quit(0)

func _a(context: Dictionary) -> void:
\t# 就地改（同一个 Dictionary 引用）—— 对象比较检测不到这种改动
\tcontext["result"]["hp"] = 200

func _b(context: Dictionary) -> void:
\tcontext["result"]["hp"] = 300
"""

#: 只有一个回调：不该产生任何记账（战斗钩子每帧都跑，不能有额外开销）
SINGLE = """extends Node

func _ready() -> void:
\tHookRuntime.declare_points([{"script": "x.gd", "method": "f", "phase": "after"}])
\tHookRuntime.register("x.gd", "f", "after", Callable(self, "_a"), 50, "mod.only")
\tvar ctx := {"result": {"hp": 100}}
\tHookRuntime.dispatch_after("x.gd", "f", ctx)
\tvar f := FileAccess.open("user://probe.json", FileAccess.WRITE)
\tf.store_string(JSON.stringify({
\t\t"result": ctx["result"],
\t\t"records": HookRuntime.overwrite_records(),
\t}))
\tf.close()
\tget_tree().quit(0)

func _a(context: Dictionary) -> void:
\tcontext["result"]["hp"] = 200
"""


def _run(tag: str, main_gd: str) -> dict:
    case = _work_dir() / tag
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)

    hook_runtime = (ROOT / "runtime" / "_compat" / "hook_runtime.gd").read_text(encoding="utf-8")
    sentinel = "extends Node\n\nfunc ok() -> bool:\n\treturn true\n"

    # exe 与 pck **必须同名**：Godot 模板 exe 只认同名 pck，
    # 异名会弹模态 ALERT 并让「没挂载」伪装成「全部通过」。
    writer = PckWriter(case / "godot.pck", line="4.6")
    writer.add_text("project.godot", PROJECT)
    writer.add_text("main.tscn", MAIN_SCENE)
    writer.add_text("main.gd", main_gd)
    writer.add_text("_compat/hook_runtime.gd", hook_runtime)
    writer.add_text("_sentinel.gd", sentinel)
    writer.commit()

    exe = case / "godot.exe"
    shutil.copyfile(ROOT / "runtime" / "host46" / "jjxf46.exe", exe)
    appdata = case / "appdata"
    appdata.mkdir(exist_ok=True)
    env = {**os.environ, "APPDATA": str(appdata), "LOCALAPPDATA": str(appdata)}

    proc = subprocess.run(
        [str(exe), "--headless", "--quit-after", "600"],
        cwd=str(case),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    # 哨兵：读不到哨兵就说明 pck 没挂上，那时「没有报错」是假阳性
    probe = appdata / "Godot" / "app_userdata" / "hook overwrite" / "probe.json"
    if not probe.is_file():
        raise AssertionError(f"探针没写出（pck 可能没挂上）\nstdout/stderr:\n{out[:2000]}")
    return json.loads(probe.read_text(encoding="utf-8"))


def test_records_who_overwrote_result() -> None:
    data = _run("multi", MULTI)
    # 结果确实是后执行的扩展写的
    assert data["result"]["hp"] == 300, data
    records = data["records"]
    assert records, "两个扩展都改了 result，必须记下来"

    # 第一条：mod.a 把 100 改成 200
    first = records[0]
    assert first["by"] == "mod.a", records
    assert '"hp":100' in first["before"].replace(" ", "")
    assert '"hp":200' in first["after"].replace(" ", "")

    # 第二条：mod.b 把 200 改成 300 —— 这就是「被覆盖」的证据
    second = records[1]
    assert second["by"] == "mod.b", records
    assert '"hp":200' in second["before"].replace(" ", "")
    assert '"hp":300' in second["after"].replace(" ", "")


def test_single_callback_is_not_recorded() -> None:
    data = _run("single", SINGLE)
    assert data["result"]["hp"] == 200, data
    assert data["records"] == [], "只有一个回调时不该记账（快路径无额外开销）"


def test_runner_helpers_are_readable() -> None:
    """覆盖记录要能转成人话，否则报告里是一堆 JSON。"""
    from app.core import smoke as smoke_mod

    lines = smoke_mod.overwrite_lines(
        [
            {
                "hook": "scripts/x.gd::character_attributes::after",
                "by": "rose.dimension_ruins",
                "before": '{"hp":100}',
                "after": '{"hp":300}',
            }
        ]
    )
    assert lines
    assert "character_attributes::after" in lines[0]
    assert "rose.dimension_ruins" in lines[0]
    assert "scripts/x.gd" not in lines[0], "路径太长，报告里只留最后两段"
