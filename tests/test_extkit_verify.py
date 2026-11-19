"""开发者校验准入工具链回归。

重点有两块：
1. ``jjm_pack.py verify`` 能把真正会被 strict_verify 拒装的情况拦下来；
2. **不能把今晚实测跑通的存量写法误杀**——schema 1/2、改基线脚本不自带原文、
   method_hooks 靠运行期注册、同包内同锚点，这四种都只能给提醒。
   2026-11-04 用 19 个扩展共存环境校准过，别把「建议」升级成硬约束。

同时保证作者端 ``verify`` 与工程端 ``verify_admission`` 结论一致。
"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
EXAMPLES = ROOT / "examples" / "minimal-combination"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def jjm():
    return _module("jjm_pack_verify_test", TOOLS / "jjm_pack.py")


@pytest.fixture(scope="module")
def admission():
    return _module("verify_admission_test", TOOLS / "verify_admission.py")


def _base(ext_id: str, **changes) -> dict:
    data = {
        "schema": 3,
        "id": ext_id,
        "name": ext_id,
        "version": "1.0.0",
        "kind": "extension",
        "baseline": "jjxf.v1.45",
        "engine": {"line": "4.6"},
        "scope": "singleplayer",
        "files": [
            {
                "source": f"mods/{ext_id}/README.md",
                "target": f"mods/{ext_id}/README.md",
            }
        ],
    }
    data.update(changes)
    return data


def _zip(tmp_path: Path, name: str, data: dict, extra: dict | None = None) -> Path:
    path = tmp_path / f"{name}.zip"
    written: set[str] = set()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("extension.json", json.dumps(data, ensure_ascii=False, indent=2))
        written.add("extension.json")
        for rec in data.get("files") or []:
            src = rec.get("source")
            if src and src not in written:
                z.writestr(src, "# placeholder")
                written.add(src)
        for name_in, content in (extra or {}).items():
            if name_in not in written:
                z.writestr(name_in, content)
    return path


def _codes(report) -> list[str]:
    return [f.code for f in report.findings if f.level == "error"]


# ---------------------------------------------------------------- 最小组合


def test_minimal_combination_files_exist_and_pack_clean(jjm, tmp_path):
    """随工具包发布的两个最小包必须真实存在、且能过 verify。"""
    for name in ("me.minimal-1.0.0.zip", "me.minimal_companion-1.0.0.zip"):
        path = EXAMPLES / name
        assert path.is_file(), f"缺最小组合样例：{path}"
        with zipfile.ZipFile(path) as z:
            assert "extension.json" in z.namelist()
        report = jjm.verify_package(path)
        assert report.ok, [str(f) for f in report.errors]


def test_companion_with_minimal_is_admitted(jjm, tmp_path):
    a = EXAMPLES / "me.minimal-1.0.0.zip"
    b = EXAMPLES / "me.minimal_companion-1.0.0.zip"
    report = jjm.verify_package(b, [a])
    assert report.ok
    assert not report.warnings, [str(f) for f in report.warnings]


def test_minimal_combination_passes_launcher_truth(admission, tmp_path):
    a = EXAMPLES / "me.minimal-1.0.0.zip"
    b = EXAMPLES / "me.minimal_companion-1.0.0.zip"
    result = admission.admit_one(b, [a], tmp_path)
    assert result["verdict"] == "ADMITTED", result["reasons"]
    assert result["combination"]["errors"] == []
    assert result["policy"]["pending_count"] == 0


# ---------------------------------------------------------------- 真正会拒装的


def test_cross_package_position_overlap_rejected(jjm, admission, tmp_path):
    a = _zip(tmp_path, "a", _base("me.a"))
    b = _zip(tmp_path, "b", _base(
        "me.b",
        files=[{"source": "mods/me.b/README.md", "target": "mods/me.a/README.md"}],
    ))
    report = jjm.verify_package(b, [a])
    assert "ADM-07" in _codes(report)

    truth = admission.admit_one(b, [a], tmp_path)
    assert truth["verdict"] == "REJECTED"
    assert truth["combination"]["errors"]


def test_takeover_missing_requires_and_expect_rejected(jjm, tmp_path):
    data = _base(
        "me.takeover",
        data_patches=[
            {
                "target": "data/items.json",
                "operations": [
                    {
                        "op": "replace",
                        "path": "/catalog/1/name",
                        "value": "x",
                        "overrides": ["me.a"],
                    }
                ],
            }
        ],
    )
    pkg = _zip(tmp_path, "takeover", data)
    codes = _codes(jjm.verify_package(pkg))
    assert "ADM-05" in codes  # 点名的 id 不在 requires
    assert "ADM-06" in codes  # 缺 expect


def test_missing_dependency_rejected_when_with_given(jjm, tmp_path):
    pkg = _zip(tmp_path, "needs", _base(
        "me.needs", requires={"me.missing": ">=1.0.0,<2.0.0"}
    ))
    other = _zip(tmp_path, "unrelated", _base("me.unrelated"))
    assert "ADM-08" in _codes(jjm.verify_package(pkg, [other]))


def test_static_hard_error_rejected(jjm, tmp_path):
    pkg = _zip(tmp_path, "badid", _base("BAD ID !!"))
    assert "ADM-02" in _codes(jjm.verify_package(pkg))


def test_bundle_with_bad_member_rejected(jjm, tmp_path):
    good = _zip(tmp_path, "good", _base("me.good"))
    bad = _zip(tmp_path, "bad", _base("BAD ID !!"))
    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(good, "good.zip")
        z.write(bad, "bad.zip")
    codes = _codes(jjm.verify_package(bundle))
    assert "ADM-09" in codes


def test_online_capability_rejected(jjm, tmp_path):
    pkg = _zip(tmp_path, "online", _base("me.online", capabilities=["multiplayer"]))
    codes = _codes(jjm.verify_package(pkg))
    assert "ADM-02" in codes or "ADM-10" in codes  # check 的 NET-00 归入 ADM-02


# ---------------------------------------------------------------- 不许误杀存量写法


@pytest.mark.parametrize("schema", [1, 2])
def test_legacy_schema_is_warning_not_error(jjm, tmp_path, schema):
    pkg = _zip(tmp_path, f"s{schema}", _base("me.legacy", schema=schema))
    report = jjm.verify_package(pkg)
    assert report.ok, [str(f) for f in report.errors]
    assert any(f.code == "ADM-01" and f.level == "warn" for f in report.findings)


def test_text_patch_on_baseline_script_is_not_fatal(jjm, tmp_path):
    """发布版启动器带 canonical_sources.zip，SELF-01 是旧口径，只能提醒。"""
    data = _base("me.patch", text_patches=[
        {"target": "scripts/player.gd", "find": "func _ready():", "replace": "func _ready():\n\tpass",
         "expect_count": 1}
    ])
    pkg = _zip(tmp_path, "patch", data)
    report = jjm.verify_package(pkg)
    assert "SELF-01" not in _codes(report)
    assert report.ok, [str(f) for f in report.errors]


def test_pack_allows_baseline_text_patch(jjm, tmp_path):
    """pack 与 verify 同口径：SELF-01 不能把「规范允许的包」卡在打包这一步。

    verify 早就把 SELF-01 降为提醒了，pack 之前却按 check 的严格口径中止，
    结果是「规范允许、工具不让打包」。2026-11-05 修掉。
    """
    folder = tmp_path / "packme"
    (folder / "mods" / "me.packme").mkdir(parents=True)
    (folder / "mods" / "me.packme" / "README.md").write_text("# x", encoding="utf-8")
    data = _base(
        "me.packme",
        text_patches=[
            {
                "target": "scripts/player.gd",
                "find": "func _ready():",
                "replace": "func _ready():\n\tpass",
                "expect_count": 1,
            }
        ],
    )
    (folder / "extension.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out = tmp_path / "me.packme-1.0.0.zip"
    assert jjm.main(["pack", str(folder), "-o", str(out)]) == 0
    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        assert "extension.json" in zf.namelist()
        assert "mods/me.packme/README.md" in zf.namelist()


def test_pack_still_blocks_real_errors(jjm, tmp_path):
    """放宽 SELF-01 不等于放过真错：清单 id 不合规仍必须中止打包。"""
    folder = tmp_path / "bad"
    folder.mkdir()
    data = _base("BAD ID !!")
    (folder / "extension.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out = tmp_path / "bad.zip"
    assert jjm.main(["pack", str(folder), "-o", str(out)]) == 1
    assert not out.is_file()


def test_runtime_registered_hooks_are_not_fatal(jjm, tmp_path):
    data = _base("me.hooks", method_hooks=[
        {"target": "scripts/game.gd", "method": "_ready"},
        {"target": "scripts/game.gd", "method": "tick"},
    ])
    pkg = _zip(tmp_path, "hooks", data)
    report = jjm.verify_package(pkg)
    assert "ADM-03" not in _codes(report)
    assert report.ok
    assert any(f.code == "ADM-03" and f.level == "warn" for f in report.findings)


def test_same_anchor_inside_one_package_is_not_fatal(jjm, tmp_path):
    """同包内两条补丁同锚点由合并层按声明顺序叠加，不是跨包冲突。"""
    data = _base("me.twin", text_patches=[
        {"target": "scripts/foo.gd", "find": "func tick():", "replace": "func tick():\n\tpass",
         "expect_count": 1},
        {"target": "scripts/foo.gd", "find": "func tick():", "replace": "func tick():\n\tpass",
         "expect_count": 1},
    ])
    pkg = _zip(tmp_path, "twin", data, extra={"scripts/foo.gd": "func tick():\n\tpass\n"})
    report = jjm.verify_package(pkg)
    assert "ADM-04" not in _codes(report)
    assert report.ok


def test_same_id_old_version_is_not_a_conflict(jjm, tmp_path):
    """升版本重装同一个 id，不该被当成抢位置。"""
    old = _zip(tmp_path, "old", _base("me.same", version="0.9.0"))
    new = _zip(tmp_path, "new", _base("me.same", version="1.0.0"))
    report = jjm.verify_package(new, [old])
    assert "ADM-07" not in _codes(report)


# ---------------------------------------------------------------- 两套工具一致


@pytest.mark.parametrize(
    "make,expected",
    [
        ("clean", "ADMITTED"),
        ("overlap", "REJECTED"),
    ],
)
def test_author_tool_and_launcher_truth_agree(jjm, admission, tmp_path, make, expected):
    a = _zip(tmp_path, "base", _base("me.a"))
    if make == "clean":
        b = _zip(tmp_path, "clean", _base("me.b"))
    else:
        b = _zip(tmp_path, "overlap", _base(
            "me.c",
            files=[{"source": "mods/me.c/README.md", "target": "mods/me.a/README.md"}],
        ))
    author = "ADMITTED" if jjm.verify_package(b, [a]).ok else "REJECTED"
    truth = admission.admit_one(b, [a], tmp_path)["verdict"]
    assert author == truth == expected


def test_cli_verify_json_and_exit_code(jjm, tmp_path, capsys):
    pkg = _zip(tmp_path, "cli", _base("me.cli"))
    rc = jjm.main(["verify", str(pkg), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"] == "ADMITTED"
    assert payload["mode"] == "strict"

    bad = _zip(tmp_path, "clibad", _base("BAD ID !!"))
    rc_bad = jjm.main(["verify", str(bad), "--json"])
    payload_bad = json.loads(capsys.readouterr().out)
    assert rc_bad == 1
    assert payload_bad["verdict"] == "REJECTED"


def test_missing_dependency_is_note_when_no_with(admission, tmp_path):
    """没带 --with 时缺依赖应标「未验证」，不能判死。"""
    pkg = _zip(tmp_path, "needs", _base(
        "me.needs", requires={"me.missing": ">=1.0.0,<2.0.0"}
    ))
    result = admission.admit_one(pkg, [], tmp_path)
    assert result["verdict"] == "ADMITTED", result["reasons"]
    assert result["combination"]["skipped"]
    assert result["notes"]
