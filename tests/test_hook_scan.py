"""hook_scan 的测试：把「清单与源码相位不一致」这个坑固化下来。

来源是 2026-11-05 的浮游炮事故：
- 清单只写 after，源码注册了 before+after
- 插桩按清单单插 → before 点不存在 → install() fail-fast → 16 个钩子全丢
"""

from __future__ import annotations

from pathlib import Path

from app.core.ext import hook_scan


LITERAL_CALLS = """
extends Node

var api

func install(context) -> bool:
\tapi = context
\tif not api.add_method_hook("scripts/game.gd", "character_attributes", "after", _hook_attrs, 10):
\t\treturn false
\tif not api.add_method_hook("scripts/slice_run.gd", "tick", "before", _hook_tick, 10):
\t\treturn false
\treturn true

func _hook_attrs(context: Dictionary) -> void:
\tpass

func _hook_tick(context: Dictionary) -> void:
\tpass
"""

CONST_SCRIPT = """
extends Node

const MONSTER_SCRIPT = "scripts/slice_monster.gd"

func _ready() -> void:
\tapi.add_method_hook(MONSTER_SCRIPT, "damage", "before", _before, 0)
\tapi.add_method_hook(MONSTER_SCRIPT, "damage", "after", _after, 0)
\tapi.add_method_hook("scripts/slice_run.gd", "request_skill", "before", _req, 0)
"""

#: 浮游炮的真实写法：钩子写在数组表里，循环注册
TABLE_CALLS = """
extends Node

var api

func install(context) -> bool:
\tapi = context
\tvar hooks: Array = [
\t\t["scripts/game.gd", "learned_skill_id", "after", learned_skill],
\t\t["scripts/slice_run.gd", "request_skill", "before", before_request],
\t\t["scripts/slice_run.gd", "request_skill", "after", after_request],
\t\t["scripts/slice_run.gd", "finish_action", "before", finish_action],
\t\t["scripts/slice_run.gd", "tick", "before", battle_tick]
\t]
\tfor hook in hooks:
\t\tif not api.add_method_hook(hook[0], hook[1], hook[2], hook[3], 50):
\t\t\treturn false
\treturn true
"""


def test_literal_calls_parsed():
    r = hook_scan.scan_source(LITERAL_CALLS)
    got = {(s.script, s.method, s.phase) for s in r.sites}
    assert ("scripts/game.gd", "character_attributes", "after") in got
    assert ("scripts/slice_run.gd", "tick", "before") in got
    assert r.unresolved == 0


def test_const_script_resolved():
    """脚本路径用常量时要能解析，否则整个包的钩子都漏。"""
    r = hook_scan.scan_source(CONST_SCRIPT)
    got = {(s.script, s.method, s.phase) for s in r.sites}
    assert ("scripts/slice_monster.gd", "damage", "before") in got
    assert ("scripts/slice_monster.gd", "damage", "after") in got


def test_table_calls_parsed():
    """浮游炮那种「数组表 + 循环注册」，必须能展开。"""
    r = hook_scan.scan_source(TABLE_CALLS)
    got = {(s.script, s.method, s.phase) for s in r.sites}
    assert ("scripts/slice_run.gd", "request_skill", "before") in got
    assert ("scripts/slice_run.gd", "request_skill", "after") in got
    assert ("scripts/slice_run.gd", "finish_action", "before") in got
    assert ("scripts/slice_run.gd", "tick", "before") in got
    assert len(r.sites) == 5


def test_comment_examples_ignored():
    """注释里的示例代码不能被当成真实注册。"""
    src = """
extends Node
# 例：api.add_method_hook("scripts/foo.gd", "bar", "before", _cb, 0)
func _ready() -> void:
\tpass
"""
    r = hook_scan.scan_source(src)
    assert r.sites == []


def test_runtime_hooks_takes_precedence(tmp_path):
    """有 runtime_hooks 时以它为准，不去反查源码。"""
    (tmp_path / "main.gd").write_text(
        'api.add_method_hook("scripts/a.gd", "m", "before", _cb, 0)',
        encoding="utf-8",
    )
    got = hook_scan.real_phases(
        tmp_path,
        [{"script": "scripts/a.gd", "method": "m", "phase": "after"}],
        "scripts/a.gd",
        "m",
    )
    assert got == {"after"}


def test_real_phases_falls_back_to_source(tmp_path):
    """没有 runtime_hooks 时从源码反查。"""
    (tmp_path / "main.gd").write_text(CONST_SCRIPT, encoding="utf-8")
    got = hook_scan.real_phases(tmp_path, None, "scripts/slice_monster.gd", "damage")
    assert got == {"before", "after"}


def test_real_phases_empty_when_unknown(tmp_path):
    """查不到就返回空，调用方据此判「未知」，不能假装 before+after 都存在。"""
    got = hook_scan.real_phases(tmp_path, None, "scripts/none.gd", "nope")
    assert got == set()


def test_scan_package_walks_all_files(tmp_path):
    """钩子可能在包内任意文件里，不只入口。"""
    (tmp_path / "mods" / "x").mkdir(parents=True)
    (tmp_path / "mods" / "x" / "a.gd").write_text(CONST_SCRIPT, encoding="utf-8")
    (tmp_path / "mods" / "x" / "b.gd").write_text(LITERAL_CALLS, encoding="utf-8")
    r = hook_scan.scan_package(tmp_path)
    assert len(r.sites) >= 4
    assert {s.source for s in r.sites}  # 记录了来源文件