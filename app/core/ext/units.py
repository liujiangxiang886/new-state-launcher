"""位置级冲突发现：同一个文件的**同一处**被两个扩展写，才算冲突。

为什么要有这一层
----------------
引擎挂载覆盖层时只知道「同名文件后挂的赢」，不知道文件里第 100 行是谁写的。
所以过去两个包改同一个 ``data/equipment.json`` 的不同字段，也会被判成冲突。
真正需要人工决定的只有**同一处**：同一个 JSON 指针、同一段脚本、同一份整文件。

本模块只做发现与授权判定，不做取舍；取舍在 :mod:`policy`。

三条判定规则
------------
1. JSON 指针相同，或父子指针相交（``/a`` ⊃ ``/a/b``）→ 冲突；
2. 在同一个父数组上增删（``add`` / ``remove`` / ``move`` / ``copy``）→
   后续下标会位移，整个父数组按冲突范围处理；
3. 文本锚点完全相同，或两个字面量锚点互相包含 → 冲突。

同文件、位置不相交**不是**冲突，交给合并层正常叠加。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from . import manifest as manifest_mod

#: 会改变数组下标的操作。命中这些操作时，父数组整体视为不稳定。
_INDEX_SHIFT_OPS = frozenset({"add", "remove", "move", "copy"})

#: 锚点「包含」判定的最小长度。短于这个长度不参与包含判定——
#: 像 ``find: "value"`` 这种通用锚点会让任何含它的补丁都被判成冲突，
#: 而真正的嵌套（一行代码套另一行）长度远超这个数。
#: ``tools/jjm_pack.py`` 里的 ``MIN_CONTAIN_LEN`` 必须与它一致。
MIN_CONTAIN_LEN = 8


def _norm(target: object) -> str:
    text = str(target or "").strip().replace("\\", "/")
    if text.startswith("res://"):
        text = text[len("res://"):]
    return text.lstrip("/")


@lru_cache(maxsize=65536)
def _tokens(pointer: str) -> tuple[str, ...]:
    # 缓存是必须的：发现阶段对同一批路径做两两比对（O(n²)），不缓存时
    # 同一个指针会被反复分词上百万次（实测 1.45 基线 + 17 个扩展要
    # 1.4 秒，全部花在重复的 replace/split 上），UI 每次切到扩展页都卡。
    if pointer in ("", "/"):
        return ()
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/"))


def _pointer_of(tokens: Iterable[str]) -> str:
    items = list(tokens)
    return "/" + "/".join(str(t).replace("~", "~0").replace("/", "~1") for t in items) if items else "/"


def _intersects(a: str, b: str) -> bool:
    """两个 JSON 指针是否相同、或互为父子。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        # 根指针对任何位置都成立
        return True
    shorter = min(len(ta), len(tb))
    return ta[:shorter] == tb[:shorter]


def _covered_by(path: str, parent: str) -> bool:
    """``path`` 是否在 ``parent`` 之下（含相等）。"""
    tp, tq = _tokens(path), _tokens(parent)
    return tp[: len(tq)] == tq


def _looks_like_index(token: str) -> bool:
    return token == "-" or (token.isdigit() and token != "")


@dataclass
class Unit:
    """一处冲突：``target`` 上的 ``locator`` 被 ``parties`` 同时写。"""

    target: str
    locator: str
    kind: str                      # data / text / files
    parties: list[str]             # 按解析顺序（先 → 后）
    detail: str = ""
    #: 原文补丁信息：文本的 find/mode，整文件的提供者等。供处置面板展示。
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.target, self.locator, self.kind, tuple(self.parties))

    @property
    def scope(self) -> str:
        """冲突的最小单位：位置级；整文件是文件级。"""
        return "file" if self.kind == "files" else "location"

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "locator": self.locator,
            "kind": self.kind,
            "parties": list(self.parties),
            "detail": self.detail,
            "scope": self.scope,
            "payload": self.payload,
        }


