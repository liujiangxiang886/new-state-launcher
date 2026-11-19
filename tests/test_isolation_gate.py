"""扩展隔离闸门：档位差异 + **规则分级**（2026-11-08 重划）。

这是「完全校验扩展模式」真正生效的地方 —— 前面所有扫描能力都在
``isolation.py`` 里，但**不接到安装/启用流程上就等于没做**。

两档的差别只在**阻断**，扫描内容完全相同：
- ``loose``   扫、记录、**不阻断**（告诉玩家「你装的包里有跨扩展访问」）
- ``strict``  扫、记录、**有 block 级才拒装/拒启用**

**2026-11-08 的关键改动（用户决策）**：判据从「代码写得干不干净」
换成「**装上去能不能用**」。

| 类别 | 规则 | 级别 |
|---|---|---|
| 耦合声明（读宿主/读别人资源/声明缺失） | L0-1/2/3/4/7/11 | 只 warn |
| 真实互踩（写同一文件/抢命名/依赖成环） | L0-5/6/8/12 | block |

原来的实现把两类同等对待：``api.host`` 一律 block，导致一次合集导入里
5 个「只是引用宿主」的包被拒，连带 25 个全部回滚。而本项目的扩展定位
正是「补全原版缺失内容」—— 那必然要访问宿主。

所以本文件的断言分两类：
- 用 ``BAD_SRC``（api.host）验证「扫描有效但只提示」；
- 用 ``BLOCK_SRC``（写共享状态文件）验证「真互踩仍阻断」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.errors import LauncherError  # noqa: E402
from app.core.ext.service import ExtensionService  # noqa: E402

#: 耦合声明类：直接读宿主内部状态。
#:
#: **只提示不阻断** —— 扩展定位是「补全原版缺失内容」，读宿主是必要手段。
BAD_SRC = """
extends Node

func _ready() -> void:
	var states = api.host._states
"""

#: 真实互踩类：直写共享状态文件。
#:
#: **仍然阻断** —— 两个包写同一份状态文件会真的互相覆盖存档数据，
#: 这不是代码风格问题，是装上去就会坏。
BLOCK_SRC = """
extends Node

func _ready() -> void:
	var f = FileAccess.open("user://mod_states.json", FileAccess.READ_WRITE)
"""

#: 合法源码：读自己的状态、读自己的资源
GOOD_SRC = """
extends Node

func _ready() -> void:
	var s = api.state({})
	var t = load("res://mods/mch.mine/data/x.json")
"""


def _svc(mode: str, exempt: list[str] | None = None) -> ExtensionService:
    return ExtensionService(
        {"extensions": {"extension_mode": mode, "strict_exempt": exempt or []}}
    )


def _src(tmp_path: Path, text: str) -> Path:
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.gd").write_text(text, encoding="utf-8")
    return src


# ---------------------------------------------------------------- 三档


def test_loose_mode_records_but_allows(tmp_path: Path) -> None:
    """宽松档：**扫、记、但不拦**。

    「扫」是硬要求，不是可选优化 —— 宽松档的承诺是「查出来 + 告诉玩家 +
    放行」。不扫就没得告诉，玩家看到的就只剩黑屏。
    """
    src = _src(tmp_path, BAD_SRC)
    findings, msgs, blocked = _svc("loose").isolation_gate(src, "mch.bad")
    assert findings, "宽松校验也要扫描并记录"
    # L0-1 现在是 warn：扫出来、告诉玩家，但不拦
    assert any(f["code"] == "L0-1" and f["severity"] == "warn" for f in findings)
    assert not blocked, "宽松档只记不拦"
    assert msgs


def test_legacy_loose_name_also_scans(tmp_path: Path) -> None:
    """旧名 normal / strict_warn 归一到宽松档，同样要扫描。

    旧实现的 normal 档会整段跳过扫描（零开销），那个档已经删了。
    """
    for legacy in ("normal", "strict_warn"):
        src = _src(tmp_path, BAD_SRC)
        findings, _msgs, blocked = _svc(legacy).isolation_gate(src, "mch.bad")
        assert findings, f"{legacy} 也必须扫描"
        assert not blocked, f"{legacy} 属于宽松档，不该拦"


def test_strict_mode_allows_coupling_warn(tmp_path: Path) -> None:
    """完全校验下，「读宿主」只提示，**不拒装**。

    这条锁住 2026-11-08 的核心决策：L0-1 从 block 降为 warn。
    改回去会导致「补全原生内容」类扩展全部装不上。
    """
    src = _src(tmp_path, BAD_SRC)
    findings, msgs, blocked = _svc("strict").isolation_gate(src, "mch.bad")
    assert findings, "仍要扫出来告诉作者"
    assert not blocked, "L0-1 只是耦合声明，不该拒装"
    assert msgs


def test_strict_mode_blocks_shared_state(tmp_path: Path) -> None:
    """完全校验下，「直写共享状态文件」仍然拒装。

    这是 L0-5：两个包写同一份 ``user://mod_states.json`` 会真的互相
    覆盖存档数据，属于「装上去就会坏」，必须拦。
    """
    src = _src(tmp_path, BLOCK_SRC)
    findings, msgs, blocked = _svc("strict").isolation_gate(src, "mch.shared")
    assert blocked, "L0-5 是真互踩，必须拒装"
    assert any(f["code"] == "L0-5" and f["severity"] == "block" for f in findings)
    assert msgs


def test_clean_extension_passes_in_verify_mode(tmp_path: Path) -> None:
    src = _src(tmp_path, GOOD_SRC)
    findings, _msgs, blocked = _svc("strict_verify").isolation_gate(src, "mch.mine")
    assert not blocked
    assert not [f for f in findings if f["severity"] == "block"]


def test_exempt_list_bypasses(tmp_path: Path) -> None:
    src = _src(tmp_path, BAD_SRC)
    _findings, msgs, blocked = _svc("strict_verify", ["mch.bad"]).isolation_gate(
        src, "mch.bad"
    )
    assert not blocked, "用户豁免名单必须能放行"
    assert any("豁免" in m for m in msgs)


def test_missing_source_is_not_blocked(tmp_path: Path) -> None:
    """扫不了就放行 —— 纯 pck 扩展没有源码，不能因此被拒。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    findings, msgs, blocked = _svc("strict_verify").isolation_gate(empty, "mch.nosrc")
    assert findings == [] and msgs == [] and not blocked


