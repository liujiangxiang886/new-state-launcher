#!/usr/bin/env python
"""4.7 线适配预检（**只读，不安装、不写 packs/**）。

回答一个问题：现成的 25 个扩展包，哪些能在 4.7 线（剧情修复版）上真正生效？

     python tools/probe_47_fit.py                    # 全部
     python tools/probe_47_fit.py --only jjxf.better_forge
     python tools/probe_47_fit.py --json out.json    # 存成机器可读

判定口径
--------
对每个包，按 4.7 基线的**真实源码**（``BaseContent.read`` 第 4 层 → pck body
里的 ``.gd``）逐条试算：

============================  ========================================
检查项                        判据
============================  ========================================
``text_patches``              ``find`` 在 4.7 源码里**恰好命中 1 次**
``method_hooks``              清单声明的 ``script::method`` 在 4.7 源码里存在
``script_members``            目标脚本存在（成员是注入的，不校验内容）
``files[]``                   只记录条数，不判定（整文件铺入与基线无关）
``data_patches``              目标 json 能读到
============================  ========================================

为什么用「恰好 1 次」而不是「能找到」：
锚点命中 2 次意味着``replace``会改到不该改的地方（或者只能改第一处），
两种都是错的。0 次是失配，2+ 次是歧义，都要报出来。

为什么这份脚本不改任何东西
--------------------------
预检的价值就是「装之前知道会怎样」。一旦写盘，玩家/作者看到的就是
「已经装了一半」的状态，反而比不装更难判断。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import paths  # noqa: E402
from app.core.ext.basecontent import BaseContent  # noqa: E402

LINE = "4.7"


@dataclass
class PackageReport:
    """一个包的预检结论。"""

    ext_id: str
    version: str = ""
    #: 该包原本装在哪条线
    installed_line: str = ""
    text_ok: int = 0
    text_total: int = 0
    hook_ok: int = 0
    hook_total: int = 0
    member_ok: int = 0
    member_total: int = 0
    data_ok: int = 0
    data_total: int = 0
    files: int = 0
    #: 具体问题（人话），最多保留 20 条
    issues: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """三档结论。

        - ``ready``：核心改动（补丁/钩子）全部命中，装上就能用
        - ``partial``：部分命中，装上会有功能缺失
        - ``broken``：核心改动全部失配，装上等于没装
        - ``noop``：这个包不含需要按基线计算的改动（纯 files[]）
        """
        core_total = self.text_total + self.hook_total
        core_ok = self.text_ok + self.hook_ok
        if core_total == 0:
            return "noop"
        if core_ok == core_total:
            return "ready"
        if core_ok == 0:
            return "broken"
        return "partial"

    def as_dict(self) -> dict:
        return {
            "id": self.ext_id,
            "version": self.version,
            "installed_line": self.installed_line,
            "verdict": self.verdict,
            "text_patches": f"{self.text_ok}/{self.text_total}",
            "method_hooks": f"{self.hook_ok}/{self.hook_total}",
            "script_members": f"{self.member_ok}/{self.member_total}",
            "data_patches": f"{self.data_ok}/{self.data_total}",
            "files": self.files,
            "issues": self.issues,
        }


def _norm(text: str) -> str:
    """统一换行与 BOM，否则锚点会因为 ``\\r\\n`` 失配。

    4.7 的源码来自 pck body，那里的换行是编辑器导出的原样；
    包里清单写锚点时是手写的。两边不统一一定会误报。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


