"""合集包在完全校验下的「一次组合冒烟」语义。

背景（2026-11-17）：逐包冒烟在 4.7 的 21 个成员上实测 21 × 21.5 秒 ≈ 7.5 分钟，
其中 20 次验的还是玩家不会跑的配置（只挂一个包）。现在改成装完统一跑一次
「全部已启用扩展一起挂」，失败按 mod_report 点名回滚。

这里不打桩真实安装（要造 pck、跑引擎），只验证编排契约：
- 多成员时成员级 ``verify_smoke`` 必须是 False，组合校验只跑一次；
- 组合校验点名的成员要单独回滚，其余保留；
- 点不到本批成员时不回滚，只记警告。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import app.core.ext.service as service_mod
from app.core import paths
from app.core.ext.service import BundleResult, ExtensionService, InstallResult, install_bundle


def _member_zip(path: Path, ext_id: str, *, requires: list[str] | None = None) -> bytes:
    manifest = {
        "schema": 1,
        "id": ext_id,
        "name": ext_id,
        "version": "1.0.0",
        "files": [],
    }
    if requires:
        manifest["requires"] = [{"id": r} for r in requires]
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("extension.json", json.dumps(manifest, ensure_ascii=False))
    return buf.getvalue()


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PACKS", tmp_path / "packs")
    monkeypatch.setattr(paths, "BUILDS", tmp_path / "builds")
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(service_mod, "REGISTRY", tmp_path / "config" / "extensions.json")
    for d in ("packs", "builds", "cache", "config"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_bundle(root: Path, members: dict[str, bytes]) -> Path:
    pkg = root / "bundle.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        for name, blob in members.items():
            z.writestr(f"mod/{name}", blob)
    return pkg


def _cfg() -> dict:
    return {
        "extensions": {"extension_mode": "strict"},
        "profiles": {"default": "default", "redirect_appdata": False},
    }


def test_strict_bundle_runs_one_combined_smoke(sandbox, monkeypatch) -> None:
    cfg = _cfg()
    pkg = _make_bundle(
        sandbox,
        {"a.zip": _member_zip(sandbox / "a.zip", "ext.a"),
         "b.zip": _member_zip(sandbox / "b.zip", "ext.b")},
    )

    monkeypatch.setattr(service_mod, "_bundle_preflight", lambda *a, **k: None)
    seen_smoke: list[bool] = []

    def fake_install(self, package, progress=None, **kwargs):
        name = Path(package).name
        seen_smoke.append(bool(kwargs.get("verify_smoke")))
        return InstallResult(
            ok=True, ext_id=f"ext.{name[0]}", name=name, version="1.0.0",
            engine_line="4.7", message=f"{name} 已安装",
        )

    monkeypatch.setattr(ExtensionService, "install", fake_install)
    verified: list[list[str]] = []
    monkeypatch.setattr(
        service_mod, "_verify_bundle_smoke",
        lambda cfg, line, timeout, say: (
            verified.append([line])
            or {"ok": True, "loaded": ["ext.a", "ext.b"], "failed": [], "missing": [],
                "reason": "冒烟通过", "seconds": 22.0}
        ),
    )
    monkeypatch.setattr(ExtensionService, "mark_verified", lambda self, ids, line: len(list(ids)))

    out = install_bundle(cfg, pkg, target_line="4.7")

    assert out.ok
    assert out.succeeded == 2
    # 成员级一律不冒烟：逐包冒烟正是那个 7.5 分钟的来源
    assert seen_smoke == [False, False]
    # 组合校验只跑一次，且用的是目标线
    assert len(verified) == 1
    assert verified[0] == ["4.7"]


def test_combined_smoke_rolls_back_only_named_member(sandbox, monkeypatch) -> None:
    cfg = _cfg()
    pkg = _make_bundle(
        sandbox,
        {"a.zip": _member_zip(sandbox / "a.zip", "ext.a"),
         "b.zip": _member_zip(sandbox / "b.zip", "ext.b")},
    )

    monkeypatch.setattr(service_mod, "_bundle_preflight", lambda *a, **k: None)

    def fake_install(self, package, progress=None, **kwargs):
        name = Path(package).name
        return InstallResult(
            ok=True, ext_id=f"ext.{name[0]}", name=name, version="1.0.0",
            engine_line="4.7", message=f"{name} 已安装",
        )

    monkeypatch.setattr(ExtensionService, "install", fake_install)

    verdicts = [
        {"ok": False, "loaded": ["ext.a"], "failed": [{"id": "ext.b", "error": "起不来"}],
         "missing": [], "reason": "ext.b 装载失败", "seconds": 22.0},
        {"ok": True, "loaded": ["ext.a"], "failed": [], "missing": [],
         "reason": "冒烟通过", "seconds": 21.0},
    ]
    calls: list[int] = []

    def fake_verify(cfg, line, timeout, say):
        calls.append(len(calls))
        # 第一次（组合校验）点名 ext.b 失败，第二次（回滚后复验）通过
        return verdicts[0] if len(calls) == 1 else verdicts[1]

    monkeypatch.setattr(service_mod, "_verify_bundle_smoke", fake_verify)

    removed: list[str] = []
    monkeypatch.setattr(
        ExtensionService, "uninstall",
        lambda self, ext_id, **kw: removed.append(ext_id) or {"ok": True},
    )
    monkeypatch.setattr(ExtensionService, "mark_verified", lambda self, ids, line: len(list(ids)))

    out = install_bundle(cfg, pkg, target_line="4.7")

    # b 回滚、a 留着：与「允许部分成功」的既定决策一致
    assert removed == ["ext.b"]
    assert out.succeeded == 1
    assert out.failed == 1
    assert out.ok
    bad = next(i for i in out.items if i.ext_id == "ext.b")
    assert not bad.ok and "回滚" in bad.message
    assert any("回滚" in w for w in out.warnings)
    # 回滚后要复验一次
    assert len(calls) == 2


def test_combined_smoke_unattributed_failure_keeps_mods(sandbox, monkeypatch) -> None:
    cfg = _cfg()
    pkg = _make_bundle(
        sandbox,
        {"a.zip": _member_zip(sandbox / "a.zip", "ext.a"),
         "b.zip": _member_zip(sandbox / "b.zip", "ext.b")},
    )

    monkeypatch.setattr(service_mod, "_bundle_preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        ExtensionService, "install",
        lambda self, package, progress=None, **kw: InstallResult(
            ok=True, ext_id=f"ext.{Path(package).name[0]}", name=Path(package).name,
            version="1.0.0", engine_line="4.7", message="ok",
        ),
    )
    monkeypatch.setattr(
        service_mod, "_verify_bundle_smoke",
        lambda cfg, line, timeout, say: {
            "ok": False, "loaded": [], "failed": [], "missing": [],
            "reason": "启动期脚本错误", "seconds": 5.0,
        },
    )
    monkeypatch.setattr(
        ExtensionService, "uninstall",
        lambda self, ext_id, **kw: pytest.fail("点不到具体包时不该回滚"),
    )
    monkeypatch.setattr(ExtensionService, "mark_verified", lambda self, ids, line: 0)

    out = install_bundle(cfg, pkg, target_line="4.7")

    assert out.succeeded == 2
    assert any("问题不在本批" in w for w in out.warnings)


def test_bundle_result_has_warnings_field() -> None:
    """回归：预检剔除成员时会写 ``result.warnings``，数据类里没这个字段就崩。"""
    out = BundleResult(ok=True)
    out.warnings.append("组合检查：1 个成员被排除")
    assert out.as_dict()["warnings"] == ["组合检查：1 个成员被排除"]