def describe(unit: Unit) -> str:
    """人话位置：给玩家/作者看的，不出现「指针」「锚点」这些词。"""
    if unit.kind == "files":
        return f"{unit.target} 整份文件"
    if unit.kind == "text":
        return f"{unit.target} 的同一段代码"
    locator = unit.locator if unit.locator not in ("", "/") else "整份内容"
    return f"{unit.target} 的 {locator}"


def _text_locator(mode: str, find: str) -> str:
    digest = hashlib.sha1(str(find).encode("utf-8")).hexdigest()[:12]
    return f"text:{mode}:{digest}"


def _manifest_of(item: dict) -> dict:
    return item


def discover(
    items: Iterable[tuple[str, dict]],
    *,
    is_array: Any = None,
) -> list[Unit]:
    """扫描一组清单，返回所有「同一处」冲突（不含同文件不同位置）。

    ``is_array(target, parent_pointer) -> bool`` 是可选的类型探测回调。
    没有它时只把 ``/-`` 当成确定的数组追加——真实包里 ``/catalog/9005115``
    这种数字是**字典键**，按数字猜数组会制造大量假冲突。
    """
    pairs = [(str(pid), dict(data or {})) for pid, data in items if pid]
    units: list[Unit] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()

    #: ``pid -> 在 pairs 里的下标``。
    #
    # 用途只有一个：按「解析顺序」排 ``parties``。原来这四处都写成
    # ``[pid for pid, _ in pairs if pid in 某集合]``，也就是每产出**一条**冲突
    # 就线性扫一遍全部扩展；而这几行位于 O(n²) 的双重循环最内层，于是
    # ``len(pairs)`` 被反复取了几千万次。
    #
    # 实测（2026-11-14，本机 24 个扩展）：``readiness.evaluate('4.7')``
    # 累计 6293 万次 ``len()`` + 3146 万次 ``min()``，光这一项 9.0 秒，
    # 全部发生在 UI 线程上——这就是「选完角色进游戏时整个启动器卡死」。
    # 改成索引后同样的输出，取 parties 降到 O(k log k)，k = 冲突涉及方数量。
    order: dict[str, int] = {}
    for index, (pid, _data) in enumerate(pairs):
        order.setdefault(pid, index)

    def parties_of(pids: Iterable[str]) -> list[str]:
        """按解析顺序排出涉及方（与旧实现的输出逐项一致）。"""
        unique = {str(p) for p in pids if str(p) in order}
        return sorted(unique, key=order.__getitem__)

    def add(unit: Unit) -> None:
        key = unit.key
        if key in seen:
            return
        seen.add(key)
        units.append(unit)

    # ---------------- 数据补丁
    data_by_target: dict[str, list[dict]] = {}
    for pid, data in pairs:
        for patch in data.get("data_patches") or []:
            if not isinstance(patch, dict):
                continue
            target = _norm(patch.get("target"))
            if not target:
                continue
            for index, op in enumerate(manifest_mod.ops_of(patch)):
                if not isinstance(op, dict):
                    continue
                path = str(op.get("path") or "/") or "/"
                data_by_target.setdefault(target, []).append(
                    {"pid": pid, "path": path, "op": str(op.get("op") or "replace"), "index": index}
                )

    for target, entries in sorted(data_by_target.items()):
        # 数组增删：父数组整体不稳定。
        # 只有 ``/-`` 是确定的数组追加；数字段可能是字典键（真实包的
        # ``/catalog/9005115`` 就是字典键），不能凭数字猜。
        unstable: dict[str, str] = {}   # parent -> pid that shifts
        for entry in entries:
            if entry["op"] not in _INDEX_SHIFT_OPS:
                continue
            tokens = _tokens(entry["path"])
            if not tokens:
                continue
            parent = _pointer_of(tokens[:-1])
            if tokens[-1] == "-" or (is_array is not None and is_array(target, parent)):
                unstable.setdefault(parent, entry["pid"])

        for parent, shifter in unstable.items():
            hits = {
                e["pid"]
                for e in entries
                if e["pid"] != shifter and _covered_by(e["path"], parent)
            }
            if hits:
                parties = parties_of(hits | {shifter})
                add(
                    Unit(
                        target=target,
                        locator=parent,
                        kind="data",
                        parties=parties,
                        detail="数组增删会让后续下标位移",
                        payload={"reason": "index-shift"},
                    )
                )

        # 同指针 / 父子指针。
        #
        # 判定条件只有一条：**一方的 tokens 是另一方的前缀**（含完全相等）；
        # 任一方是根指针时对任何位置都成立。
        #
        # 旧实现直接对「不同扩展 × 不同扩展」做全量两两比对，规模是
        # ``sum(ga) × sum(gb)``：光 ``qt6663.exp_equipment`` 一个扩展就有
        # 6891 条 op，与另外两个 qt6663 包两两组合就是 1.6 亿量级。
        #
        # 实测（2026-11-14，本机 24 个扩展 / 12925 条 op）：
        #   · 两两比对实际执行 3146 万次 ``min()`` + 6293 万次 ``len()``，
        #     ``discover`` 耗时 1337 ms，而**产出的冲突是 0 条**；
        #   · 它跑在 UI 线程上（``readiness.evaluate``），4.7 线首次评估
        #     5.8 秒里有 1.3 秒来自这里 —— 玩家感知是「选完角色进游戏时
        #     启动器整个卡死」。
        #
        # 优化只换「怎么找候选」，**不改判定与产出顺序**：
        #   · 外层仍按 pid 桶顺序遍历（保证 ``parties`` 与 ``locator`` 与旧
        #     实现逐项一致，否则去重键变化会导致冲突条数变化）；
        #   · 内层不再扫全部桶，改为查两个索引：
        #       ``by_tokens[n]`` —— 完整 tokens 为 n 的条目（对方是 a 的前缀）
        #       ``by_prefix[n]`` —— 以 n 为前缀的条目（a 是对方的前缀）
        #     每次查询只返回真正可能构成前缀关系的条目。
        buckets: dict[str, list[dict]] = {}
        by_tokens: dict[tuple[str, ...], list[dict]] = {}
        by_prefix: dict[tuple[str, ...], list[dict]] = {}
        for e in entries:
            tok = _tokens(e["path"])
            e["tok"] = tok
            buckets.setdefault(e["pid"], []).append(e)
            by_tokens.setdefault(tok, []).append(e)
            for depth in range(1, len(tok) + 1):
                by_prefix.setdefault(tok[:depth], []).append(e)

        groups = list(buckets.values())
        #: 条目 → 它在「旧实现遍历顺序」里的位置 ``(桶号, 桶内序号)``。
        #: 用它把候选排回原来的顺序：产出的 ``Unit`` 顺序会影响
        #: ``resolver`` / UI 的呈现顺序，必须与旧实现逐项一致。
        #:
        #: 用挂在条目上的整数字段而不是 ``id(obj)`` 做键：``id()`` 是内存
        #: 地址，对象被回收后会被复用，用它排序会得到不稳定的结果。
        for gi, ga in enumerate(groups):
            for ii, e in enumerate(ga):
                e["seq"] = gi
                e["sub"] = ii
        order_key = lambda c: (c["seq"], c["sub"])  # noqa: E731

        for gi, ga in enumerate(groups):
            later = [e for gb in groups[gi + 1:] for e in gb]
            later_roots = [e for e in later if not e["tok"]]
            later_pids = {e["pid"] for e in later}
            for a in ga:
                ta = a["tok"]
                # 候选 = 后面桶里、与 a 有前缀关系（或任一方是根指针）的条目
                candidates: dict[int, dict] = {}
                if ta:
                    for depth in range(1, len(ta) + 1):
                        for c in by_tokens.get(ta[:depth], ()):
                            if c["pid"] in later_pids:
                                candidates[id(c)] = c
                    for c in by_prefix.get(ta, ()):
                        if c["pid"] in later_pids:
                            candidates[id(c)] = c
                else:
                    # a 自己是根指针：对后面所有条目都成立
                    for c in later:
                        candidates[id(c)] = c
                # 后面桶里的根指针对 a 的任何位置都成立（旧实现里
                # ``ta and tb`` 为假时直接判冲突）
                for c in later_roots:
                    candidates[id(c)] = c
                # 排回旧顺序：先按桶号、再按桶内序号
                for b in sorted(candidates.values(), key=order_key):
                    tb = b["tok"]
                    locator = a["path"] if len(ta) <= len(tb) else b["path"]
                    parties = parties_of({a["pid"], b["pid"]})
                    add(
                        Unit(
                            target=target,
                            locator=locator,
                            kind="data",
                            parties=parties,
                            detail="两边写了同一个位置",
                            payload={"paths": sorted({a["path"], b["path"]})},
                        )
                    )

    # ---------------- 文本补丁
    text_by_target: dict[str, list[dict]] = {}
    for pid, data in pairs:
        for patch in data.get("text_patches") or []:
            if not isinstance(patch, dict):
                continue
            target = _norm(patch.get("target"))
            find = str(patch.get("find") or "")
            if not target or not find:
                continue
            text_by_target.setdefault(target, []).append(
                {
                    "pid": pid,
                    "mode": str(patch.get("mode") or "literal"),
                    "find": find,
                    "replace": str(patch.get("replace") or ""),
                }
            )

    for target, entries in sorted(text_by_target.items()):
        for i, a in enumerate(entries):
            for b in entries[i + 1:]:
                if a["pid"] == b["pid"]:
                    continue
                same = a["mode"] == b["mode"] and a["find"] == b["find"]
                shorter = min(len(a["find"]), len(b["find"]))
                contained = (
                    a["mode"] == "literal"
                    and b["mode"] == "literal"
                    and shorter >= MIN_CONTAIN_LEN
                    and (a["find"] in b["find"] or b["find"] in a["find"])
                )
                if not (same or contained):
                    continue
                locator = _text_locator(a["mode"], a["find"] if same else min(a["find"], b["find"], key=len))
                parties = parties_of({a["pid"], b["pid"]})
                add(
                    Unit(
                        target=target,
                        locator=locator,
                        kind="text",
                        parties=parties,
                        detail="两边改了同一段代码",
                        payload={
                            "find": a["find"] if same else min(a["find"], b["find"], key=len),
                            "mode": a["mode"],
                            "replace": a["replace"],
                        },
                    )
                )

    # ---------------- 整文件提供
    file_owners: dict[str, list[str]] = {}
    for pid, data in pairs:
        for rec in data.get("files") or []:
            if not isinstance(rec, dict):
                continue
            target = _norm(rec.get("target"))
            if target:
                file_owners.setdefault(target, [])
                if pid not in file_owners[target]:
                    file_owners[target].append(pid)
    for target, owners in sorted(file_owners.items()):
        if len(owners) > 1:
            parties = parties_of(owners)
            add(
                Unit(
                    target=target,
                    locator="@file",
                    kind="files",
                    parties=parties,
                    detail="两边都提供了整份文件",
                )
            )

    # 同一组人、同一处，只留一条：``add /routes/-`` 同时命中「父数组位移」
    # 和「同指针」两条规则，不去重的话处置面板会出现两行说同一件事。
    units = _prune_index_shift(units)

    # 按目标、位置稳定排序，UI 与报告的顺序才不会随安装顺序抖
    units.sort(key=lambda u: (u.target, u.kind, u.locator, u.parties))
    return units