# ---------------------------------------------------------------- 启用闸门


def test_enable_blocker_uses_stored_findings(monkeypatch, tmp_path: Path) -> None:
    # 用 L0-5（真互踩）而不是 L0-1 —— 后者已降为提示，不该产生启用阻塞
    src = _src(tmp_path, BLOCK_SRC)
    svc = _svc("strict_verify")
    findings, _msgs, _blocked = svc.isolation_gate(src, "mch.bad")

    stored = {"id": "mch.bad", "engine_line": "4.6", "lines": {"4.6": {"isolation": findings}}}
    monkeypatch.setattr(svc, "get", lambda _eid: stored)

    msg = svc.enable_blocker("mch.bad")
    assert msg and "禁止启用" in msg
    assert "peer_access" in msg, "提示里要给出可执行的下一步"


def test_enable_blocker_allows_clean(monkeypatch, tmp_path: Path) -> None:
    svc = _svc("strict_verify")
    monkeypatch.setattr(svc, "get", lambda _eid: {"id": "mch.ok", "lines": {"4.6": {"isolation": []}}})
    assert svc.enable_blocker("mch.ok") == ""


def test_enable_blocker_inactive_when_not_verify(monkeypatch) -> None:
    for mode in ("normal", "strict_warn"):
        svc = _svc(mode)
        monkeypatch.setattr(
            svc,
            "get",
            lambda _eid: {
                "id": "x",
                "lines": {"4.6": {"isolation": [{"code": "L0-1", "severity": "block"}]}},
            },
        )
        assert svc.enable_blocker("x") == "", f"{mode} 档不该阻断启用"


def test_set_enabled_refuses_in_verify(monkeypatch, tmp_path: Path) -> None:
    """启用被判违规的扩展要抛错；停用必须永远放行。

    **必须把注册表重定向到 tmp**：``set_enabled`` 成功时会写
    ``config/extensions.json``，直接跑这个测试会把玩家真实的已装扩展清单
    覆盖成一条假数据。这类「测试写真实用户数据」是所有测试里最危险的一种。
    """
    import app.core.runtime_lock as runtime_lock
    from app.core.ext import service as service_mod

    monkeypatch.setattr(runtime_lock, "ensure_modifiable", lambda *_a, **_k: None)
    monkeypatch.setattr(service_mod, "REGISTRY", tmp_path / "extensions.json")

    # 用 L0-5（真互踩）而不是 L0-1 —— 后者已降为提示，不该阻止启用
    src = _src(tmp_path, BLOCK_SRC)
    svc = _svc("strict_verify")
    findings, _m, _b = svc.isolation_gate(src, "mch.bad")
    stored = {"id": "mch.bad", "engine_line": "4.6", "lines": {"4.6": {"isolation": findings}}}
    monkeypatch.setattr(svc, "installed", lambda: [stored])
    monkeypatch.setattr(svc, "get", lambda _eid: stored)

    with pytest.raises(LauncherError, match="禁止启用"):
        svc.set_enabled("mch.bad", True)

    # 停用必须永远放行，否则玩家连「关掉问题扩展」都做不到
    svc.set_enabled("mch.bad", False)
    assert stored["enabled"] is False
    assert (tmp_path / "extensions.json").is_file(), "停用应落盘到被重定向的注册表"
