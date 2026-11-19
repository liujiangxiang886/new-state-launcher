"""19 个真实扩展的回归：换了一套判定之后，现网的东西还能不能用。

这一份不测「逻辑对不对」，测的是**不变量**：

1. 19 个包逐个解析，0 error；
2. **数据指针两两不相交**——这是「同一文件不算冲突」成立的前提，
   一旦这里出现重叠，说明判定开始误报；
3. 文本锚点的重叠只允许出现在已知的那一对战斗类扩展上。它们是**真重叠**：
   一方替换函数头一行，另一方替换「函数头 + 函数体」，谁先改谁就把对方的
   定位串改掉。1.5.x 合并时同样会因锚点失配整体放弃那个脚本，只是不告诉你
   在哪；这里把它显式列出来，别让它悄悄扩大；
4. 普通模式下这些重叠**只标降级、不阻断**；开发者档才阻断。

新增重叠会让第 3 条失败——那不是「测试该改」，是先确认新重叠是不是真的。
找不到真实包时跳过，仓库可能只带源码不带数据。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.core.ext import manifest, policy, units

ROOT = Path(__file__).resolve().parents[1]
ADAPTED = ROOT / "adapted-1.5" / "mods"
INSTALLED = ROOT / "packs"

#: 已知的真实文本重叠。要加进来之前先确认「两边改的是同几行」。
KNOWN_TEXT_OVERLAP_PAIRS = {
    ("ai_act.combat_workbench", "jjxf.better_experience"),
}


def load_tool():
    """把 tools/jjm_pack.py 当模块加载（它不是包的一部分）。"""
    path = ROOT / "tools" / "jjm_pack.py"
    spec = importlib.util.spec_from_file_location("jjm_pack_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def adapted_zips() -> list[Path]:
    return sorted(ADAPTED.glob("*.zip")) if ADAPTED.is_dir() else []


def installed_manifests() -> list[Path]:
    if not INSTALLED.is_dir():
        return []
    return sorted(INSTALLED.glob("*/manifest.v2.json"))


def collect_all() -> dict[str, dict]:
    """把能拿到手的真实清单按 id 去重成 ``{id: data}``。"""
    pairs: dict[str, dict] = {}
    for path in adapted_zips():
        m = manifest.load(path)
        pairs.setdefault(m.data["id"], m.data)
    for path in installed_manifests():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # 已装注册表里的 manifest.v2.json 是归一后的形态，直接当 data 用
        m = manifest.normalize(raw, [])
        if m.data.get("id"):
            pairs.setdefault(m.data["id"], m.data)
    return pairs


def require_real_data() -> dict[str, dict]:
    pairs = collect_all()
    if len(pairs) < 2:
        pytest.skip("仓库里没有真实扩展包，跳过真实包回归")
    return pairs


def ordered_pairs(pairs: dict[str, dict]) -> list[tuple[str, dict]]:
    return sorted(pairs.items())


# ---------------------------------------------------------------- 解析


def test_every_adapted_package_parses_without_error() -> None:
    items = adapted_zips()
    if not items:
        pytest.skip("没有真实扩展包")
    for path in items:
        m = manifest.load(path)
        bad = [i.as_dict() for i in m.issues if i.level == "error"]
        assert not bad, f"{path.name}: {bad}"


def test_installed_packages_parse_without_error() -> None:
    for path in installed_manifests():
        raw = json.loads(path.read_text(encoding="utf-8"))
        m = manifest.normalize(raw, [])
        bad = [i.as_dict() for i in m.issues if i.level == "error"]
        assert not bad, f"{path}: {bad}"


# ---------------------------------------------------------------- 数据指针


def test_no_data_pointer_overlap_across_real_packages() -> None:
    """数据指针必须两两不相交。这一条是硬底线。"""
    pairs = require_real_data()
    found = [u for u in units.discover(ordered_pairs(pairs)) if u.kind == "data"]
    detail = [f"{u.target} {u.locator} {u.parties}" for u in found]
    assert found == [], f"真实包的数据指针出现重叠，判定可能开始误报：{detail}"


def test_no_whole_file_overlap_across_real_packages() -> None:
    pairs = require_real_data()
    found = [u for u in units.discover(ordered_pairs(pairs)) if u.kind == "files"]
    assert found == [], [f"{u.target} {u.parties}" for u in found]


# ---------------------------------------------------------------- 文本锚点


def test_text_overlaps_are_only_the_known_pair() -> None:
    pairs = require_real_data()
    found = [u for u in units.discover(ordered_pairs(pairs)) if u.kind == "text"]
    for unit in found:
        assert tuple(sorted(unit.parties)) in {
            tuple(sorted(pair)) for pair in KNOWN_TEXT_OVERLAP_PAIRS
        }, (
            f"{unit.target} 上出现了新的文本重叠：{unit.parties}。"
            f"先确认两边是不是真的改了同几行，再把它加进 KNOWN_TEXT_OVERLAP_PAIRS"
        )


def test_text_overlaps_are_explained_as_same_lines() -> None:
    """已知那 4 处必须确实是「一方锚点包含另一方」，否则规则就误报了。"""
    pairs = require_real_data()
    by_id = pairs
    found = [u for u in units.discover(ordered_pairs(pairs)) if u.kind == "text"]
    for unit in found:
        finds = []
        for pid in unit.parties:
            for patch in by_id[pid].get("text_patches") or []:
                target = str(patch.get("target") or "").replace("\\", "/").lstrip("/")
                if target == unit.target:
                    finds.append(str(patch.get("find") or ""))
        assert any(a and b and (a in b or b in a) for a in finds for b in finds if a != b), (
            f"{unit.target} 被判重叠，但两边的 find 并不互相包含"
        )


# ---------------------------------------------------------------- 三档不变量


def test_normal_mode_never_blocks_real_packages() -> None:
    pairs = require_real_data()
    items = ordered_pairs(pairs)
    plan = policy.plan(items, [pid for pid, _ in items], mode="normal")

    assert all(s.severity != "error" for s in plan.states), "普通模式不允许产生 error"
    assert plan.pending == [], "普通模式不该让人去处置"
    # 每一处重叠都要有对应的降级记录，不能默默丢掉
    found = units.discover(items)
    assert len(plan.degraded) == len(found)


def test_strict_verify_blocks_exactly_the_overlaps() -> None:
    pairs = require_real_data()
    items = ordered_pairs(pairs)
    found = units.discover(items)
    plan = policy.plan(items, [pid for pid, _ in items], mode="strict_verify")
    assert len(plan.pending) == len(found)
    assert all(s.severity == "error" for s in plan.pending)


def test_real_overlap_reports_an_actionable_reason() -> None:
    """真实重叠在「没有声明接管」时，给的理由要能照着改。

    这条锁的是提示文案的可操作性：作者看到之后应该知道差什么，
    而不是只看到「冲突」两个字。
    """
    pairs = require_real_data()
    items = ordered_pairs(pairs)
    found = [u for u in units.discover(items) if u.kind == "text"]
    if not found:
        pytest.skip("这批包没有文本重叠")

    meta = dict(items)
    for unit in found:
        ok, reason = units.authorized(unit, meta)
        assert not ok, "真实包里没人声明接管，不该判为已授权"
        assert any(word in reason for word in ("接管", "requires", "expect")), reason


# ---------------------------------------------------------------- 打包工具


def test_jjm_pack_check_passes_on_adapted_packages() -> None:
    items = adapted_zips()
    if not items:
        pytest.skip("没有真实扩展包")
    tool = load_tool()
    for path in items:
        report = tool.check_package(path)
        assert report.ok, f"{path.name}: {[f.as_dict() for f in report.errors]}"


def test_tool_accepts_schema3_manifest(tmp_path: Path) -> None:
    tool = load_tool()
    data = {
        "schema": 3,
        "id": "test.v3",
        "name": "v3 样例",
        "version": "1.0.0",
        "kind": "extension",
        "baseline": {"id": "jjxf.v1.45", "engine": {"line": "4.6"}},
        "data_patches": [
            {
                "target": "data/e.json",
                "operations": [
                    {"op": "replace", "path": "/a/b", "expect": 1, "value": 2, "on_miss": "abort"}
                ],
            }
        ],
    }
    path = tmp_path / "v3.zip"
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("extension.json", json.dumps(data, ensure_ascii=False))
    report = tool.check_package(path)
    assert report.ok, [f.as_dict() for f in report.errors]


def test_tool_and_runtime_agree_on_anchor_threshold() -> None:
    """判定阈值漂移过一次（工具 24 / 运行时 8），结论会不一致。"""
    tool = load_tool()
    assert tool.MIN_CONTAIN_LEN == units.MIN_CONTAIN_LEN


def test_tool_pair_reports_overlap(tmp_path: Path) -> None:
    import zipfile

    tool = load_tool()

    def write(name: str, ext_id: str, path: str) -> Path:
        data = {
            "schema": 3,
            "id": ext_id,
            "name": ext_id,
            "version": "1.0.0",
            "kind": "extension",
            "baseline": {"id": "jjxf.v1.45", "engine": {"line": "4.6"}},
            "data_patches": [
                {"target": "data/e.json", "operations": [{"op": "replace", "path": path, "value": 1}]}
            ],
        }
        out = tmp_path / name
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("extension.json", json.dumps(data, ensure_ascii=False))
        return out

    a = write("a.zip", "test.a", "/catalog/1/power")
    b = write("b.zip", "test.b", "/catalog/1/power")
    c = write("c.zip", "test.c", "/catalog/2/power")

    assert {f.code for f in tool.check_pair(a, b).findings} >= {"PAIR-01"}
    assert {f.code for f in tool.check_pair(a, c).findings} >= {"PAIR-OK"}
