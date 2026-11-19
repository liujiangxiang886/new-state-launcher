"""hook_keys 的判定口径测试：把 2026-11-05 实测确认的结论固化下来。

这些用例的期望值来自现网真实包的实际代码，不是凭空构造的：
- better_experience 改 2005..2017，rose 改 2019/2020 → 安全
- better 改 arguments[0]，treasure_dup 设 skip_original → 真冲突
"""

from __future__ import annotations

from pathlib import Path

from app.core.ext import hook_keys


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


BETTER = """
const ATTACK_KEYS := ["2007", "2009"]
const DEFENSE_KEYS := ["2008", "2010"]

func _hook_attributes(context: Dictionary) -> void:
\tvar value: Variant = context.get("result")
\tif value is Dictionary:
\t\tcontext["result"] = _apply(value)

func _apply(attributes: Dictionary) -> Dictionary:
\tvar result: Dictionary = attributes.duplicate(true)
\t_scale_key(result, "2005", 2.0, 1)
\t_scale_key(result, "2006", 2.0, 1)
\t_scale_key(result, "2017", 1.5, 1)
\tfor key in ATTACK_KEYS:
\t\tresult[key] = int(result[key]) * 2
\tfor key in DEFENSE_KEYS:
\t\tresult[key] = int(result[key]) * 2
\treturn result

func _hook_monster_damage(context: Dictionary) -> void:
\tvar arguments: Array = context.get("arguments", [])
\tif arguments.is_empty():
\t\treturn
\targuments[0] = int(arguments[0]) * 2

func _ready() -> void:
\tapi.add_method_hook("scripts/game.gd", "character_attributes", "after", _hook_attributes, 10)
\tapi.add_method_hook("scripts/slice_monster.gd", "damage", "before", _hook_monster_damage, 10)
"""

ROSE = """
func _ready() -> void:
\tapi.add_method_hook("scripts/game.gd", "character_attributes", "after", _after_attributes, 10)

func _after_attributes(context: Dictionary) -> void:
\tvar result: Variant = context.get("result")
\tif not (result is Dictionary):
\t\treturn
\tvar attrs: Dictionary = result as Dictionary
\tattrs["2019"] = 1
\tattrs["2020"] = 1
\tcontext["result"] = attrs
"""

TREASURE = """
func _ready() -> void:
\tapi.add_method_hook("scripts/slice_monster.gd", "damage", "before", _before_damage, 50)

func _before_damage(context: Dictionary) -> void:
\tvar monster: Variant = context.get("instance")
\tif monster == null:
\t\treturn
\t_handle(monster, context)

func _handle(monster: Variant, context: Dictionary) -> void:
\tcontext["result"] = null
\tcontext["skip_original"] = true
"""


def test_resolve_callback_from_add_hook():
    """回调要从 add_method_hook 反查，不能指望函数和原版方法同名。"""
    cbs = hook_keys.resolve_callbacks(BETTER, "scripts/game.gd", "character_attributes", "after")
    assert cbs == ["_hook_attributes"]


def test_resolve_callback_ignores_other_phase():
    """相位不同不算同一个点。"""
    cbs = hook_keys.resolve_callbacks(BETTER, "scripts/game.gd", "character_attributes", "before")
    assert cbs == []


def test_better_writes_expected_keys():
    info = hook_keys.extract_writes_deep(
        {Path("main.gd"): BETTER},
        "jjxf.better_experience",
        "scripts/game.gd",
        "character_attributes",
        "after",
        "_hook_attributes",
    )
    assert {"2005", "2006", "2017", "2007", "2009", "2008", "2010"} <= info.keys
    assert not info.whole, "读—改—写回不是整份替换"


def test_rose_writes_only_guard_keys():
    info = hook_keys.extract_writes_deep(
        {Path("main.gd"): ROSE},
        "rose.dimension_ruins",
        "scripts/game.gd",
        "character_attributes",
        "after",
        "_after_attributes",
    )
    assert info.keys == {"2019", "2020"}


def test_better_and_rose_are_safe():
    """实测结论：这两者键不重叠，可安全共存。"""
    texts_a = {Path("a.gd"): BETTER}
    texts_b = {Path("b.gd"): ROSE}
    a = hook_keys.extract_writes_deep(
        texts_a, "jjxf.better_experience",
        "scripts/game.gd", "character_attributes", "after", "_hook_attributes",
    )
    b = hook_keys.extract_writes_deep(
        texts_b, "rose.dimension_ruins",
        "scripts/game.gd", "character_attributes", "after", "_after_attributes",
    )
    level, message = hook_keys.compare([a, b], "after")
    assert level == "safe", message


