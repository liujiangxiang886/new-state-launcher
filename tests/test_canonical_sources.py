"""随包反解源码（``baseline/canonical_sources.zip``）的读取与优先级。

1.6 的事故背景：基线 pck 里脚本只有 ``.gdc``（109 个）没有 ``.gd``（0 个），
而 1.4~1.6 又丢掉了 1.45.1 里「随包带一份反解工程」的做法。结果是所有
``text_patches`` / 合并层 / 钩子插桩都拿不到原文，19 个扩展全灭。

这组测试守住三件事：

1. 没有反解工程目录时，仍能从随包 zip 取到脚本文本；
2. 优先级正确：反解工程 > 随包 zip > 基线 pck > 已装扩展；
3. 多线共存时优先用线专属文件名，不会把 4.6 的源码当成别的线的原文。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.core import paths
from app.core.ext import basecontent


def _write_zip(path: Path, files: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


@pytest.fixture
def baseline_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把 ``paths.BASELINE`` 指到临时目录，避免读到仓库里那份真文件。"""
    target = tmp_path / "baseline"
    target.mkdir()
    monkeypatch.setattr(paths, "BASELINE", target)
    return target


def test_missing_canonical_returns_none(baseline_dir: Path) -> None:
    assert basecontent.canonical_zip("4.6") is None
    assert basecontent.canonical_signature("4.6") == ""

    bc = basecontent.BaseContent({"baselines": {}}, "4.6", include_installed=False)
    assert bc.read("scripts/game.gd") is None
    assert bc.origin("scripts/game.gd") == "missing"
    bc.close()


def test_reads_script_from_canonical(baseline_dir: Path) -> None:
    _write_zip(
        baseline_dir / "canonical_sources.zip",
        {"scripts/game.gd": b"extends Node\n\nfunc _ready():\n\tpass\n"},
    )
    bc = basecontent.BaseContent({"baselines": {}}, "4.6", include_installed=False)
    data = bc.read("scripts/game.gd")
    assert data is not None and b"func _ready" in data
    assert bc.origin("scripts/game.gd") == "canonical"
    # res:// 前缀也要能剥掉
    assert bc.read("res://scripts/game.gd") == data
    # 不在压缩包里的目标仍按缺失处理，不会伪造内容
    assert bc.read("data/equipment.json") is None
    bc.close()


def test_source_project_wins_over_canonical(
    baseline_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_zip(baseline_dir / "canonical_sources.zip", {"scripts/game.gd": b"canonical"})
    root = tmp_path / "reversed"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "game.gd").write_bytes(b"from-source-project")

    cfg = {"sources": {"reversed_project": str(root)}}
    bc = basecontent.BaseContent(cfg, "4.6", include_installed=False)
    assert bc.read("scripts/game.gd") == b"from-source-project"
    assert bc.origin("scripts/game.gd") == "source"
    bc.close()


def test_line_specific_zip_preferred(baseline_dir: Path) -> None:
    _write_zip(baseline_dir / "canonical_sources.zip", {"scripts/game.gd": b"generic"})
    _write_zip(baseline_dir / "canonical_sources-46.zip", {"scripts/game.gd": b"line46"})
    chosen = basecontent.canonical_zip("4.6")
    assert chosen is not None and chosen.name == "canonical_sources-46.zip"

    bc = basecontent.BaseContent({"baselines": {}}, "4.6", include_installed=False)
    assert bc.read("scripts/game.gd") == b"line46"
    bc.close()


def test_corrupt_zip_degrades_instead_of_raising(baseline_dir: Path) -> None:
    (baseline_dir / "canonical_sources.zip").write_bytes(b"not a zip")
    bc = basecontent.BaseContent({"baselines": {}}, "4.6", include_installed=False)
    assert bc.read("scripts/game.gd") is None
    assert bc.origin("scripts/game.gd") == "missing"
    bc.close()


def test_signature_changes_with_file(baseline_dir: Path) -> None:
    path = baseline_dir / "canonical_sources.zip"
    _write_zip(path, {"scripts/game.gd": b"a"})
    first = basecontent.canonical_signature("4.6")
    assert first.startswith("canonical_sources.zip:")
    _write_zip(path, {"scripts/game.gd": b"a" * 500})
    assert basecontent.canonical_signature("4.6") != first


# ---------------------------------------------------------------- 本体指纹


def _write_meta(baseline_dir: Path, **meta) -> None:
    import json

    (baseline_dir / "canonical_sources.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def test_matching_baseline_keeps_canonical(baseline_dir: Path) -> None:
    _write_zip(baseline_dir / "canonical_sources.zip", {"scripts/game.gd": b"ok"})
    _write_meta(baseline_dir, exe_sha256="AA", exe_size=100, entry_count=9)
    cfg = {"baselines": {"jjxf.v1.45": {"engine": "4.6", "sha256": "aa", "size": 100, "entry_count": 9}}}

    bc = basecontent.BaseContent(cfg, "4.6", include_installed=False)
    assert bc.read("scripts/game.gd") == b"ok"
    assert bc.canonical_skipped == ""
    bc.close()


def test_sha_mismatch_disables_canonical(baseline_dir: Path) -> None:
    _write_zip(baseline_dir / "canonical_sources.zip", {"scripts/game.gd": b"ok"})
    _write_meta(baseline_dir, exe_sha256="aa")
    cfg = {"baselines": {"jjxf.v1.45": {"engine": "4.6", "sha256": "bb"}}}

    bc = basecontent.BaseContent(cfg, "4.6", include_installed=False)
    assert bc.read("scripts/game.gd") is None
    assert "sha256" in bc.canonical_skipped
    assert basecontent.canonical_signature("4.6", cfg) == ""
    bc.close()


def test_size_mismatch_disables_canonical(baseline_dir: Path) -> None:
    _write_zip(baseline_dir / "canonical_sources.zip", {"scripts/game.gd": b"ok"})
    _write_meta(baseline_dir, exe_size=100)
    cfg = {"baselines": {"jjxf.v1.45": {"engine": "4.6", "size": 101}}}

    assert "大小" in basecontent.canonical_mismatch(cfg, "4.6")


def test_missing_identity_does_not_block(baseline_dir: Path) -> None:
    """没导入本体、或指纹字段缺失时不阻断——不能凭空禁用。"""
    _write_zip(baseline_dir / "canonical_sources.zip", {"scripts/game.gd": b"ok"})
    assert basecontent.canonical_mismatch({"baselines": {}}, "4.6") == ""
    assert basecontent.canonical_mismatch(None, "4.6") == ""
