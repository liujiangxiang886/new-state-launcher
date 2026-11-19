"""多扩展解析：加载顺序、冲突检测、lock.json。

单个扩展装得上不代表多个扩展能共存。真正会出问题的是三类：

1. **加载顺序**：后挂载的覆盖先挂载的。``load_after`` / ``load_before`` /
   ``requires`` / ``priority`` 共同决定顺序，且必须无环——有环就说明作者对
   「谁在谁之后」的约定自相矛盾。
2. **重复声明**：两个扩展都改 ``data/x.json#/daily_limit``，或都 ``replace``
   同一个方法，谁生效取决于安装时间，属于不可接受的不确定。
3. **依赖缺失**：``requires`` 指向没装的包。

解析结果写进 ``lock.json``（顺序 + 每包哈希 + 基线指纹），启用前校验即可复现
「上次能跑的那套组合」。纯计算，不写盘（写盘由调用方决定路径）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import manifest as manifest_mod
from .. import config as cfg_mod

#: kind 的波次：框架 → 扩展 → 主题（后挂覆盖先挂，所以主题压在最上面）。
#:
#: 1.4 起剧情层（原 ``"story": 2``）随剧情功能一起移除；``theme`` 的数值
#: 保持不变，避免历史 lock.json 里的波次语义发生位移。
KIND_WAVES: dict[str, int] = {
    "framework": 0,
    "extension": 1,
    "mod": 1,
    "theme": 3,
}


@dataclass
class Problem:
    level: str          # error / warn / info
    code: str
    message: str
    ids: list[str] = field(default_factory=list)
    #: 撞在哪个目标上（``data/items.json`` / ``scripts/game.gd``）。
    #: 只给「有明确目标」的问题填，界面据此按目标聚合展示，
    #: 不必从 message 里猜路径。
    target: str = ""
    #: 目标内的位置：JSON 指针 / ``text:...`` / ``@file``。空表示整份目标。
    locator: str = ""
    #: 该问题的处置动作（degraded / pending / compatible / dependent / disable）。
    action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "ids": self.ids,
            "target": self.target,
            "locator": self.locator,
            "action": self.action,
        }


@dataclass
class Resolution:
    order: list[str] = field(default_factory=list)
    waves: list[list[str]] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    #: 本次的位置级处置（:class:`app.core.ext.policy.MergePlan`）。
    plan: Any = None

    @property
    def ok(self) -> bool:
        return not any(p.level == "error" for p in self.problems)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "order": self.order,
            "waves": self.waves,
            "errors": [p.as_dict() for p in self.problems if p.level == "error"],
            "warnings": [p.as_dict() for p in self.problems if p.level == "warn"],
            "infos": [p.as_dict() for p in self.problems if p.level == "info"],
        }


# ---------------------------------------------------------------- claims


def claims_overlap(a: str, b: str) -> bool:
    """两条 claim 是否指向同一块内容。

    ``scripts/game.gd`` 与 ``scripts/game.gd#character_attributes`` 算重叠
    （前者覆盖整个文件，后者覆盖文件里的一处）；``scripts/game.gd`` 与
    ``scripts/game.gd.remap`` 不算。
    """
    if a == b:
        return True
    for short, long in ((a, b), (b, a)):
        if long.startswith(short) and len(long) > len(short) and long[len(short)] in "#/":
            return True
    return False


def _norm_target(target: object) -> str:
    """把清单里的目标路径归一成同一个可比形式。

    实测现有 18 份清单里没有带 ``res://`` 或反斜杠的写法，所以归一化
    **不会**放大命中面；但 ``_replace_hooks`` 早就这么做了，统一之后
    两处判定对同一份清单给出同一个结论，避免「钩子认得、文件不认得」。
    """
    text = str(target or "").strip().replace("\\", "/")
    if text.startswith("res://"):
        text = text[len("res://"):]
    return text.lstrip("/")


def claims_by_kind(data: dict) -> dict[str, set[str]]:
    """从一个清单里派生「它到底动了哪些东西」，按覆盖方式分类。

    为什么要分「类」而不是一股脑并成一个集合：``files`` 是**整文件覆盖**
    （作者自带一份完整副本），和 ``data_patches`` 的增量补丁在失效方式上
    完全不同——前者一旦撞车就是确定的覆盖，后者还有机会在 JSON 层合并。
    分级判定必须知道是哪一类撞的。

    ``members`` 归到补丁类而不是整文件类：它是在目标脚本上注入声明，
    与 ``text_patches`` 同一条失效路径（后挂覆盖先挂）。
    """
    out: dict[str, set[str]] = {
        "declared": set(),
        "file": set(),
        "data": set(),
        "text": set(),
        "members": set(),
    }
    for claim in data.get("claims") or []:
        target = _norm_target(claim)
        if target:
            out["declared"].add(target)

    for item in data.get("files") or []:
        if isinstance(item, dict):
            target = _norm_target(item.get("target"))
            if target:
                out["file"].add(target)

    for item in data.get("data_patches") or []:
        if isinstance(item, dict):
            target = _norm_target(item.get("target"))
            if target:
                out["data"].add(target)

    for item in data.get("text_patches") or []:
        if isinstance(item, dict):
            target = _norm_target(item.get("target"))
            if target:
                out["text"].add(target)

    # scripts.members 的条目可能写成 {target: ...} 或直接是路径字符串，
    # 两种都见过，所以两种都收。
    scripts = data.get("scripts") or {}
    for item in (scripts.get("members") or []) if isinstance(scripts, dict) else []:
        raw = item.get("target") if isinstance(item, dict) else item
        target = _norm_target(raw)
        if target:
            out["members"].add(target)
    return out


def _text_keys(data: dict) -> set[tuple[str, str, str]]:
    """文本补丁的「同一条」判据：目标 + 匹配方式 + 查找串完全一致。

    ``find`` 不同就不算同一条——两个扩展改同一脚本的不同片段是合法用法，
    只有完全相同的一条才会让第二条必然命中数不符。
    """
    out: set[tuple[str, str, str]] = set()
    for item in data.get("text_patches") or []:
        if not isinstance(item, dict):
            continue
        target = _norm_target(item.get("target"))
        find = str(item.get("find") or "")
        if target and find:
            out.add((target, str(item.get("mode") or "literal"), find))
    return out


def _pointer_collisions(meta: dict[str, dict], ids: list[str]) -> list[Problem]:
    """同一 JSON 指针被两个扩展写 → 真冲突（叠加顺序决定结果，不能容忍）。

    为什么按指针判、不按文件判：``data/equipment.json`` 被 5 个扩展改，
    但它们写的是 ``/catalog/360261/icon_path`` 和 ``/catalog/300001/attributes``
    这种互不相交的位置，叠加之后各方改动都在。只有**同一个位置**被两方写
    才叫冲突——那才是「谁生效取决于顺序」。

    父子指针（``/a`` 与 ``/a/b``）也算冲突：一方整体替换对象，另一方改其中
    一个字段，结果同样取决于顺序。
    """
    per: dict[str, dict[str, set[str]]] = {}
    for pid in ids:
        for patch in meta[pid].get("data_patches") or []:
            if not isinstance(patch, dict):
                continue
            target = _norm_target(patch.get("target"))
            if not target:
                continue
            for op in manifest_mod.ops_of(patch):
                if not isinstance(op, dict):
                    continue
                path = str(op.get("path") or "")
                if path:
                    per.setdefault(target, {}).setdefault(path, set()).add(pid)

    out: list[Problem] = []
    for target, pointers in sorted(per.items()):
        clash: dict[frozenset[str], list[str]] = {}
        for path, owners in pointers.items():
            parts = path.split("/")
            # 精确相同 + 逐级父指针
            candidates = [path] + ["/".join(parts[:k]) for k in range(1, len(parts))]
            for candidate in candidates:
                others = pointers.get(candidate)
                if not others:
                    continue
                both = owners | others
                if len(both) < 2:
                    continue
                label = path if candidate == path else f"{candidate} ⊃ {path}"
                clash.setdefault(frozenset(both), []).append(label)
        for owners, paths in clash.items():
            uniq = sorted(set(paths))[:3]
            out.append(
                Problem(
                    "error",
                    "merge.pointer",
                    f"{'、'.join(sorted(owners))} 写同一个 JSON 位置：{'、'.join(uniq)}"
                    "（叠加顺序会决定结果，无法合并）",
                    sorted(owners),
                    target=target,
                )
            )
    return out


def _anchor_collisions(
    meta: dict[str, dict],
    ids: list[str],
    text_error_pairs: set[frozenset[str]] | None = None,
) -> list[Problem]:
    """两个扩展的文本锚点互相包含 → 真冲突。

    判据取「锚点文本包含」而不是「改同一份文件」：同一脚本里改两处不同的
    语句（实测 19 个包有 6 个脚本如此）按顺序应用完全没问题；只有一条补丁
    的 find 落在另一条的 find 里面时，前一条替换完，后一条必然命中 0 处。

    ``text_error_pairs`` 里的组合已经由 ``conflict.text_patch`` 判过 error
    （两条补丁的 ``find`` 完全相同），这里跳过——同一处问题换两种措辞报两遍
    只会让人以为是两个问题。
    """
    text_error_pairs = text_error_pairs or set()
    per: dict[str, list[tuple[str, str]]] = {}
    for pid in ids:
        for patch in meta[pid].get("text_patches") or []:
            if not isinstance(patch, dict):
                continue
            target = _norm_target(patch.get("target"))
            find = str(patch.get("find") or "")
            if target and find and str(patch.get("mode") or "literal") == "literal":
                per.setdefault(target, []).append((pid, find))

    out: list[Problem] = []
    for target, items in sorted(per.items()):
        seen: set[frozenset[str]] = set()
        for i, (a_pid, a_find) in enumerate(items):
            for b_pid, b_find in items[i + 1:]:
                if a_pid == b_pid:
                    continue
                if not (a_find in b_find or b_find in a_find):
                    continue
                pair = frozenset((a_pid, b_pid))
                if pair in seen or pair in text_error_pairs:
                    continue
                seen.add(pair)
                out.append(
                    Problem(
                        "error",
                        "merge.anchor",
                        f"{a_pid} 与 {b_pid} 的补丁锚点互相包含（{target}），"
                        "先应用的一条会把另一条的锚点改掉",
                        sorted(pair),
                        target=target,
                    )
                )
    return out


def _derived_problems(
    meta: dict[str, dict],
    *,
    mode: str = "loose",
    plan: Any = None,
) -> list[Problem]:
    """同一份文件被多个扩展修改时的**非冲突**说明。

    真正的「同一处」由 :mod:`units` 发现、:mod:`policy` 处置，结果已经在
    ``plan`` 里；这里只负责说清「同一份文件、位置不重叠，会自动合并」，
    以及目录级前缀重叠这种合并层也处理不了的情况。

    分级：
    - 同目标、位置不相交 → 两条线**都是** ``info``：这不是问题，是正常合并，
      说一句就够了。旧的三档制里 ``strict_warn`` 会把它升成 warn（那一档的
      用途是给作者做灰度体检，要看全量影响面），但那个档已经删了 ——
      现在宽松档是**玩家的默认档**，把「两个包各改一个字段」当提示刷给玩家
      只会淹没真正需要看的东西。作者要的影响面报告由 ``ext.inspect`` 出。
    - 目录与目录下文件的前缀重叠 → 宽松 ``warn``、完全 ``error``。
    """
    problems: list[Problem] = []
    ids = sorted(meta)
    if len(ids) < 2:
        return problems

    claims = {pid: claims_by_kind(meta[pid] or {}) for pid in ids}
    level = "info"
    tail = "；位置不重叠时会自动合并"
    planned_files = {
        str(state.unit.target)
        for state in getattr(plan, "states", [])
        if getattr(state.unit, "kind", "") == "files"
    }

    # --- 目录与目录下文件的前缀重叠（合并层也拼不出来）
    for index, a in enumerate(ids):
        for b in ids[index + 1:]:
            hits = sorted(
                {
                    f"{x} / {y}"
                    for x in claims[a]["file"]
                    for y in claims[b]["file"]
                    if x != y and claims_overlap(x, y)
                }
            )
            if hits:
                problems.append(
                    Problem(
                        # 目录与子文件的前缀重叠连合并层都拼不出来，两条线都必须
                        # 说清；完全校验档直接算 error（由 gate 剔除），宽松档
                        # 只 warn（照启动，但告到底谁撞了谁）。
                        "error" if cfg_mod.is_strict(mode) else "warn",
                        "conflict.files",
                        f"{a} 与 {b} 的提供范围重叠：{'、'.join(hits)}",
                        [a, b],
                        target="、".join(hits),
                    )
                )

    # --- 同目标、位置不相交：只是说明，不是冲突
    data_owners: dict[str, set[str]] = {}
    for pid in ids:
        for target in claims[pid]["data"]:
            data_owners.setdefault(target, set()).add(pid)
    grouped: dict[tuple[str, ...], list[str]] = {}
    for target, owners in data_owners.items():
        if len(owners) > 1:
            grouped.setdefault(tuple(sorted(owners)), []).append(target)
    for owners, targets in sorted(grouped.items()):
        problems.append(
            Problem(
                level,
                "overlap.data",
                f"{'、'.join(owners)} 都修改 {'、'.join(sorted(targets))}{tail}",
                list(owners),
                target="、".join(sorted(targets)),
            )
        )

    script_owners: dict[str, set[str]] = {}
    for pid in ids:
        for target in claims[pid]["text"] | claims[pid]["members"]:
            script_owners.setdefault(target, set()).add(pid)
    for target, owners in sorted(script_owners.items()):
        ordered = sorted(owners)
        if len(ordered) < 2:
            continue
        problems.append(
            Problem(
                level,
                "overlap.text",
                f"{'、'.join(ordered)} 都改 {target}{tail}",
                ordered,
                target=target,
            )
        )

    for index, a in enumerate(ids):
        for b in ids[index + 1:]:
            a_file, b_file = claims[a]["file"], claims[b]["file"]
            a_patch = claims[a]["data"] | claims[a]["text"] | claims[a]["members"]
            b_patch = claims[b]["data"] | claims[b]["text"] | claims[b]["members"]
            hits = [(x, y) for x in a_file for y in b_patch if claims_overlap(x, y)]
            hits += [(x, y) for y in b_file for x in a_patch if claims_overlap(x, y)]
            if hits:
                targets = sorted({x if x == y else f"{x} / {y}" for x, y in hits})
                if any(t in planned_files for t in targets):
                    continue
                problems.append(
                    Problem(
                        level,
                        "overlap.mixed",
                        f"{a} 与 {b} 对 {'、'.join(targets)} 的处理方式不同"
                        f"（一方提供整份文件，一方打补丁{tail}）",
                        [a, b],
                        target="、".join(targets),
                    )
                )
    return problems


def _replace_hooks(data: dict) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for hook in data.get("method_hooks") or []:
        if not isinstance(hook, dict):
            continue
        if str(hook.get("phase") or "after") != "replace":
            continue
        target = str(hook.get("target") or "").replace("res://", "")
        out.add((target, str(hook.get("method") or "")))
    return out


# ---------------------------------------------------------------- 依赖


def _topo(ids: list[str], deps: dict[str, set[str]], meta: dict[str, dict]) -> tuple[list[str], list[Problem]]:
    """按依赖与优先级排序一组 id；有环则报错并回退到 priority/id 顺序。"""
    problems: list[Problem] = []
    remaining = set(ids)
    ordered: list[str] = []
    while remaining:
        ready = [
            i
            for i in remaining
            if not (deps.get(i, set()) & remaining)
        ]
        if not ready:
            cycle = sorted(remaining)
            problems.append(
                Problem("error", "order.cycle", f"加载顺序成环：{' → '.join(cycle)}", cycle)
            )
            ready = sorted(remaining, key=lambda i: (int(meta[i].get("priority", 50)), i))
        ready.sort(key=lambda i: (int(meta[i].get("priority", 50)), i))
        for i in ready:
            ordered.append(i)
            remaining.discard(i)
    return ordered, problems


def resolve(
    items: Iterable[tuple[str, dict]],
    *,
    derive: bool = True,
    compose: bool = True,
    mode: str = "loose",
    line: str = "4.6",
    store: Any = None,
    extra_deps: Iterable[tuple[str, str]] | None = None,
    plan: Any = None,
) -> Resolution:
    """``items`` 是 ``(id, manifest.data)`` 序列，只应包含**已启用**的包。

    冲突判定分两层：

    - :mod:`units` 找「同一处」：同指针、父子指针、同锚点、双整文件；
    - :mod:`policy` 决定怎么处理：已声明接管 / 本地处置 / 按档位降级或待处置。

    ``mode`` 决定严格程度：``loose`` 只产出 warn（不阻断，但会把影响面
    列清楚），``strict`` 把未处置的冲突升成 error，交给 gate 剔除。

    ``compose`` 参数保留只为兼容旧调用：合并层现在是默认且唯一路径，
    关掉它只会让 ``overlap.*`` 变成 warn，不再改变冲突判定。
    """
    pairs = [(str(i), dict(d or {})) for i, d in items if i]
    meta = {i: d for i, d in pairs}
    res = Resolution()
    if not meta:
        return res

    # --- 依赖缺失
    for pid, data in meta.items():
        for req in data.get("requires") or []:
            rid = str(req.get("id") if isinstance(req, dict) else req)
            if rid and rid not in meta:
                res.problems.append(
                    Problem("error", "requires.missing", f"{pid} 依赖未启用的 {rid}", [pid, rid])
                )

    # --- 显式互斥
    for pid, data in meta.items():
        for other in data.get("conflicts") or []:
            oid = str(other.get("id") if isinstance(other, dict) else other)
            if oid in meta:
                res.problems.append(
                    Problem("error", "conflict.declared", f"{pid} 与 {oid} 声明互斥", [pid, oid])
                )

    # --- claims 重叠
    #
    # ``claims`` 是作者**主动声明**的「我占用了哪个业务规则」，
    # 例如 ``rule:equipment.enhancement``（整套强化概率/扣费/保底）。
    #
    # **这是本启动器唯一能表达「业务语义互斥」的机制。** 静态分析永远
    # 看不出「两个 mod 都重写了强化规则会打架」—— 它们的代码可能完全不
    # 相交（一个改概率公式、一个改保底计数），JSON 指针与文本锚点都不重叠。
    #
    # 三条硬约定（对齐参考启动器 柳江巷 V1.45.1 的 compat.py:325）：
    #
    #   1. 同键被两包声明 → **直接 error，不可豁免**；
    #   2. ``decisions.json`` 那种「记一次玩家选择」**不能**用于 claims
    #      —— 所以本函数从头到尾不读 store，这是有意为之，别加；
    #   3. 新增内容**不占**键（加一件新装备不占 enhancement），
    #      否则作者会给所有包都加，反而造出假冲突。
    #
    # 判定用 claims_overlap：它把 ``rule:x`` 与 ``rule:x.y`` 视为相交
    # （占用了整个 x 的包，与只占 x.y 的包确实冲突）。
    ids = sorted(meta)
    for index, a in enumerate(ids):
        claims_a = [str(c) for c in (meta[a].get("claims") or [])]
        for b in ids[index + 1:]:
            claims_b = [str(c) for c in (meta[b].get("claims") or [])]
            hits = [(x, y) for x in claims_a for y in claims_b if claims_overlap(x, y)]
            if hits:
                x, y = hits[0]
                res.problems.append(
                    Problem(
                        "error",
                        "conflict.claims",
                        f"{a} 与 {b} 都声明了 {x if x == y else f'{x} / {y}'}"
                        "（两者都是对同一套规则的完整替代，只能二选一）",
                        [a, b],
                    )
                )

    # --- 同方法 replace 撞车
    replacers: dict[tuple[str, str], list[str]] = {}
    for pid, data in meta.items():
        for key in _replace_hooks(data):
            replacers.setdefault(key, []).append(pid)
    for (target, method), owners in replacers.items():
        if len(owners) > 1:
            res.problems.append(
                Problem(
                    "error",
                    "conflict.replace_hook",
                    f"{'、'.join(owners)} 都想整体替换 {target}::{method}",
                    owners,
                    target=target,
                )
            )

    # --- 分波 + 波内拓扑（先算顺序，位置判定要知道谁在前谁在后）
    waves: dict[int, list[str]] = {}
    for pid, data in meta.items():
        wave = KIND_WAVES.get(str(data.get("kind") or "extension"), 1)
        waves.setdefault(wave, []).append(pid)

    deps: dict[str, set[str]] = {pid: set() for pid in meta}
    for pid, data in meta.items():
        for req in data.get("requires") or []:
            rid = str(req.get("id") if isinstance(req, dict) else req)
            if rid in meta:
                deps[pid].add(rid)
        for other in data.get("load_after") or []:
            if str(other) in meta:
                deps[pid].add(str(other))
        for other in data.get("load_before") or []:
            if str(other) in meta:
                deps.setdefault(str(other), set()).add(pid)
    for before, after in extra_deps or []:
        if before in meta and after in meta and before != after:
            deps[after].add(before)

    for wave_index in sorted(waves):
        ordered, problems = _topo(sorted(waves[wave_index]), deps, meta)
        res.problems.extend(problems)
        res.waves.append(ordered)
        res.order.extend(ordered)

    # --- 位置级判定 + 处置
    from . import policy as policy_mod

    if plan is None:
        plan = policy_mod.plan(pairs, res.order, mode=mode, store=store, line=line)
    res.plan = plan
    for item in plan.problems():
        res.problems.append(
            Problem(
                str(item.get("level") or "info"),
                str(item.get("code") or "merge.decision"),
                str(item.get("message") or ""),
                list(item.get("ids") or []),
                target=str(item.get("target") or ""),
                locator=str(item.get("locator") or ""),
                action=str(item.get("action") or ""),
            )
        )

    # --- 同文件不同位置：只做说明，不是冲突
    if derive:
        res.problems.extend(_derived_problems(meta, mode=mode, plan=plan))

    # 跨波依赖如果被排到了后面，顺序就不成立，给个警告
    if not any(p.code == "order.cycle" for p in res.problems):
        position = {pid: idx for idx, pid in enumerate(res.order)}
        for pid, data in meta.items():
            for other in data.get("load_after") or []:
                oid = str(other)
                if oid in position and position[oid] > position.get(pid, -1):
                    res.problems.append(
                        Problem(
                            "warn",
                            "order.cross_wave",
                            f"{pid} 想排在 {oid} 之后，但两者 kind 不同波次",
                            [pid, oid],
                        )
                    )
    return res


# ---------------------------------------------------------------- lock


def build_lock(
    resolution: Resolution,
    packages: dict[str, dict],
    *,
    line: str = "",
    baseline: dict | None = None,
) -> dict[str, Any]:
    """生成 ``lock.json``：顺序 + 每包哈希 + 基线指纹。

    ``packages`` 形如 ``{id: {"version","kind","overlay","sha256","entries","priority"}}``。
    """
    entries = []
    for pid in resolution.order:
        info = dict(packages.get(pid) or {})
        entries.append(
            {
                "id": pid,
                "version": str(info.get("version") or ""),
                "kind": str(info.get("kind") or "extension"),
                "priority": int(info.get("priority") or 50),
                "overlay": str(info.get("overlay") or ""),
                "sha256": str(info.get("sha256") or ""),
                "entries": int(info.get("entries") or 0),
            }
        )
    fingerprint = hashlib.sha256(
        "".join(f"{e['id']}:{e['sha256']};" for e in entries).encode("utf-8")
    ).hexdigest()
    return {
        "schema": 1,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "line": line,
        "baseline": baseline or {},
        "resolution_order": entries,
        "fingerprint": fingerprint,
        "ok": resolution.ok,
        "errors": [p.as_dict() for p in resolution.problems if p.level == "error"],
        "warnings": [p.as_dict() for p in resolution.problems if p.level == "warn"],
    }
