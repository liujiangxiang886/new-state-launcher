"""就绪度分级：哪些 warn 要弹确认、哪些只是提示。

界面上靠 ``code`` 区分，不能靠文案——文案随时会改，改一个字就重新弹窗
是很烦人的，所以指纹也不含 message。
"""

from __future__ import annotations

import app.core.readiness as readiness_mod
from app.core import config as cfg_mod


def test_classify_splits_confirm_from_plain_warn() -> None:
    problems = [
        {"level": "error", "code": "conflict.files", "message": "撞车", "ids": ["a", "b"]},
        {"level": "warn", "code": "conflict.location", "message": "同位置", "ids": ["a", "b"]},
        {"level": "warn", "code": "overlay.missing", "message": "缺文件", "ids": ["a"]},
    ]
    # 完全校验档：冲突 error 是硬阻断
    blockers, warnings, confirm = readiness_mod.classify(problems, "strict")
    assert len(blockers) == 1
    assert blockers[0].startswith("扩展冲突：")
    assert len(warnings) == 2
    assert [p["code"] for p in confirm] == ["conflict.location"], "缺覆盖层不该弹确认窗"

    # 宽松档：撞车的包启动时会被降级跳过，不拦整个游戏
    blockers, warnings, _ = readiness_mod.classify(problems, "loose")
    assert blockers == []
    assert any(w.startswith("将跳过：") for w in warnings)


def test_overlap_is_not_a_confirm_item() -> None:
    """「同一文件、位置不重叠」**不该**弹确认窗。

    那是正常合并，不是问题。旧的三档制把它算确认项，是因为那一档要
    给作者看全量影响面；现在宽松档是玩家默认档，把正常合并当提示刷
    给玩家只会淹没真正该看的东西。
    """
    problems = [
        {"level": "warn", "code": "overlap.data", "message": "正常合并", "ids": ["a", "b"]},
    ]
    _blockers, _warnings, confirm = readiness_mod.classify(problems, "loose")
    assert confirm == []


def test_fingerprint_ignores_message() -> None:
    a = readiness_mod.Readiness(
        problems=[{"level": "warn", "code": "conflict.location", "message": "一", "ids": ["a", "b"]}],
        mode="loose",
    )
    b = readiness_mod.Readiness(
        problems=[{"level": "warn", "code": "conflict.location", "message": "二（改了文案）", "ids": ["a", "b"]}],
        mode="loose",
    )
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != ""


def test_fingerprint_changes_with_ids() -> None:
    a = readiness_mod.Readiness(
        problems=[{"level": "warn", "code": "conflict.location", "message": "x", "ids": ["a", "b"]}],
        mode="loose",
    )
    b = readiness_mod.Readiness(
        problems=[{"level": "warn", "code": "conflict.location", "message": "x", "ids": ["a", "c"]}],
        mode="loose",
    )
    assert a.fingerprint() != b.fingerprint()


def test_empty_problems_need_no_confirm() -> None:
    r = readiness_mod.Readiness()
    assert r.needs_confirm() is False
    assert r.fingerprint() == ""


def test_evaluate_marks_confirm_without_blocking(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "host.exe"
    exe.write_bytes(b"x")
    pck = tmp_path / "host.pck"
    pck.write_bytes(b"x")

    monkeypatch.setattr(readiness_mod.runner, "host_paths", lambda line: (exe, pck))
    monkeypatch.setattr(cfg_mod, "baseline_health", lambda cfg, bid: ("ok", ""))
    monkeypatch.setattr(readiness_mod.ext_enable, "enabled_mods", lambda cfg, line: [])
    # 真正的同位置冲突：两个包改**同一个 JSON 指针**。
    # 这才是「相关功能可能不完整」的事实依据，也是宽松档唯一值得
    # 在启动前提醒玩家的情况。
    monkeypatch.setattr(
        readiness_mod.ext_enable,
        "enabled_manifests",
        lambda cfg, line: [
            (
                "a",
                {
                    "data_patches": [
                        {"target": "data/e.json", "ops": [{"op": "replace", "path": "/catalog/1/power", "value": 2}]}
                    ]
                },
            ),
            (
                "b",
                {
                    "data_patches": [
                        {"target": "data/e.json", "ops": [{"op": "replace", "path": "/catalog/1/power", "value": 3}]}
                    ]
                },
            ),
        ],
    )

    # 宽松档才会把「谁和谁撞了」列成确认项；完全档恒为空（它靠剔除而非确认）
    r = readiness_mod.evaluate(
        {"extensions": {"extension_mode": "loose"}}, "4.6"
    )

    assert r.ok is True, "同位置冲突只警告，不能阻断启动"
    assert r.mode == "loose"
    assert r.needs_confirm() is True
    assert "conflict.location" in [p["code"] for p in r.problems]
    assert not any("覆盖层缺失" in w for w in r.warnings)
