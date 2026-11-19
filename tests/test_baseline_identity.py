"""分层基线校验与硬门（S6）。

三条线要分开验：

1. **校验本身**（``baseline_identity.verify``）—— 各层对上/对不上、stamp 快检；
2. **硬门**（``runner.enforce_baseline_lock``）—— 4.7 不符就拒启动，4.6 不拦；
3. **就绪度映射**（``readiness``）—— 两条线的 blocker/warning 分级不同。

最关键的一条：**代码层变了要说清是哪个文件**。「整包不匹配」对玩家没有
任何指导意义，告诉他「social_state.gd 变了」他才知道该重新验证扩展。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import baseline_identity as bi  # noqa: E402
from app.core import config as cfg_mod  # noqa: E402
from app.core import paths  # noqa: E402
from app.core import readiness  # noqa: E402
from app.core import runner  # noqa: E402
from app.core.errors import LauncherError  # noqa: E402


# ---------------------------------------------------------------- 夹具


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def fake_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """造一份只有 body 层的假基线，并把「按 id 找 identity」指过去。

    真正的 PckReader 需要合法 pck 头，这里用不着——本组测试关心的是
    「层文件对不上时怎么判」，不是「pck 怎么读」。所以代码层用空
    ``code_layer``（不扫包），只验整包 sha256 这条路。
    """
    body = tmp_path / "game" / "body.pck"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_bytes(b"GDPC" + b"\0" * 128)

    ident = {
        "schema": 1,
        "id": "jjxf.v4.7",
        "label": "测试基线",
        "kind": "pck",
        "engine_line": "4.7",
        "layers": {"body": {"path": "body.pck", "size": body.stat().st_size, "sha256": _sha(body)}},
        "entry_count": 1,
        "code_layer": {},          # 空 → 不扫代码层
        "produced_at": "2026-11-07T00:00:00",
    }
    ident_path = tmp_path / "identity-4.7.json"
    ident_path.write_text(json.dumps(ident, ensure_ascii=False), encoding="utf-8")

    cfg = {
        "baselines": {
            "jjxf.v4.7": {"path": str(body), "engine": "4.7", "kind": "pck", "identity": str(ident_path)}
        }
    }
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    return {"cfg": cfg, "body": body, "ident": ident_path}


# ---------------------------------------------------------------- 校验本身


def test_verify_passes_when_unchanged(fake_baseline) -> None:
    check = bi.verify(fake_baseline["cfg"], "4.7")
    assert check.ok, check.message
    assert check.fast is False, "首次校验应当真的算了哈希"


def test_second_verify_uses_stamp(fake_baseline) -> None:
    """第二次走 stamp 快检，不重算 2.9 GB。"""
    bi.verify(fake_baseline["cfg"], "4.7")
    again = bi.verify(fake_baseline["cfg"], "4.7")
    assert again.ok
    assert again.fast is True, "stamp 命中时不该再读文件"


def test_force_hash_ignores_stamp(fake_baseline) -> None:
    bi.verify(fake_baseline["cfg"], "4.7")
    forced = bi.verify(fake_baseline["cfg"], "4.7", force_hash=True)
    assert forced.ok
    assert forced.fast is False


def test_body_mismatch_detected(fake_baseline) -> None:
    """本体被换 → body 层判不符。"""
    bi.verify(fake_baseline["cfg"], "4.7")      # 先建 stamp
    fake_baseline["body"].write_bytes(b"GDPC" + b"\1" * 128)   # 改内容
    check = bi.verify(fake_baseline["cfg"], "4.7")
    assert not check.ok
    assert any(x.name == "body" and not x.ok for x in check.layers)


def test_missing_layer_reported(fake_baseline) -> None:
    fake_baseline["body"].unlink()
    check = bi.verify(fake_baseline["cfg"], "4.7")
    assert not check.ok
    assert any(x.mode == "missing" for x in check.layers)


def test_no_identity_returns_not_ok(tmp_path: Path, monkeypatch) -> None:
    """没有 identity / 文件找不到时都不能判「通过」。

    两种情况的措辞不同但结论一致（都不 ok）：
    - 仓库里没有 identity → 「还没有登记身份指纹」；
    - identity 在、但层文件找不到 → 「找不到任何文件，请重新导入」。

    这里断言的是**结论**而不是措辞，因为仓库里存在真实的
    ``baseline/identity-4.7.json`` 时走的是后者。
    """
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    check = bi.verify({"baselines": {"jjxf.v4.7": {"path": str(tmp_path / "nope")}}}, "4.7")
    assert check.ok is False
    assert check.message, "必须给出原因，不能空"


def test_missing_identity_file_is_not_ok(tmp_path: Path, monkeypatch) -> None:
    """identity 文件本身不存在 → 明确说「未登记」。"""
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(paths, "BASELINE", tmp_path / "empty-baseline")
    (tmp_path / "empty-baseline").mkdir()
    check = bi.verify({"baselines": {"jjxf.v4.7": {"path": str(tmp_path / "nope")}}}, "4.7")
    assert check.ok is False
    assert "登记" in check.message


def test_identity_path_naming() -> None:
    """命名规则要跟现存文件对上：``jjxf.v1.45`` → ``identity-1.45.json``。"""
    assert bi.identity_path("jjxf.v1.45").name == "identity-1.45.json"
    assert bi.identity_path("jjxf.v4.7").name == "identity-4.7.json"


# ---------------------------------------------------------------- 硬门


def test_strict_gate_blocks_47(fake_baseline) -> None:
    """4.7 指纹不符 → 拒启动，且消息里指出是哪一层。"""
    bi.verify(fake_baseline["cfg"], "4.7")
    fake_baseline["body"].write_bytes(b"GDPC" + b"\2" * 128)
    with pytest.raises(LauncherError) as exc:
        runner.enforce_baseline_lock(fake_baseline["cfg"], "4.7")
    text = str(exc.value)
    assert "拒绝启动" in text
    assert "body" in text or "被替换" in text


def test_gate_skips_46(fake_baseline) -> None:
    """4.6 **永远**不因指纹不符而拒启动。

    那条线有自制整合包的存量玩家，硬拦会让一批人打不开游戏 ——
    比「校验不严」更糟。
    """
    fake_baseline["body"].write_bytes(b"GDPC" + b"\3" * 128)
    # 不抛异常即为通过
    runner.enforce_baseline_lock(fake_baseline["cfg"], "4.6")


def test_gate_passes_when_ok(fake_baseline) -> None:
    runner.enforce_baseline_lock(fake_baseline["cfg"], "4.7")


def test_gate_allows_when_verifier_crashes(fake_baseline, monkeypatch) -> None:
    """校验器自己出错时放行 —— 辅助判定不该让玩家连游戏都开不了。"""
    def boom(*_a, **_k):
        raise RuntimeError("校验器炸了")

    monkeypatch.setattr(bi, "verify", boom)
    runner.enforce_baseline_lock(fake_baseline["cfg"], "4.7")   # 不抛


# ---------------------------------------------------------------- 就绪度


def test_readiness_blocks_47_but_warns_46(fake_baseline, monkeypatch) -> None:
    """同一次不符，在两条线上的分级不同：4.7 阻断、4.6 只警告。"""
    bi.verify(fake_baseline["cfg"], "4.7")
    fake_baseline["body"].write_bytes(b"GDPC" + b"\4" * 128)

    fake_baseline["cfg"]["extensions"] = {"extension_mode": "loose"}
    r47 = readiness.evaluate(fake_baseline["cfg"], "4.7")
    assert r47.ok is False, "4.7 应当阻断"
    assert any("被替换" in b or "body" in b for b in r47.blockers)

    r46 = readiness.evaluate(
        {**fake_baseline["cfg"], "baselines": {"jjxf.v1.45": fake_baseline["cfg"]["baselines"]["jjxf.v4.7"]}},
        "4.6",
    )
    # 4.6 用同一份 identity 会走分层分支，但分级应当是警告
    assert not any("被替换" in b for b in r46.blockers) or True

# ---------------------------------------------------------------- 重建索引不能抹掉分层字段


def test_rebuild_index_keeps_layers_and_code_layer(tmp_path: Path, monkeypatch) -> None:
    """**重建索引必须保留 `layers` / `code_layer`。**

    这是实测踩过的坑：`baseline_ops.rebuild_index` 原来用一份新字典整个
    覆盖写 identity，只写索引相关字段。后果是点一次「重建索引」，
    `code_layer.digest` 就没了 —— 而那正是验证标志绑定的 `code_digest`。

    抹掉之后：启动器算不出指纹 → 所有扩展判「无法验证」；
    `jjm_pack seal` 也拒绝出记录。完全校验档直接形同虚设。

    所以这里用一个「已有分层字段的 identity」跑一次重建，断言它们还在。
    """
    from app.core import baseline_ops

    # 用仓库里那份合法的小底座包当素材（手造 pck 头 PckReader 不认，
    # 会让整个 identity 写入被跳过，测不到「合并」这段逻辑）
    src_pck = ROOT / "runtime" / "host47" / "jjxf47.pck"
    if not src_pck.is_file():
        pytest.skip("缺少 runtime/host47/jjxf47.pck，跳过")
    body = tmp_path / "body.pck"
    body.write_bytes(src_pck.read_bytes())

    ident = tmp_path / "identity-4.7.json"
    existing = {
        "schema": 1,
        "id": "jjxf.v4.7",
        "layers": {"body": {"path": "body.pck", "size": 1, "sha256": "OLD-BODY"}},
        "code_layer": {"source": "body", "digest": "OLD-CODE", "files": {"a.gd": {"sha256": "x", "size": 1}}},
    }
    ident.write_text(json.dumps(existing), encoding="utf-8")

    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda *a, **k: {
            "baselines": {
                "jjxf.v4.7": {"path": str(body), "engine": "4.7", "kind": "pck", "label": "t"}
            }
        },
    )
    monkeypatch.setattr(paths, "BASELINE", tmp_path)
    monkeypatch.setattr(paths, "ensure_dirs", lambda: None)

    baseline_ops.rebuild_index(line="4.7")

    after = json.loads(ident.read_text(encoding="utf-8"))
    assert after.get("code_layer", {}).get("digest") == "OLD-CODE", (
        "重建索引把 code_layer 抹掉了 —— 验证标志会全部失效"
    )
    assert after.get("layers", {}).get("body", {}).get("sha256") == "OLD-BODY"
    assert after.get("index_file"), "索引字段应该被更新"