def test_skip_original_detected_through_delegate():
    """skip_original 藏在第二级函数里，必须追到。"""
    info = hook_keys.extract_writes_deep(
        {Path("main.gd"): TREASURE},
        "mch.treasure_dup",
        "scripts/slice_monster.gd",
        "damage",
        "before",
        "_before_damage",
    )
    assert info.skips_original is True


def test_skip_original_makes_it_a_clash():
    """会跳过原函数的包，和同点其它包必然冲突（后者被吞掉）。"""
    a = hook_keys.extract_writes_deep(
        {Path("a.gd"): BETTER},
        "jjxf.better_experience",
        "scripts/slice_monster.gd", "damage", "before", "_hook_monster_damage",
    )
    b = hook_keys.extract_writes_deep(
        {Path("b.gd"): TREASURE},
        "mch.treasure_dup",
        "scripts/slice_monster.gd", "damage", "before", "_before_damage",
    )
    level, message = hook_keys.compare([a, b], "before")
    assert level == "clash"
    assert "skip_original" in message


def test_whole_replacement_is_clash():
    """赋空字典 = 整份替换，任何键都可能被清掉。"""
    src = """
func _after(context: Dictionary) -> void:
\tcontext["result"] = {}
"""
    info = hook_keys.extract_writes_deep(
        {Path("x.gd"): src}, "x", "s.gd", "m", "after", "_after"
    )
    assert info.whole is True


def test_unparsed_writes_are_unknown_not_safe():
    """解析不出写入时必须报「需人工确认」，不能假装安全。"""
    src = """
func _after(context: Dictionary) -> void:
\tvar helper = load("res://other.gd").new()
\thelper.do_something(context)
"""
    info = hook_keys.extract_writes_deep(
        {Path("x.gd"): src}, "x", "s.gd", "m", "after", "_after"
    )
    other = hook_keys.WriteInfo(
        ext_id="y", script="s.gd", method="m", phase="after",
        keys={"2005"}, empty=False,
    )
    level, _message = hook_keys.compare([info, other], "after")
    assert level == "unknown"


def test_analyze_point_finds_callback_in_same_package(tmp_path):
    """回调可以拆到别的模块文件，analyze_point 要能追过去。

    用 pytest 的 ``tmp_path``：本机有删除保护，测试里自己 rmdir 会撞上
    SHFileOperationW 失败，交给 pytest 清理才稳。
    """
    (tmp_path / "main.gd").write_text(ROSE, encoding="utf-8")
    analysis = hook_keys.analyze_point(
        {"rose.dimension_ruins": tmp_path},
        "scripts/game.gd",
        "character_attributes",
        "after",
        ["rose.dimension_ruins"],
    )
    assert analysis.infos[0].keys == {"2019", "2020"}

# ---------------------------------------------------------------- 静态调用跟随
#
# 2026-11-13 实测：author.pet_guard 的钩子只有一行
# `Core.apply_to_live(result)`，写入在另一个文件、而且是 `static func`。
# 旧实现两个都认不出（函数头正则不认 static、跨文件只按名字瞎撞），
# 于是这条共享钩子点永远报「写入没解析出来」。

GUARD_MAIN = """
const Core = preload("guard_core.gd")

func _on_attributes(context: Dictionary) -> void:
\tvar result = context.get("result")
\tif not (result is Dictionary):
\t\treturn
\tCore.apply_to_live(result)

func _ready() -> void:
\tapi.add_method_hook("scripts/game.gd", "character_attributes", "after", _on_attributes, 10)
"""

GUARD_CORE = """
const SLOTS: Array = [
\t{"key": "hp", "attr": "2005"},
\t{"key": "crit", "attr": "2013"},
]

static var _term_attrs: PackedStringArray = PackedStringArray()

static func apply_to_live(attributes: Dictionary) -> void:
\tif _term_attrs.is_empty():
\t\t_build()
\tfor index in _term_attrs.size():
\t\tvar attr: String = _term_attrs[index]
\t\tvar base: float = float(attributes.get(attr, 0))
\t\tattributes[attr] = int(round(base * 1.5))

static func _build() -> void:
\t_term_attrs = PackedStringArray()
\tfor index in SLOTS.size():
\t\tvar attr: String = SLOTS[index].attr
\t\t_term_attrs.append(attr)
"""


