"""合并层（merged.pck）与 overlay 签名缓存的正确性。

背景：``run_dir`` 按时间戳每次新建，所以 ``merged.pck`` 原先**永远缓存
miss** —— 插桩层有缓存、合并层没有。19 个扩展每次启动都要把每个重叠目标
的大文件读一遍再叠一遍，这是启动慢的主要来源之一。

这组测试守住两件事：

1. 同一套扩展第二次调用命中缓存，且产出的内容与第一次**逐字节相同**；
2. 内容变了（overlay 被改）缓存立即失效，不会拿旧的合并结果糊弄。

以及 ``_overlay_signature`` 的「同一个 overlay 只读一次」：
19 个扩展 × 每个改多个脚本时，原实现对每次出现都整包读盘。
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
from app.core.ext import enable as enable_mod  # noqa: E402


def _manifest(data_patches: list[dict]) -> dict:
    return {"schema": 3, "data_patches": data_patches}


def _ops(target: str, pointer: str, value) -> dict:
    return {
        "target": target,
        "ops": [{"op": "replace", "path": pointer, "value": value}],
    }


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 CACHE / BUILDS，并提供一个可读的「原版内容」来源。"""
    cache = tmp_path / "cache"
    builds = tmp_path / "builds"
    packs = tmp_path / "packs"
    for d in (cache, builds, packs):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "CACHE", cache)
    monkeypatch.setattr(paths, "BUILDS", builds)
    monkeypatch.setattr(paths, "PACKS", packs)
    return tmp_path


