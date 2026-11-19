"""钩子注册点扫描：常量形式不能漏（实战 bug 的回归测试）。

背景
----

``app/core/ext/hooks.py`` 的扫描器原来只认字符串字面量：

.. code-block:: gdscript

    context.add_method_hook("scripts/player.gd", "_ready", "after", cb, 0)   # 认得
    context.add_method_hook(PLAYER_SCRIPT, "_ready", "after", cb, 0)          # 认不得

扩展常把脚本路径提成常量再传进去（``ai_act.combat_workbench`` 就是这么写的），
漏扫的后果是连锁的：

    漏扫 → 该钩子没被插桩 → 运行期 ``has_point()`` 拒绝注册
         → 扩展的 ``install()`` 返回 false → **整个扩展静默失效**

实测那次 ``ai_act.combat_workbench`` 36 个钩子挂了 3 个，整个扩展就全不生效了。

本文件锁定三件事：

1. 常量形式能被扫出来；
2. 字面量形式仍然正常（别把老写法弄坏）；
3. 跨文件的常量表也能用（常量定义在 A 文件、调用在 B 文件）。
"""

from __future__ import annotations

from app.core.ext import hooks as hooks_mod


def _scan(text: str) -> list[tuple[str, str, str]]:
    return [(h.script, h.method, h.phase) for h in hooks_mod.scan_registrations(text)]


def test_literal_form_still_scanned() -> None:
    """字面量写法是既有行为，不能因为加了常量支持而坏掉。"""
    got = _scan('context.add_method_hook("scripts/player.gd", "_ready", "after", cb, 0)')
    assert got == [("scripts/player.gd", "_ready", "after")]


def test_single_quoted_literal() -> None:
    got = _scan("context.add_method_hook('scripts/foo.gd', '_ready', 'before', cb, 0)")
    assert got == [("scripts/foo.gd", "_ready", "before")]


def test_const_form_scanned() -> None:
    """常量形式必须能扫出来——这是本次修复的核心。"""
    src = (
        'const PLAYER_SCRIPT := "scripts/player.gd"\n'
        'context.add_method_hook(PLAYER_SCRIPT, "_physics_process", "before", cb, 0)\n'
    )
    got = _scan(src)
    assert got == [("scripts/player.gd", "_physics_process", "before")]


def test_const_form_multiple_hooks() -> None:
    """一个常量被用于多个钩子时，每个都要扫出来。"""
    src = (
        'const PLAYER_SCRIPT := "scripts/player.gd"\n'
        'context.add_method_hook(PLAYER_SCRIPT, "_physics_process", "before", a, 0)\n'
        'context.add_method_hook(PLAYER_SCRIPT, "_physics_process", "after", b, 0)\n'
        'context.add_method_hook(PLAYER_SCRIPT, "direction_event", "before", c, 0)\n'
    )
    got = _scan(src)
    assert ("scripts/player.gd", "_physics_process", "before") in got
    assert ("scripts/player.gd", "_physics_process", "after") in got
    assert ("scripts/player.gd", "direction_event", "before") in got


def test_const_without_colon() -> None:
    """``const X = "v"``（不带 ``:=``）也要认。"""
    src = (
        'const PLAYER_SCRIPT = "scripts/player.gd"\n'
        'context.add_method_hook(PLAYER_SCRIPT, "_ready", "after", cb, 0)\n'
    )
    assert _scan(src) == [("scripts/player.gd", "_ready", "after")]


def test_res_prefix_stripped() -> None:
    """``res://`` 前缀要和字面量形式一样被剥掉。"""
    src = (
        'const S := "res://scripts/player.gd"\n'
        'context.add_method_hook(S, "_ready", "after", cb, 0)\n'
    )
    assert _scan(src) == [("scripts/player.gd", "_ready", "after")]


def test_unknown_const_yields_nothing_but_no_crash() -> None:
    """常量解析不出来时**不产出钩子也不抛异常**。

    静态扫描本来就是尽力而为，真正的把关是运行期的注册结果。
    这里不能因为认不出常量就把整个扩展的扫描搞崩。
    """
    got = _scan('context.add_method_hook(UNKNOWN_CONST, "_ready", "after", cb, 0)')
    assert got == []


def test_cross_file_const_table() -> None:
    """常量定义在 A 文件、调用在 B 文件时也要能扫出来。

    扩展常把脚本路径集中定义在一个公共文件里。
    """
    files = {
        "combat/constants.gd": 'const PLAYER_SCRIPT := "scripts/player.gd"\n',
        "main.gd": (
            'context.add_method_hook(PLAYER_SCRIPT, "_physics_process", "before", cb, 0)\n'
        ),
    }
    got = [(h.script, h.method, h.phase) for h in hooks_mod.scan_tree(files)]
    assert got == [("scripts/player.gd", "_physics_process", "before")]


def test_scan_tree_dedup() -> None:
    """同一个钩子出现多次只保留一条。"""
    files = {
        "a.gd": 'context.add_method_hook("scripts/x.gd", "_ready", "after", cb, 0)\n',
        "b.gd": 'context.add_method_hook("scripts/x.gd", "_ready", "after", cb2, 0)\n',
    }
    assert len(hooks_mod.scan_tree(files)) == 1


def test_scan_consts_returns_mapping() -> None:
    """常量扫描本身要能用。"""
    consts = hooks_mod.scan_consts(
        'const A := "scripts/a.gd"\n'
        "const B = 'scripts/b.gd'\n"
        "const C := 42\n"  # 非字符串常量，不该进表
    )
    assert consts == {"A": "scripts/a.gd", "B": "scripts/b.gd"}
