"""跨线存量收敛：把「同一扩展装了两条线」的旧数据收成一条。

1.4 起跨线安装是硬拒绝，但老版本装出来的双线数据还在盘上。这里测的是
「能不能安全地把它清掉」——尤其是**注册表先写、文件后删**这个顺序，
写失败时不能把文件删了。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core import paths
from app.core.ext import enable as enable_mod
from app.core.ext import migrate, service as service_mod
from app.core.fsutil import atomic_write_json, read_json


def _manifest(ext_id: str, line: str, declared: str) -> None:
    folder = paths.PACKS / ext_id
    folder.mkdir(parents=True, exist_ok=True)
    data = {"name": ext_id, "baseline": {"id": f"jjxf.v{declared}", "engine": {"line": declared}}}
    (folder / f"manifest-{line.replace('.', '')}.v2.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _overlay(ext_id: str, line: str, size: int = 16) -> Path:
    folder = paths.PACKS / ext_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"overlay-{line.replace('.', '')}.pck"
    path.write_bytes(b"x" * size)
    return path


def _dual_entry(ext_id: str, declared: str = "4.6") -> dict:
    other = "4.5" if declared == "4.6" else "4.6"
    return {
        "id": ext_id,
        "name": ext_id,
        "enabled": True,
        "engine_line": other,
        "baseline": f"jjxf.v{other}",
        "overlay": f"packs/{ext_id}/overlay-{other.replace('.', '')}.pck",
        "lines": {
            declared: {
                "line": declared,
                "baseline": f"jjxf.v{declared}",
                "overlay": f"packs/{ext_id}/overlay-{declared.replace('.', '')}.pck",
                "entry_count": 3,
                "status": "ready",
                "installed_at": "2026-01-01 00:00:00",
            },
            other: {
                "line": other,
                "baseline": f"jjxf.v{other}",
                "overlay": f"packs/{ext_id}/overlay-{other.replace('.', '')}.pck",
                "entry_count": 99,
                "status": "ready",
                "installed_at": "2026-06-01 00:00:00",
            },
        },
    }


def _write(items: list[dict]) -> None:
    atomic_write_json(service_mod.REGISTRY, {"schema": 1, "items": items})


def _patch_config_dir(monkeypatch, tmp_path: Path) -> None:
    """备份写盘要落在临时目录，不能碰真实 config/。"""
    target = tmp_path / "config"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "CONFIG_DIR", target)


# ---------------------------------------------------------------- 扫描


def test_scan_finds_dual_line_and_lists_files(tmp_path: Path) -> None:
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    _overlay("mod.a", "4.6", 8)
    _overlay("mod.a", "4.5", 64)
    _write([_dual_entry("mod.a")])

    plan = migrate.scan_single_line({})

    assert len(plan.drops) == 1
    row = plan.drops[0]
    assert row.declared == "4.6" and row.keep_line == "4.6"
    assert row.drop_lines == ["4.5"]
    assert len(row.files) == 2
    # 列出来的都该是被丢弃那条线（4.5）的文件
    assert all("45" in f for f in row.files)
    assert row.bytes >= 64


def test_scan_keeps_single_line_package() -> None:
    _manifest("mod.b", "4.6", "4.6")
    _overlay("mod.b", "4.6")
    _write([{
        "id": "mod.b", "name": "B", "enabled": True, "engine_line": "4.6",
        "lines": {"4.6": {"line": "4.6", "baseline": "jjxf.v1.45", "status": "ready"}},
    }])

    plan = migrate.scan_single_line({})

    assert plan.drops == []
    assert [i.action for i in plan.items] == ["keep"]


def test_scan_flags_package_without_matching_line() -> None:
    """只装了别条线、但声明内置线 —— 删文件没用，得让用户重装。"""
    _manifest("mod.c", "4.5", "4.6")
    _overlay("mod.c", "4.5", 128)
    _write([{
        "id": "mod.c", "name": "C", "enabled": True, "engine_line": "4.5",
        "lines": {"4.5": {"line": "4.5", "baseline": "jjxf.v1.44", "status": "ready"}},
    }])

    plan = migrate.scan_single_line({})

    rows = plan.needs_reinstall
    assert [r.id for r in rows] == ["mod.c"]
    assert rows[0].files == [], "不确定能不能装回来之前，不要动文件"
    assert plan.drops == []


def test_scan_handles_legacy_entry_without_lines() -> None:
    _manifest("mod.old", "4.5", "4.6")
    _write([{"id": "mod.old", "name": "OLD", "engine_line": "4.5", "baseline": "jjxf.v1.44"}])

    plan = migrate.scan_single_line({})

    assert [r.action for r in plan.items] == ["needs_reinstall"]


def test_scan_skips_entries_without_id() -> None:
    _write([{"name": "无名"}, {"id": "", "name": "空 id"}])
    assert migrate.scan_single_line({}).items == []


# ---------------------------------------------------------------- 执行


def test_dry_run_touches_nothing(tmp_path: Path, monkeypatch) -> None:
    _patch_config_dir(monkeypatch, tmp_path)
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    overlay = _overlay("mod.a", "4.5")
    _write([_dual_entry("mod.a")])

    plan = migrate.migrate_single_line({}, dry_run=True)

    assert plan.applied is False
    assert overlay.exists()
    assert plan.backup is None
    assert len((read_json(service_mod.REGISTRY)["items"][0]["lines"])) == 2


def test_migrate_removes_only_the_other_line(tmp_path: Path, monkeypatch) -> None:
    _patch_config_dir(monkeypatch, tmp_path)
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    keep = _overlay("mod.a", "4.6", 8)
    drop = _overlay("mod.a", "4.5", 64)
    _write([_dual_entry("mod.a")])

    plan = migrate.migrate_single_line({})

    assert plan.applied is True and plan.errors == []
    assert drop.exists() is False
    assert keep.exists(), "声明的线那份不能删"
    assert (paths.PACKS / "mod.a" / "manifest-47.v2.json").exists() is False
    entry = read_json(service_mod.REGISTRY)["items"][0]
    assert list(entry["lines"]) == ["4.6"]


def test_migrate_rewrites_top_level_mirror(tmp_path: Path, monkeypatch) -> None:
    """顶层字段只是「最近一次安装」的镜像，收敛后必须跟着保留线走。"""
    _patch_config_dir(monkeypatch, tmp_path)
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    _overlay("mod.a", "4.6")
    _overlay("mod.a", "4.5")
    _write([_dual_entry("mod.a")])

    migrate.migrate_single_line({})

    entry = read_json(service_mod.REGISTRY)["items"][0]
    assert entry["engine_line"] == "4.6"
    assert entry["entry_count"] == 3, "99 是另一条线那份的条目数，不能留在顶层"
    assert entry["overlay"].endswith("overlay-46.pck")


def test_migrate_backs_up_registry_and_file_list(tmp_path: Path, monkeypatch) -> None:
    _patch_config_dir(monkeypatch, tmp_path)
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    _overlay("mod.a", "4.6")
    _overlay("mod.a", "4.5")
    _write([_dual_entry("mod.a")])
    before = read_json(service_mod.REGISTRY)

    plan = migrate.migrate_single_line({})

    assert plan.backup is not None and plan.backup.is_file()
    assert read_json(plan.backup) == before, "备份必须是原样的注册表"
    deleted = plan.backup.parent / "deleted-files.txt"
    assert deleted.is_file()
    assert "overlay-45.pck" in deleted.read_text(encoding="utf-8")


def test_migrate_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _patch_config_dir(monkeypatch, tmp_path)
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    _overlay("mod.a", "4.6")
    _overlay("mod.a", "4.5")
    _write([_dual_entry("mod.a")])

    migrate.migrate_single_line({})
    second = migrate.migrate_single_line({})

    assert second.drops == []
    assert second.applied is False
    assert second.message() == "没有需要清理的双线扩展"


def test_migrate_purges_other_line_cache(tmp_path: Path, monkeypatch) -> None:
    _patch_config_dir(monkeypatch, tmp_path)
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    _overlay("mod.a", "4.6")
    _overlay("mod.a", "4.5")
    _write([_dual_entry("mod.a")])

    cache = paths.CACHE / "instrumented"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "a47.pck").write_bytes(b"x")
    (cache / "a45.json").write_text(json.dumps({"line": "4.5"}), encoding="utf-8")
    (cache / "a46.pck").write_bytes(b"x")
    (cache / "a46.json").write_text(json.dumps({"line": "4.6"}), encoding="utf-8")

    plan = migrate.migrate_single_line({})

    assert plan.errors == []
    assert not (cache / "a47.pck").exists(), "被删的那条线的插桩缓存必须作废"
    assert (cache / "a46.pck").exists()


def test_migrate_keeps_empty_package_dir_cleaned(tmp_path: Path, monkeypatch) -> None:
    """两条线的文件都删干净后不该在 packs/ 下留空壳目录。"""
    _patch_config_dir(monkeypatch, tmp_path)
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    _overlay("mod.a", "4.6")
    _overlay("mod.a", "4.5")
    _write([_dual_entry("mod.a")])

    # 把「保留线」的那份也删掉，模拟文件已被手动清理
    (paths.PACKS / "mod.a" / "overlay-46.pck").unlink()
    (paths.PACKS / "mod.a" / "manifest-46.v2.json").unlink()

    migrate.migrate_single_line({})

    assert not (paths.PACKS / "mod.a").exists()


def test_message_reports_bytes(tmp_path: Path, monkeypatch) -> None:
    _patch_config_dir(monkeypatch, tmp_path)
    _manifest("mod.a", "4.6", "4.6")
    _manifest("mod.a", "4.5", "4.6")
    _overlay("mod.a", "4.6")
    _overlay("mod.a", "4.5", 1024 * 1024)
    _write([_dual_entry("mod.a")])

    plan = migrate.scan_single_line({})

    assert "清理 1 个扩展" in plan.message()
    assert "MB" in plan.message()


def test_registry_mtime_tracks_writes(tmp_path: Path, monkeypatch) -> None:
    _patch_config_dir(monkeypatch, tmp_path)
    _write([])
    first = migrate.registry_mtime()
    assert first > 0
    import os
    import time

    os.utime(service_mod.REGISTRY, (time.time() + 5, time.time() + 5))
    assert migrate.registry_mtime() > first


def test_declared_line_used_by_migrate_matches_install_guard() -> None:
    """迁移与安装拒绝必须用同一套「声明线」判定，否则会互相打架。"""
    _manifest("mod.x", "4.5", "4.6")
    item = {
        "id": "mod.x",
        "engine_line": "4.5",
        "baseline": "jjxf.v1.44",
        "lines": {"4.5": {"line": "4.5", "baseline": "jjxf.v1.44"}},
    }
    assert enable_mod.declared_line_of(item) == "4.6"
