"""开发期档位名（smoke*）的迁移。

1.5 之前的发布包把 ``profiles.default`` 一起发了出去，玩家机上就出现
「存档 profile = smoke46」。只改配置不改目录不行 —— 玩家会以为角色丢了；
只改目录不改配置也不行 —— 下次启动又建一个空档位回来。

这里钉住四个分支：正常迁移、已有 default 时不合并、普通名字不动、目录不存在时只改配置。
"""

from __future__ import annotations

from pathlib import Path

from app.core import config as cfg_mod
from app.core import paths, profiles


def _env(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "profiles"
    monkeypatch.setattr(paths, "PROFILES", root)
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "launcher.json")
    return root


def test_migrates_smoke_name_and_directory(monkeypatch, tmp_path: Path) -> None:
    root = _env(monkeypatch, tmp_path)
    (root / "smoke46" / "appdata").mkdir(parents=True)
    (root / "smoke46" / "appdata" / "save.dat").write_bytes(b"role")
    cfg = {"profiles": {"default": "smoke46"}}

    name = profiles.migrate_legacy_default(cfg)

    assert name == "default"
    assert cfg["profiles"]["default"] == "default"
    assert (root / "default" / "appdata" / "save.dat").read_bytes() == b"role"
    assert not (root / "smoke46").exists(), "旧目录要一起改名，否则存档看起来丢了"
    assert cfg_mod.CONFIG_PATH.is_file(), "迁移结果要落盘"


def test_does_not_merge_when_default_exists(monkeypatch, tmp_path: Path) -> None:
    """两份存档都在时**不能合并** —— 宁可名字难看，也不能丢档。"""
    root = _env(monkeypatch, tmp_path)
    (root / "smoke46" / "appdata").mkdir(parents=True)
    (root / "default" / "appdata").mkdir(parents=True)
    cfg = {"profiles": {"default": "smoke46"}}

    name = profiles.migrate_legacy_default(cfg)

    assert name == "smoke46"
    assert cfg["profiles"]["default"] == "smoke46"
    assert (root / "smoke46").is_dir() and (root / "default").is_dir()


def test_leaves_normal_name_alone(monkeypatch, tmp_path: Path) -> None:
    root = _env(monkeypatch, tmp_path)
    (root / "我的存档" / "appdata").mkdir(parents=True)
    cfg = {"profiles": {"default": "我的存档"}}

    assert profiles.migrate_legacy_default(cfg) == "我的存档"
    assert cfg["profiles"]["default"] == "我的存档"
    assert (root / "我的存档").is_dir()


def test_missing_directory_only_updates_config(monkeypatch, tmp_path: Path) -> None:
    """目录还没建出来（导入前就崩了之类）：只把指针改回 default。"""
    root = _env(monkeypatch, tmp_path)
    cfg = {"profiles": {"default": "smoke47"}}

    name = profiles.migrate_legacy_default(cfg)

    assert name == "default"
    assert cfg["profiles"]["default"] == "default"
    assert not (root / "default").exists()


def test_is_legacy_test_name() -> None:
    assert profiles.is_legacy_test_name("smoke46")
    assert profiles.is_legacy_test_name("SMOKE-1")
    assert profiles.is_legacy_test_name("test")
    assert profiles.is_legacy_test_name("qa_47")
    assert not profiles.is_legacy_test_name("default")
    assert not profiles.is_legacy_test_name("烟雾测试")
    assert not profiles.is_legacy_test_name("")
