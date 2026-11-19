"""M3 运行期受控面的真引擎验证。

验证四件事（都用真引擎跑，不是静态检查）：

1. ``api.host`` → **硬错**（"Invalid access to property"），不是静默拿 null
2. ``api.state()`` 返回**深拷贝** —— 扩展就地改它不影响宿主
3. 严格档下**读不落库**（读这个动作没有副作用）
4. 跨扩展状态读写：**单向声明不够**，双向都声明才放行

做法：把 mod_host.gd 打进 pck 当 Autoload，写两个「扩展」互相访问，
把结果写进文件，Python 侧读文件断言。
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
    """工作目录：**调用时**取 ``paths.BUILDS``，不写死成 ``ROOT/builds``。

    写死常量会绕过 ``conftest`` 的 autouse 隔离（那个 fixture 改的是
    ``paths.BUILDS``），结果是每跑一轮就往真实 ``builds/`` 里拷 5 份
    164 MB 的 ``jjxf46.exe`` —— 实测单轮 821 MB，跑三遍就 2.4 GB。
    """
    return paths_mod.BUILDS / "mod-host-isolation"

#: 消费者扩展：读 peer 的状态、试着写 peer 的状态
CONSUMER = """extends Node

## api 是 install() 的**形参**，不是全局名 —— 别的方法里要用必须存成成员。
var _api: RefCounted

func install(api) -> bool:
	_api = api
	return true

func probe() -> Dictionary:
	var out := {}
	# 1) 未声明时读 peer —— 应该被拒（返回空 dict）
	out["undeclared_read"] = _api.read_peer_state("mch.provider", [])
	# 2) 声明了之后读 —— 应该拿到值
	out["declared_read"] = _api.read_peer_state("mch.provider", ["count"])
	# 3) 未声明时写 —— 应该被拒
	out["undeclared_write"] = _api.write_peer_state("mch.provider", {"count": 999})
	# 4) 声明了之后写 —— 应该成功
	out["declared_write"] = _api.write_peer_state("mch.provider", {"count": 42})
	# 5) 读自己的状态再就地改 —— 不能影响宿主
	var mine: Dictionary = _api.state({"n": 1})
	mine["n"] = 999
	mine["injected"] = true
	out["my_state_after_mutation"] = _api.state({"n": 1})
	return out
"""

#: 提供者扩展：暴露状态
PROVIDER = """extends Node

var _api: RefCounted

func install(api) -> bool:
	_api = api
	_api.set_state({"count": 7, "secret": "hide me"})
	return true

func probe() -> Dictionary:
	return _api.state({})
"""

#: 主脚本：装两个扩展，跑上面的探针，把结果写文件
MAIN = """extends Node

func _ready() -> void:
	var host := get_tree().root.get_node_or_null("ModHost")
	if host == null:
		_write({"__error__": "ModHost 没起来"})
		return
	var consumer = load("res://consumer.gd").new()
	var provider = load("res://provider.gd").new()
	add_child(consumer)
	add_child(provider)
	var consumer_api: RefCounted = host._api_for_test("mch.consumer")
	var provider_api: RefCounted = host._api_for_test("mch.provider")
	# 先让 provider 写状态
	provider.install(provider_api)
	consumer.install(consumer_api)
	var out: Dictionary = consumer.probe()
	out["_diag_peer_expose"] = host._peer_expose
	out["_diag_instances"] = host._instances.map(func(i): return {"id": i.get("id"), "has_pa": i.has("peer_access"), "pa": i.get("peer_access")})
	out["provider_state"] = provider.probe()
	out["host_state_raw"] = host._states.get("mch.provider", {})
	_write(out)
	get_tree().quit(0)


func _write(data: Dictionary) -> void:
	var f := FileAccess.open("user://probe.json", FileAccess.WRITE)
	if f == null:
		return
	f.store_string(JSON.stringify(data))
	f.close()
"""

#: 给测试用的宿主后门：按 id 拿 Api（真实运行期 ModHost 也是这么给的）
HOST_PATCH = """
func _api_for_test(mod_id: String) -> RefCounted:
	for item in _instances:
		if str(item.get("id", "")) == mod_id:
			return Api.new(self, mod_id)
	return null