def _read_manifest(ext_id: str, line: str) -> dict | None:
    """读这个包在某条线下的清单。

    **会回落到其它线的清单** —— 这是有意的，且是预检的关键前提：

    现在的 25 个包**只有 ``manifest-46.v2.json``**，没有 47 版。
    原因是「一条线一份清单」：清单里的补丁锚点是按那条线的原版脚本
    算出来的，换线必须重算。而重算需要真的把包装到那条线上。

    预检要回答的正是「如果装到 4.7 会怎样」，所以必须拿 4.6 那份
    清单里的锚点**去 4.7 的源码里试**。回落到别的线不是偷懒，
    就是这个工具的全部意义。

    找不到任何清单才返回 ``None``。
    """
    base = paths.PACKS / ext_id
    if not base.is_dir():
        return None
    tag = line.replace(".", "")
    # 1) 本线专用清单
    for name in (
        f"manifest-{tag}.v2.json",
        f"manifest-{tag}.json",
        f"manifest-{line}.v2.json",
    ):
        p = base / name
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    # 2) 回落到其它线的清单（预检的主要路径）
    for p in sorted(base.glob("manifest-*.json")):
        if p.name.startswith("manifest-4") and p.name != f"manifest-{tag}.v2.json":
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    # 3) 无版本号的通用名
    for name in ("manifest.v2.json", "manifest.json"):
        p = base / name
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return None


def _probe_one(cfg: dict, ext_id: str, src: BaseContent) -> PackageReport:
    """对一个包做全部检查。``src`` 是 4.7 线的 BaseContent。"""
    rep = PackageReport(ext_id=ext_id)

    # 原安装线（用于提示「它现在装在 4.6」）
    for item in (cfg.get("extensions") or {}).get("items") or []:
        if str(item.get("id") or "") == ext_id:
            rep.version = str(item.get("version") or "")
            rep.installed_line = str(item.get("engine_line") or "")
            break

    m = _read_manifest(ext_id, LINE)
    if m is None:
        rep.issues.append("找不到清单文件（packs/ 下没有 manifest-*.json）")
        return rep

    rep.version = rep.version or str(m.get("version") or "")

    # ---------------------------------------------------------- text_patches
    # 按目标脚本聚合：同一个文件只读一次（38 万条目的 pck 每读一次 0.6 秒）
    by_target: dict[str, list[dict]] = {}
    for tp in m.get("text_patches") or []:
        if not isinstance(tp, dict):
            continue
        target = _strip_target(tp.get("target"))
        if target:
            by_target.setdefault(target, []).append(tp)

    for target, patches in sorted(by_target.items()):
        raw = src.read(target)
        if raw is None:
            rep.text_total += len(patches)
            rep.issues.append(f"{target}：4.7 线上读不到这个文件")
            continue
        text = _norm(raw.decode("utf-8", "replace"))
        for tp in patches:
            rep.text_total += 1
            find = tp.get("find") or ""
            if not find:
                rep.issues.append(f"{target}：补丁缺少 find")
                continue
            count = text.count(_norm(find))
            if count == 1:
                rep.text_ok += 1
            elif count == 0:
                snippet = _norm(find).strip().splitlines()[0][:50] if find.strip() else ""
                rep.issues.append(f"{target}：锚点未命中「{snippet}」")
            else:
                snippet = _norm(find).strip().splitlines()[0][:50] if find.strip() else ""
                rep.issues.append(
                    f"{target}：锚点命中 {count} 次（歧义，只会改第一处）「{snippet}」"
                )

    # ---------------------------------------------------------- method_hooks
    hooks = m.get("method_hooks") or []
    hooks_by_script: dict[str, list[dict]] = {}
    for h in hooks:
        if not isinstance(h, dict):
            continue
        script = _strip_target(h.get("script") or h.get("target"))
        if script:
            hooks_by_script.setdefault(script, []).append(h)

    for script, hs in sorted(hooks_by_script.items()):
        raw = src.read(script)
        rep.hook_total += len(hs)
        if raw is None:
            rep.issues.append(f"{script}：钩子目标脚本在 4.7 上读不到")
            continue
        text = _norm(raw.decode("utf-8", "replace"))
        for h in hs:
            method = str(h.get("method") or "")
            if not method:
                rep.issues.append(f"{script}：钩子缺少 method")
                continue
            # 只认精确的 func 声明，避免 foo 命中 foo_bar
            needle = f"func {method}("
            if needle in text or f"func {method} (" in text:
                rep.hook_ok += 1
            else:
                rep.issues.append(f"{script}：找不到方法 {method}()")

    # -------------------------------------------------------- script_members
    members = ((m.get("scripts") or {}).get("members")) or []
    for grp in members:
        if not isinstance(grp, dict):
            continue
        target = _strip_target(grp.get("target"))
        raw = src.read(target) if target else None
        for _name in (grp.get("members") or {}):
            rep.member_total += 1
            if raw is None:
                rep.issues.append(f"{target}：注入目标在 4.7 上读不到")
            else:
                rep.member_ok += 1

    # ---------------------------------------------------------- data_patches
    for dp in m.get("data_patches") or []:
        if not isinstance(dp, dict):
            continue
        target = _strip_target(dp.get("target"))
        rep.data_total += 1
        if target and src.read(target) is not None:
            rep.data_ok += 1
        else:
            rep.issues.append(f"{target}：4.7 线上读不到这份数据")

    # ---------------------------------------------------------------- files
    rep.files = len(m.get("files") or [])

    if len(rep.issues) > 20:
        extra = len(rep.issues) - 20
        rep.issues = rep.issues[:20] + [f"… 另有 {extra} 条"]

    return rep


