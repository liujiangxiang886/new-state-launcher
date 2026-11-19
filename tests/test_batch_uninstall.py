"""批量卸载：一次写盘、先写后删、运行期锁、依赖反查。

「先写注册表、后删目录」的顺序是这组测试的核心：反过来的话，写盘一旦
失败就会留下「注册表里有、文件已没了」的坏条目，用户点卸载也删不到东西。
"""

from __future__ import annotations

import json

import pytest

import app.core.ext.service as service_mod
from app.core import paths, runtime_lock
from app.core.ext.service import ExtensionService


def _entry(ext_id: str, line: str = "4.6") -> dict:
    overlay = f"packs/{ext_id}/overlay-46.pck"
    return {
        "id": ext_id,
        "name": ext_id,
        "version": "1.0.0",
        "engine_line": line,
        "enabled": True,
        "status": "ready",
        "overlay": overlay,
        "entry_count": 1,
        "sha256": "",
        "lines": {
            line: {
                "line": line,
                "baseline": "jjxf.v1.45",
                "status": "ready",
                "overlay": overlay,
                "entry_count": 1,
                "sha256": "",
                "verified": True,
                "runtime_hooks": [],
                "patches": [],
                "installed_at": "",
                "hooks_pending": 0,
            }
        },
    }


def _write_registry(root, items: list[dict]):
    target = root / "config" / "extensions.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"schema": 1, "items": items}, ensure_ascii=False), encoding="utf-8"
    )
    return target


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PACKS", tmp_path / "packs")
    monkeypatch.setattr(service_mod, "REGISTRY", tmp_path / "config" / "extensions.json")
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda *a, **k: False)
    (tmp_path / "packs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_dirs(root, *ids: str) -> None:
    for ext_id in ids:
        target = root / "packs" / ext_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "overlay-46.pck").write_bytes(b"x")


def test_uninstall_many_writes_registry_once(sandbox, monkeypatch) -> None:
    _write_registry(sandbox, [_entry("a"), _entry("b"), _entry("c")])
    _make_dirs(sandbox, "a", "b", "c")

    calls = {"n": 0}
    original = ExtensionService._save_registry

    def counting(self, items):
        calls["n"] += 1
        return original(self, items)

    monkeypatch.setattr(ExtensionService, "_save_registry", counting)

    report = ExtensionService({}).uninstall_many(["a", "b"])

    assert report.ok is True
    assert report.removed == ["a", "b"]
    assert calls["n"] == 1, "批量卸载必须一次写盘，不能逐个读写"
    assert not (sandbox / "packs" / "a").exists()
    assert not (sandbox / "packs" / "b").exists()
    assert (sandbox / "packs" / "c").exists()
    remaining = json.loads((sandbox / "config" / "extensions.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in remaining["items"]] == ["c"]


def test_uninstall_many_reports_missing(sandbox) -> None:
    _write_registry(sandbox, [_entry("a")])
    _make_dirs(sandbox, "a")

    report = ExtensionService({}).uninstall_many(["a", "ghost"])

    assert report.removed == ["a"]
    assert report.missing == ["ghost"]
    assert "1 个不在注册表里" in report.message


def test_uninstall_keeps_dirs_when_registry_write_fails(sandbox, monkeypatch) -> None:
    _write_registry(sandbox, [_entry("a")])
    _make_dirs(sandbox, "a")

    def boom(self, items):
        raise OSError("disk full")

    monkeypatch.setattr(ExtensionService, "_save_registry", boom)

    with pytest.raises(OSError):
        ExtensionService({}).uninstall_many(["a"])

    # 写盘失败 = 什么都没发生。目录还在，注册表还是原样。
    assert (sandbox / "packs" / "a").exists()
    data = json.loads((sandbox / "config" / "extensions.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in data["items"]] == ["a"]


def test_uninstall_warns_when_dir_survives(sandbox, monkeypatch) -> None:
    _write_registry(sandbox, [_entry("a")])
    _make_dirs(sandbox, "a")

    # 模拟文件被占用：rmtree 不删也不抛
    monkeypatch.setattr(service_mod.shutil, "rmtree", lambda *a, **k: None)

    report = ExtensionService({}).uninstall_many(["a"])

    assert report.ok is True
    assert report.warnings, "删不掉目录要如实报告，不能静默"
    assert "未能完全删除" in report.warnings[0]
    # 条目已经移除：注册表是唯一真相源，孤儿目录不该把坏条目留下
    data = json.loads((sandbox / "config" / "extensions.json").read_text(encoding="utf-8"))
    assert data["items"] == []


def test_uninstall_refuses_while_game_running(sandbox, monkeypatch) -> None:
    _write_registry(sandbox, [_entry("a")])
    _make_dirs(sandbox, "a")
    monkeypatch.setattr(runtime_lock, "is_game_running", lambda *a, **k: True)

    with pytest.raises(runtime_lock.ModLockedError):
        ExtensionService({}).uninstall_many(["a"])

    assert (sandbox / "packs" / "a").exists(), "拒绝时必须零副作用"

    # 调用方已经确认过运行状态时，可以跳过重复的进程表查询
    report = ExtensionService({}).uninstall_many(["a"], check_lock=False)
    assert report.ok is True


def test_dependents_of_finds_reverse_requires(sandbox) -> None:
    _write_registry(sandbox, [_entry("a"), _entry("b")])
    _make_dirs(sandbox, "a", "b")
    manifest_dir = sandbox / "packs" / "b"
    (manifest_dir / "manifest-46.v2.json").write_text(
        json.dumps({"schema": 2, "id": "b", "requires": [{"id": "a"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    found = ExtensionService({}).dependents_of(["a"])

    assert len(found) == 1
    assert found[0]["id"] == "b"
    assert found[0]["requires"] == ["a"]
