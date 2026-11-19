"""一条线只装自己匹配的扩展：跨线安装硬拒绝。

为什么必须硬拒绝：``target_line`` 重算补丁看着能跑，实测会让「补丁只命中
一部分」的包装上去但功能静默失效——比装不上更难排查。所以这里测的不只是
「返回 False」，还包括「磁盘上一点残留都不能有」。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.core import paths
from app.core.ext import enable as enable_mod
from app.core.ext import service as service_mod
from app.core.fsutil import atomic_write_json, read_json


def _make_pkg(tmp: Path, ext_id: str, baseline: str | None, *, with_payload: bool = True) -> Path:
    root = tmp / f"src-{ext_id}"
    payload = root / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "main.gd").write_text("extends Node\n", encoding="utf-8")
    manifest: dict = {
        "schema": 1,
        "id": ext_id,
        "name": f"跨线测试 {ext_id}",
        "version": "1.0.0",
        "files": [{"source": "payload/main.gd", "target": f"mods/{ext_id}/main.gd"}],
    }
    if baseline:
        manifest["baseline"] = baseline
    (root / "extension.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    if not with_payload:
        (payload / "main.gd").unlink()

    out = tmp / f"{ext_id}.zip"
    with zipfile.ZipFile(out, "w") as z:
        for f in root.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
    return out


def _svc() -> service_mod.ExtensionService:
    return service_mod.ExtensionService({"extensions": {"extension_mode": "normal"}})


def _install_leftovers() -> list[str]:
    return sorted(p.name for p in paths.BUILDS.iterdir() if p.name.startswith("_install_"))


# ---------------------------------------------------------------- 拒绝


# 跨线守卫的语义：扩展声明的适配线与安装目标线不一致时**硬拒绝**，
# 且必须在解包之前就拒。1.4 起只有内置线一条，所以「目标线」一律取
# 一个不存在的线别名来制造不一致——校验逻辑本身与有几条线无关。


def test_package_rejected_on_other_line(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, "test.line.a", "jjxf.v1.45")

    result = _svc().install(pkg, verify_smoke=False, target_line="9.9")

    assert result.ok is False
    assert result.line_mismatch is True
    assert result.declared_line == "4.6"
    assert "只适配 4.6" in result.message
    assert "不能装到 9.9" in result.message


def test_undeclared_package_defaults_to_builtin(tmp_path: Path) -> None:
    """没写 baseline 的包按内置线处理，装到别的线一样拒绝并说明原因。"""
    pkg = _make_pkg(tmp_path, "test.line.c", None)

    result = _svc().install(pkg, verify_smoke=False, target_line="9.9")

    assert result.line_mismatch is True
    assert result.declared_line == "4.6"
    assert "未声明引擎线" in result.message


def test_rejection_leaves_no_files(tmp_path: Path) -> None:
    """拒绝必须发生在解包之前：不留 packs/<id>、不留 _install_ 中间目录。"""
    pkg = _make_pkg(tmp_path, "test.line.d", "jjxf.v1.45")

    _svc().install(pkg, verify_smoke=False, target_line="9.9")

    assert not (paths.PACKS / "test.line.d").exists()
    assert _install_leftovers() == []


def test_rejection_does_not_touch_registry(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, "test.line.e", "jjxf.v1.45")

    _svc().install(pkg, verify_smoke=False, target_line="9.9")

    data = read_json(service_mod.REGISTRY, default={"items": []}) or {}
    assert data.get("items") == []


def test_matching_line_is_not_rejected(tmp_path: Path) -> None:
    """同线的包不会因这条规则被拒（后面可能因别的检查失败，但不该是跨线）。"""
    pkg = _make_pkg(tmp_path, "test.line.f", "jjxf.v1.45")

    result = _svc().install(pkg, verify_smoke=False, target_line="4.6")

    assert result.line_mismatch is False
    assert result.declared_line == "" or result.declared_line == "4.6"


def test_install_result_serializes_flag(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, "test.line.g", "jjxf.v1.45")

    data = _svc().install(pkg, verify_smoke=False, target_line="9.9").as_dict()

    assert data["ok"] is False
    assert data["line_mismatch"] is True
    assert data["declared_line"] == "4.6"


# ---------------------------------------------------------------- 升级候选


def _entry(ext_id: str, line: str, baseline: str) -> dict:
    return {
        "id": ext_id,
        "name": ext_id,
        "enabled": True,
        "engine_line": line,
        "baseline": baseline,
        "lines": {line: {"line": line, "baseline": baseline, "status": "ready"}},
    }


def _manifest(ext_id: str, line: str, declared: str) -> None:
    folder = paths.PACKS / ext_id
    folder.mkdir(parents=True, exist_ok=True)
    data = {"name": ext_id, "baseline": {"id": f"jjxf.v{declared}", "engine": {"line": declared}}}
    (folder / f"manifest-{line.replace('.', '')}.v2.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_declared_line_reads_manifest_not_registry() -> None:
    """注册表顶层写的线不能信——那是当年跨线重算留下的痕迹。"""
    _manifest("mod.legacy", "4.5", "4.6")
    item = _entry("mod.legacy", "4.5", "jjxf.v1.44")

    assert enable_mod.declared_line_of(item) == "4.6"


def test_declared_line_falls_back_to_baseline_id() -> None:
    # 没有清单时回落看 baseline id；无法识别的一律归到内置线，
    # 这样「声明不明的包」不会被误判成可用。
    item = {"id": "mod.noManifest", "engine_line": "4.5", "baseline": "jjxf.v1.44"}
    assert enable_mod.declared_line_of(item) == "4.6"