def _strip_target(value: object) -> str:
    """``res://scripts/x.gd`` → ``scripts/x.gd``。"""
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("res://"):
        text = text[7:]
    return text.lstrip("/")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"{LINE} 线适配预检（只读）")
    ap.add_argument("--only", action="append", default=[], help="只查指定扩展 id（可多次）")
    ap.add_argument("--json", help="把结论写成 json")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印每条问题")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    cfg = cfg_mod.load()

    # 4.7 本体必须在，否则所有 read() 都会返回 None（看起来全是失配）
    base = cfg_mod.resolve_path(((cfg.get("baselines") or {}).get("jjxf.v4.7") or {}).get("path")) \
        if ((cfg.get("baselines") or {}).get("jjxf.v4.7")) else None
    if base is None or not base.is_file():
        print(f"找不到 {LINE} 本体（配置里 jjxf.v4.7.path 指向的文件不存在）")
        return 2
    print(f"{LINE} 本体：{base}（{base.stat().st_size:,} 字节）")

    src = BaseContent(cfg, LINE)

    # 要查的包：packs/ 下所有目录（含已装与未装）
    ids = sorted(p.name for p in paths.PACKS.iterdir() if p.is_dir())
    if args.only:
        want = set(args.only)
        ids = [i for i in ids if i in want]

    print(f"待查 {len(ids)} 个包\n")

    reports = [_probe_one(cfg, ext_id, src) for ext_id in ids]

    # ------------------------------------------------------------ 输出
    order = {"ready": 0, "noop": 1, "partial": 2, "broken": 3}
    reports.sort(key=lambda r: (order.get(r.verdict, 9), r.ext_id))

    label = {
        "ready": "可装",
        "noop": "纯铺文件",
        "partial": "部分生效",
        "broken": "装不上",
    }
    print(f"{'结论':10} {'扩展':32} {'补丁':8} {'钩子':8} {'成员':8}")
    print("-" * 72)
    for r in reports:
        print(
            f"{label.get(r.verdict, r.verdict):10} {r.ext_id:32} "
            f"{r.text_ok}/{r.text_total:<6} {r.hook_ok}/{r.hook_total:<6} "
            f"{r.member_ok}/{r.member_total:<6}"
        )

    # 汇总
    from collections import Counter

    counts = Counter(r.verdict for r in reports)
    print()
    print(
        "汇总："
        + "，".join(f"{label.get(k, k)} {v} 个" for k, v in sorted(counts.items()))
    )

    # 问题明细
    bad = [r for r in reports if r.issues]
    if bad:
        print()
        print("=" * 72)
        print("问题明细")
        print("=" * 72)
        for r in bad:
            print(f"\n[{label.get(r.verdict, r.verdict)}] {r.ext_id}")
            shown = r.issues if args.verbose else r.issues[:5]
            for issue in shown:
                print(f"  · {issue}")
            if not args.verbose and len(r.issues) > 5:
                print(f"  … 另有 {len(r.issues) - 5} 条（加 -v 看全部）")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "line": LINE,
                    "baseline": str(base),
                    "generated_at": __import__("datetime").datetime.now().isoformat(
                        timespec="seconds"
                    ),
                    "summary": dict(counts),
                    "packages": [r.as_dict() for r in reports],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n已写出：{out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())