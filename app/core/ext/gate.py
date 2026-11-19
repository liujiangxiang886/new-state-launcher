"""完全校验档的启动闸门：没验证的剔掉、冲突的剔掉、依赖断链的一并剔掉。

为什么单独一层
--------------

``resolver`` 只负责「报告问题」，真正决定「谁被挂载」的是
``enable.build_for_launch``。这套判定必须同时被**就绪度**（按钮是否可点）
和**实际组装**使用，否则会出现「按钮说能开、点下去被拒」或反过来的情况。

两关，顺序有意义
----------------

1. **验证标志**（:func:`_drop_unverified`）：没验过的包先剔。
   理由见该函数文档——「没验过」和「有验证但冲突」是两件事，
   后者可以靠处置解决，前者不行。
2. **冲突剔除 + 依赖闭包**：在剩下的包里找真正撞车的位置。

设计取舍
--------

- **整组剔除**：``conflict.files`` / ``conflict.text_patch`` /
  ``merge.pointer`` / ``merge.anchor`` 都是「两方或三方写同一处内容」，
  保留任何一方都等于「以它为准」。所以 ``p.ids`` 里出现的 id 全部剔除。
- **「改同一个文件」不算未通过**：``overlap.*`` 在 ``compose`` 打开时
  只是 info（运行时会把它们按解析顺序叠加上去）。
- **依赖闭包**：B 被剔除后，``requires`` 指向 B 的 A 也活不了，同一轮剔掉。
- **收敛上限**：正常两三轮就收敛；上限只防御病态输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config as cfg_mod
from ..fsutil import atomic_write_json
from . import resolver as resolver_mod

#: 关掉合并层时，「改同一个文件」也算未通过（因为那时后挂真的覆盖先挂）。
#: 合并层打开时这几类只是 info，不参与剔除。
OVERLAP_CODES = frozenset({"overlap.data", "overlap.text", "overlap.mixed"})

#: 迭代上限。4.6 现网 10 个启用包实测 2 轮收敛。
MAX_ROUNDS = 8


@dataclass
class GateDrop:
    """一轮剔除：同一问题涉及的整组 id + 原因。"""

    ids: list[str]
    code: str
    reason: str
    round: int
    #: 撞在哪个目标上（``data/items.json`` / ``scripts/game.gd``）。
    #: 依赖闭包这类没有具体目标的记录留空。
    target: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ids": list(self.ids),
            "code": self.code,
            "reason": self.reason,
            "round": self.round,
            "target": self.target,
        }


@dataclass
class GatePlan:
    line: str
    mode: str
    keep: list[str] = field(default_factory=list)
    dropped: list[GateDrop] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rounds: int = 0
    #: 不影响裁决、但需要让人知道的说明（例如「这条线暂不支持验证标志」）。
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """剩余集合可以启动（全剔光也算：那就是裸启动）。"""
        return not self.errors

    @property
    def dropped_ids(self) -> list[str]:
        out: list[str] = []
        for item in self.dropped:
            for pid in item.ids:
                if pid not in out:
                    out.append(pid)
        return sorted(out)

    def summary(self) -> str:
        if not self.dropped:
            return "未剔除任何扩展"
        return f"剔除 {len(self.dropped_ids)} 个，剩 {len(self.keep)} 个可启动"

    def by_ext(self) -> list[tuple[str, list[str]]]:
        """按扩展归并：``id → 它撞上的目标列表``（去重、排序）。

        为什么按包归并：``dropped`` 是「一个问题一条」，同一个包会出现在
        好几个问题里（实测 13 个包产生 8 条问题、id 重复 26 次）。
        直接铺开念，读的人只看到一堆重复名字，看不出「这个包到底撞了什么」。
        """
        per: dict[str, list[str]] = {}
        for d in self.dropped:
            tag = d.target or d.reason
            for pid in d.ids:
                bucket = per.setdefault(pid, [])
                if tag not in bucket:
                    bucket.append(tag)
        return sorted(per.items())

    def drop_lines(self) -> list[str]:
        """一行一个包：``id —— 撞的目标1；目标2``。"""
        return [f"{pid} —— {'；'.join(tags)}" for pid, tags in self.by_ext()]

    def drop_groups(self) -> list[str]:
        """按目标分组：``目标 → 涉及的包``。说明「谁跟谁撞在一起」。"""
        groups: dict[str, list[str]] = {}
        for d in self.dropped:
            tag = d.target or d.reason
            bucket = groups.setdefault(tag, [])
            for pid in d.ids:
                if pid not in bucket:
                    bucket.append(pid)
        return [f"{tag} → {'、'.join(sorted(ids))}" for tag, ids in sorted(groups.items())]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "line": self.line,
            "mode": self.mode,
            "ok": self.ok,
            "rounds": self.rounds,
            "keep": list(self.keep),
            "dropped": [d.as_dict() for d in self.dropped],
            "dropped_ids": self.dropped_ids,
            "errors": list(self.errors),
            "notes": list(self.notes),
        }


def _requirements(data: dict) -> set[str]:
    out: set[str] = set()
    for req in data.get("requires") or []:
        rid = str(req.get("id") if isinstance(req, dict) else req)
        if rid:
            out.add(rid)
    return out


def _drop_unverified(
    cfg: dict, line: str, alive: dict[str, dict], result: GatePlan
) -> None:
    """完全校验档的第一关：没有有效验证记录的包直接剔除。

    为什么把「标志」放在「冲突」之前：两者的含义不同。

    - **没验证** = 我们不知道它能不能在这条线上装出正确结果。这不是
      「两个包抢同一个位置」那种可以靠处置解决的事，而是「压根没验过」。
      留着它进组合，后面所有冲突判定都建立在不可靠的输入上。
    - **有验证但冲突** = 单独装没问题，组合起来有问题。这个交给后面的
      剔除 + 依赖闭包处理。

    所以顺序是：先剔没验证的 → 再在剩下的里面找冲突。

    **一个必要的前置判断**：如果这条线**根本产不出**验证记录
    （``code_digest`` 为空，通常是这条线的 identity 没有 ``code_layer``），
    那么「没标志」就**不是包的问题**，而是这条线不支持验证。此时一剔了之
    等于让玩家所有扩展凭空消失，且作者做什么都补救不了（``seal`` 也会
    因为拿不到 digest 拒绝出记录）。

    这种情况下只记一条 ``verify.unavailable`` 说明，不动任何包 —— 不能
    因为「我们暂时没法验」就把玩家的东西全停掉。等这条线补上 code_layer
    （例如 4.6 做一次 freeze）之后，这段逻辑会自动开始生效。

    **剔除后要重跑依赖闭包**：A 因为没验证被剔掉，依赖 A 的 B 也活不了。
    """
    from . import verify_mark

    if not alive:
        return

    try:
        code_digest = verify_mark.code_digest_of(cfg, line)
    except Exception:  # noqa: BLE001 - 拿不到指纹时按「无法判定」处理
        code_digest = ""

    if not code_digest:
        result.notes.append(
            f"verify.unavailable：{line} 线还没有代码层指纹，"
            "本次不做验证标志判定（所有扩展照常参与）"
        )
        return

    gone: set[str] = set()
    for pid in list(alive):
        root = _ext_root(cfg, line, pid)
        try:
            verdict = verify_mark.judge(
                cfg, line, pid, root, code_digest=code_digest or None
            )
        except Exception:  # noqa: BLE001 - 判不出来不该拦启动
            continue
        if verdict.ok:
            continue
        alive.pop(pid, None)
        gone.add(pid)
        result.dropped.append(
            GateDrop(
                ids=[pid],
                code=f"verify.{verdict.state}",
                reason=verdict.reason or "没有有效的验证记录",
                round=0,
            )
        )

    # 依赖闭包：被剔的包被谁依赖，谁一起剔
    while gone:
        orphans = [
            pid for pid, data in alive.items() if _requirements(data) & gone
        ]
        if not orphans:
            break
        for pid in orphans:
            moved = sorted(_requirements(alive[pid]) & gone)
            result.dropped.append(
                GateDrop(
                    ids=[pid],
                    code="gate.dependency",
                    reason=f"{pid} 依赖了被剔除的 {'、'.join(moved)}，一并剔除",
                    round=0,
                )
            )
            alive.pop(pid, None)
            gone.add(pid)


def _ext_root(cfg: dict, line: str, ext_id: str):
    """扩展的解包目录（``packs/<id>``），用于算内容指纹。

    拿不到就返回 None —— 此时 ``verify_mark.judge`` 会把内容指纹当成
    空串，从而判 ``stale``/``absent``（不会误判成 verified）。
    """
    from .. import paths as paths_mod

    root = paths_mod.PACKS / ext_id
    return root if root.is_dir() else None


def plan(cfg: dict, line: str, *, mode: str | None = None) -> GatePlan:
    """算出这条线在开发者模式下「保留谁、剔除谁」。纯计算，不写盘。"""
    from . import enable as ext_enable
    from . import decisions as decisions_mod
    from .. import paths as paths_mod

    # 启动路径必须和「导入/界面」用同一份处置结论。过去这里漏传 store，
    # 结果界面上已经逐项处置过的重叠，启动时又被当成未处置而整组剔除，
    # 表现为「装了 N 个包、实际只挂上 N-3 个」。
    store = decisions_mod.Store.load(paths_mod.CONFIG_DIR / "decisions.json")
    resolved_mode = cfg_mod.normalize_mode(mode or cfg_mod.extension_mode(cfg))
    pairs = ext_enable.enabled_manifests(cfg, line)
    result = GatePlan(
        line=line,
        mode=resolved_mode,
        keep=[pid for pid, _ in pairs],
    )
    if not cfg_mod.is_strict(resolved_mode):
        return result

    alive: dict[str, dict] = {pid: data for pid, data in pairs}

    # ---- 第一关：验证标志
    #
    # **必须在「少于 2 个包就返回」之前做**：只装了一个包的玩家同样需要
    # 被校验。旧实现在 ``len(pairs) < 2`` 时直接返回，那意味着「只装一个
    # 未经验证的包」完全不受管 —— 而这恰恰是最常见的情况。
    _drop_unverified(cfg, line, alive, result)

    if len(alive) < 2:
        result.keep = sorted(alive)
        return result
    # 合并层打开时，「同一个文件被多个包改」不再是问题——运行时会把各方的
    # 改动按解析顺序叠上去。只有真正撞在同一处内容的（error）才剔除。
    compose_on = cfg_mod.extension_compose(cfg)
    drop_codes = frozenset() if compose_on else OVERLAP_CODES
    for round_no in range(1, MAX_ROUNDS + 1):
        result.rounds = round_no
        resolution = resolver_mod.resolve(
            list(alive.items()),
            compose=compose_on,
            mode=resolved_mode,
            line=line,
            store=store,
        )
        bad = [
            p
            for p in resolution.problems
            if p.level == "error" or p.code in drop_codes
        ]
        if not bad:
            break

        hit: set[str] = set()
        unattributed: list[str] = []
        for problem in bad:
            ids = sorted(pid for pid in problem.ids if pid in alive)
            if not ids:
                unattributed.append(problem.message)
                continue
            hit.update(ids)
            result.dropped.append(
                GateDrop(
                    ids=ids,
                    code=problem.code,
                    reason=problem.message,
                    round=round_no,
                    target=str(getattr(problem, "target", "") or ""),
                )
            )

        if not hit:
            # 没有可归因的 id（例如空 ids 的排序错误）→ 无法通过剔除修复
            result.errors.extend(unattributed or [p.message for p in bad])
            break

        for pid in hit:
            alive.pop(pid, None)

        # 依赖闭包：把「依赖了被剔包」的也剔掉，直到不再扩大
        while True:
            orphans = [
                pid
                for pid, data in alive.items()
                if _requirements(data) & hit
            ]
            if not orphans:
                break
            for pid in orphans:
                moved = sorted(_requirements(alive[pid]) & hit)
                result.dropped.append(
                    GateDrop(
                        ids=[pid],
                        code="gate.dependency",
                        reason=f"{pid} 依赖了被剔除的 {'、'.join(moved)}，一并剔除",
                        round=round_no,
                    )
                )
                alive.pop(pid, None)
                hit.add(pid)
    else:
        result.errors.append(f"剔除 {MAX_ROUNDS} 轮仍未收敛，拒绝启动")

    if not result.errors:
        # 剩余集合必须自己也是干净的；否则说明还有无法归因的问题
        left = resolver_mod.resolve(
            list(alive.items()),
            compose=compose_on,
            mode=resolved_mode,
            line=line,
            store=store,
        )
        result.errors.extend(
            p.message
            for p in left.problems
            if p.level == "error" or p.code in drop_codes
        )

    result.keep = sorted(alive)
    return result


def apply_to_mods(mods: list[dict], gate: GatePlan) -> list[dict]:
    """按 gate 结果过滤 ``enabled_mods`` 的返回值（保序）。"""
    if not gate.dropped:
        return list(mods)
    keep = set(gate.keep)
    return [m for m in mods if str(m.get("id") or "") in keep]


def write_report(gate: GatePlan, run_dir: str | Path) -> Path:
    """把本次裁决落盘，便于事后对照「为什么这个包没挂上」。"""
    return atomic_write_json(Path(run_dir) / "gate.json", gate.as_dict())
