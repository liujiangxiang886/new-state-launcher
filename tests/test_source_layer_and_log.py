"""源码层解析 + 日志折叠的回归（2026-11-13 玩家日志事故）。

三条被钉住的行为：

1. ``smoke.default_baseline(line)`` 必须**按线**返回基线 id。它曾经写死
   1.45，于是「导入 4.7 本体」的后续冒烟拿 1.45 的 exe 去验，报
   ``基线文件缺失：game/mchGame.exe (baseline='jjxf.v1.45')``。
2. 4.7 的随包源码 ``baseline/sources-4.7.zip`` 必须真的被运行时用上——
   发布包一直带着它，但 ``canonical_zip`` 只找 ``canonical_sources-*.zip``，
   于是「本体不带 .gd 的导出构建」上所有文本补丁都取不到原文。
   同时：本体自带 ``.gd`` 时**以本体为准**（那是玩家这台机器的真实版本）。
3. 日志的「整块重复」不能折叠单行块，否则每次安装都会重复的进度行
   （``[ 45%] 生成覆盖层 pck``）被整片吞掉，日志只剩「（整块重复 N 次）」。
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app.core import config as cfg_mod  # noqa: E402
from app.core import smoke  # noqa: E402
from app.core.ext.basecontent import BaseContent  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402


# ---------------------------------------------------------------- 基线选择

def test_default_baseline_follows_line() -> None:
    assert smoke.default_baseline("4.6") == "jjxf.v1.45"
    assert smoke.default_baseline("4.7") == "jjxf.v4.7"
    # 未知线退回内置线，而不是抛异常
    assert smoke.default_baseline("9.9") == cfg_mod.BUILTIN_BASELINE


# ---------------------------------------------------------------- 源码层

@pytest.fixture
def bundle(monkeypatch, tmp_path: Path) -> Path:
    """把 ``paths.BASELINE`` 指到临时目录，放一份假的 4.7 随包源码。"""
    from app.core import paths

    base = tmp_path / "baseline"
    base.mkdir()
    with zipfile.ZipFile(base / "sources-4.7.zip", "w") as z:
        z.writestr("scripts/game.gd", "extends Node\n# from bundle\n")
        z.writestr("scripts/hud.gd", "extends Control\n# from bundle\n")
    monkeypatch.setattr(paths, "BASELINE", base, raising=False)
    return base


def _make_baseline_pck(
    path: Path, *, with_source: bool, extra_gdc: tuple[str, ...] = ()
) -> Path:
    """造一个 4.7 基线 pck：``with_source=False`` 时只有 .gdc（导出构建）。"""
    w = PckWriter(path, line="4.7")
    if with_source:
        w.add_text("scripts/game.gd", "extends Node\n# from pck\n")
    else:
        w.add_bytes("scripts/game.gdc", b"\x00gdc-bytes")
    for rel in extra_gdc:
        w.add_bytes(rel, b"\x00gdc-bytes")
    w.add_text("data/npcs.json", "{}")
    w.commit()
    return path


def _cfg_for(pck: Path) -> dict:
    return {
        "baselines": {
            "jjxf.v4.7": {
                "engine": "4.7",
                "kind": "pck",
                "path": str(pck),
                "entry_count": 3,
                "size": pck.stat().st_size,
            }
        },
        "sources": {"reversed_project": ""},
        "runtime_sources": {},
    }


def test_bundled_sources_used_when_body_has_no_gd(bundle, tmp_path: Path) -> None:
    """本体只有 .gdc（导出构建）→ 用随包 sources-4.7.zip 兜底。"""
    pck = _make_baseline_pck(tmp_path / "body.pck", with_source=False)
    bc = BaseContent(_cfg_for(pck), "4.7")
    raw = bc.read("scripts/game.gd")
    assert raw is not None and b"from bundle" in raw
    assert bc.origin("scripts/game.gd") == "canonical"
    # .gdc 存在但 .gd 没有 → 诊断文案能判断出来
    assert bc.compiled_only("scripts/game.gd") is True


def test_body_source_wins_over_bundle(bundle, tmp_path: Path) -> None:
    """本体自带 .gd → 以本体为准，不被随包源码覆盖。"""
    pck = _make_baseline_pck(tmp_path / "body2.pck", with_source=True)
    bc = BaseContent(_cfg_for(pck), "4.7")
    raw = bc.read("scripts/game.gd")
    assert raw is not None and b"from pck" in raw
    assert bc.origin("scripts/game.gd") == "baseline"
    assert bc.compiled_only("scripts/game.gd") is False


def test_canonical_zip_finds_47_bundle(bundle) -> None:
    from app.core.ext.basecontent import canonical_zip

    got = canonical_zip("4.7")
    assert got is not None and got.name == "sources-4.7.zip"
    # 4.6 不应回落到 4.7 的源码包
    assert canonical_zip("4.7") != canonical_zip("4.6")


# ------------------------------------------------------------ 预检端到端

def _make_patch_package(path: Path, *, find: str) -> Path:
    """造一个只有一条文本补丁的最小扩展包（``extension.json`` 在根）。"""
    import json

    data = {
        "schema": 3,
        "id": "qa.patch_only",
        "name": "QA 补丁包",
        "version": "1.0.0",
        "kind": "extension",
        "priority": 50,
        "baseline": {"id": "jjxf.v4.7", "engine": {"line": "4.7"}},
        "engine": {"line": "4.7"},
        "requires": [],
        "conflicts": [],
        "claims": [],
        "capabilities": [],
        "files": [],
        "data_patches": [],
        "text_patches": [
            {"target": "scripts/game.gd", "find": find, "replace": "# patched", "expect_count": 1}
        ],
        "method_hooks": [],
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("extension.json", json.dumps(data, ensure_ascii=False))
    return path


@pytest.fixture
def fake_index(monkeypatch):
    """预检要先读「基线路径索引」；临时 pck 没有索引，这里直接给一份。

    索引只影响「覆盖 / 新增」的判定，本组测试关心的是补丁取原文，
    所以给一个最小集合就够。
    """
    from app.core.ext import compat as compat_mod

    monkeypatch.setattr(
        compat_mod, "path_set", lambda line: frozenset({"scripts/game.gd", "data/npcs.json"})
    )


def test_probe_hits_bundled_source_on_source_less_body(bundle, fake_index, tmp_path: Path) -> None:
    """本体只有 .gdc 时，预检必须能从随包源码取到原文 —— 这就是玩家
    「所有补丁都报目标基线里没有这个文件」的那条路径。"""
    from app.core.ext import compat

    pck = _make_baseline_pck(tmp_path / "body3.pck", with_source=False)
    pkg = _make_patch_package(tmp_path / "mod.zip", find="# from bundle")
    r = compat.probe(pkg, "4.7", _cfg_for(pck))
    assert r.sources_missing is False
    assert (r.patch_ok, r.patch_total) == (1, 1)
    assert r.verdict == compat.COMPATIBLE, r.failures


def test_probe_explains_missing_sources(bundle, fake_index, tmp_path: Path) -> None:
    """随包源码也对不上时，至少要说清「只有 .gdc 字节码」，别只报没有文件。"""
    from app.core.ext import compat

    pck = _make_baseline_pck(tmp_path / "body4.pck", with_source=False)
    pkg = _make_patch_package(tmp_path / "mod2.zip", find="# 不存在的锚点")
    r = compat.probe(pkg, "4.7", _cfg_for(pck))
    # 锚点不在随包源码里 → 命中 0 处；但文件是取到了的
    assert r.patch_ok == 0
    assert any("命中 0 处" in f for f in r.failures), r.failures

    # 目标脚本连随包源码都没有、本体只有 .gdc 时，才是「没有这个文件」
    # + 「只有 .gdc 字节码」这条诊断。
    import json

    pck2 = _make_baseline_pck(
        tmp_path / "body5.pck", with_source=False, extra_gdc=("scripts/not_there.gdc",)
    )
    with zipfile.ZipFile(tmp_path / "mod3.zip", "w") as z:
        z.writestr(
            "extension.json",
            json.dumps(
                {
                    "schema": 3,
                    "id": "qa.patch_only2",
                    "name": "QA",
                    "version": "1.0.0",
                    "kind": "extension",
                    "baseline": {"id": "jjxf.v4.7", "engine": {"line": "4.7"}},
                    "engine": {"line": "4.7"},
                    "files": [],
                    "data_patches": [],
                    "text_patches": [
                        {"target": "scripts/not_there.gd", "find": "x", "expect_count": 1}
                    ],
                },
                ensure_ascii=False,
            ),
        )
    r2 = compat.probe(tmp_path / "mod3.zip", "4.7", _cfg_for(pck2))
    assert r2.sources_missing is True
    assert any("目标基线里没有这个文件" in f for f in r2.failures), r2.failures
    assert any(".gdc" in f for f in r2.failures), r2.failures


# ---------------------------------------------------------------- 日志折叠

@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_log_does_not_fold_single_line_progress(qapp) -> None:
    from app.ui.pyside.widgets import LogView

    view = LogView()
    # 三个包各自的进度行：同一行文本重复出现，但它们是**不同步骤**的信息
    for _ in range(3):
        view.log("[ 45%] 生成覆盖层 pck", "dim")
        view.log("[ 94%] 写入注册表", "dim")
    view.flush_now()
    text = view.toPlainText()
    assert "整块重复" not in text
    assert text.count("生成覆盖层 pck") == 3


def test_log_folds_repeated_multiline_error(qapp) -> None:
    from app.ui.pyside.widgets import LogView

    view = LogView()
    for _ in range(3):
        view.log("SCRIPT ERROR: boom")
        view.log("   at res://scripts/a.gd:12")
        view.log("   at res://scripts/a.gd:20")
        view.log("另一个步骤")     # 界定上一块的边界
        view.flush_now()           # 让缓冲定案
    view.flush_now()
    text = view.toPlainText()
    assert "整块重复" in text
    # 多行报错块只留一行标记 + 每次的「另一个步骤」
    assert text.count("SCRIPT ERROR: boom") == 1
    assert text.count("另一个步骤") == 3


# ---------------------------------------------------------------- 合集包

def test_combo_zip_asks_to_unzip(tmp_path: Path) -> None:
    """「zip 里装着一堆 zip」是最常见的误拖，报错必须给出下一步动作。"""
    from app.core.ext import manifest as ext_manifest

    pkg = tmp_path / "4.7 mod.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        for name in ("a.zip", "b.zip", "c.zip"):
            z.writestr(name, b"not a real package")
    with pytest.raises(ext_manifest.ManifestError) as excinfo:
        ext_manifest.load(pkg)
    assert "解压" in str(excinfo.value)
    assert "3 个扩展包" in str(excinfo.value)