def test_static_func_is_recognized():
    """`static func` 必须能被切成函数体 —— 宠物守护整文件都是这种写法。"""
    bodies = hook_keys._function_bodies(GUARD_CORE)
    assert "apply_to_live" in bodies
    assert "_build" in bodies


def test_follow_static_call_across_preloaded_script():
    """跟随 preload + 静态调用，并把常量表里的键展开出来。"""
    files = {Path("main.gd"): GUARD_MAIN, Path("guard_core.gd"): GUARD_CORE}
    info = hook_keys.extract_writes_deep(
        files, "author.pet_guard", "scripts/game.gd", "character_attributes", "after",
        "_on_attributes",
    )
    assert info.keys == {"2005", "2013"}, info.keys
    assert not info.empty
    assert info.rmw_keys == {"2005", "2013"}, "读—改—写应被识别成叠加"


def test_rmw_on_shared_key_is_composable_not_clash():
    """两边都读—改—写同一个键：叠加，不是冲突。"""
    src_a = """
func _hook(context: Dictionary) -> void:
\tvar result = context.get("result")
\tresult["2005"] = int(round(float(result.get("2005", 0)) * 2.0))
"""
    src_b = """
func _hook(context: Dictionary) -> void:
\tvar result = context.get("result")
\tvar base: float = float(result.get("2005", 0))
\tresult["2005"] = int(round(base * 1.5))
"""
    a = hook_keys.extract_writes_deep({Path("a.gd"): src_a}, "a", "s.gd", "m", "after", "_hook")
    b = hook_keys.extract_writes_deep({Path("b.gd"): src_b}, "b", "s.gd", "m", "after", "_hook")
    level, message = hook_keys.compare([a, b], "after")
    assert level == "safe", message
    assert "读—改—写" in message


def test_conditional_overwrite_is_clash_with_condition():
    """直接赋值会盖掉别人的加成；消息里要写清触发条件。"""
    src_a = """
func _hook(context: Dictionary) -> void:
\tvar result = context.get("result")
\tvar base: float = float(result.get("2013", 0))
\tresult["2013"] = int(round(base * 1.5))
"""
    src_b = """
func _hook(context: Dictionary) -> void:
\tvar result = context.get("result")
\tvar player: Dictionary = config.get("player", {})
\tif bool(player.get("force_crit", false)):
\t\tresult["2013"] = int(round(float(player.get("crit_rate", 50.0)) * 100.0))
"""
    a = hook_keys.extract_writes_deep({Path("a.gd"): src_a}, "a", "s.gd", "m", "after", "_hook")
    b = hook_keys.extract_writes_deep({Path("b.gd"): src_b}, "b", "s.gd", "m", "after", "_hook")
    level, message = hook_keys.compare([a, b], "after")
    assert level == "clash"
    assert "2013" in message and "force_crit" in message


def test_guard_and_better_compose(tmp_path):
    """宠物守护 vs 更好的体验：键重叠但都是读—改—写 —— 不该报 unknown。"""
    (tmp_path / "main.gd").write_text(GUARD_MAIN, encoding="utf-8")
    (tmp_path / "guard_core.gd").write_text(GUARD_CORE, encoding="utf-8")
    better = """
func _hook_attributes(context: Dictionary) -> void:
\tvar value: Variant = context.get("result")
\tif value is Dictionary:
\t\tcontext["result"] = _apply(value)

func _apply(attributes: Dictionary) -> Dictionary:
\tvar result: Dictionary = attributes.duplicate(true)
\t_scale_key(result, "2005", 2.0, 1)
\t_scale_key(result, "2013", 2.0, 1)
\treturn result

func _scale_key(target: Dictionary, key: String, mult: float, floor_value: int) -> void:
\ttarget[key] = maxi(floor_value, int(round(float(target.get(key, 0)) * mult)))

func _ready() -> void:
\tapi.add_method_hook("scripts/game.gd", "character_attributes", "after", _hook_attributes, 10)
"""
    pkg = tmp_path / "better"
    pkg.mkdir()
    (pkg / "main.gd").write_text(better, encoding="utf-8")
    analysis = hook_keys.analyze_point(
        {"author.pet_guard": tmp_path, "jjxf.better_experience": pkg},
        "scripts/game.gd", "character_attributes", "after",
        ["author.pet_guard", "jjxf.better_experience"],
    )
    assert analysis.level == "safe", analysis.message
    by_id = {i.ext_id: i for i in analysis.infos}
    assert by_id["author.pet_guard"].keys == {"2005", "2013"}


