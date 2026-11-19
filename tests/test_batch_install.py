"""批量安装的编排语义。

这里不打桩真实安装流程（那要先造 pck、跑冒烟，慢且脆），只验证
``install_many`` 的编排契约：**串行、失败不中断、N==1 仍然严格**。
单包能不能装上由 ``install`` 自己的测试覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.core.ext.service as service_mod
from app.core import paths
from app.core.ext.service import InstallResult


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PACKS", tmp_path / "packs")
    monkeypatch.setattr(paths, "BUILDS", tmp_path / "builds")
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(service_mod, "REGISTRY", tmp_path / "config" / "extensions.json")
    (tmp_path / "packs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "builds").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _touch(root: Path, *names: str) -> list[Path]:
    out = []
    for name in names:
        path = root / name
        path.write_bytes(b"x")
        out.append(path)
    return out


def test_install_many_continues_after_failure(sandbox, monkeypatch) -> None:
    seen: list[str] = []

    def fake(cfg, package, progress=None, **kwargs):
        name = Path(package).name
        seen.append(name)
        if name == "bad.zip":
            raise RuntimeError("boom")
        return InstallResult(
            ok=True,
            ext_id=f"id.{Path(package).stem}",
            name=name,
            version="1.0.0",
            message=f"{name} 已安装",
        )

    monkeypatch.setattr(service_mod, "install_any", fake)
    packages = _touch(sandbox, "a.zip", "bad.zip", "c.zip")

    out = service_mod.install_many({}, packages)

    assert out.total == 3
    assert out.succeeded == 2
    assert out.failed == 1
    assert [item.ok for item in out.items] == [True, False, True]
    assert "boom" in out.items[1].message
    # 串行且失败后继续：顺序不能被并发打乱，也不能因为中间坏一个就停
    assert seen == ["a.zip", "bad.zip", "c.zip"]
    assert out.ok is False


def test_install_many_skips_smoke_but_single_keeps_it(sandbox, monkeypatch) -> None:
    flags: list[bool] = []

    def fake(cfg, package, progress=None, **kwargs):
        flags.append(bool(kwargs.get("verify_smoke")))
        return InstallResult(ok=True, ext_id=Path(package).stem, name=Path(package).name)

    monkeypatch.setattr(service_mod, "install_any", fake)
    pair = _touch(sandbox, "a.zip", "b.zip")

    service_mod.install_many({}, pair)
    assert flags == [False, False], "批量默认跳过挂载冒烟（速度优先）"

    service_mod.install_many({}, pair[:1])
    assert flags[-1] is True, "单个包必须保持严格：装完就要能玩"


def test_install_many_progress_is_monotonic(sandbox, monkeypatch) -> None:
    def fake(cfg, package, progress=None, **kwargs):
        if progress:
            progress(50, "half")
            progress(100, "done")
        return InstallResult(ok=True, ext_id=Path(package).stem, name=Path(package).name)

    monkeypatch.setattr(service_mod, "install_any", fake)
    packages = _touch(sandbox, "a.zip", "b.zip", "c.zip")

    events: list[tuple[int, str]] = []
    service_mod.install_many({}, packages, lambda pct, label: events.append((pct, label)))

    percents = [pct for pct, _ in events]
    assert percents == sorted(percents), "进度不能倒退"
    assert percents[-1] == 100
    assert any("[2/3]" in label for _, label in events)
    assert any("[3/3]" in label for _, label in events)


def test_install_many_accepts_empty_list(sandbox) -> None:
    out = service_mod.install_many({}, [])
    assert out.total == 0
    assert out.message
    assert out.ok is False


def test_install_many_on_item_callback(sandbox, monkeypatch) -> None:
    monkeypatch.setattr(
        service_mod,
        "install_any",
        lambda cfg, package, progress=None, **kwargs: InstallResult(
            ok=True, ext_id=Path(package).stem, name=Path(package).name
        ),
    )
    packages = _touch(sandbox, "a.zip", "b.zip")
    seen: list[str] = []
    service_mod.install_many({}, packages, on_item=lambda item: seen.append(item.package))
    assert len(seen) == 2
