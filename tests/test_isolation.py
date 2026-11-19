"""L0 构建期隔离扫描的规则测试。

十二个校验点里，这一份覆盖**纯源码可判**的那些（L0-1..L0-7、L0-10）。
L0-8（钩子点撞车）、L0-12（peer 环/缺失）、L0-11（声明核对）另有测试。

**双向要求**：
- 正向：每种越界写法必须被抓到
- 反向：合法写法**不能**被报（误报会让整个机制失去意义）

反向那半更重要 —— 扫描器的第一版把 ``get_parent()``、``Engine.set_meta``、
``context.get("hud")`` 全报了，实测 9 个真实扩展命中 55 处，**全是误报**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ext import isolation as iso  # noqa: E402

OWN = "mch.mine"


def _codes(src: str, ext_id: str = OWN, **kw) -> list[str]:
    return [f.code for f in iso.scan_source(src, ext_id, "x.gd", **kw)]


# ---------------------------------------------------------------- 正向


def test_detects_api_host_access() -> None:
    """L0-1：直接取宿主。"""
    assert "L0-1" in _codes("func f():\n\tvar s = api.host._states\n")


def test_detects_reflection_bypass() -> None:
    """L0-1：反射写法 —— 实测能完全绕过改名，扫描必须覆盖。"""
    assert "L0-1" in _codes('func f():\n\tvar s = api.get("host").get("_states")\n')


def test_detects_modhost_path() -> None:
    """L0-2：按路径取宿主。"""
    assert "L0-2" in _codes('func f():\n\tvar n = get_node("/root/ModHost")\n')


def test_detects_sibling_extension_node() -> None:
    """L0-2：指名道姓找兄弟扩展。"""
    assert "L0-2" in _codes('func f():\n\tvar n = get_node("Mod_mch_sell_all")\n')


def test_detects_peer_resource_read() -> None:
    """L0-4：读别人扩展的资源。"""
    assert "L0-4" in _codes(
        'func f():\n\tvar t = load("res://mods/mch.sell_all/data/x.json")\n'
    )


def test_detects_shared_state_write() -> None:
    """L0-5：直接读写共享状态文件。"""
    assert "L0-5" in _codes(
        'func f():\n\tvar f = FileAccess.open("user://mod_states.json", FileAccess.WRITE)\n'
    )


def test_detects_meta_collision() -> None:
    """L0-6：全局 meta 撞名。"""
    assert "L0-6" in _codes('func f():\n\tEngine.set_meta("someone_elses_key", 1)\n')


def test_detects_cross_extension_service() -> None:
    """L0-7：读其它扩展的服务。"""
    assert "L0-7" in _codes('func f():\n\tvar s = api.service("mch.sell_all.get_price")\n')


def test_detects_extended_context_key_when_disabled() -> None:
    """L0-10：宿主没注入 instance/arguments，且开关关着 → 那段代码是死代码。"""
    src = 'func f():\n\tvar i: Node = context.get("instance")\n'
    assert "L0-10" in _codes(src, extended_ctx=False)
    # 开关开着时宿主会注入 → 不该报
    assert "L0-10" not in _codes(src, extended_ctx=True)


# ---------------------------------------------------------------- 反向（误报）


def test_ignores_own_resources() -> None:
    """读自己的资源是最常见的做法，不能报。"""
    assert not _codes('func f():\n\tvar t = load("res://mods/mch.mine/data/x.json")\n')


def test_ignores_get_parent() -> None:
    """``get_parent()`` 访问自己场景树里的节点是完全合法的用法。

    扫描器第一版把它算进 L0-2，实测 30 处命中**全是误报**
    （``actor.get_parent().get("slice_run")`` 这种）。
    """
    assert not _codes('func f():\n\tvar n = actor.get_parent().get("slice_run")\n')


def test_ignores_own_meta() -> None:
    """用自己的 id 打头的 meta 不算撞名。

    扩展的惯例是「厂商_用途」（``ai_act_wb_attraction_runtime``），
    不是原样 id —— 用完整 id 判断会误报 10 处。
    """
    assert not _codes('func f():\n\tEngine.set_meta("mch_mine_runtime", self)\n')
    # 厂商名不同就不算自己的 —— 这一条要显式给对 ext_id
    assert not _codes(
        'func f():\n\tEngine.set_meta("ai_act_wb_hit_runtime", self)\n',
        ext_id="ai_act.combat_workbench",
    )


def test_ignores_private_context_keys() -> None:
    """扩展之间用 context 传数据的私有键不是「宿主缺键」。

    一个回调写 ``context["mch_mine_flag"] = true``、另一个读回来 ——
    那个键宿主本来就不该有，报成死代码是误报。
    """
    assert not _codes('func f():\n\tcontext["mch_mine_flag"] = true\n')
    assert not _codes('func f():\n\tif context.get("mch_mine_flag"): pass\n')


def test_ignores_host_injected_context_keys() -> None:
    """宿主注入的那几个键不该报。"""
    assert not _codes('func f():\n\tvar s = context.get("result")\n')
    assert not _codes('func f():\n\tvar s = context.get("skip_original", false)\n')


def test_ignores_unrelated_context_keys() -> None:
    """扩展约定的其它 context 键（``hud``/``game``）不该报。

    「谁写谁读」是跨文件的，静态判断不了 —— 报了就是误报。
    实测现网 floating_cannons / rose / treasure_dup 都命中这个。
    """
    assert not _codes('func f():\n\tif is_instance_valid(context.get("hud")): pass\n')


# ---------------------------------------------------------------- 钩子点撞车


def test_detects_hook_point_collision() -> None:
    """L0-8：两个扩展注册同一个钩子点。

    后果很实际：``hook_runtime`` 把同一个 context 字典传给同点所有回调，
    ``result`` 是**就地改写**的嵌套字典 → 后跑的覆盖先跑的。
    """
    hooks = {
        "rose.dimension_ruins": [
            {"script": "scripts/game.gd", "method": "character_attributes", "phase": "after"}
        ],
        "ai_act.combat_workbench": [
            {"script": "scripts/game.gd", "method": "character_attributes", "phase": "after"}
        ],
    }
    found = iso.detect_hook_collisions(hooks)
    assert len(found) == 2, found
    assert {f.code for f in found} == {"L0-8"}
    assert {f.ext_id for f in found} == {"rose.dimension_ruins", "ai_act.combat_workbench"}


def test_no_collision_for_distinct_points() -> None:
    """不同点不该报。"""
    hooks = {
        "a": [{"script": "s.gd", "method": "m1", "phase": "after"}],
        "b": [{"script": "s.gd", "method": "m2", "phase": "after"}],
    }
    assert not iso.detect_hook_collisions(hooks)


def test_no_collision_for_same_ext_twice() -> None:
    """同一个扩展注册两次不算撞车（扩展内部的事）。"""
    hooks = {
        "a": [
            {"script": "s.gd", "method": "m1", "phase": "after"},
            {"script": "s.gd", "method": "m1", "phase": "after"},
        ],
    }
    assert not iso.detect_hook_collisions(hooks)


# ---------------------------------------------------------------- peer 环


def test_detects_peer_cycle() -> None:
    """L0-12：互读成环。"""
    decls = {
        "a": {"state_read": [{"peer": "b"}]},
        "b": {"state_read": [{"peer": "a"}]},
    }
    found = iso.detect_peer_cycles(decls)
    assert found, "没检出互读环"
    assert {f.code for f in found} == {"L0-12"}


def test_no_cycle_for_acyclic() -> None:
    """单向依赖不是环。"""
    decls = {
        "a": {"state_read": [{"peer": "b"}]},
        "b": {"state_read": [{"peer": "c"}]},
        "c": {},
    }
    assert not iso.detect_peer_cycles(decls)


def test_detects_missing_peer() -> None:
    """L0-12：声明要访问的扩展没启用。"""
    decls = {"a": {"state_read": [{"peer": "b"}]}}
    found = iso.missing_peers(decls, ["a"])
    assert len(found) == 1
    assert found[0].peer == "b"


# ---------------------------------------------------------------- 声明核对


def test_reconcile_flags_undeclared_access() -> None:
    """L0-11：实际访问了但清单没声明 → 违规。"""
    findings = [
        iso.PeerFinding(
            code="L0-4", severity="block", ext_id="a",
            detail="读其它扩展的资源", peer="b",
        )
    ]
    out = iso.reconcile({}, findings)
    assert len(out) == 1
    assert out[0].code == "L0-11"


def test_reconcile_accepts_declared_access() -> None:
    """声明了就不算违规。"""
    findings = [
        iso.PeerFinding(
            code="L0-4", severity="block", ext_id="a",
            detail="读其它扩展的资源", peer="b",
        )
    ]
    out = iso.reconcile({"resources_read": [{"peer": "b", "path": "x"}]}, findings)
    assert not out


# ---------------------------------------------------------------- 补丁生成


def test_build_peer_access_patch() -> None:
    """扫描结果要能直接生成可粘贴的声明草稿。"""
    findings = [
        iso.PeerFinding(code="L0-4", severity="block", ext_id="a", detail="", peer="b"),
        iso.PeerFinding(code="L0-7", severity="warn", ext_id="a", detail='读 api.service("b.svc")', peer="b"),
    ]
    patch = iso.build_peer_access_patch(findings)
    assert patch.get("resources_read"), patch
    assert patch.get("services_read"), patch


def test_summarize() -> None:
    """汇总按级别与编号。"""
    findings = [
        iso.PeerFinding(code="L0-1", severity="block", ext_id="a", detail=""),
        iso.PeerFinding(code="L0-1", severity="block", ext_id="a", detail=""),
        iso.PeerFinding(code="L0-6", severity="warn", ext_id="a", detail=""),
    ]
    s = iso.summarize(findings)
    assert s["total"] == 3
    assert s["block"] == 2
    assert s["warn"] == 1
    assert s["by_code"] == {"L0-1": 2, "L0-6": 1}


# ---------------------------------------------------------------- 现网基线


def test_real_installed_extensions_have_no_cross_access() -> None:
    """现网扩展的跨扩展访问数必须是 0（防回归）。

    实测结论：9 个真实扩展、0 处跨扩展访问、0 处 block 级违规。
    这条断言把那个结论钉住 —— 将来任何扩展引入跨扩展访问，
    这条测试立刻红，作者必须走 ``peer_access`` 声明流程。

    找不到扩展源码时跳过（那取决于用户装了什么）。
    """
    import app.core.paths as paths

    src_root_base = paths.BUILDS
    if not src_root_base.is_dir():
        pytest.skip("没有 builds 目录（没装过扩展？）")

    checked = 0
    problems: list[str] = []
    for src in sorted(src_root_base.glob("_install_*_47/src")):
        ext_id = src.parent.name[len("_install_") : -len("_47")]
        files = iso.load_src_files(src)
        if not files:
            continue
        checked += 1
        for f in iso.scan_tree(files, ext_id):
            if f.severity == "block":
                problems.append(f"{ext_id}: {f.human()}")
    assert checked == 0 or not problems, (
        "现网扩展出现了 block 级跨扩展访问：\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------- 现网基线


def test_real_installed_extensions_have_no_cross_access() -> None:
    """现网扩展的跨扩展访问数必须是 0（防回归）。

    实测结论：9 个真实扩展、0 处跨扩展访问、0 处 block 级违规。
    这条断言把那个结论钉住 —— 将来任何扩展引入跨扩展访问，
    这条测试立刻红，作者必须走 ``peer_access`` 声明流程。

    找不到扩展源码时跳过（那取决于用户装了什么）。
    """
    import app.core.paths as paths

    src_root_base = paths.BUILDS
    if not src_root_base.is_dir():
        pytest.skip("没有 builds 目录（没装过扩展？）")

    checked = 0
    problems: list[str] = []
    for src in sorted(src_root_base.glob("_install_*_47/src")):
        ext_id = src.parent.name[len("_install_") : -len("_47")]
        files = iso.load_src_files(src)
        if not files:
            continue
        checked += 1
        for f in iso.scan_tree(files, ext_id):
            if f.severity == "block":
                problems.append(f"{ext_id}: {f.human()}")
    assert checked == 0 or not problems, (
        "现网扩展出现了 block 级跨扩展访问：\n  " + "\n  ".join(problems)
    )
