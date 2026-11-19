"""BaseContent 回归测试：补丁型扩展必须能从基线 pck 取到原文。

背景：移除 4.7 时 ``BaseContent._open`` 被误删，``read()`` / ``origin()``
调用它直接 ``AttributeError``，导致所有带 ``data_patches`` / ``text_patches``
的扩展在「生成覆盖层」阶段失败（纯 ``files`` 覆盖的扩展不受影响）。

这里用一个真实 pck 锁住该行为：删掉 ``_open`` 时本测试立即变红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ext.basecontent import BaseContent  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402


def _baseline_pck(dest: Path) -> bytes:
    payload = b'{"catalog": {"9005115": {"name": "test"}}}'
    w = PckWriter(dest, line="4.6")
    w.add_bytes("data/equipment.json", payload)
    w.commit()
    return payload


def test_read_from_baseline_pck(tmp: Path) -> None:
    pck = tmp / "baseline.pck"
    payload = _baseline_pck(pck)

    cfg = {"baselines": {"jjxf.v1.45": {"engine": "4.6", "path": str(pck)}}}
    bc = BaseContent(cfg, "4.6", include_installed=False)

    # 这两行在 _open 缺失时都会抛 AttributeError
    assert bc.read("data/equipment.json") == payload
    assert bc.origin("data/equipment.json") == "baseline"

    # 基线里没有的路径应安静地返回 None / missing，而不是崩
    assert bc.read("data/not_there.json") is None
    assert bc.origin("data/not_there.json") == "missing"
    bc.close()


def test_missing_baseline_degrades_quietly(tmp: Path) -> None:
    # 没有登记基线：不能抛异常，交给调用方按 on_test_fail 处理
    bc = BaseContent({"baselines": {}}, "4.6", include_installed=False)
    assert bc.read("data/equipment.json") is None
    assert bc.origin("data/equipment.json") == "missing"
    bc.close()


# ---------------------------------------------------------------- source_root
# 背景：``source_root`` 曾经无条件优先 ``sources.reversed_project``，不管请求
# 哪条线都拿同一个目录去比对。本机那个目录是 4.7 的，于是**所有 4.6 线校验
# 都在跟 4.7 源码比锚点**，27 个本来完好的 4.6 包被报成 9 个「部分兼容 /
# 不兼容」——纯假警报，把好包判死。下面三条锁住「按引擎线取源码」。


def _fake_project(root: Path, line: str) -> Path:
    """造一个最小反解工程：只要 project.godot 里写清引擎版本。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.godot").write_text(
        'config_version=5\n'
        f'config/features=PackedStringArray("{line}", "GL Compatibility")\n',
        encoding="utf-8",
    )
    return root


def test_source_root_matches_engine_line(tmp: Path) -> None:
    from app.core.ext.basecontent import source_root

    p47 = _fake_project(tmp / "rev47", "4.7")
    p46 = _fake_project(tmp / "rev46", "4.6")
    cfg = {
        "sources": {"reversed_project": str(p47)},
        "runtime_sources": {"4.6": {"project_godot": str(p46 / "project.godot")}},
    }

    assert source_root(cfg, "4.6") == p46
    assert source_root(cfg, "4.7") == p47


def test_source_root_rejects_other_line_project(tmp: Path) -> None:
    """reversed_project 是 4.7 的，就别拿它当 4.6 的源码 —— 宁愿返回 None。"""
    from app.core.ext.basecontent import source_root

    p47 = _fake_project(tmp / "rev47", "4.7")
    cfg = {"sources": {"reversed_project": str(p47)}, "runtime_sources": {}}

    assert source_root(cfg, "4.7") == p47
    # 关键断言：以前这里会错误地返回 p47
    assert source_root(cfg, "4.6") is None


def test_source_root_without_features_keeps_legacy_behavior(tmp: Path) -> None:
    """project.godot 没写 features 时不拦（读不出引擎版本就信任配置）。"""
    from app.core.ext.basecontent import source_root

    plain = tmp / "plain"
    plain.mkdir(parents=True, exist_ok=True)
    (plain / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    cfg = {"sources": {"reversed_project": str(plain)}, "runtime_sources": {}}

    assert source_root(cfg, "4.6") == plain


def test_project_engine_line_parses_features(tmp: Path) -> None:
    from app.core.ext.basecontent import _project_engine_line

    root = _fake_project(tmp / "rev", "4.7")
    assert _project_engine_line(root / "project.godot") == "4.7"
    assert _project_engine_line(tmp / "nope.godot") == ""
