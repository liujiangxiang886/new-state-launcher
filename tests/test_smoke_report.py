"""安装期校验判据的回归测试。

这三个点以前都出过错，必须钉住：

1. ``mod_report.json`` **缺失**时不能当通过——旧实现「没证据也算成功」，
   导致「显示已安装、游戏里没效果」；
2. 传给游戏的路径必须是绝对路径——游戏进程的 cwd 是引擎目录，相对路径
   会被它按自己的 cwd 解析，表现就是「点开始游戏进不去」；
3. ``judge`` 只认客观证据（退出码 / smoke 标记 / 节点数 / 挂载报告），
   不能因为退出码是 0 就放过脚本错误。

运行::

    _build_tools\\venv\\Scripts\\python.exe -m pytest tests/test_smoke_report.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import runner  # noqa: E402
from app.core import smoke as smoke_mod  # noqa: E402


def test_missing_report_is_failure(tmp: Path) -> None:
    loaded, failed, reasons, events = smoke_mod.read_mod_report(tmp / "nope.json")
    assert loaded == [] and failed == []
    assert reasons, "报告缺失必须给出判不过的原因"
    assert events == []
    print("  ✓ 报告缺失判失败")


def test_report_loaded_and_failed(tmp: Path) -> None:
    p = tmp / "mod_report.json"
    p.write_text(
        json.dumps(
            {
                "schema": 1,
                "ok": False,
                "loaded": ["a.one"],
                "failed": [{"id": "b.two", "error": "install() 返回 false"}],
                "game_events": ["equipment_changed", "save_loaded"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded, failed, reasons, events = smoke_mod.read_mod_report(p)
    assert loaded == ["a.one"]
    assert failed and failed[0]["id"] == "b.two"
    assert any("ok=false" in r for r in reasons)
    assert events == ["equipment_changed", "save_loaded"]

    text = "；".join(smoke_mod.mod_failed_reasons(failed))
    assert "b.two" in text and "install() 返回 false" in text
    print("  ✓ 报告解析：已装载 / 失败明细 / ok=false / 事件订阅")


def test_bad_json_report_is_failure(tmp: Path) -> None:
    p = tmp / "mod_report.json"
    p.write_text("{ not json", encoding="utf-8")
    _loaded, _failed, reasons, events = smoke_mod.read_mod_report(p)
    assert reasons and "解析" in reasons[0]
    assert events == []
    print("  ✓ 报告损坏判失败")


def test_judge_accepts_clean_run() -> None:
    result = runner.LaunchResult(
        ok=True,
        returncode=0,
        seconds=1.0,
        command=[],
        smoke_report={"smoke": "ok", "nodes": 5},
        mount_report={"ok": True, "mounted": ["a.pck"], "errors": []},
        stderr="",
    )
    ok, reasons = smoke_mod.judge(result)
    assert ok, reasons
    print("  ✓ 干净结果判通过")


def test_judge_rejects_silent_script_error() -> None:
    """退出码 0 但 stderr 有 SCRIPT ERROR：必须判失败。"""
    result = runner.LaunchResult(
        ok=True,
        returncode=0,
        seconds=1.0,
        command=[],
        smoke_report={"smoke": "ok", "nodes": 5},
        mount_report={"ok": True, "mounted": ["a.pck"], "errors": []},
        stderr="SCRIPT ERROR: Parse Error: unexpected token\n",
    )
    ok, reasons = smoke_mod.judge(result)
    assert not ok
    assert any("脚本错误" in r for r in reasons), reasons
    print("  ✓ 退出码 0 但脚本报错仍判失败")


def test_judge_rejects_low_nodes() -> None:
    result = runner.LaunchResult(
        ok=True,
        returncode=0,
        seconds=1.0,
        command=[],
        smoke_report={"smoke": "ok", "nodes": 1},
        mount_report={"ok": True, "mounted": [], "errors": []},
        stderr="",
    )
    ok, reasons = smoke_mod.judge(result)
    assert not ok and any("节点数" in r for r in reasons)
    print("  ✓ 节点数不足判失败")


def test_build_command_uses_absolute_paths() -> None:
    cfg = cfg_mod.load()
    cmd = runner.build_command(
        cfg,
        "4.6",
        packset="builds/some/packset.json",
        main_scene="res://scenes/start_page.tscn",
        smoke=False,
        headless=True,
        log_path="builds/some/mount_report.json",
        mods_path="builds/some/mods.json",
    )
    for arg in cmd:
        if arg.startswith(("--packset=", "--log=", "--mods=")):
            value = arg.split("=", 1)[1]
            assert Path(value).is_absolute(), arg
            assert value.startswith(ROOT.as_posix()), arg
    assert "--headless" in cmd
    print("  ✓ packset/log/mods 全部绝对路径")


def test_make_packset_absolutizes(tmp: Path) -> None:
    cfg = cfg_mod.load()
    blob = tmp / "dummy.bin"
    blob.write_bytes(b"not a pck")
    out = runner.make_packset(
        tmp / "packset.json",
        packset_id="t",
        line="4.6",
        packs=[{"path": str(blob), "offset": 0, "role": "overlay"}],
        baseline_id="jjxf.v1.45",
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    for item in data["packs"]:
        assert Path(item["path"]).is_absolute(), item
    print("  ✓ 分片路径写盘前转绝对路径")


def test_full_report_reads_isolation_fields(tmp: Path) -> None:
    """``warnings`` / ``violations`` / ``mode`` 必须被读出来。

    这些字段 GDScript 侧一直在写，Python 侧长期没读 —— 结果是严格模式
    把越界拦下来了，界面却什么都不显示，等于白开。
    """
    p = tmp / "mod_report.json"
    p.write_text(
        json.dumps(
            {
                "schema": 1,
                "ok": True,
                "mode": "strict_verify",
                "loaded": ["a.one"],
                "failed": [],
                "warnings": ["a.one：钩子点未生效"],
                "violations": [
                    {
                        "code": "peer.read.undeclared",
                        "from": "a.one",
                        "target": "b.two",
                        "detail": "未声明读取对方状态",
                    }
                ],
                "hooks": 7,
                "ready_state": "ready",
                "services": ["a.one.price"],
                "pending_events": 2,
                "game_events": ["save_loaded"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = smoke_mod.read_mod_report_full(p)
    assert report.mode == "strict_verify"
    assert report.is_strict
    assert report.warnings == ["a.one：钩子点未生效"]
    assert len(report.violations) == 1
    assert report.violations[0]["from"] == "a.one"
    assert report.hooks == 7
    assert report.ready_state == "ready"
    assert report.services == ["a.one.price"]
    assert report.pending_events == 2
    assert report.loaded == ["a.one"]
    assert report.reasons == [], "ok=true 时不该有判不过的原因"

    lines = smoke_mod.violation_lines(report.violations)
    assert lines and "a.one" in lines[0] and "b.two" in lines[0]

    # 4 元组旧接口必须仍然可用（既有调用面不能破）
    loaded, failed, reasons, events = smoke_mod.read_mod_report(p)
    assert loaded == ["a.one"] and failed == [] and reasons == [] and events == ["save_loaded"]


def test_full_report_missing_has_safe_defaults(tmp: Path) -> None:
    report = smoke_mod.read_mod_report_full(tmp / "nope.json")
    assert report.reasons, "缺失报告必须给原因"
    assert report.mode == "loose"
    assert report.violations == [] and report.warnings == []


def test_violation_lines_truncates() -> None:
    items = [
        {"from": f"m{i}", "target": "x", "detail": ""} for i in range(12)
    ]
    lines = smoke_mod.violation_lines(items, limit=3)
    assert len(lines) == 4
    assert "还有 9 条越界记录" in lines[-1]


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        print("安装期校验判据测试：")
        test_missing_report_is_failure(tmp)
        test_report_loaded_and_failed(tmp)
        test_bad_json_report_is_failure(tmp)
        test_judge_accepts_clean_run()
        test_judge_rejects_silent_script_error()
        test_judge_rejects_low_nodes()
        test_build_command_uses_absolute_paths()
        test_make_packset_absolutizes(tmp)
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
