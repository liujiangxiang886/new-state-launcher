"""自定义游戏线：exe / pck 两种形态、参数配置、旧配置迁移。

重点盯三件事：
1. **旧配置不能坏** —— 早期只有 ``exe`` 字段，读进来要自动当 exe 形态。
2. **pck 形态必须借底座引擎** —— 生成的命令要有 ``--packset``，且模板 exe
   不能传 ``--main-pack``（传了引擎直接报 path overrides 不支持）。
3. **文件后缀要校验** —— 选错文件不该等到启动时才炸。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import custom_lines as cl  # noqa: E402


@pytest.fixture()
def cfg() -> dict:
    # 用 4.5 当「比内置线更低」的替身：验证「没指定时取最高版本」的排序逻辑
    # 在只剩内置线的情况下仍然成立。
    return {"engines": {"4.6": {"kind": "editor"}, "4.5": {"kind": "template"}}}


@pytest.fixture()
def exe_file(tmp_path: Path) -> Path:
    p = tmp_path / "game.exe"
    p.write_bytes(b"MZ fake")
    return p


@pytest.fixture()
def pck_file(tmp_path: Path) -> Path:
    p = tmp_path / "story.pck"
    p.write_bytes(b"GDPC fake")
    return p


@pytest.fixture(autouse=True)
def _stub_identity(monkeypatch) -> None:
    """这些用例测的是登记/序列化/启动命令，不是身份闸门。

    文件都是 ``b"MZ fake"`` / ``b"GDPC fake"``，真实闸门必然拒绝；
    闸门的放行与拒绝由 test_custom_line_identity.py 专项验证。
    """
    from app.core import identity_gate

    def fake(path: Path):
        return identity_gate.Verdict(
            True, "test-stub", "测试夹具放行", str(path),
            engine_line="4.6", identity_hash="stub-hash",
        )

    monkeypatch.setattr(cl, "_probe_identity", fake)


# ---------------------------------------------------------------- 迁移


def test_legacy_exe_field_is_migrated() -> None:
    """旧配置只有 ``exe`` 字段 —— 读进来必须还是 exe 形态，路径不能丢。"""
    cfg = {
        "custom_lines": [
            {
                "id": "custom:old12345",
                "name": "老改版",
                "exe": "D:/games/x.exe",
                "args": "--foo",
                "profile": "",
                "added_at": "2025-01-01 00:00:00",
                "note": "旧记录",
            }
        ]
    }
    line = cl.load_all(cfg)[0]
    assert line.kind == cl.KIND_EXE
    assert line.path == "D:/games/x.exe"
    assert line.args == "--foo"
    # 新格式写回时用 path，不再写 exe
    saved = line.as_dict()
    assert saved["path"] == "D:/games/x.exe"
    assert "exe" not in saved


def test_bad_kind_falls_back_to_exe() -> None:
    line = cl.CustomLine.from_dict({"id": "custom:x", "name": "n", "kind": "乱七八糟", "path": "a.exe"})
    assert line.kind == cl.KIND_EXE


# ---------------------------------------------------------------- 校验


def test_add_exe(cfg: dict, exe_file: Path) -> None:
    line = cl.add_line(cfg, "改版A", str(exe_file))
    assert line.kind == cl.KIND_EXE
    assert line.engine_line == "", "exe 形态不该留下引擎线"
    assert cl.get_line(cfg, line.id) is not None


def test_add_pck_defaults_to_newest_engine(cfg: dict, pck_file: Path) -> None:
    line = cl.add_line(cfg, "剧情包", str(pck_file), kind=cl.KIND_PCK)
    assert line.kind == cl.KIND_PCK
    assert line.engine_line == "4.6", "没指定时取最高版本引擎"


def test_reject_wrong_suffix(cfg: dict, exe_file: Path, pck_file: Path) -> None:
    with pytest.raises(ValueError, match=r"pck 形态需要 \.pck"):
        cl.add_line(cfg, "错", str(exe_file), kind=cl.KIND_PCK)
    with pytest.raises(ValueError, match=r"exe 形态需要 \.exe"):
        cl.add_line(cfg, "错", str(pck_file), kind=cl.KIND_EXE)


def test_reject_duplicate_file(cfg: dict, exe_file: Path) -> None:
    cl.add_line(cfg, "第一次", str(exe_file))
    with pytest.raises(ValueError, match="已经登记过"):
        cl.add_line(cfg, "第二次", str(exe_file))


def test_reject_missing_file(cfg: dict, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="文件不存在"):
        cl.add_line(cfg, "没有", str(tmp_path / "nope.exe"))


def test_bad_engine_line_rejected(cfg: dict, pck_file: Path) -> None:
    with pytest.raises(ValueError, match="引擎线"):
        cl.add_line(cfg, "x", str(pck_file), kind=cl.KIND_PCK, engine_line="9.9")


# ---------------------------------------------------------------- 改


def test_update_switches_kind_and_clears_engine(cfg: dict, exe_file: Path, pck_file: Path) -> None:
    line = cl.add_line(cfg, "A", str(exe_file))
    assert cl.update_line(cfg, line.id, kind=cl.KIND_PCK, path=str(pck_file))
    got = cl.get_line(cfg, line.id)
    assert got is not None
    assert got.kind == cl.KIND_PCK
    assert got.engine_line == "4.6"

    # 切回 exe 要清空引擎线，否则配置里留着一条误导字段
    assert cl.update_line(cfg, line.id, kind=cl.KIND_EXE, path=str(exe_file))
    got = cl.get_line(cfg, line.id)
    assert got is not None and got.engine_line == ""


def test_update_switching_kind_without_path_still_validates(cfg: dict, exe_file: Path) -> None:
    """把 exe 线切成 pck 但没换文件 → 后缀不匹配，必须拒绝。"""
    line = cl.add_line(cfg, "A", str(exe_file))
    with pytest.raises(ValueError, match=r"pck 形态需要 \.pck"):
        cl.update_line(cfg, line.id, kind=cl.KIND_PCK)


def test_update_all_config_fields(cfg: dict, pck_file: Path) -> None:
    line = cl.add_line(cfg, "A", str(pck_file), kind=cl.KIND_PCK)
    assert cl.update_line(
        cfg,
        line.id,
        name="新名字",
        main_scene="res://main.tscn",
        args="--fullscreen",
        profile="我的存档",
        category="cat_abc",
        note="备注",
        engine_line="4.6",
    )
    got = cl.get_line(cfg, line.id)
    assert got is not None
    assert (got.name, got.main_scene, got.args) == ("新名字", "res://main.tscn", "--fullscreen")
    assert got.profile_name() == "我的存档"
    assert got.category == "cat_abc"
    assert got.engine_line == "4.6"


# ---------------------------------------------------------------- 启动


def test_exe_launch_is_direct(cfg: dict, exe_file: Path, tmp_path: Path) -> None:
    line = cl.add_line(cfg, "A", str(exe_file), args="--windowed")
    plan = cl.prepare_launch(cfg, line, tmp_path / "profiles")
    assert plan.command[0] == str(exe_file)
    assert plan.command[1] == "--windowed"
    assert plan.packset is None
    assert plan.env_appdata is not None


def test_pck_launch_uses_host_engine(
    cfg: dict, pck_file: Path, tmp_path: Path, monkeypatch
) -> None:
    """pck 形态必须走底座引擎 + packset，且模板 exe 不能传 --main-pack。"""
    import app.core.paths as paths

    runtime = tmp_path / "runtime" / "host45"
    runtime.mkdir(parents=True)
    (runtime / "jjxf45.exe").write_bytes(b"x")
    (runtime / "jjxf45.pck").write_bytes(b"x")
    monkeypatch.setattr(paths, "ROOT", tmp_path)

    line = cl.add_line(cfg, "自定义资源包", str(pck_file), kind=cl.KIND_PCK, engine_line="4.5")
    plan = cl.prepare_launch(cfg, line, tmp_path / "profiles")

    assert plan.command[0].endswith("jjxf45.exe")
    assert "--main-pack" not in plan.command, "模板 exe 传 --main-pack 会报错"
    joined = " ".join(plan.command)
    assert "--packset=" in joined
    assert "--main=res://scenes/start_page.tscn" in joined, "没填主场景时用默认"
    assert "--log=" in joined, "必须显式给 --log，否则报告会写进发布目录"

    # packset 真的写出来了，且只含玩家那个包
    assert plan.packset is not None and plan.packset.is_file()
    data = json.loads(plan.packset.read_text(encoding="utf-8"))
    assert data["line"] == "4.5"
    assert len(data["packs"]) == 1
    assert Path(data["packs"][0]["path"]) == pck_file.resolve()
    assert data["packs"][0]["role"] == "baseline"


def test_pck_launch_custom_main_scene(cfg: dict, pck_file: Path, tmp_path: Path, monkeypatch) -> None:
    import app.core.paths as paths

    runtime = tmp_path / "runtime" / "host46"
    runtime.mkdir(parents=True)
    (runtime / "jjxf46.exe").write_bytes(b"x")
    (runtime / "jjxf46.pck").write_bytes(b"x")
    monkeypatch.setattr(paths, "ROOT", tmp_path)

    line = cl.add_line(
        cfg,
        "老包",
        str(pck_file),
        kind=cl.KIND_PCK,
        engine_line="4.6",
        main_scene="res://boot.tscn",
    )
    plan = cl.prepare_launch(cfg, line, tmp_path / "profiles")
    assert "--main=res://boot.tscn" in " ".join(plan.command)
    # 4.6 是编辑器构建，走 --main-pack
    assert "--main-pack" in plan.command


def test_missing_file_blocks_launch(cfg: dict, exe_file: Path, tmp_path: Path) -> None:
    line = cl.add_line(cfg, "A", str(exe_file))
    exe_file.unlink()
    with pytest.raises(ValueError, match="不在了"):
        cl.prepare_launch(cfg, line, tmp_path / "profiles")
