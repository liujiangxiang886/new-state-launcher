"""整体校验入口（原 tools/verify_mods.py）的可测试性回归。

背景：这个功能过去只在 ``tools/verify_mods.py`` 里有一份实现，界面用
``importlib`` 按路径加载。开发态能跑，打包成 onedir exe 后必崩——
``paths.project_root()`` 指向 exe 所在目录，而 ``tools/`` 从不随发布
复制，于是用户点「整体校验」拿到的是::

    FileNotFoundError: 找不到 ...\\tools\\verify_mods.py

这批用例守两件事：

1. 逻辑已在 ``app.core.ext.verify``，界面不再依赖 ``tools/`` 目录；
2. ``tools/verify_mods.py`` 只是薄包装，命令行行为（含 ``--json`` 与
   退出码）保持不变，避免「命令行能跑、界面不能跑」再次分叉。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_verify_line_lives_in_core_not_tools() -> None:
    """核心模块必须能被导入，且不依赖 tools/ 目录存在。"""
    from app.core.ext import verify as verify_mod

    assert callable(verify_mod.verify_line)
    assert Path(verify_mod.__file__).is_relative_to(ROOT / "app" / "core")


def test_ui_does_not_load_tools_by_path() -> None:
    """界面不能再按路径加载 tools/verify_mods.py。

    这是打包后失效的直接原因：源码里一旦出现用 ``paths.TOOLS`` 拼
    ``verify_mods.py`` 再 ``importlib`` 加载的写法，发布版就又会崩。
    用源码文本断言防回退——注释里提到文件名是允许的（那是说明来历），
    真正要禁的是 **拼路径 + 按路径加载** 这两个动作。
    """
    source = (ROOT / "app" / "ui" / "pyside" / "pages" / "extensions.py").read_text(
        encoding="utf-8"
    )
    assert 'paths.TOOLS / "verify_mods.py"' not in source
    assert "spec_from_file_location" not in source
    # 走核心模块
    assert "from ....core.ext.verify import verify_line" in source


def test_format_report_covers_all_outcome_kinds() -> None:
    """format_report 要把每类结果都写出来，不能只报个「未通过」。"""
    from app.core.ext.verify import format_report

    data = {
        "line": "4.6",
        "enabled": ["a", "b", "c"],
        "packs": ["a.pck"],
        "expected": ["a", "b"],
        "loaded": ["b"],
        "missing": ["a"],
        "unexpected": ["zzz"],
        "failed": [{"id": "c", "error": "入口不存在"}],
        "problems": ["冒烟未通过：退出码 1"],
        "ok": False,
    }
    text = "\n".join(format_report(data))
    assert "未装载：a" in text
    assert "未声明却被装载：zzz" in text
    assert "装载失败：c — 入口不存在" in text
    assert "退出码 1" in text


def test_format_report_hides_problems_when_ok() -> None:
    """通过时不输出 problems，避免界面刷一堆红字让人以为失败了。"""
    from app.core.ext.verify import format_report

    data = {
        "line": "4.6",
        "enabled": ["a"],
        "packs": [],
        "expected": ["a"],
        "loaded": ["a"],
        "missing": [],
        "unexpected": [],
        "failed": [],
        "problems": ["这条不该出现"],
        "ok": True,
    }
    text = "\n".join(format_report(data))
    assert "这条不该出现" not in text


def test_verify_line_shape_without_mods(monkeypatch) -> None:
    """没有扩展时也必须返回完整字段，不能让界面 KeyError。

    ``enabled_mods`` 返回空 → 走裸基线冒烟分支。这里把冒烟替成假结果，
    只验返回结构（真跑要 20 秒，不适合进单测）。
    """
    from app.core.ext import verify as verify_mod

    class _Probe:
        ok = True
        run_dir = "builds/_fake"
        mods_loaded = []
        nodes = 5
        reasons = []

        def summary(self) -> str:
            return "冒烟通过（5 节点，0 秒）"

    monkeypatch.setattr(verify_mod.enable_mod, "enabled_mods", lambda cfg, line: [])
    monkeypatch.setattr(verify_mod.smoke_mod, "run", lambda *a, **k: _Probe())

    out = verify_mod.verify_line({}, "4.6", 1.0)
    for key in (
        "line",
        "enabled",
        "expected",
        "loaded",
        "missing",
        "unexpected",
        "failed",
        "warnings",
        "problems",
        "ok",
        "reason",
        "seconds",
        "run_dir",
        "packs",
        "nodes",
    ):
        assert key in out, f"缺少字段 {key}"
    assert out["ok"] is True
    assert out["nodes"] == 5


def test_tool_wrapper_parses_json_output(tmp_path, monkeypatch, capsys) -> None:
    """命令行包装仍要支持 --json，并把结果写成可解析 JSON。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "jjxf_verify_mods_tool", ROOT / "tools" / "verify_mods.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake = {
        "line": "4.6",
        "enabled": [],
        "expected": [],
        "loaded": [],
        "missing": [],
        "unexpected": [],
        "failed": [],
        "warnings": [],
        "problems": [],
        "ok": True,
        "reason": "冒烟通过",
        "seconds": 0.1,
        "run_dir": "",
        "packs": [],
        "nodes": 5,
    }
    monkeypatch.setattr(module, "verify_line", lambda cfg, line, timeout: dict(fake))

    target = tmp_path / "out.json"
    code = module.main(["4.6", "--json", str(target)])
    assert code == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["reports"][0]["ok"] is True