def _mods_two_overlap(tmp: Path | None = None) -> list[dict]:
    """两个包改同一个 JSON 指针 —— 这是会产生合并层的典型情况。

    ``_build_merged`` 通过 ``_manifest_of`` **从磁盘**读清单
    （``packs/<id>/manifest-46.v2.json``），所以必须真的落盘。
    """
    mods = []
    for ext_id, value in (("mod.a", 2), ("mod.b", 3)):
        item = {"id": ext_id, "overlay": ""}
        if tmp is not None:
            root = paths.PACKS / ext_id
            root.mkdir(parents=True, exist_ok=True)
            (root / "manifest-46.v2.json").write_text(
                json.dumps(
                    _manifest([_ops("data/e.json", "/catalog/1/power", value)]),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        mods.append(item)
    return mods


def test_merged_is_cached_and_identical(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次调用命中缓存，内容与第一次完全一致。"""
    calls: list[str] = []

    def fake_build(pairs, *, read_base, read_overlay, out_pck, line, origin_base, plan):
        calls.append(str(out_pck))
        from app.core.ext import compose as compose_mod

        pck = Path(out_pck)
        pck.write_bytes(b"GDPC-fake-merged")
        res = compose_mod.Result(
            entries={"data/e.json": b'{"catalog":{"1":{"power":3}}}'},
            notes=["测试用合并结果"],
            pck=pck,
            sha256="deadbeef",
            size=17,
        )
        return res

    monkeypatch.setattr("app.core.ext.compose.build", fake_build)
    monkeypatch.setattr(
        enable_mod, "_baseline_signature", lambda cfg, line: "baseline-fixed"
    )

    cfg = cfg_mod.load(sandbox / "launcher.json")
    mods = _mods_two_overlap(sandbox)

    run1 = sandbox / "builds" / "run-1"
    run1.mkdir(parents=True)
    first, text1 = enable_mod._build_merged(cfg, "4.6", run1, mods, None)
    assert first is not None and first.entries
    assert first.entries["data/e.json"].startswith(b"{")
    assert len(calls) == 1, "第一次必须真的构建"

    run2 = sandbox / "builds" / "run-2"
    run2.mkdir(parents=True)
    second, text2 = enable_mod._build_merged(cfg, "4.6", run2, mods, None)

    assert len(calls) == 1, "第二次不该再重建"
    assert second is not None
    assert second.entries == first.entries, "命中缓存后内容必须一致"
    assert text2 == text1, "命中缓存后脚本文本必须一致"
    assert (run2 / "merged.pck").is_file(), "缓存命中也要把产物放进本次 run_dir"
    assert (run2 / "merged.pck").read_bytes() == (run1 / "merged.pck").read_bytes()
    assert any("命中合并层缓存" in n for n in second.notes)


def test_merged_cache_invalidated_when_overlay_changes(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """overlay 变了（版本升级 / 内容被改）→ 缓存必须失效。"""
    calls: list[int] = []

    def fake_build(pairs, *, read_base, read_overlay, out_pck, line, origin_base, plan):
        calls.append(1)
        from app.core.ext import compose as compose_mod

        pck = Path(out_pck)
        pck.write_bytes(b"GDPC-v" + str(len(calls)).encode())
        return compose_mod.Result(
            entries={"data/e.json": b"{}"}, pck=pck, sha256=f"s{len(calls)}", size=10
        )

    monkeypatch.setattr("app.core.ext.compose.build", fake_build)
    monkeypatch.setattr(
        enable_mod, "_baseline_signature", lambda cfg, line: "baseline-fixed"
    )

    cfg = cfg_mod.load(sandbox / "launcher.json")
    # 给两个包各自一个真实存在的 overlay 文件，才能被 stat 进缓存键
    ov_a = sandbox / "a.pck"
    ov_b = sandbox / "b.pck"
    ov_a.write_bytes(b"AAA")
    ov_b.write_bytes(b"BBB")
    mods = _mods_two_overlap(sandbox)
    mods[0]["overlay"] = str(ov_a)
    mods[1]["overlay"] = str(ov_b)

    run1 = sandbox / "builds" / "r1"
    run1.mkdir(parents=True)
    enable_mod._build_merged(cfg, "4.6", run1, mods, None)
    assert len(calls) == 1

    run2 = sandbox / "builds" / "r2"
    run2.mkdir(parents=True)
    enable_mod._build_merged(cfg, "4.6", run2, mods, None)
    assert len(calls) == 1, "内容没变应当命中缓存"

    # 改一个字节（并确保 mtime 变化）
    import os
    import time

    time.sleep(0.01)
    ov_a.write_bytes(b"AAB")
    os.utime(ov_a, None)

    run3 = sandbox / "builds" / "r3"
    run3.mkdir(parents=True)
    enable_mod._build_merged(cfg, "4.6", run3, mods, None)
    assert len(calls) == 2, "overlay 变了必须重建"


def test_no_overlap_produces_no_cache(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有重叠时不产出合并层，也不写缓存。

    否则下次会被「有一份空缓存」误导成「有合并层要挂」。
    """
    from app.core.ext import compose as compose_mod

    def fake_build(pairs, *, read_base, read_overlay, out_pck, line, origin_base, plan):
        return compose_mod.Result()  # 无 entries、无 pck

    monkeypatch.setattr("app.core.ext.compose.build", fake_build)
    monkeypatch.setattr(
        enable_mod, "_baseline_signature", lambda cfg, line: "baseline-fixed"
    )

    cfg = cfg_mod.load(sandbox / "launcher.json")
    run = sandbox / "builds" / "r"
    run.mkdir(parents=True)
    merged, text = enable_mod._build_merged(cfg, "4.6", run, _mods_two_overlap(sandbox), None)
    assert merged is not None and not merged.entries
    assert text == {}
    cache_dir = paths.CACHE / "merged"
    assert not cache_dir.exists() or not list(cache_dir.glob("*.pck"))


def test_overlay_signature_reads_each_overlay_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个 overlay 被多个脚本引用时，只读一次。

    ``overlay_targets`` 是倒排索引（脚本 → 覆盖它的 overlay），所以同一个
    overlay 会因为改了 N 个脚本而出现 N 次。原实现对每次出现都整包
    ``read_bytes()``，19 个扩展就是几十次全量读盘 —— 而算出来的只是缓存键。
    """
    ov = tmp_path / "overlay-46.pck"
    ov.write_bytes(b"GDPC" + b"\0" * 64)

    reads: list[Path] = []
    real_read = Path.read_bytes

    def counting(self):  # noqa: ANN001
        reads.append(self)
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting)

    by_script = {f"scripts/s{i}.gd": [] for i in range(5)}
    targets = {f"scripts/s{i}.gd": [ov] for i in range(5)}

    sig = enable_mod._overlay_signature(by_script, targets)
    assert len(sig) == 5, "每个脚本都要有签名"
    assert len({d for _, d in sig}) == 1, "同一个文件签名应当相同"
    assert len(reads) == 1, f"同一个 overlay 只该读一次，实际读了 {len(reads)} 次"




def test_overlay_signature_stable_across_calls(tmp_path: Path) -> None:
    """同一份文件重复算签名，结果必须一致（缓存键才稳定）。"""
    ov = tmp_path / "overlay-46.pck"
    ov.write_bytes(b"GDPC" + b"x" * 32)
    by_script = {"scripts/a.gd": []}
    targets = {"scripts/a.gd": [ov]}
    assert enable_mod._overlay_signature(by_script, targets) == enable_mod._overlay_signature(
        by_script, targets
    )