# ---------------------------------------------------------------- 顺序与副作用
#
# 2026-11-17：28 个包全量导入后，体检面板报的 4 条里有 3 条是判定过粗：
#   · 只建节点 / 清冷却的回调被判成「写入没解析出来」（2 条）
#   · 「先改入参、后 skip 且 skip 会读入参」被判成真冲突（1 条）
# 这些用例把新口径钉死。

SIDE_EFFECT = """
func _world_ready(context: Dictionary) -> void:
\tvar world = context.instance
\tif world == null:
\t\treturn
\tvar node := Node.new()
\tnode.name = "AiTeammateStatus"
\tworld.add_child(node)
"""

CONTEXT_HANDOFF = """
func _after(context: Dictionary) -> void:
\tvar helper = load("res://other.gd").new()
\thelper.do_something(context)
"""

TABLE_MOD = """
var api

func install(context) -> bool:
\tapi = context
\tvar hooks: Array = [
\t\t["scripts/game_world.gd", "_ready", "after", world_ready],
\t\t["scripts/slice_run.gd", "tick", "before", battle_tick]
\t]
\tfor hook in hooks:
\t\tif not api.add_method_hook(hook[0], hook[1], hook[2], hook[3], 50):
\t\t\treturn false
\treturn true

func world_ready(context: Dictionary) -> void:
\tvar world = context.instance
\tworld.add_child(Node.new())

func battle_tick(context: Dictionary) -> void:
\tpass
"""


def test_pure_side_effect_is_safe_not_unknown():
    """只建节点的回调不可能改返回值，同点另一个同样如此 —— 不该报需确认。"""
    a = hook_keys.extract_writes_deep(
        {Path("a.gd"): SIDE_EFFECT},
        "mch.ai_teammate_humanize",
        "scripts/game_world.gd", "_ready", "after", "_world_ready",
    )
    b = hook_keys.extract_writes_deep(
        {Path("b.gd"): SIDE_EFFECT},
        "jjxf.floating_cannons",
        "scripts/game_world.gd", "_ready", "after", "_world_ready",
    )
    assert a.empty and a.opaque is False, "应认出是纯副作用"
    level, message = hook_keys.compare([a, b], "after")
    assert level == "safe", message


def test_context_handoff_stays_unknown():
    """把整个 context 转交给认不出的函数 —— 仍然证明不了，必须保持需确认。"""
    info = hook_keys.extract_writes_deep(
        {Path("x.gd"): CONTEXT_HANDOFF}, "x", "s.gd", "m", "after", "_after"
    )
    assert info.opaque is True
    other = hook_keys.WriteInfo(
        ext_id="y", script="s.gd", method="m", phase="after",
        keys={"2005"}, empty=False, opaque=False,
    )
    level, _message = hook_keys.compare([info, other], "after")
    assert level == "unknown"


def test_table_driven_registration_is_resolved(tmp_path):
    """`for hook in hooks` 这种注册要能反查到回调，否则整包永远判需确认。"""
    pkg = tmp_path / "fc"
    pkg.mkdir()
    (pkg / "main.gd").write_text(TABLE_MOD, encoding="utf-8")
    analysis = hook_keys.analyze_point(
        {"jjxf.floating_cannons": pkg},
        "scripts/game_world.gd", "_ready", "after",
        ["jjxf.floating_cannons"],
    )
    info = analysis.infos[0]
    assert info.opaque is False, "表驱动注册的 world_ready 应被认出是纯副作用"
    assert analysis.level == "safe", analysis.message


def test_scan_reads_priority_from_table():
    """表驱动注册的优先级（第五个参数）要能读出来，顺序判定靠它。"""
    from app.core.ext import hook_scan

    sites = hook_scan.scan_source(TABLE_MOD).sites
    assert len(sites) == 2
    assert all(s.priority == 50 for s in sites), [s.priority for s in sites]