def _prune_index_shift(units: list[Unit]) -> list[Unit]:
    """父数组位移的判定比具体位置更宽，有具体位置时让位给具体位置。"""
    specific: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for unit in units:
        if unit.payload.get("reason") == "index-shift":
            continue
        specific.setdefault((unit.target, tuple(unit.parties)), []).append(unit.locator)

    kept: list[Unit] = []
    for unit in units:
        if unit.payload.get("reason") == "index-shift":
            children = specific.get((unit.target, tuple(unit.parties)), [])
            if any(k != unit.locator and _covered_by(k, unit.locator) for k in children):
                continue
        kept.append(unit)
    return kept


def _requires(item: dict) -> set[str]:
    out: set[str] = set()
    for req in item.get("requires") or []:
        rid = str(req.get("id") if isinstance(req, dict) else req)
        if rid:
            out.add(rid)
    return out


def _overrides_of(unit: Unit, data: dict) -> set[str]:
    """后包在这个位置上声明要「接管」谁。"""
    out: set[str] = set()
    if unit.kind == "files":
        for rec in data.get("files") or []:
            if isinstance(rec, dict) and _norm(rec.get("target")) == unit.target:
                out.update(str(x) for x in (rec.get("overrides") or []))
        return out

    if unit.kind == "text":
        for patch in data.get("text_patches") or []:
            if not isinstance(patch, dict):
                continue
            if _norm(patch.get("target")) != unit.target:
                continue
            locator = _text_locator(str(patch.get("mode") or "literal"), str(patch.get("find") or ""))
            if locator == unit.locator:
                out.update(str(x) for x in (patch.get("overrides") or []))
        return out

    for patch in data.get("data_patches") or []:
        if not isinstance(patch, dict) or _norm(patch.get("target")) != unit.target:
            continue
        patch_overrides = {str(x) for x in (patch.get("overrides") or [])}
        for op in manifest_mod.ops_of(patch):
            if not isinstance(op, dict):
                continue
            path = str(op.get("path") or "/") or "/"
            if _intersects(path, unit.locator):
                out.update(patch_overrides)
                out.update(str(x) for x in (op.get("overrides") or []))
    return out


