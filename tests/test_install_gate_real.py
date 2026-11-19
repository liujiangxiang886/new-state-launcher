"""闸门接在**真实安装流程**上的验证。

``test_isolation_gate.py`` 只验了 ``isolation_gate()`` 本身的行为，
但「它有没有真的被 install() 调用、拦下来之后会不会留下半成品」是另一回事。
这里走完整 install：造包 → 解包 → 扫描 → 拒绝/放行 → 查注册表。

为什么要真装而不是 mock：上一轮的经验是「单元测试绿了但链路没接上」——
``read_mod_report`` 就是 GDScript 早就写了、Python 侧一直没读。
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core.ext.service import ExtensionService  # noqa: E402

#: 真互踩：直写共享状态文件（L0-5，**仍阻断**）。
#:
#: 注：不再用 ``api.host`` 做「阻断用例」—— 2026-11-08 起 L0-1 只是提示，
#: 因为扩展定位是「补全原版缺失内容」，读宿主是必要手段。
#: 这里换成 L0-5：两个包写同一份状态文件会真的互相覆盖存档数据。
BAD_GD = """extends Node

func _ready() -> void:
\tvar f = FileAccess.open("user://mod_states.json", FileAccess.READ_WRITE)
"""

#: 合法：读自己的状态
GOOD_GD = """extends Node

func _ready() -> void:
\tvar s = api.state({})
"""


def _make_pkg(tmp: Path, tag: str, gd: str) -> Path:
    """造一个最小可安装包：payload 里放一个 .gd，扩展把它投到 mods/<id>/。"""
    ext_id = f"test.gate.{tag}"
    root = tmp / f"src-{tag}"
    payload = root / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "main.gd").write_text(gd, encoding="utf-8")
    manifest = {
        "schema": 1,
        "id": ext_id,
        "name": f"闸门测试 {tag}",
        "version": "1.0.0",
        "baseline": "jjxf.v1.45",
        "files": [{"source": "payload/main.gd", "target": f"mods/{ext_id}/main.gd"}],
    }
    (root / "extension.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    out = tmp / f"{tag}.zip"
    with zipfile.ZipFile(out, "w") as z:
        for f in root.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
    return out


def _svc(mode: str) -> ExtensionService:
    cfg = json.loads(json.dumps(cfg_mod.load()))
    cfg.setdefault("extensions", {})["extension_mode"] = mode
    return ExtensionService(cfg)


def test_verify_blocks_install_and_leaves_no_overlay(tmp: Path) -> None:
    pkg = _make_pkg(tmp, "bad", BAD_GD)
    svc = _svc("strict")
    res = svc.install(pkg, None, verify_smoke=False, target_line="4.6")

    assert not res.ok, "严格校验必须拒绝越界扩展"
    assert res.isolation_blocked is True
    # 拒绝文案是给玩家看的人话（1.6 起不再用「严格校验未通过」这种术语）
    assert "互相干扰" in res.message
    assert res.isolation, "拒绝时也要带回扫描结果，否则界面上说不出原因"
    # L0-5（直写共享状态）—— 真互踩，仍阻断。
    # L0-1（读宿主）已降为提示，不再产生拒装。
    assert any(f["code"] == "L0-5" for f in res.isolation)

    # 被拒的包不该进注册表，也不该在 packs/ 下留孤儿覆盖层
    assert svc.get("test.gate.bad") is None
    from app.core import paths

    overlay = paths.PACKS / "test.gate.bad" / "overlay-47.pck"
    assert not overlay.exists(), f"被拒后仍留下了覆盖层：{overlay}"


def test_loose_records_but_installs(tmp: Path) -> None:
    """宽松档：**照装，但必须记录扫描结果**。

    这是设计要点不是可选优化 —— 宽松档的承诺是「查出来 + 告诉玩家 +
    放行」。不扫就没得告诉，玩家看到的就只剩黑屏。而且扫描结果要进
    注册表，玩家之后切到完全校验时，启用闸门才有判据。
    """
    pkg = _make_pkg(tmp, "warn", BAD_GD)
    svc = _svc("loose")
    res = svc.install(pkg, None, verify_smoke=False, target_line="4.6")

    assert res.ok, f"宽松校验不该拒绝：{res.message}"
    assert res.isolation, "宽松校验也要记录问题"
    stored = svc.get("test.gate.warn")
    assert stored is not None
    iso = (stored.get("lines") or {}).get("4.6", {}).get("isolation")
    assert isinstance(iso, list) and iso, "扫描结果必须进注册表，启用闸门要读它"


def test_legacy_loose_names_behave_like_loose(tmp: Path) -> None:
    """旧名 normal / strict_warn 归一成宽松档：照装 + 记录，不再「跳过扫描」。"""
    for legacy in ("normal", "strict_warn"):
        pkg = _make_pkg(tmp, f"legacy{legacy.replace('_', '')}", BAD_GD)
        svc = _svc(legacy)
        res = svc.install(pkg, None, verify_smoke=False, target_line="4.6")
        assert res.ok, legacy
        assert res.isolation, f"{legacy} 也必须扫描并记录"


def test_clean_extension_installs_in_verify(tmp: Path) -> None:
    pkg = _make_pkg(tmp, "ok", GOOD_GD)
    svc = _svc("strict")
    res = svc.install(pkg, None, verify_smoke=False, target_line="4.6")
    assert res.ok, f"合法扩展不该被拦：{res.message}"
    assert not [f for f in res.isolation if f["severity"] == "block"]


def test_stored_findings_block_enable(tmp: Path) -> None:
    """装的时候是 warn 档，之后切到 verify 档 → 启用要被拒。"""
    pkg = _make_pkg(tmp, "late", BAD_GD)
    warn_svc = _svc("loose")
    assert warn_svc.install(pkg, None, verify_smoke=False, target_line="4.6").ok

    import app.core.runtime_lock as runtime_lock

    original = runtime_lock.ensure_modifiable
    runtime_lock.ensure_modifiable = lambda *_a, **_k: None
    try:
        verify_svc = _svc("strict")
        msg = verify_svc.enable_blocker("test.gate.late")
        assert msg and "禁止启用" in msg, msg
    finally:
        runtime_lock.ensure_modifiable = original