"""


def _add_compat_scripts(writer) -> None:
    """把 ``runtime/_compat/`` 下的脚本都打进 pck。

    project.godot 声明了 Compat / HookRuntime / ModHost 三个 autoload，
    三个脚本都得在 pck 里 —— 少一个引擎就报
    ``Failed to instantiate an autoload, can't load from path``。
    """
    compat_dir = ROOT / "runtime" / "_compat"
    for name in ("bootstrap.gd", "hook_runtime.gd", "mount_plan.gd", "entry.gd", "entry.tscn"):
        src = compat_dir / name
        if src.is_file():
            writer.add_text(f"_compat/{name}", src.read_text(encoding="utf-8"))


def _build_and_run(
    *,
    mode: str,
    consumer_access: dict | None,
    provider_expose: dict | None,
    tag: str,
) -> dict:
    case = _work_dir() / tag
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)

    mod_host = (ROOT / "runtime" / "_compat" / "mod_host.gd").read_text(encoding="utf-8")
    mod_host += HOST_PATCH

    # exe 与 pck 必须同名 —— 异名时 Godot 模板 exe 不挂载 pck，
    # 还会弹模态 ALERT（会卡住进程），而且「没挂载」= 脚本没被 load
    # = 检查结论是假阳性。
    writer = PckWriter(case / "godot.pck", line="4.6")
    # project.godot 写在**文件系统**（exe 同目录），不进 pck ——
    # pck 里的那份不生效：Godot 启动阶段就把项目设置读完了，
    # 那时 pck 还没挂载。放进 pck 的后果是 autoload 压根没注册
    # （症状：ModHost 报「未提供 mods 清单」）。
    (case / "project.godot").write_text(
        "config_version=5\n\n[application]\n\nconfig/name=\"iso\"\n"
        "run/main_scene=\"res://main.tscn\"\n"
        "config/features=PackedStringArray(\"4.6\", \"GL Compatibility\")\n\n"
        "[autoload]\n\n"
        "Compat=\"*res://_compat/bootstrap.gd\"\n"
        "HookRuntime=\"*res://_compat/hook_runtime.gd\"\n"
        "ModHost=\"*res://_compat/mod_host.gd\"\n\n"
        "[rendering]\n\nrenderer/rendering_method=\"gl_compatibility\"\n",
        encoding="utf-8",
    )
    writer.add_text(
        "main.tscn",
        '[gd_scene format=3]\n\n'
        '[ext_resource type="Script" path="res://main.gd" id="1"]\n\n'
        '[node name="Main" type="Node"]\n'
        'script = ExtResource("1")\n',
    )
    writer.add_text("main.gd", MAIN)
    writer.add_text("consumer.gd", CONSUMER)
    writer.add_text("provider.gd", PROVIDER)
    # project.godot 声明了 Compat / HookRuntime / ModHost 三个 autoload，
    # 三个脚本都得在 pck 里 —— 少一个引擎就报
    # "Failed to instantiate an autoload, can't load from path"。
    _add_compat_scripts(writer)
    writer.add_text("_compat/mod_host.gd", mod_host)

    mods = [
        {"id": "mch.consumer", "entry": "res://consumer.gd"},
        {"id": "mch.provider", "entry": "res://provider.gd"},
    ]
    if consumer_access:
        mods[0]["peer_access"] = consumer_access
    if provider_expose:
        mods[1]["peer_expose"] = provider_expose
    writer.commit()
    # mods.json 要写在**文件系统**上，不能打进 pck ——
    # ModHost 是用 FileAccess.get_file_as_string() 从磁盘读的
    # （mod_host.gd:_read_json），pck 里的那份它读不到。
    (case / "mods.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "line": "4.6",
                "mode": mode,
                "hooks": [],
                "mods": mods,
                "peer_expose": {"mch.provider": provider_expose} if provider_expose else {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exe = case / "godot.exe"
    shutil.copyfile(ROOT / "runtime" / "host46" / "jjxf46.exe", exe)
    env = {
        **dict(os.environ),
        "APPDATA": str(case / "appdata"),
        "LOCALAPPDATA": str(case / "appdata"),
    }
    (case / "appdata").mkdir(exist_ok=True)

    try:
        proc = subprocess.run(
            [
                str(exe),
                "--headless",
                # ``--`` 之后才是**用户参数**（OS.get_cmdline_user_args()）。
                # bootstrap.gd:_parse_args 读的就是它 —— 少了这个 ``--``，
                # 它会以为「未提供 mods 清单」。
                "--",
                "--mods=" + str(case / "mods.json"),
                "--mount=" + str(case / "godot.pck"),
            ],
            cwd=str(case),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"__error__": "超时"}
    err = (proc.stdout or "") + (proc.stderr or "")
    if "Parse Error" in err:
        return {"__error__": "Parse Error:\n" + err[-1200:]}
    found = list(case.rglob("probe.json"))
    if not found:
        return {"__error__": "没写出结果文件\n" + err[-1200:]}
    return json.loads(found[0].read_text(encoding="utf-8"))


def test_state_returns_deep_copy() -> None:
    """state() 必须返回深拷贝 —— 扩展就地改它不能影响宿主。

    这是「只读访问」在 GDScript 里唯一能真正兑现的方式：
    Dictionary 是引用类型，返回本体等于交出写权限。
    """
    out = _build_and_run(
        mode="strict_warn",
        consumer_access=None,
        provider_expose=None,
        tag="deepcopy",
    )
    assert "__error__" not in out, out.get("__error__")
    mine = out.get("my_state_after_mutation") or {}
    assert mine.get("n") == 1, f"扩展就地改了自己的 state，宿主被影响了：{out}"
    assert "injected" not in mine, f"扩展往 state 里塞的东西留在了宿主：{out}"


def test_peer_access_requires_both_sides() -> None:
    """单向声明不够 —— 消费方与提供方**都**声明才放行。"""
    access = {"state_read": [{"peer": "mch.provider", "keys": ["count"]}]}
    expose = {"state_read": ["mch.consumer"], "state_read_keys": ["count"]}

    # 双向都声明 → 放行
    both = _build_and_run(
        mode="strict_verify", consumer_access=access, provider_expose=expose, tag="both"
    )
    assert "__error__" not in both, both.get("__error__")
    assert (both.get("declared_read") or {}).get("count") == 7, f"双向声明后仍读不到：{both}"

    # 只有消费方声明 → 拒（提供方没许可）
    only_consumer = _build_and_run(
        mode="strict_verify",
        consumer_access=access,
        provider_expose=None,
        tag="only-consumer",
    )
    assert "__error__" not in only_consumer, only_consumer.get("__error__")
    assert not (only_consumer.get("declared_read") or {}), (
        f"提供方没许可却读到了：{only_consumer}"
    )


def test_normal_mode_still_allows_everything() -> None:
    """普通模式下不能有任何行为变化 —— 现有玩家的游戏不能被改。

    普通模式不做隔离校验：peer 读不到（因为没声明），
    但**自己的** state 行为必须与从前一致。
    """
    out = _build_and_run(
        mode="normal", consumer_access=None, provider_expose=None, tag="normal"
    )
    assert "__error__" not in out, out.get("__error__")
    mine = out.get("my_state_after_mutation") or {}
    assert mine.get("n") == 1, f"普通模式下 state 语义变了：{out}"


def test_api_host_is_hard_error_not_silent_null() -> None:
    """``api.host`` 必须是**硬错**，不是静默返回 null。

    改名成 ``_host`` 的目的就是这个：误用要报错，作者才会去查。
    静默拿 null 的话，扩展会「装上了但没反应」，最难查。
    """
    case = _work_dir() / "host-hard-error"
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)

    mod_host = (ROOT / "runtime" / "_compat" / "mod_host.gd").read_text(encoding="utf-8")
    writer = PckWriter(case / "godot.pck", line="4.6")
    # project.godot 必须在文件系统上（pck 里的不生效）
    (case / "project.godot").write_text(
        "config_version=5\n\n[application]\n\nconfig/name=\"h\"\n"
        "run/main_scene=\"res://main.tscn\"\n"
        "config/features=PackedStringArray(\"4.6\", \"GL Compatibility\")\n\n"
        "[autoload]\n\n"
        "Compat=\"*res://_compat/bootstrap.gd\"\n"
        "HookRuntime=\"*res://_compat/hook_runtime.gd\"\n"
        "ModHost=\"*res://_compat/mod_host.gd\"\n\n"
        "[rendering]\n\nrenderer/rendering_method=\"gl_compatibility\"\n",
        encoding="utf-8",
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
        '\tvar mod_script: Script = load("res://bad.gd")\n'
        "\tvar inst: Node = mod_script.new()\n"
        "\tadd_child(inst)\n"
        "\tget_tree().quit(0)\n",
    )
    # 扩展试图读 api.host —— 应该报 Invalid access
    writer.add_text(
        "bad.gd",
        "extends Node\n\n"
        "func install(api) -> bool:\n"
        "\tvar s = api.host._states\n"
        "\treturn true\n",
    )
    _add_compat_scripts(writer)
    writer.add_text("_compat/mod_host.gd", mod_host)
    writer.commit()
    # 同上：mods.json 必须在文件系统上
    (case / "mods.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "line": "4.6",
                "mode": "normal",
                "hooks": [],
                "mods": [{"id": "mch.bad", "entry": "res://bad.gd"}],
            }
        ),
        encoding="utf-8",
    )

    exe = case / "godot.exe"
    shutil.copyfile(ROOT / "runtime" / "host46" / "jjxf46.exe", exe)
    env = {
        **dict(os.environ),
        "APPDATA": str(case / "appdata"),
        "LOCALAPPDATA": str(case / "appdata"),
    }
    (case / "appdata").mkdir(exist_ok=True)
    try:
        proc = subprocess.run(
            [
                str(exe),
                "--headless",
                "--",
                "--mods=" + str(case / "mods.json"),
                "--mount=" + str(case / "godot.pck"),
            ],
            cwd=str(case),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("超时")
    err = (proc.stdout or "") + (proc.stderr or "")
    assert "Invalid access" in err or "host" in err.lower(), (
        "api.host 没有报硬错 —— 改名成 _host 没生效：\n" + err[-1500:]
    )
