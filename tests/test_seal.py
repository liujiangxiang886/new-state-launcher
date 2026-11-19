"""打包工具的 ``seal``：验证记录的产出一侧。

与 ``tests/test_inspect.py`` 的分工：那边测**判读**（启动器读到记录后怎么判），
这里测**产出**（工具怎么写出合法记录）。

最关键的一条是**两端算法必须一致**：工具算的 ``content_digest`` 和启动器算的
必须逐位相同，否则作者 seal 完之后启动器立刻判 ``stale`` —— 那种 bug 极难
发现，因为两边「看起来都对」。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tools")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import jjm_pack as J  # noqa: E402

from app.core.ext import verify_mark as vm  # noqa: E402


@pytest.fixture
def pkg47(tmp_path: Path) -> Path:
    """一个最小的 4.7 扩展目录。"""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "extension.json").write_text(
        json.dumps(
            {
                "schema": 3,
                "id": "test.seal",
                "name": "seal 测试",
                "version": "1.0.0",
                "kind": "extension",
                "baseline": {"id": "jjxf.v4.7", "engine": {"line": "4.7"}},
                "files": [],
                "data_patches": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "mod.gd").write_text("extends Node\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------- 线解析


def test_resolve_line_prefers_forced() -> None:
    assert J._resolve_line("jjxf.v1.45", None, "4.7") == "4.7"


def test_resolve_line_by_declared_engine() -> None:
    assert J._resolve_line(None, "4.7", None) == "4.7"
    assert J._resolve_line(None, "4.6", None) == "4.6"


def test_resolve_line_by_baseline_alias() -> None:
    """老包常见只写 baseline 不写 engine，只能靠 id 认线。"""
    assert J._resolve_line("jjxf.v4.7", None, None) == "4.7"
    assert J._resolve_line("v4.7", None, None) == "4.7"
    assert J._resolve_line("jjxf.v1.45", None, None) == "4.6"


def test_resolve_line_unknown() -> None:
    assert J._resolve_line("completely.unknown", None, None) is None


# ---------------------------------------------------------------- 两端算法一致


def test_content_digest_matches_launcher_side(pkg47: Path) -> None:
    """**工具算的指纹必须与启动器算的逐位相同。**

    这是整条链能成立的唯一前提。算法在两边各写一份（工具要能独立发给
    作者，不能 import 启动器源码），所以必须有这个测试把它们钉在一起。
    """
    tool_side = J._content_digest_of(pkg47)
    launcher_side = vm.content_digest(pkg47)
    assert tool_side == launcher_side, (
        "打包工具与启动器的 content_digest 算法已漂移 —— "
        "作者 seal 完会被立刻判 stale"
    )


def test_content_digest_ignores_record_file(pkg47: Path) -> None:
    """记录文件不参与自身指纹，否则写完就对不上。"""
    before_tool = J._content_digest_of(pkg47)
    before_launcher = vm.content_digest(pkg47)
    (pkg47 / "verification.json").write_text('{"schema":1}', encoding="utf-8")
    assert J._content_digest_of(pkg47) == before_tool
    assert vm.content_digest(pkg47) == before_launcher


# ---------------------------------------------------------------- seal


def test_seal_writes_record_and_leaves_manifest(pkg47: Path) -> None:
    """seal 写独立记录文件，**不动 extension.json**。

    参考启动器对清单字段是严格白名单（``tools/compat.py:293``）：
    多一个字段就拒接。所以记录必须是独立文件。
    """
    manifest_before = (pkg47 / "extension.json").read_text(encoding="utf-8")

    report, written = J.seal_package(pkg47)
    assert written is not None and written.name == "verification.json"
    assert report.ok, [f.message for f in report.errors]

    manifest_after = (pkg47 / "extension.json").read_text(encoding="utf-8")
    assert manifest_before == manifest_after, "清单必须一个字节都不动"

    record = json.loads(written.read_text(encoding="utf-8"))
    assert record["schema"] == 1
    assert record["line"] == "4.7"
    assert record["code_digest"] and record["content_digest"]
    assert record["tool"] == "jjm_pack"


def test_seald_package_judges_verified(pkg47: Path) -> None:
    """seal 完，启动器侧必须判 ``verified``。

    这是端到端闭环：工具产出的记录要能被启动器的判读逻辑接受。
    """
    cfg = {"baselines": {}}
    J.seal_package(pkg47)
    verdict = vm.judge(cfg, "4.7", "test.seal", pkg47)
    assert verdict.state == vm.VERIFIED
    assert verdict.source == "package"


def test_seal_detects_content_change_afterwards(pkg47: Path) -> None:
    """seal 之后作者又改了代码 → 启动器侧判 stale。

    这条正是「只绑 code_digest 不够」的实证：游戏基线没变，
    变的是包自己。
    """
    cfg = {"baselines": {}}
    J.seal_package(pkg47)
    (pkg47 / "mod.gd").write_text("extends Node\n# 改了一行\n", encoding="utf-8")
    verdict = vm.judge(cfg, "4.7", "test.seal", pkg47)
    assert verdict.state == vm.STALE


def test_seal_refuses_without_code_digest(tmp_path: Path, monkeypatch) -> None:
    """拿不到基线代码层指纹时**不出记录**。

    出了也只会被判 stale，反而让作者以为「已经验过了」。
    """
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "extension.json").write_text(
        json.dumps(
            {
                "schema": 3,
                "id": "test.seal",
                "name": "t",
                "version": "1.0.0",
                "baseline": {"id": "jjxf.v4.7", "engine": {"line": "4.7"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(J, "_code_digest_for", lambda line: "")
    report, written = J.seal_package(root)
    assert written is None
    assert any(f.code == "SEAL-03" for f in report.errors)


def test_seal_refuses_missing_manifest(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    report, written = J.seal_package(root)
    assert written is None
    assert any(f.code == "SEAL-01" for f in report.errors)


def test_seal_zip_writes_side_file(pkg47: Path, tmp_path: Path) -> None:
    """zip 形态不覆盖原包，另存 ``*.sealed.zip``。

    覆盖原 zip 等于销毁作者手上那份，出问题没法对比。
    """
    src = tmp_path / "pkg.zip"
    J.pack_dir(pkg47, src)
    report, written = J.seal_package(src)
    assert written is not None
    assert written.name.endswith(".sealed.zip")
    assert src.is_file(), "原包必须保留"
    assert not written.with_name("verification.json").exists()