DAMAGE_WRITER = """
func _before(context: Dictionary) -> void:
\tvar arguments: Array = context.get("arguments", [])
\targuments[0] = int(arguments[0]) * 2
"""

DAMAGE_SKIPPER = """
func _before(context: Dictionary) -> void:
\tvar arguments: Array = context.get("arguments", [])
\tvar dmg: int = int(arguments[0])
\tmonster.hp = maxi(0, int(monster.hp) - dmg)
\tcontext["skip_original"] = true
"""


def _damage_pair():
    a = hook_keys.extract_writes_deep(
        {Path("a.gd"): DAMAGE_WRITER},
        "jjxf.better_experience",
        "scripts/slice_monster.gd", "damage", "before", "_before",
    )
    b = hook_keys.extract_writes_deep(
        {Path("b.gd"): DAMAGE_SKIPPER},
        "mch.treasure_dup",
        "scripts/slice_monster.gd", "damage", "before", "_before",
    )
    return a, b


def test_arg_writer_before_skipper_is_safe():
    """10 先改伤害、20 后 skip 并自己读入参扣血 —— 加成不会丢，判安全。"""
    a, b = _damage_pair()
    level, message = hook_keys.compare(
        [a, b], "before",
        {"jjxf.better_experience": 10, "mch.treasure_dup": 20},
    )
    assert level == "safe", message
    assert "skip" in message


def test_arg_writer_after_skipper_is_clash():
    """顺序反过来就是真问题：先 skip，后面改入参的回调根本不执行。"""
    a, b = _damage_pair()
    level, _message = hook_keys.compare(
        [a, b], "before",
        {"jjxf.better_experience": 30, "mch.treasure_dup": 20},
    )
    assert level == "clash"


def test_skip_without_priority_stays_conservative():
    """拿不到优先级就不许猜，仍按冲突报。"""
    a, b = _damage_pair()
    level, _message = hook_keys.compare([a, b], "before")
    assert level == "clash"


REJECT_A = """
func _before(context: Dictionary) -> void:
\tif not in_dup():
\t\treturn
\tcontext.result = false
\tcontext.skip_original = true
"""

REJECT_B = """
func _before(context: Dictionary) -> void:
\tcontext["result"] = false
\tcontext["skip_original"] = true
"""


def test_all_reject_style_skips_are_safe():
    """两边都是「拒绝该技能」，谁先触发结果一样，只差提示文案。"""
    a = hook_keys.extract_writes_deep(
        {Path("a.gd"): REJECT_A}, "mch.treasure_dup",
        "scripts/slice_run.gd", "request_skill", "before", "_before",
    )
    b = hook_keys.extract_writes_deep(
        {Path("b.gd"): REJECT_B}, "jjxf.floating_cannons",
        "scripts/slice_run.gd", "request_skill", "before", "_before",
    )
    assert a.rejects and b.rejects
    level, message = hook_keys.compare([a, b], "before", {"mch.treasure_dup": 0, "jjxf.floating_cannons": 50})
    assert level == "safe", message
    assert "只差提示文案" in message


def test_priority_flows_into_compare_without_dict(tmp_path):
    """安装期撞车检测只传 infos，优先级必须能从源码一路带到 WriteInfo。

    否则会出现「体检面板说安全、装包时说撞车」的自相矛盾。
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "main.gd").write_text(
        DAMAGE_WRITER
        + '\nfunc _ready() -> void:\n'
        '\tapi.add_method_hook("scripts/slice_monster.gd", "damage", "before", _before, 10)\n',
        encoding="utf-8",
    )
    (b / "main.gd").write_text(
        DAMAGE_SKIPPER
        + '\nfunc _ready() -> void:\n'
        '\tapi.add_method_hook("scripts/slice_monster.gd", "damage", "before", _before, 20)\n',
        encoding="utf-8",
    )
    analysis = hook_keys.analyze_point(
        {"a": a, "b": b},
        "scripts/slice_monster.gd", "damage", "before",
        ["a", "b"],
    )
    assert {i.ext_id: i.priority for i in analysis.infos} == {"a": 10, "b": 20}
    assert analysis.level == "safe", analysis.message
    # 不给优先级字典（isolation.detect_hook_collisions 就是这么调的）
    level, message = hook_keys.compare(analysis.infos, "before")
    assert level == "safe", message
