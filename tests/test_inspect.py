"""检查器与验证标志。

这组测试守住 S3 的**四条设计约束**，每条都对应一个真实的坑：

1. **宽松档也要检查** —— 「查出来 + 告诉玩家 + 放行」才有意义；
2. **查不了不能算通过** —— 基线读不到时报「通过」是假安全感，比不查更坏；
3. **验证记录不能写进 extension.json** —— 参考启动器是严格白名单，
   多一个字段就拒接（``tools/compat.py:293``）；
4. **标志必须绑两个指纹** —— 只绑 code_digest 时，作者改完自己的 mod
   代码后旧记录仍然有效，是个真漏洞。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import paths  # noqa: E402
from app.core.ext import inspect as insp  # noqa: E402
from app.core.ext import verify_mark as vm  # noqa: E402


# ---------------------------------------------------------------- 检查器


def test_findings_are_structured() -> None:
    """每条发现都要有 check / severity / code / message —— 界面和存档靠它。"""
    f = insp.Finding(
        check="anchor", severity=insp.ERROR, code="anchor.miss", message="找不到"
    )
    d = f.as_dict()
    assert d["check"] == "anchor" and d["severity"] == "error"
    assert d["code"] == "anchor.miss" and d["message"]


def test_report_ok_only_when_no_errors() -> None:
    r = insp.Report(line="4.7")
    r.ran = ["manifest"]
    assert r.ok is True
    r.findings.append(
        insp.Finding(check="manifest", severity=insp.WARN, code="w", message="提示")
    )
    assert r.ok is True, "warn 不影响通过"
    r.findings.append(
        insp.Finding(check="anchor", severity=insp.ERROR, code="e", message="错")
    )
    assert r.ok is False


def test_passed_checks_excludes_failed() -> None:
    """有 error 的检查项不能进 ``passed`` —— 否则记录会撒谎。"""
    r = insp.Report(line="4.7")
    r.ran = ["manifest", "anchor", "units"]
    r.findings.append(
        insp.Finding(check="anchor", severity=insp.ERROR, code="e", message="错")
    )
    assert r.passed_checks() == ["manifest", "units"]


def test_unavailable_is_not_counted_as_passed() -> None:
    """**没跑成的检查项不能算通过。**

    基线内容读不到时（没导入本体），anchor 一条都验不了。这时候如果
    报「通过」，玩家会以为「检查过了没问题」，实际什么都没查。
    """
    r = insp.Report(line="4.7")
    r.ran = ["manifest"]          # anchor / hooks / members 都没跑
    assert r.unavailable() == ["anchor", "units", "hooks", "members"]
    assert "anchor" not in r.passed_checks()
    assert "未能验证" in r.summary()


def test_manifest_schema_rejected() -> None:
    """schema 不认识 → error（装上也跑不起来）。"""
    r = insp.inspect({}, "4.6", [("bad.ext", {"schema": 99})])
    codes = [f.code for f in r.errors]
    assert "manifest.schema" in codes


def test_manifest_unknown_field_is_info_only() -> None:
    """未知字段只提示，不判不合格。

    新版本启动器加了字段，不该让按老规范写的包不能用。
    """
    r = insp.inspect({}, "4.6", [("old.ext", {"schema": 3, "some_new_field": 1})])
    assert not r.errors
    assert any(f.code == "manifest.unknown_field" and f.severity == insp.INFO for f in r.findings)


def test_members_invalid_name_is_error() -> None:
    """注入非法标识符 → Parse Error → 黑屏，必须是 error。"""
    r = insp.inspect(
        {},
        "4.6",
        [
            (
                "bad.ext",
                {
                    "schema": 3,
                    "script_members": [{"script": "scripts/game.gd", "name": "1bad-name"}],
                },
            )
        ],
    )
    assert any(f.code == "members.invalid_name" for f in r.errors)


def test_units_overlap_is_reported_as_warn() -> None:
    """两个包改同一处 → warn（不是 error：交处置面板，不是直接判死）。"""
    payload = [
        (
            "a",
            {
                "schema": 3,
                "data_patches": [
                    {"target": "data/e.json", "ops": [{"op": "replace", "path": "/x", "value": 1}]}
                ],
            },
        ),
        (
            "b",
            {
                "schema": 3,
                "data_patches": [
                    {"target": "data/e.json", "ops": [{"op": "replace", "path": "/x", "value": 2}]}
                ],
            },
        ),
    ]
    r = insp.inspect({}, "4.6", payload)
    overlaps = [f for f in r.findings if f.code == "units.overlap"]
    assert overlaps, "同一处必须被发现"
    assert overlaps[0].severity == insp.WARN
    assert set(overlaps[0].ids) == {"a", "b"}


# ---------------------------------------------------------------- 验证标志


@pytest.fixture
def ext_root(tmp_path: Path) -> Path:
    """一个「解包后的扩展目录」。"""
    root = tmp_path / "packs" / "test.ext"
    root.mkdir(parents=True)
    (root / "extension.json").write_text(
        json.dumps({"schema": 3, "id": "test.ext", "name": "t"}), encoding="utf-8"
    )
    (root / "src").mkdir()
    (root / "src" / "a.gd").write_text("extends Node\n", encoding="utf-8")
    return root


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # conftest 的 autouse 已经建过 tmp_path/config，这里只确保它存在
    cfgdir = tmp_path / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "CONFIG_DIR", cfgdir)
    return cfgdir


def test_absent_when_no_record(ext_root: Path, isolated_config: Path) -> None:
    v = vm.judge({}, "4.7", "test.ext", ext_root, code_digest="code1")
    assert v.state == vm.ABSENT
    assert v.source == "none"


def test_verified_when_both_digests_match(ext_root: Path, isolated_config: Path) -> None:
    content = vm.content_digest(ext_root)      # 必须在写记录**之前**算
    vm.write_record(
        ext_root,
        code_digest="code1",
        content_digest_value=content,
        line="4.7",
        passed=["manifest", "anchor"],
    )
    v = vm.judge({}, "4.7", "test.ext", ext_root, code_digest="code1")
    assert v.state == vm.VERIFIED
    assert v.source == "package"


def test_record_file_does_not_affect_own_digest(ext_root: Path, isolated_config: Path) -> None:
    """写了记录之后，content_digest 不能变 —— 否则永远对不上。"""
    before = vm.content_digest(ext_root)
    vm.write_record(
        ext_root,
        code_digest="code1",
        content_digest_value=before,
        line="4.7",
        passed=["manifest"],
    )
    after = vm.content_digest(ext_root)
    assert before == after, "记录文件必须被排除在自身指纹之外"


def test_stale_when_code_changed(ext_root: Path, isolated_config: Path) -> None:
    """游戏代码层更新 → 旧记录过期。"""
    content = vm.content_digest(ext_root)
    vm.write_record(
        ext_root,
        code_digest="code1",
        content_digest_value=content,
        line="4.7",
        passed=["manifest"],
    )
    v = vm.judge({}, "4.7", "test.ext", ext_root, code_digest="code2")
    assert v.state == vm.STALE
    assert "代码层已更新" in v.reason


def test_stale_when_mod_content_changed(ext_root: Path, isolated_config: Path) -> None:
    """**作者改完自己的 mod 代码 → 旧记录必须失效。**

    这是「只绑 code_digest」的真漏洞：游戏基线没变，但 mod 内容变了，
    旧记录不能继续算有效。所以必须再绑一个 content_digest。
    """
    content = vm.content_digest(ext_root)
    vm.write_record(
        ext_root,
        code_digest="code1",
        content_digest_value=content,
        line="4.7",
        passed=["manifest"],
    )
    # 作者改了自己的脚本
    (ext_root / "src" / "a.gd").write_text("extends Node\n# changed\n", encoding="utf-8")

    v = vm.judge({}, "4.7", "test.ext", ext_root, code_digest="code1")
    assert v.state == vm.STALE, "mod 内容变了，记录必须失效"
    assert "内容已改动" in v.reason


def test_local_record_is_equivalent_to_package_record(
    ext_root: Path, isolated_config: Path
) -> None:
    """本地记录与包内记录同权。

    没有这一条，4.7 首发的 19 个扩展（作者都还没工具出记录）会全部被判
    ``absent``，玩家装上一堆东西结果全被剔除。
    """
    content = vm.content_digest(ext_root)
    vm.put_local(
        "test.ext",
        line="4.7",
        code_digest="code1",
        content_digest_value=content,
        passed=["manifest"],
    )
    v = vm.judge({}, "4.7", "test.ext", ext_root, code_digest="code1")
    assert v.state == vm.VERIFIED
    assert v.source == "local"


def test_local_record_expires_with_code(ext_root: Path, isolated_config: Path) -> None:
    content = vm.content_digest(ext_root)
    vm.put_local(
        "test.ext",
        line="4.7",
        code_digest="code1",
        content_digest_value=content,
        passed=["manifest"],
    )
    v = vm.judge({}, "4.7", "test.ext", ext_root, code_digest="code9")
    assert v.state == vm.STALE


def test_no_code_digest_never_verified(ext_root: Path, isolated_config: Path) -> None:
    """没有代码层指纹时不能判 verified —— 「无法判定」不是「通过」。"""
    content = vm.content_digest(ext_root)
    vm.write_record(
        ext_root,
        code_digest="code1",
        content_digest_value=content,
        line="4.7",
        passed=["manifest"],
    )
    v = vm.judge({}, "4.7", "test.ext", ext_root, code_digest="")
    assert v.state != vm.VERIFIED


def test_record_never_touches_manifest(ext_root: Path, isolated_config: Path) -> None:
    """**验证记录绝不能写进 extension.json。**

    参考启动器（``tools/compat.py:293``）对清单字段是严格白名单：

        require(not set(m) - known, ...)

    往清单里加任何新字段，那个启动器会直接拒绝接入。历史上作者署名字段
    已经踩过一次这个坑。
    """
    before = (ext_root / "extension.json").read_text(encoding="utf-8")
    content = vm.content_digest(ext_root)
    vm.write_record(
        ext_root,
        code_digest="code1",
        content_digest_value=content,
        line="4.7",
        passed=["manifest"],
    )
    after = (ext_root / "extension.json").read_text(encoding="utf-8")
    assert before == after, "清单必须一个字节都不动"
    assert (ext_root / vm.RECORD_NAME).is_file(), "记录应该落在独立文件里"


def test_record_rejects_bad_schema(ext_root: Path, isolated_config: Path) -> None:
    (ext_root / vm.RECORD_NAME).write_text(
        json.dumps({"schema": 99, "code_digest": "x", "content_digest": "y"}),
        encoding="utf-8",
    )
    assert vm.read_record(ext_root) is None


def test_record_requires_both_digests(ext_root: Path, isolated_config: Path) -> None:
    """缺任一指纹的记录视为无效（不能只绑一个）。"""
    (ext_root / vm.RECORD_NAME).write_text(
        json.dumps({"schema": 1, "code_digest": "x"}), encoding="utf-8"
    )
    assert vm.read_record(ext_root) is None