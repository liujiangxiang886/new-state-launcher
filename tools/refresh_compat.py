"""把 runtime/_compat 下的引导脚本刷进已构建好的 host pck，并同步所有副本。

为什么不重跑 tools/build_base.py：那会重新向引擎二进制里塞一遍底座
（引擎二进制加底座 pck 近 200MB，重跑要几分钟）。
改了 `_compat/*.gd` 之后，只需要替换这几个条目，其余条目原样复制。

为什么要管「所有副本」
--------------------

``runtime/_compat/`` 是**唯一真源**（``tools/build_base.py`` 与本脚本都从它取）。
但仓库里还有它的多份派生副本：

- ``runtime/base46/_compat/``、``runtime/base47/_compat/`` —— 带线号的运行时副本
- ``dist/机甲旋风兼容启动器/runtime/{_compat,base46,base47}/`` —— 发布目录里的副本

**踩过的坑**：只改了带线号的副本，忘了真源，于是 ``tools/refresh_compat.py``
刷进 pck 的还是旧逻辑 —— 界面上看着「修好了」，游戏里跑的根本不是那份代码
（``_warnings`` 就是这么丢的：改了 base46/base47，但真源 ``runtime/_compat/``
没动，host pck 里始终是 19555 字节的旧版）。

所以本脚本现在**一次做完四件事**，并且 ``tests/test_compat_sync.py``
会把「副本与真源不一致」钉成测试失败。

用法::

    python tools/refresh_compat.py            # 两条线都刷 + 同步全部副本
    python tools/refresh_compat.py --line 4.6 # 只刷一条线
    python tools/refresh_compat.py --check    # 只检查一致性，不改任何文件
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.pck.reader import PckReader  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402

#: **唯一真源**。tools/build_base.py 与本脚本都从这里取。
COMPAT_SRC = ROOT / "runtime" / "_compat"
#: 注意：``mem_slim.gd`` 故意**不在**这里。
#: 2026-11-11 实测：它确实能把 78% 的动画帧去重，但**进程 RSS 没有下降**
#: （装 6171 MB / 不装 6148 MB）—— 原因是去重发生在 JSON 解析之后，
#: 190 万个字典早就分配完了，Godot 也不把释放的页还给系统。
#: 它只会让启动多花 4 秒，收益为零，所以不给 4.7 装。
#: 真正有效的方向是「解析前就去重」（改数据结构），那是另一件事。
COMPAT_FILES = ("bootstrap.gd", "entry.gd", "entry.tscn", "hook_runtime.gd",
                "mod_host.gd", "mount_plan.gd")

#: 仓库里所有需要跟着真源走的副本目录（相对仓库根）。
#: 含 dist/ —— 发布目录里的副本不更新的话，玩家跑的还是旧逻辑。
COMPAT_COPIES = (
    "runtime/base46/_compat",
    "runtime/base47/_compat",
    "dist/机甲旋风兼容启动器/runtime/_compat",
    "dist/机甲旋风兼容启动器/runtime/base46/_compat",
    "dist/机甲旋风兼容启动器/runtime/base47/_compat",
)

#: host pck 的所有副本位置（相对仓库根，``{tag}`` 会被 46/47 替换）。
#: 仓库根下的 runtime/hostXX/ 是构建源，dist 里还有一份发布副本 ——
#: 两份都要刷，否则 dist 里的（玩家真正跑的那份）还是旧逻辑。
HOST_PCK_COPIES = (
    "runtime/host{tag}",
    "dist/机甲旋风兼容启动器/runtime/host{tag}",
)


def _copies() -> list[Path]:
    """实际存在的副本目录（dist 不在时跳过，不该因此报错）。"""
    out: list[Path] = []
    for rel in COMPAT_COPIES:
        d = ROOT / rel
        if d.is_dir():
            out.append(d)
    return out


def sync_copies(check_only: bool = False) -> int:
    """把真源同步到所有副本。``check_only=True`` 时只报告差异，不写文件。

    返回不一致的副本数量（0 = 全部一致）。
    """
    mismatched: list[Path] = []
    for dest in _copies():
        for name in COMPAT_FILES:
            src = COMPAT_SRC / name
            dst = dest / name
            if not dst.exists():
                mismatched.append(dst)
                if not check_only:
                    shutil.copyfile(src, dst)
                continue
            if filecmp.cmp(src, dst, shallow=False):
                continue
            mismatched.append(dst)
            if not check_only:
                shutil.copyfile(src, dst)
    if mismatched and not check_only:
        for path in mismatched:
            print(f"   已同步 {path.relative_to(ROOT)}")
    return len(mismatched)


def refresh(line: str) -> int:
    """把真源刷进该线**所有** host pck 副本（仓库根 + dist）。"""
    tag = line.replace(".", "")
    targets = [ROOT / rel.format(tag=tag) for rel in HOST_PCK_COPIES]
    targets = [d for d in targets if (d / f"jjxf{tag}.pck").is_file()]
    if not targets:
        print(f"[{line}] 未找到任何 host pck（找过：{', '.join(str(t) for t in targets)}）")
        return 1

    failed = 0
    for host_dir in targets:
        failed += 1 if _refresh_one(line, host_dir) else 0
    return failed


def _refresh_one(line: str, host_dir: Path) -> int:
    tag = line.replace(".", "")
    pck_path = host_dir / f"jjxf{tag}.pck"

    reader = PckReader.open(pck_path)
    old_entries = list(reader)
    replaced = {f"_compat/{n}" for n in COMPAT_FILES}

    out = host_dir / f"jjxf{tag}.pck.new"
    writer = PckWriter(out, line=line)
    total = 0
    kept = 0
    for entry in old_entries:
        path = str(entry.path)
        if path in replaced:
            continue
        # 必须用 read_bytes：PckEntry.offset 是**相对数据区起点**的偏移，
        # 直接拿它当文件偏移去 seek 会读到别人的数据（曾把 project.godot
        # 读成 mount_plan.gd 的尾巴，导致引擎报 "Unterminated string"）。
        data = reader.read_bytes(entry)
        writer.add_bytes(path, data)
        kept += 1
        total += len(data)

    for name in COMPAT_FILES:
        writer.add_file(f"_compat/{name}", COMPAT_SRC / name)

    result = writer.commit()
    old_size = pck_path.stat().st_size

    # 原子替换：先备份，再改名。中途失败不会留下半个 pck。
    backup = host_dir / f"jjxf{tag}.pck.bak"
    if backup.exists():
        backup.unlink()
    pck_path.rename(backup)
    out.rename(pck_path)

    print(f"[{line}] 已刷新 {pck_path.relative_to(ROOT)}")
    print(f"   复用条目 : {kept}（{total:,} 字节）")
    print(f"   替换条目 : {len(COMPAT_FILES)} 个 _compat/*")
    print(f"   体积     : {old_size:,} -> {result.size:,} 字节")
    print(f"   备份     : {backup.relative_to(ROOT)}")
    return 0


def verify_pck(line: str) -> int:
    """校验**所有** host pck 副本里的 _compat/* 与真源逐字节一致。

    这是「改了真源但忘了刷 pck」的最后一道闸门 —— 刷完必须验，
    否则「以为刷了」和「真的刷了」没法区分。
    """
    tag = line.replace(".", "")
    targets = [ROOT / rel.format(tag=tag) for rel in HOST_PCK_COPIES]
    targets = [d for d in targets if (d / f"jjxf{tag}.pck").is_file()]
    if not targets:
        print(f"[{line}] 未找到任何 host pck")
        return 1

    failed = 0
    for host_dir in targets:
        pck_path = host_dir / f"jjxf{tag}.pck"
        reader = PckReader.open(pck_path)
        bad: list[str] = []
        for name in COMPAT_FILES:
            want = (COMPAT_SRC / name).read_bytes()
            try:
                got = reader.read_bytes(f"_compat/{name}")
            except (KeyError, OSError):
                bad.append(f"{name}（缺失）")
                continue
            if got != want:
                bad.append(f"{name}（内容不一致）")
        if bad:
            failed += 1
            print(f"[{line}] pck 与真源不一致：{pck_path.relative_to(ROOT)}")
            for b in bad:
                print(f"   - {b}")
        else:
            print(f"[{line}] pck 与真源一致 ✓ {pck_path.relative_to(ROOT)}")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", default="", help="只刷某条线（目前只有 4.6），默认全部")
    ap.add_argument("--check", action="store_true", help="只检查一致性，不改任何文件")
    args = ap.parse_args()

    lines = [args.line] if args.line else ["4.6"]

    if args.check:
        failed = 0
        for line in lines:
            failed += 1 if verify_pck(line) else 0
        n = sync_copies(check_only=True)
        print(f"副本检查：{len(_copies())} 个目录，{n} 处不一致")
        return 1 if (failed or n) else 0

    # 先同步副本，再刷 pck —— pck 是从真源取的，与副本无关，
    # 但先同步能保证「刷完之后全仓库一致」。
    n = sync_copies()
    print(f"副本同步：{len(_copies())} 个目录，{n} 处已更新")

    failed = 0
    for line in lines:
        failed += 1 if refresh(line) else 0

    # 刷完必须验：pck 里的内容要与真源一致。
    for line in lines:
        failed += 1 if verify_pck(line) else 0

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
