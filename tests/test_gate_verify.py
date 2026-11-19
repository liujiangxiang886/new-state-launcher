"""完全校验档的验证标志闸门（S4）。

这条闸门与「冲突剔除」是两关，顺序有意义：

1. **没验证的先剔** —— 「压根没验过」不是靠处置能解决的问题；
2. **再在剩下的里面找冲突** —— 这个才归处置面板。

本组测试守住三件事：

- 有有效记录的包留下，没记录的被剔；
- 这条线**产不出**验证记录时（没有 code_digest），不剔任何包 ——
  否则玩家所有扩展凭空消失，而作者做什么都补救不了；
- 剔除后依赖闭包生效（依赖被剔包的也一起走）。
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
from app.core.ext import gate  # noqa: E402
from app.core.ext import service as service_mod  # noqa: E402
from app.core.ext import verify_mark as vm  # noqa: E402
from app.core.fsutil import atomic_write_json  # noqa: E402

LINE = "4.7"


def _registry(ids: list[str]) -> None:
    atomic_write_json(
        service_mod.REGISTRY,
        {
            "schema": 1,
            "items": [
                {
                    "id": ext_id,
                    "name": ext_id,
                    "enabled": True,
                    "engine_line": LINE,
                    "lines": {LINE: {"line": LINE, "baseline": "jjxf.v4.7", "status": "ready"}},
                }
                for ext_id in ids
            ],
        },
    )


def _manifest(ext_id: str, data: dict | None = None) -> None:
    folder = paths.PACKS / ext_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"manifest-{LINE.replace('.', '')}.v2.json").write_text(
        json.dumps({"schema": 3, "id": ext_id, "name": ext_id, **(data or {})}, ensure_ascii=False),
        encoding="utf-8",
    )


def _seal(ext_id: str, code: str = "CODE-OK") -> None:
    """给这个包造一份「本机验证通过」的记录。"""
    root = paths.PACKS / ext_id
    vm.put_local(
        ext_id,
        line=LINE,
        code_digest=code,
        content_digest_value=vm.content_digest(root),
        passed=["manifest"],
    )


@pytest.fixture
def cfg_strict() -> dict:
    return {"extensions": {"extension_mode": "strict"}}


# ---------------------------------------------------------------- 基本判定


def test_verified_mod_survives(monkeypatch, cfg_strict) -> None:
    monkeypatch.setattr(vm, "code_digest_of", lambda cfg, line: "CODE-OK")
    _registry(["good.ext"])
    _manifest("good.ext")
    _seal("good.ext")

    plan = gate.plan(cfg_strict, LINE)
    assert plan.keep == ["good.ext"]
    assert plan.dropped == []


def test_absent_mod_is_dropped(monkeypatch, cfg_strict) -> None:
    monkeypatch.setattr(vm, "code_digest_of", lambda cfg, line: "CODE-OK")
    _registry(["naked.ext"])
    _manifest("naked.ext")          # 没有验证记录

    plan = gate.plan(cfg_strict, LINE)
    assert plan.keep == []
    assert "naked.ext" in plan.dropped_ids
    assert any(d.code == "verify.absent" for d in plan.dropped)


def test_stale_mod_is_dropped(monkeypatch, cfg_strict) -> None:
    """有记录但代码层变了 → 剔除。"""
    monkeypatch.setattr(vm, "code_digest_of", lambda cfg, line: "CODE-NEW")
    _registry(["old.ext"])
    _manifest("old.ext")
    _seal("old.ext", code="CODE-OLD")   # 按旧代码验的

    plan = gate.plan(cfg_strict, LINE)
    assert "old.ext" in plan.dropped_ids
    assert any(d.code == "verify.stale" for d in plan.dropped)


def test_single_mod_is_still_checked(monkeypatch, cfg_strict) -> None:
    """**只装一个包也要校验。**

    旧实现在 ``len(pairs) < 2`` 时直接返回，等于「只装一个未经验证的包」
    完全不受管 —— 而这恰恰是最常见的玩家状态。
    """
    monkeypatch.setattr(vm, "code_digest_of", lambda cfg, line: "CODE-OK")
    _registry(["only.ext"])
    _manifest("only.ext")

    plan = gate.plan(cfg_strict, LINE)
    assert plan.keep == []
    assert "verify.absent" in [d.code for d in plan.dropped]


# ---------------------------------------------------------------- 前置判断


def test_no_code_digest_skips_verdict(monkeypatch, cfg_strict) -> None:
    """这条线**产不出**验证记录时，不剔任何包。

    没有 code_digest 意味着「我们暂时没法验」，不是「包有问题」。
    一剔了之会让玩家所有扩展凭空消失，而作者做什么都补救不了
    （``seal`` 也会因为拿不到 digest 拒绝出记录）。

    正确做法是记一条说明，等这条线补上 code_layer 后自动生效。
    """
    monkeypatch.setattr(vm, "code_digest_of", lambda cfg, line: "")
    _registry(["a.ext", "b.ext"])
    _manifest("a.ext")
    _manifest("b.ext")

    plan = gate.plan(cfg_strict, LINE)
    assert plan.keep == ["a.ext", "b.ext"], "不能因为「没法验」就剔包"
    assert plan.dropped == []
    assert any("verify.unavailable" in n for n in plan.notes)


# ---------------------------------------------------------------- 依赖闭包


def test_dependents_follow_when_dependency_dropped(monkeypatch, cfg_strict) -> None:
    """A 因为没验证被剔 → 依赖 A 的 B 也一起剔。"""
    monkeypatch.setattr(vm, "code_digest_of", lambda cfg, line: "CODE-OK")
    _registry(["base.ext", "user.ext"])
    _manifest("base.ext")                                   # 没验证 → 会被剔
    _manifest("user.ext", {"requires": [{"id": "base.ext"}]})
    _seal("user.ext")                                       # 它自己有记录

    plan = gate.plan(cfg_strict, LINE)
    assert "base.ext" in plan.dropped_ids
    assert "user.ext" in plan.dropped_ids, "依赖被剔包的必须一并剔除"
    assert any(d.code == "gate.dependency" for d in plan.dropped)


def test_loose_mode_ignores_verification(monkeypatch) -> None:
    """宽松档完全不看验证标志 —— 它的承诺是「照装 + 提示」。"""
    monkeypatch.setattr(vm, "code_digest_of", lambda cfg, line: "CODE-OK")
    _registry(["naked.ext"])
    _manifest("naked.ext")

    plan = gate.plan({"extensions": {"extension_mode": "loose"}}, LINE)
    assert plan.keep == ["naked.ext"]
    assert plan.dropped == []