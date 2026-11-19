"""源码兜底：所有基线都必须能自动拿到反解源码，且缺源码时要**硬拦**。

背景（2026-11 事故）：4.7 线随包带了 ``sources-4.7.zip``，但运行时从来没读它，
于是所有 ``text_patches`` / 钩子插桩都拿不到原文，包显示「已启用」、功能全无。
修完之后必须用测试锁住三件事：

1. 每条内置线都能解析到自己的源码包（``sources-<line>.zip`` 优先）；
2. 缺源码 / 源码与本体对不上时，``source_ready`` 返回可读的原因；
3. 依赖源码的包在缺源码的线上**不允许安装**（不是警告，是拒装）。

另外守住清单声明：schema 4 的包用了 text_patches / method_hooks 就必须写
``sources``，并且 sha256 要与本机随包源码一致。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.core import paths
from app.core.ext import basecontent, manifest as manifest_mod


def _zip_pkg(tmp: Path, manifest: dict) -> Path:
    path = tmp / "pkg.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("extension.json", json.dumps(manifest, ensure_ascii=False))
    return path


def _manifest(line: str, schema: int = 4, sources: dict | None = None) -> dict:
    data = {
        "schema": schema,
        "id": "test.source.gate",
        "name": "源码闸门测试",
        "version": "1.0.0",
        "baseline": {
            "id": "jjxf.v1.45" if line == "4.6" else "jjxf.v4.7",
            "engine": {"line": line},
        },
        "method_hooks": [{"target": "scripts/game.gd", "method": "_ready"}],
    }
    if sources is not None:
        data["sources"] = sources
    return data


@pytest.fixture
def empty_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "baseline"
    target.mkdir()
    monkeypatch.setattr(paths, "BASELINE", target)
    return target


def test_each_line_resolves_its_own_sources() -> None:
    """两条内置线都必须有源码包，且互不串用。"""
    names = {}
    for line in ("4.6", "4.7"):
        path = basecontent.canonical_zip(line)
        assert path is not None, f"{line} 没有随包源码"
        names[line] = path.name
    assert names["4.6"] == "sources-4.6.zip", names
    assert names["4.7"] == "sources-4.7.zip", names


def test_source_ready_explains_missing(empty_baseline: Path) -> None:
    ok, why = basecontent.source_ready({"baselines": {}}, "4.6")
    assert ok is False
    assert "sources-4.6.zip" in why
    assert "反解" in why


def test_schema4_requires_sources(tmp_path: Path) -> None:
    pkg = _zip_pkg(tmp_path, _manifest("4.6", schema=4))
    m = manifest_mod.load(pkg)
    codes = [i.code for i in m.issues if i.level == "error"]
    assert "sources.missing" in codes, [(i.level, i.code) for i in m.issues]


def test_schema3_missing_sources_is_only_warning(tmp_path: Path) -> None:
    pkg = _zip_pkg(tmp_path, _manifest("4.6", schema=3))
    m = manifest_mod.load(pkg)
    assert not [i for i in m.issues if i.level == "error"]
    assert any(i.code == "sources.missing" and i.level == "warn" for i in m.issues)


def test_sources_sha_must_match_local_zip(tmp_path: Path) -> None:
    info = basecontent.source_info("4.6")
    pkg = _zip_pkg(
        tmp_path,
        _manifest(
            "4.6",
            sources={"name": "sources-4.6.zip", "sha256": "0" * 64, "line": "4.6"},
        ),
    )
    m = manifest_mod.load(pkg)
    codes = [i.code for i in m.issues if i.level == "error"]
    assert "sources.sha_mismatch" in codes
    # 正确哈希则不应报错
    good_dir = tmp_path / "good"
    good_dir.mkdir(exist_ok=True)
    good = _zip_pkg(
        good_dir,
        _manifest(
            "4.6",
            sources={
                "name": info["name"],
                "sha256": info["sha256"],
                "line": "4.6",
            },
        ),
    )
    m2 = manifest_mod.load(good)
    assert "sources.sha_mismatch" not in [i.code for i in m2.issues]


def test_install_rejects_package_that_needs_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """依赖源码的包在「本机没有源码」的线上必须拒装。"""
    from app.core.ext import basecontent as bc
    from app.core.ext.service import ExtensionService

    monkeypatch.setattr(bc, "source_ready", lambda cfg, line: (False, "测试：这条线没有源码"))
    monkeypatch.setattr(bc, "canonical_zip", lambda line: None)
    monkeypatch.setattr(bc, "source_info", lambda line: {"name": "", "sha256": "", "path": "", "size": 0})

    pkg = _zip_pkg(tmp_path, _manifest("4.6", schema=3))
    svc = ExtensionService({"extensions": {"extension_mode": "loose"}})
    res = svc.install(pkg, None, verify_smoke=False, target_line="4.6")
    assert res.ok is False
    assert res.code == "install.no_source", (res.code, res.message)
    assert "源码" in res.message


def test_launch_blocks_when_hooks_have_no_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有包 + 源码被删：启动时必须硬失败，而不是静默不插桩。

    静默的后果是「界面显示已启用、进游戏完全没效果」，那是最难排查的一类。
    """
    from app.core.errors import LauncherError
    from app.core.ext import basecontent as bc
    from app.core.ext import enable as enable_mod

    item = {
        "id": "test.source.hooks",
        "name": "钩子包",
        "version": "1.0.0",
        "engine_line": "4.6",
        "enabled": True,
        "overlay": "",
        "lines": {"4.6": {"line": "4.6", "status": "needs_hooks", "enabled": True}},
    }
    monkeypatch.setattr(enable_mod, "enabled_mods", lambda cfg, line: [item])
    monkeypatch.setattr(
        enable_mod, "resolve_order", lambda cfg, line, mods, rd: (mods, {}, [], None)
    )
    monkeypatch.setattr(
        enable_mod,
        "_collect_hooks",
        lambda cfg, mods, line: (
            [{"script": "scripts/game.gd", "method": "_ready", "phase": "before"}],
            [],
            {"test.source.hooks": [{"script": "scripts/game.gd", "method": "_ready"}]},
        ),
    )
    monkeypatch.setattr(
        enable_mod,
        "_build_instrumented",
        lambda cfg, line, rd, hooks, targets, merged=None: (None, ["找不到源码"], set()),
    )
    monkeypatch.setattr(bc, "source_ready", lambda cfg, line: (False, "测试：这条线没有源码"))

    with pytest.raises(LauncherError) as exc:
        enable_mod.build_for_launch({"extensions": {"extension_mode": "loose"}}, "4.6", tmp_path)
    assert "无法" in str(exc.value) and "源码" in str(exc.value)
