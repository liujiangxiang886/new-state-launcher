"""防「改了副本忘了改真源」——这类漂移已经坑过一次。

背景
----

``runtime/_compat/`` 是**唯一真源**（``tools/build_base.py:36`` 与
``tools/refresh_compat.py:27`` 都从它取）。但仓库里还有它的多份派生副本：

- ``runtime/base46/_compat/``、``runtime/base47/_compat/``
- ``dist/机甲旋风兼容启动器/runtime/{_compat,base46,base47}/``
- 以及被打进 ``runtime/hostXX/jjxfXX.pck`` 的那一份

**踩过的坑**：改了 ``base46``/``base47`` 两份副本，忘了真源 ``runtime/_compat/``，
于是 ``refresh_compat.py`` 刷进 pck 的还是旧逻辑 —— 界面上看着「修好了」，
游戏里跑的根本不是那份代码（``_warnings`` 就是这么丢的）。

本文件把「全仓库一致」钉成断言：**任何一处漂移都让测试变红**。
"""

from __future__ import annotations

import filecmp
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.pck.reader import PckReader  # noqa: E402

#: 唯一真源
COMPAT_SRC = ROOT / "runtime" / "_compat"

#: 真源里的文件清单
COMPAT_FILES = (
    "bootstrap.gd",
    "entry.gd",
    "entry.tscn",
    "hook_runtime.gd",
    "mod_host.gd",
    "mount_plan.gd",
)

#: 所有派生副本目录。缺目录不算失败（dist 不在时该跳过）——
#: 但 dist 在的时候**必须**一致，否则玩家跑的是旧逻辑。
COMPAT_COPIES = (
    "runtime/base46/_compat",
    "runtime/base47/_compat",
    "dist/机甲旋风兼容启动器/runtime/_compat",
    "dist/机甲旋风兼容启动器/runtime/base46/_compat",
    "dist/机甲旋风兼容启动器/runtime/base47/_compat",
)

#: host pck 副本。``{tag}`` 会被 46 / 47 替换。
HOST_PCK_COPIES = (
    "runtime/host{tag}",
    "dist/机甲旋风兼容启动器/runtime/host{tag}",
)


def _copies_present() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for rel in COMPAT_COPIES:
        d = ROOT / rel
        if d.is_dir():
            out.append((rel, d))
    return out


def test_compat_source_exists() -> None:
    """真源必须在，且文件齐全。"""
    assert COMPAT_SRC.is_dir(), f"真源目录不存在：{COMPAT_SRC}"
    for name in COMPAT_FILES:
        assert (COMPAT_SRC / name).is_file(), f"真源缺文件：{name}"


def test_copies_match_source() -> None:
    """每份副本的每个文件都必须与真源逐字节一致。"""
    problems: list[str] = []
    for rel, dest in _copies_present():
        for name in COMPAT_FILES:
            src = COMPAT_SRC / name
            dst = dest / name
            if not dst.exists():
                problems.append(f"{rel}/{name} 缺失")
                continue
            if not filecmp.cmp(src, dst, shallow=False):
                problems.append(f"{rel}/{name} 与真源不一致")
    assert not problems, (
        "compat 副本与真源漂移（改真源后要跑 tools/refresh_compat.py）：\n  "
        + "\n  ".join(problems)
    )


@pytest.mark.parametrize("tag", ["46", "47"])
def test_host_pck_matches_source(tag: str) -> None:
    """host pck 里的 _compat/* 必须与真源一致。

    这是「改了真源但忘了刷 pck」的最后一道闸门 ——
    pck 是游戏真正加载的东西，它不一致就等于没改。
    """
    for rel in HOST_PCK_COPIES:
        host_dir = ROOT / rel.format(tag=tag)
        pck_path = host_dir / f"jjxf{tag}.pck"
        if not pck_path.is_file():
            continue
        reader = PckReader.open(pck_path)
        for name in COMPAT_FILES:
            want = (COMPAT_SRC / name).read_bytes()
            got = reader.read_bytes(f"_compat/{name}")
            assert got == want, (
                f"{rel}/jjxf{tag}.pck 里的 _compat/{name} 与真源不一致 —— "
                f"跑 python tools/refresh_compat.py --line 4.{tag[-1]}"
            )


def test_refresh_compat_check_passes() -> None:
    """``refresh_compat.py --check`` 必须返回 0（真源、副本、pck 三者一致）。"""
    import subprocess

    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "refresh_compat.py"), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert p.returncode == 0, (
        "refresh_compat.py --check 失败（副本或 pck 与真源漂移）：\n"
        + p.stdout[-2000:]
        + p.stderr[-1000:]
    )


def test_mod_host_has_warnings_channel() -> None:
    """mod_host.gd 必须带 _warnings 通道。

    这条单独钉住：**``install() 返回 false`` 不能等于整个扩展失效**
    （36 个钩子挂上 33 个时，扩展主体功能应该还在）。
    """
    src = (COMPAT_SRC / "mod_host.gd").read_text(encoding="utf-8")
    assert "_warnings" in src, "mod_host.gd 缺少 _warnings 通道"
    assert "保留实例" in src, "mod_host.gd 未实现「install() false 仍保留实例」"
