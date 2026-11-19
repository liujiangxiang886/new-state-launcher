"""开发者校验（strict_verify）下的合集包整包预检。

产品约定：
- 任一成员静态校验失败 → 整包不装；
- 任一成员与已启用组合存在未处置冲突 → 整包不装；
- 全部通过 → 返回 None，进入逐个安装（安装期再失败则回滚）。
"""

from __future__ import annotations

from types import SimpleNamespace

import app.core.ext.service as service_mod
from app.core.ext.service import _bundle_preflight


class FakeIssue:
    def __init__(self, level: str, message: str) -> None:
        self.level = level
        self.message = message


class FakeProbe:
    def __init__(self, ext_id: str, *, issues=None, data=None) -> None:
        self.id = ext_id
        self.engine_line = "4.6"
        self.issues = issues or []
        self.data = data or {"id": ext_id, "name": ext_id, "version": "1.0.0"}


class FakeOuter:
    def __init__(self) -> None:
        self.blobs = {"a.zip": b"a", "b.zip": b"b"}

    def read(self, name: str) -> bytes:
        return self.blobs[name]


def _svc() -> SimpleNamespace:
    return SimpleNamespace(cfg={"extensions": {"extension_mode": "strict_verify"}})


def _patch_load(monkeypatch, probes: dict[str, FakeProbe]) -> None:
    def fake_load(path):
        return probes[path.name]

    monkeypatch.setattr(service_mod.ext_manifest, "load", fake_load)
    monkeypatch.setattr(
        "app.core.ext.enable.enabled_manifests", lambda cfg, line: []
    )


def test_preflight_excludes_only_broken_member(tmp_path, monkeypatch) -> None:
    """一个成员有静态错误 → **只排除它**，其余照装。

    2026-11-08 改：原来任何一个成员出错就整包不装，实测后果是 25 个包里
    5 个被误判、剩下 20 个一个都没装成。玩家明明有 20 个能用的包，
    却什么都得不到。
    """
    _patch_load(
        monkeypatch,
        {
            "a.zip": FakeProbe("a", issues=[FakeIssue("error", "清单坏了")]),
            "b.zip": FakeProbe("b"),
        },
    )
    out = _bundle_preflight(
        _svc(), FakeOuter(), ["a.zip", "b.zip"], tmp_path, "4.6", lambda *_: None
    )
    assert out is not None
    # 坏的被排除，好的留下
    assert "a.zip" in out["exclude"]
    assert "b.zip" not in out["exclude"]
    assert "清单坏了" in out["exclude"]["a.zip"]


def test_preflight_excludes_conflicting_members_only(tmp_path, monkeypatch) -> None:
    """组合冲突 → **只排除问题里点名的成员**。

    冲突至少涉及两个包，但只有本批里的那些才该被剔除；
    玩家早先装好的老包不该被本批连累。
    """
    _patch_load(
        monkeypatch,
        {"a.zip": FakeProbe("a"), "b.zip": FakeProbe("b")},
    )
    # resolver.resolve 在函数内部被导入，这里直接打桩模块函数
    import app.core.ext.resolver as resolver_mod

    monkeypatch.setattr(
        resolver_mod,
        "resolve",
        lambda *a, **k: SimpleNamespace(
            problems=[
                SimpleNamespace(level="error", ids=["a", "b"], message="同一处冲突")
            ]
        ),
    )
    out = _bundle_preflight(
        _svc(), FakeOuter(), ["a.zip", "b.zip"], tmp_path, "4.6", lambda *_: None
    )
    assert out is not None
    # 两个都被点名 → 两个都排除
    assert set(out["exclude"]) == {"a.zip", "b.zip"}
    assert "同一处冲突" in out["exclude"]["a.zip"]


def test_preflight_passes_when_clean(tmp_path, monkeypatch) -> None:
    _patch_load(
        monkeypatch,
        {"a.zip": FakeProbe("a"), "b.zip": FakeProbe("b")},
    )
    import app.core.ext.resolver as resolver_mod

    monkeypatch.setattr(
        resolver_mod, "resolve", lambda *a, **k: SimpleNamespace(problems=[])
    )
    out = _bundle_preflight(
        _svc(), FakeOuter(), ["a.zip", "b.zip"], tmp_path, "4.6", lambda *_: None
    )
    # 新语义：不是 None，而是「没有任何成员被排除」
    assert out is not None
    assert out["exclude"] == {}
