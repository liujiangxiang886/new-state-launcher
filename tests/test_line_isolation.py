"""分线隔离：4.6 / 4.7（以及未来的 custom:*）必须是**互相独立**的安装。

守住四件事：

1. 同一条包可以分别装到两条线，覆盖层文件各一份；
2. **启用状态按线独立**——4.6 关掉不影响 4.7；
3. 重装到一条线不会把另一条线手动关掉的包重新打开；
4. 只卸载一条线时，另一条线的覆盖层与启用状态原样保留；
5. 自定义线 id（``custom:<uuid>``）的文件名标签必须合法（冒号在 Windows
   文件名里非法，直接用 ``line.replace(".", "")`` 会建不出路径）。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.core import config as cfg_mod
from app.core import paths
from app.core.ext import enable as enable_mod
from app.core.ext.service import ExtensionService


def _make_pkg(tmp: Path, tag: str, line: str, ext_id: str) -> Path:
    """造一个最小扩展包：只铺一个自己的文件，不依赖源码。"""
    baseline = "jjxf.v1.45" if line == "4.6" else "jjxf.v4.7"
    root = tmp / f"src-{tag}-{line.replace('.', '')}"
    (root / "payload").mkdir(parents=True, exist_ok=True)
    (root / "payload" / "x.txt").write_text(f"{tag}-{line}", encoding="utf-8")
    manifest = {
        "schema": 3,
        "id": ext_id,
        "name": f"分线测试 {tag}",
        "version": "1.0.0",
        "baseline": {"id": baseline, "engine": {"line": line}},
        "files": [{"source": "payload/x.txt", "target": f"mods/{tag}/x.txt"}],
    }
    (root / "extension.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    out = tmp / f"{tag}-{line.replace('.', '')}.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for path in root.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(root).as_posix())
    return out


@pytest.fixture
def cfg() -> dict:
    """宽松档配置：只装文件、不跑冒烟，避免测试依赖引擎。"""
    return {"extensions": {"extension_mode": "loose"}}


def _enabled_ids(cfg: dict, line: str) -> list[str]:
    return [str(m.get("id")) for m in enable_mod.enabled_mods(cfg, line)]


def test_line_tag_is_filename_safe() -> None:
    assert paths.line_tag("4.6") == "46"
    assert paths.line_tag("4.7") == "47"
    # 自定义线：冒号、斜杠、尖括号都不能进文件名
    tag = paths.line_tag("custom:3f2a/b<c>")
    assert tag == "custom_3f2a_b_c", tag
    assert not any(ch in tag for ch in ':\\/<>"|?*')


def test_install_same_package_to_both_lines(tmp_path: Path, cfg: dict) -> None:
    svc = ExtensionService(cfg)
    ext_id = "test.lineiso.both"
    pkg46 = _make_pkg(tmp_path, "both", "4.6", ext_id)
    pkg47 = _make_pkg(tmp_path, "both", "4.7", ext_id)

    r46 = svc.install(pkg46, None, verify_smoke=False, target_line="4.6")
    assert r46.ok, r46.message
    r47 = svc.install(pkg47, None, verify_smoke=False, target_line="4.7")
    assert r47.ok, r47.message

    item = svc.get(ext_id) or {}
    lines = item.get("lines") or {}
    assert set(lines) == {"4.6", "4.7"}, lines

    overlay46 = Path(str(lines["4.6"]["overlay"]))
    overlay47 = Path(str(lines["4.7"]["overlay"]))
    assert overlay46.exists() and overlay46.name == "overlay-46.pck"
    assert overlay47.exists() and overlay47.name == "overlay-47.pck"
    # 两份覆盖层是不同文件，不是同一份被改名
    assert overlay46 != overlay47


def test_enabled_state_is_per_line(tmp_path: Path, cfg: dict) -> None:
    svc = ExtensionService(cfg)
    ext_id = "test.lineiso.toggle"
    for line in ("4.6", "4.7"):
        res = svc.install(
            _make_pkg(tmp_path, "toggle", line, ext_id),
            None,
            verify_smoke=False,
            target_line=line,
        )
        assert res.ok, res.message

    # 4.6 停用：4.7 必须仍然启用
    assert svc.set_enabled(ext_id, False, line="4.6") is True
    assert ext_id not in _enabled_ids(cfg, "4.6")
    assert ext_id in _enabled_ids(cfg, "4.7")

    item = svc.get(ext_id) or {}
    assert item["lines"]["4.6"]["enabled"] is False
    assert item["lines"]["4.7"]["enabled"] is True
    # 顶层镜像：任意一条线启用即为 True，不能因为 4.6 关了就把整包标成停用
    assert item["enabled"] is True

    # 再装一次 4.6（升级）不能把 4.7 的开关状态带跑
    res = svc.install(
        _make_pkg(tmp_path, "toggle2", "4.6", ext_id),
        None,
        verify_smoke=False,
        target_line="4.6",
    )
    assert res.ok, res.message
    item = svc.get(ext_id) or {}
    assert item["lines"]["4.6"]["enabled"] is True
    assert item["lines"]["4.7"]["enabled"] is True


def test_uninstall_one_line_keeps_the_other(tmp_path: Path, cfg: dict) -> None:
    svc = ExtensionService(cfg)
    ext_id = "test.lineiso.uninstall"
    for line in ("4.6", "4.7"):
        res = svc.install(
            _make_pkg(tmp_path, "uni", line, ext_id),
            None,
            verify_smoke=False,
            target_line=line,
        )
        assert res.ok, res.message
    svc.set_enabled(ext_id, False, line="4.7")

    before = svc.get(ext_id) or {}
    overlay46 = Path(str(before["lines"]["4.6"]["overlay"]))
    overlay47 = Path(str(before["lines"]["4.7"]["overlay"]))
    assert overlay46.exists() and overlay47.exists()

    report = svc.uninstall_line([ext_id], "4.6")
    assert report.ok, report.message

    after = svc.get(ext_id) or {}
    assert set(after.get("lines") or {}) == {"4.7"}
    assert not overlay46.exists(), "只卸 4.6，它的覆盖层应被删"
    assert overlay47.exists(), "4.7 的覆盖层不能被顺手删掉"
    assert after["lines"]["4.7"]["enabled"] is False, "另一条线的启用状态必须保留"
    assert ext_id not in _enabled_ids(cfg, "4.6"), "4.6 已卸载，不该再参与启动"
    assert ext_id not in _enabled_ids(cfg, "4.7"), "4.7 是停用状态"


def test_mark_launched_flips_only_that_line(cfg: dict) -> None:
    cfg_mod_ready = "ready"
    needs = "needs_hooks"
    svc = ExtensionService(cfg)
    svc._save_registry(
        [
            {
                "id": "test.ready.one",
                "name": "就绪测试",
                "version": "1.0.0",
                "engine_line": "4.6",
                "enabled": True,
                "status": needs,
                "lines": {
                    "4.6": {"line": "4.6", "status": needs, "enabled": True},
                    "4.7": {"line": "4.7", "status": needs, "enabled": True},
                },
            },
            {
                "id": "test.ready.other",
                "name": "没启动的包",
                "version": "1.0.0",
                "engine_line": "4.6",
                "enabled": True,
                "status": needs,
                "lines": {"4.6": {"line": "4.6", "status": needs, "enabled": True}},
            },
        ]
    )

    assert svc.mark_launched("4.6", ["test.ready.one"]) == 1
    items = {i["id"]: i for i in svc.installed()}
    assert items["test.ready.one"]["lines"]["4.6"]["status"] == cfg_mod_ready
    assert items["test.ready.one"]["lines"]["4.6"]["hooks_verified_at"]
    assert items["test.ready.one"]["lines"]["4.7"]["status"] == needs, "别把另一条线也标成就绪"
    assert items["test.ready.other"]["lines"]["4.6"]["status"] == needs, "没传进来的包不能动"

    # 幂等：第二次不再计数
    assert svc.mark_launched("4.6", ["test.ready.one"]) == 0


def _seed_two_lines(svc: ExtensionService) -> None:
    svc._save_registry(
        [
            {
                "id": "test.only46",
                "name": "只在 4.6",
                "version": "1.0.0",
                "engine_line": "4.6",
                "enabled": True,
                "status": "ready",
                "runtime_hooks": [{"script": "scripts/game.gd", "method": "f"}],
                "lines": {
                    "4.6": {
                        "line": "4.6",
                        "status": "ready",
                        "enabled": True,
                        "runtime_hooks": [{"script": "scripts/game.gd", "method": "f"}],
                    }
                },
            },
            {
                "id": "test.only47",
                "name": "只在 4.7",
                "version": "1.0.0",
                "engine_line": "4.7",
                "enabled": False,
                "status": "needs_hooks",
                "runtime_hooks": [],
                "lines": {
                    "4.7": {
                        "line": "4.7",
                        "status": "needs_hooks",
                        "enabled": False,
                        "runtime_hooks": [],
                    }
                },
            },
        ]
    )


def test_cli_mods_list_filters_by_line(capsys) -> None:
    """``mods list --line X`` 必须真按线过滤。

    回归用例：这个参数过去被 argparse 收下，然后在实现里**完全没用**，
    于是 `--line 4.6` 与 `--line 4.7` 输出一模一样（2026-11-13 实测），
    命令行上看不出「两条线互相独立」这件事。
    """
    import argparse

    from app import cli

    svc = ExtensionService({})
    _seed_two_lines(svc)

    assert cli.cmd_mods(argparse.Namespace(action="list", ext_id=None, paths=[], line="4.7")) == 0
    out47 = capsys.readouterr().out
    assert "test.only47" in out47
    assert "test.only46" not in out47, "4.6 的包不应出现在 4.7 的列表里"
    assert "停用" in out47, "应按 4.7 那条线的启用位显示，而不是顶层的 enabled"

    assert cli.cmd_mods(argparse.Namespace(action="list", ext_id=None, paths=[], line="4.6")) == 0
    out46 = capsys.readouterr().out
    assert "test.only46" in out46 and "test.only47" not in out46

    # 不带 --line：两个都在，但要能看出各在哪条线
    assert cli.cmd_mods(argparse.Namespace(action="list", ext_id=None, paths=[], line=None)) == 0
    out_all = capsys.readouterr().out
    assert "test.only46" in out_all and "test.only47" in out_all
    assert "[4.6]" in out_all and "[4.7]" in out_all


def test_cli_mods_list_reports_empty_line(capsys) -> None:
    """线上没有包时要说清楚，而不是打印别人的包。"""
    import argparse

    from app import cli

    svc = ExtensionService({})
    _seed_two_lines(svc)
    svc.uninstall_line(["test.only47"], "4.7")

    assert cli.cmd_mods(argparse.Namespace(action="list", ext_id=None, paths=[], line="4.7")) == 0
    out = capsys.readouterr().out
    assert "没有已安装的扩展" in out
    assert "test.only46" not in out
