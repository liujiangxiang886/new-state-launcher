"""存档路径解析 / 导入 / 游戏本体卸载的回归测试。

跑法：

- ``pytest tests/test_userdir.py``
- 或 ``python tests/test_userdir.py``（自带 main，退出码非 0 表示失败）

这几个用例守的是三条曾经踩过的坑：

1. 启动器底座工程名与原版不同 → 原版存档「看不见」，导入必须落到
   ``appdata/Godot/app_userdata/<底座工程名>/``；
2. 导入时不能把 ``*.lock`` / ``logs/`` 复制过去（会让游戏误判已有实例）；
3. 卸载游戏本体只能删登记过的 ``.pck`` / ``.exe`` 单文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import paths, runtime_lock, uninstall, userdir  # noqa: E402


def _fake_source(base: Path) -> Path:
    src = base / "roaming" / "Godot" / "app_userdata" / "机甲旋风 · 单机"
    src.mkdir(parents=True)
    (src / "offline_roles.json").write_text('{"roles": []}', encoding="utf-8")
    (src / "offline_roles.json.bak").write_text("{}", encoding="utf-8")
    (src / "offline_roles.json.compat.lock").write_text("", encoding="utf-8")
    (src / "logs").mkdir()
    (src / "logs" / "godot.log").write_text("x", encoding="utf-8")
    (src / "shader_cache").mkdir()
    (src / "shader_cache" / "a.bin").write_text("x", encoding="utf-8")
    return src


def test_base_project_name_is_versioned():
    """底座工程名带线号后缀，这是存档目录隔离的基础。"""
    assert userdir.base_project_name("4.6") == "机甲旋风 · 兼容底座 4.6"
    assert userdir.base_project_name("4.5") == "机甲旋风 · 兼容底座 4.5"


def test_list_save_files_skips_lock_and_caches(tmp_path: Path):
    src = _fake_source(tmp_path)
    rels = [f.rel for f in userdir.list_save_files(src)]
    assert "offline_roles.json" in rels
    assert "offline_roles.json.bak" in rels
    assert all("logs/" not in r for r in rels)
    assert all("shader_cache/" not in r for r in rels)
    assert all(not r.endswith(".lock") for r in rels)


def test_launcher_dir_honours_redirect(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paths, "PROFILES", tmp_path / "profiles")
    on = userdir.launcher_dir("default", "4.6", {"profiles": {"redirect_appdata": True}})
    assert on == (
        tmp_path / "profiles" / "default" / "appdata" / "Godot" / "app_userdata"
        / "机甲旋风 · 兼容底座 4.6"
    )
    # 关掉隔离时写到真实 APPDATA（目录名仍是底座工程名）
    monkeypatch.setattr(userdir, "system_appdata", lambda: tmp_path / "roaming")
    off = userdir.launcher_dir("default", "4.6", {"profiles": {"redirect_appdata": False}})
    assert off == tmp_path / "roaming" / "Godot" / "app_userdata" / "机甲旋风 · 兼容底座 4.6"


def test_import_copies_files_and_skips_noise(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paths, "PROFILES", tmp_path / "profiles")
    monkeypatch.setattr(userdir, "system_appdata", lambda: tmp_path / "roaming")
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda: False)
    src = _fake_source(tmp_path)

    result = userdir.import_saves("default", "4.6", src, cfg={"profiles": {"redirect_appdata": True}})
    target = userdir.launcher_dir("default", "4.6", {"profiles": {"redirect_appdata": True}})

    assert (target / "offline_roles.json").is_file()
    assert (target / "offline_roles.json.bak").is_file()
    assert not (target / "offline_roles.json.compat.lock").exists()
    assert not (target / "logs").exists()
    assert not (target / "shader_cache").exists()
    assert result["copied"] >= 2
    # 来源目录一个文件都不能少（只复制，不搬移）
    assert (src / "offline_roles.json").is_file()


def test_import_refuses_while_game_running(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda: True)
    src = _fake_source(tmp_path)
    try:
        userdir.import_saves("default", "4.6", src, cfg={"profiles": {}})
    except Exception as exc:  # noqa: BLE001
        assert "正在运行" in str(exc)
    else:  # pragma: no cover - 真的没抛才算失败
        raise AssertionError("游戏运行中导入应当被拒绝")


def test_remove_game_body_only_touches_registered_files(tmp_path: Path):
    body = tmp_path / "body"
    body.mkdir()
    pck = body / "demo.pck"
    pck.write_bytes(b"x" * 64)
    exe = body / "demo.exe"
    exe.write_bytes(b"y" * 32)
    txt = body / "keep.txt"
    txt.write_text("keep", encoding="utf-8")

    # 扫描只认 GAME_BODY_SCOPES 里登记的 id，所以两个文件都挂在同一个本体上
    cfg = {
        "baselines": {
            "jjxf.v1.45": {"path": str(pck), "exe_path": str(exe)},
        }
    }
    removed, freed, failures = uninstall.remove_game_body(cfg, ["jjxf.v1.45"])
    assert (removed, freed) == (2, 96)
    assert not pck.exists() and not exe.exists()
    assert txt.exists()

    # 非 .pck/.exe 后缀一律跳过，并给出说明
    cfg2 = {"baselines": {"jjxf.v1.45": {"path": str(txt)}}}
    removed2, _freed2, failures2 = uninstall.remove_game_body(cfg2, ["jjxf.v1.45"])
    assert removed2 == 0
    assert failures2 and "非游戏文件" in failures2[0]
    assert txt.exists()


def test_scan_game_body_reports_sizes(tmp_path: Path):
    pck = tmp_path / "a.pck"
    pck.write_bytes(b"z" * 128)
    cfg = {"baselines": {"jjxf.v1.45": {"path": str(pck)}}}
    stats = {s.key: s for s in uninstall.scan_game_body(cfg)}
    assert stats["jjxf.v1.45"].bytes == 128
    assert stats["jjxf.v1.45"].exists
    # 没登记的本体不出现在列表里（界面按配置里的 baselines 生成行）
    assert len(stats) == len(uninstall.GAME_BODY_SCOPES), "只列登记过的本体"


def main() -> int:
    import shutil
    import tempfile

    class _MP:
        """最小 monkeypatch：用完把属性还原。"""

        def __init__(self) -> None:
            self._undo: list[tuple] = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self) -> None:
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)

    cases = [
        test_base_project_name_is_versioned,
        test_list_save_files_skips_lock_and_caches,
        test_launcher_dir_honours_redirect,
        test_import_copies_files_and_skips_noise,
        test_import_refuses_while_game_running,
        test_remove_game_body_only_touches_registered_files,
        test_scan_game_body_reports_sizes,
    ]
    failed = 0
    for case in cases:
        tmp = Path(tempfile.mkdtemp(prefix="jjxf-ud-"))
        mp = _MP()
        try:
            params = case.__code__.co_varnames[: case.__code__.co_argcount]
            kwargs = {}
            if "tmp_path" in params:
                kwargs["tmp_path"] = tmp
            if "monkeypatch" in params:
                kwargs["monkeypatch"] = mp
            case(**kwargs)
            print(f"  OK   {case.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {case.__name__}: {exc}")
        finally:
            mp.undo()
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'全部通过' if not failed else f'{failed} 个失败'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