def _has_expect(unit: Unit, data: dict) -> bool:
    """接管方必须带前置断言，否则「接管」会退化成「后装即赢」。"""
    if unit.kind == "files":
        for rec in data.get("files") or []:
            if isinstance(rec, dict) and _norm(rec.get("target")) == unit.target:
                exp = rec.get("expect")
                if isinstance(exp, dict) and (exp.get("sha256") or exp.get("size") is not None):
                    return True
        return False
    if unit.kind == "text":
        for patch in data.get("text_patches") or []:
            if not isinstance(patch, dict):
                continue
            if _norm(patch.get("target")) != unit.target:
                continue
            if patch.get("expect_sha256"):
                return True
        return False
    for patch in data.get("data_patches") or []:
        if not isinstance(patch, dict) or _norm(patch.get("target")) != unit.target:
            continue
        for op in manifest_mod.ops_of(patch):
            if not isinstance(op, dict):
                continue
            path = str(op.get("path") or "/") or "/"
            if not _intersects(path, unit.locator):
                continue
            # 1.45.1 的写法是 `"expect": 值`（非 dict），归一后等价 {"eq": 值}，
            # 两种都算带了断言；空 dict 视为没写。
            expect = op.get("expect")
            if expect is not None and expect != {}:
                return True
    return False


def authorized(unit: Unit, meta: dict[str, dict]) -> tuple[bool, str]:
    """后包是否被允许接管前包。

    三个条件缺一不可：``overrides`` 点名被接管方、``requires`` 声明依赖、
    补丁带 ``expect``（运行时会校验前值）。缺 ``expect`` 的接管视为无效——
    否则「接管」会退化成「后装即赢」。
    """
    if len(unit.parties) < 2:
        return False, "只有一个扩展"
    later = unit.parties[-1]
    earlier = unit.parties[:-1]
    data = meta.get(later) or {}
    named = _overrides_of(unit, data)
    missing = [pid for pid in earlier if pid not in named]
    if missing:
        return False, f"{later} 没有声明接管 {'、'.join(missing)}"
    req = _requires(data)
    missing_req = [pid for pid in earlier if pid not in req]
    if missing_req:
        return False, f"{later} 的 requires 里没有 {'、'.join(missing_req)}"
    if not _has_expect(unit, data):
        return False, f"{later} 的接管没有带 expect（无法校验前值）"
    return True, ""