def test_tool_wrapper_exit_code_on_failure(tmp_path, monkeypatch) -> None:
    """有任一线未通过时退出码必须非 0，供 CI/脚本判断。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "jjxf_verify_mods_tool2", ROOT / "tools" / "verify_mods.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    bad = {
        "line": "4.6",
        "enabled": ["a"],
        "expected": ["a"],
        "loaded": [],
        "missing": ["a"],
        "unexpected": [],
        "failed": [],
        "warnings": [],
        "problems": ["未装载：a"],
        "ok": False,
        "reason": "冒烟未通过",
        "seconds": 1.0,
        "run_dir": "",
        "packs": [],
        "nodes": 0,
    }
    monkeypatch.setattr(module, "verify_line", lambda cfg, line, timeout: dict(bad))
    assert module.main(["4.6"]) == 1


# ---------------------------------------------------------------- 事件总线


def test_mod_host_supports_game_events_and_services() -> None:
    """兼容层必须真的实现扩展会调用的 API。

    背景（实测踩过的坑）：

    1. 过去 ``subscribe()`` 只认 ``tree.node_added``，扩展订阅
       ``game.equipment_changed`` 等真实存在的信号会被拒，只留一条
       ``push_warning``。扩展以为自己订上了，功能却永不触发。
    2. ``mch.dragon_appearance`` 调用了 ``api.register_service()``，
       而宿主从来没实现过。Godot 对调用不存在的成员是运行期硬错，
       直接打断 ``on_game_ready``，最终只剩一句
       ``on_game_ready() 返回 false``，真因完全看不出来。

    这两类问题在老冒烟里都查不到——因为 ``_ping_game_ready`` 在
    ``--smoke`` 模式下从不执行（entry.gd 不切场景）。所以这里用源码
    断言把「支持哪些 API」钉死，防止回退。
    """
    src = (ROOT / "runtime" / "_compat" / "mod_host.gd").read_text(encoding="utf-8")
    # 事件转发
    assert "_subscribe_game_event" in src
    assert 'event.begins_with("game.")' in src
    # 跨扩展服务（dragon_appearance 依赖）
    assert "func register_service(" in src
    assert "func _register_service(" in src
    # smoke 模式下也要能判定主场景已就绪
    assert "_game_scene_present" in src


def test_entry_waits_long_enough_for_mod_host() -> None:
    """smoke 模式必须给 ModHost 留出执行 on_game_ready 的帧数。

    只等 1~2 帧的话，ModHost 的 ``await`` 还没醒来进程就 quit 了，
    ``on_game_ready`` 与事件订阅等于从未被执行过，缺陷也就永远查不出来。
    """
    src = (ROOT / "runtime" / "_compat" / "entry.gd").read_text(encoding="utf-8")
    assert "for _i in range(6):" in src or "range(6)" in src
    # 不能再退回只等两帧的写法
    assert "await get_tree().process_frame\n\tawait get_tree().process_frame\n" not in src


def test_mod_host_suspends_mod_until_ready() -> None:
    """扩展进树前必须先挂起，且就绪后只在仍挂起时才恢复。

    这是一个「只有真按键盘才会暴露」的 bug：``add_child`` 之后扩展立刻在树
    上，``_input`` 会被调用；而它的 UI 要到 ``on_game_ready`` 才建。实测
    ``mch.sell_all`` 的 ``_input`` 首行 ``if not _dlg.visible`` 直接空引用。

    同时要防止另一种反向错误：扩展自己会把 ``process_mode`` 设成
    ``PROCESS_MODE_ALWAYS``（要暂停时也工作），宿主若无条件改回
    ``INHERIT``，就把作者的意图冲掉了。
    """
    src = (ROOT / "runtime" / "_compat" / "mod_host.gd").read_text(encoding="utf-8")
    # 进树前挂起
    assert "inst.process_mode = Node.PROCESS_MODE_DISABLED" in src
    # 只在仍为 DISABLED 时恢复，不能无条件覆盖
    assert "node.process_mode == Node.PROCESS_MODE_DISABLED" in src
    # 就绪状态要落进报告，否则「装载 ok 但功能不工作」查不出来
    assert "_ready_state" in src


def test_mod_host_suspends_mod_until_ready() -> None:
    """扩展进树前必须先挂起，且就绪后只在仍挂起时才恢复。

    这是一个「只有真按键盘才会暴露」的 bug：``add_child`` 之后扩展立刻在树
    上，``_input`` 会被调用；而它的 UI 要到 ``on_game_ready`` 才建。实测
    ``mch.sell_all`` 的 ``_input`` 首行 ``if not _dlg.visible`` 直接空引用。

    同时要防止另一种反向错误：扩展自己会把 ``process_mode`` 设成
    ``PROCESS_MODE_ALWAYS``（要暂停时也工作），宿主若无条件改回
    ``INHERIT``，就把作者的意图冲掉了。
    """
    src = (ROOT / "runtime" / "_compat" / "mod_host.gd").read_text(encoding="utf-8")
    # 进树前挂起
    assert "inst.process_mode = Node.PROCESS_MODE_DISABLED" in src
    # 只在仍为 DISABLED 时恢复，不能无条件覆盖
    assert "node.process_mode == Node.PROCESS_MODE_DISABLED" in src
    # 就绪状态要落进报告，否则「装载 ok 但功能不工作」查不出来
    assert "_ready_state" in src
