"""导入本体后必须校验通过才允许启动。

来源是一次真实事故（2026-11-16，``launcher-20261116-113827.log``）：
4.7 的包在往 4.6 线导入、校验还没完成时就被放行启动，游戏进程读到
半截 pck，26 秒后以 ``0xC0000005``（访问违例）崩掉。

这组测试钉住四件事：

1. ``adopt``（登记本体的唯一入口）一定把状态置 ``pending``；
2. ``pending`` / ``failed`` 一定阻断启动；
3. 校验通过后一定放行；
4. **老配置没有这个字段时不能拦** —— 否则升级即「所有老玩家无法启动」，
   那是比原 bug 更糟的回归。
"""

from __future__ import annotations

from pathlib import Path

import app.core.readiness as readiness_mod
from app.core import baseline_ops
from app.core import config as cfg_mod


def _probe(**kw) -> baseline_ops.BaselineProbe:
    defaults = dict(
        path=Path("D:/g/pack.pck"),
        kind="pck",
        size=300,
        pck_size=280,
        pack_version=4,
        engine_version=(4, 7, 2),
        entry_count=9,
        sha256="",
    )
    defaults.update(kw)
    return baseline_ops.BaselineProbe(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 状态机


def test_adopt_marks_pending() -> None:
    """登记本体的同时必须是「待校验」，且不能带着旧的验证痕迹。"""
    cfg = {
        "baselines": {
            "jjxf.v4.7": {
                "path": "old.pck",
                "verify_state": "verified",
                "verify_at": "2026-11-01T00:00:00+00:00",
                "verify_by": "smoke",
            }
        }
    }

    slot = baseline_ops.adopt(cfg, "jjxf.v4.7", _probe())

    assert slot["verify_state"] == baseline_ops.VERIFY_PENDING
    assert slot["verify_at"] == ""
    assert slot["verify_by"] == ""
    assert baseline_ops.is_launchable(cfg, "jjxf.v4.7") is False


def test_missing_field_is_not_blocking() -> None:
    """1.5.8x 之前的配置没有 verify_state —— 不能当成待校验。"""
    cfg = {"baselines": {"jjxf.v4.7": {"path": "x.pck", "size": 1}}}

    assert baseline_ops.verify_state(cfg, "jjxf.v4.7") == ""
    assert baseline_ops.is_launchable(cfg, "jjxf.v4.7") is True


def test_unknown_baseline_is_not_blocking() -> None:
    assert baseline_ops.verify_state({"baselines": {}}, "jjxf.v9") == ""
    assert baseline_ops.is_launchable({"baselines": {}}, "jjxf.v9") is True


def test_mark_verified_then_failed_round_trip() -> None:
    cfg = {"baselines": {}}

    baseline_ops.mark_verified(cfg, "jjxf.v4.7", by="smoke", note="6 个节点")
    assert baseline_ops.is_launchable(cfg, "jjxf.v4.7") is True
    assert cfg["baselines"]["jjxf.v4.7"]["verify_by"] == "smoke"
    assert cfg["baselines"]["jjxf.v4.7"]["verify_at"]

    baseline_ops.mark_failed(cfg, "jjxf.v4.7", by="smoke", note="冒烟未通过")
    assert baseline_ops.is_launchable(cfg, "jjxf.v4.7") is False
    assert "冒烟未通过" in baseline_ops.verify_summary(cfg, "jjxf.v4.7")


def test_verify_summary_marks_legacy_config() -> None:
    assert "旧配置" in baseline_ops.verify_summary({"baselines": {}}, "jjxf.v4.7")


# ---------------------------------------------------------------- 门禁


def _clean_env(monkeypatch, tmp_path) -> None:
    """把「基线指纹 / 运行时 / 扩展」三关都置成通过，只留校验门禁在测。"""
    exe = tmp_path / "host.exe"
    exe.write_bytes(b"x")
    pck = tmp_path / "host.pck"
    pck.write_bytes(b"x")

    monkeypatch.setattr(readiness_mod.runner, "host_paths", lambda line: (exe, pck))
    monkeypatch.setattr(cfg_mod, "baseline_health", lambda cfg, bid: ("ok", ""))
    monkeypatch.setattr(readiness_mod, "_layered_check", lambda cfg, line, res: None)
    monkeypatch.setattr(readiness_mod.ext_enable, "enabled_mods", lambda cfg, line: [])
    monkeypatch.setattr(readiness_mod.ext_enable, "enabled_manifests", lambda cfg, line: [])


def _cfg(state: str, note: str = "") -> dict:
    return {
        "extensions": {"extension_mode": "loose"},
        "baselines": {
            "jjxf.v4.7": {
                "path": "x.pck",
                "size": 1,
                "verify_state": state,
                "verify_note": note,
            }
        },
    }


def test_pending_blocks_launch(monkeypatch, tmp_path) -> None:
    _clean_env(monkeypatch, tmp_path)

    r = readiness_mod.evaluate(_cfg("pending"), "4.7")

    assert r.ok is False
    assert "待校验" in r.blockers[0]
    assert "baseline.verify_pending" in [p["code"] for p in r.problems]
    # 提示要指出下一步，否则玩家只知道「不能启动」却不知道该干什么
    assert "校验本体" in r.blockers[0]


def test_failed_blocks_launch_and_surfaces_reason(monkeypatch, tmp_path) -> None:
    _clean_env(monkeypatch, tmp_path)

    r = readiness_mod.evaluate(_cfg("failed", note="冒烟未通过：实例化节点不足"), "4.7")

    assert r.ok is False
    assert "校验未通过" in r.blockers[0]
    assert "实例化节点不足" in r.blockers[0]


def test_verified_allows_launch(monkeypatch, tmp_path) -> None:
    _clean_env(monkeypatch, tmp_path)

    r = readiness_mod.evaluate(_cfg("verified"), "4.7")

    assert r.ok is True, r.blockers
    assert not any(p["code"].startswith("baseline.verify_") for p in r.problems)


def test_legacy_config_still_launches(monkeypatch, tmp_path) -> None:
    """没有状态字段的老配置必须照旧可启动 —— 升级不能一刀切拦死。"""
    _clean_env(monkeypatch, tmp_path)
    cfg = _cfg("")
    cfg["baselines"]["jjxf.v4.7"].pop("verify_state")

    r = readiness_mod.evaluate(cfg, "4.7")

    assert r.ok is True, r.blockers


def test_verify_gate_takes_priority_over_other_blockers(monkeypatch, tmp_path) -> None:
    """没校验过的本体，「基线文件不存在」之类的结论都排它后面。"""
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cfg_mod, "baseline_health", lambda cfg, bid: ("missing", "基线文件不存在：x.pck")
    )

    r = readiness_mod.evaluate(_cfg("pending"), "4.7")

    assert r.ok is False
    assert "待校验" in r.blockers[0], "校验门禁必须是第一条原因"
