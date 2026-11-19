"""把「发现到的冲突」变成「这次合成怎么处理」。

三档模式共用同一份判定，只是阈值不同：

============  ==================  ==================  ==================
情形           普通                宽松校验             开发者
============  ==================  ==================  ==================
已声明接管      直接采用             直接采用              直接采用
有本地处置      按处置执行           按处置执行            按处置执行
未处置         后者生效，标降级      列出来，可跳过         必须处置，否则报错
============  ==================  ==================  ==================

普通模式**永远不产生 error**——玩家只该看到「能不能启动」，不该被组合判定拦住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from . import decisions as decisions_mod
from . import units as units_mod
from .units import Unit
from .. import config as cfg_mod


@dataclass
class UnitState:
    unit: Unit
    action: str                 # override / compatible / dependent / disable / degraded / pending
    source: str                 # declared / decision / default
    severity: str = "info"      # info / warn / error
    code: str = "merge.decision"
    message: str = ""
    value: Any = None
    decision: decisions_mod.Decision | None = None

    @property
    def is_pending(self) -> bool:
        return self.action == "pending"

    def as_dict(self) -> dict[str, Any]:
        out = self.unit.as_dict()
        out.update(
            {
                "action": self.action,
                "source": self.source,
                "severity": self.severity,
                "code": self.code,
                "message": self.message,
                "value": self.value,
            }
        )
        if self.decision is not None:
            out["decision"] = self.decision.as_dict()
        return out


@dataclass
class MergePlan:
    states: list[UnitState] = field(default_factory=list)
    disabled: set[str] = field(default_factory=set)
    extra_deps: list[tuple[str, str]] = field(default_factory=list)
    synthetic: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[UnitState] = field(default_factory=list)
    pending: list[UnitState] = field(default_factory=list)

    def find(self, target: str, locator: str, kind: str) -> UnitState | None:
        for state in self.states:
            u = state.unit
            if u.target == target and u.locator == locator and u.kind == kind:
                return state
        return None

    def for_target(self, target: str) -> list[UnitState]:
        return [s for s in self.states if s.unit.target == target]

    def problems(self) -> list[dict[str, Any]]:
        return [
            {
                "level": s.severity,
                "code": s.code,
                "message": s.message,
                "ids": list(s.unit.parties),
                "target": s.unit.target,
                "locator": s.unit.locator,
                "action": s.action,
            }
            for s in self.states
            if s.severity in ("warn", "error", "info")
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "states": [s.as_dict() for s in self.states],
            "disabled": sorted(self.disabled),
            "extra_deps": [list(x) for x in self.extra_deps],
            "synthetic": self.synthetic,
            "degraded": [s.as_dict() for s in self.degraded],
            "pending": [s.as_dict() for s in self.pending],
        }


def _ordered_parties(unit: Unit, order: list[str]) -> list[str]:
    position = {pid: index for index, pid in enumerate(order)}
    return sorted(unit.parties, key=lambda pid: (position.get(pid, 999), pid))


def _decision_state(
    unit: Unit, decision: decisions_mod.Decision
) -> tuple[UnitState | None, set[str], list[tuple[str, str]], dict[str, Any] | None]:
    """把一条本地决定翻译成合成参数。"""
    if decision.action == "disable":
        party = decision.party or (unit.parties[-1] if unit.parties else "")
        if party and party in unit.parties:
            state = UnitState(
                unit=unit,
                action="disable",
                source="decision",
                severity="info",
                code="merge.decision",
                message=f"已停用 {party}（该扩展整体不参与合成）",
                decision=decision,
            )
            return state, {party}, [], None
        return None, set(), [], None

    if decision.action == "compatible":
        state = UnitState(
            unit=unit,
            action="compatible",
            source="decision",
            severity="info",
            code="merge.decision",
            message=f"{units_mod.describe(unit)} 已按兼容值合成",
            value=decision.value,
            decision=decision,
        )
        synthetic = {
            "target": unit.target,
            "locator": unit.locator,
            "kind": unit.kind,
            "value": decision.value,
            "parties": list(unit.parties),
            "payload": unit.payload,
        }
        return state, set(), [], synthetic

    if decision.action == "dependent":
        before = decision.before or (unit.parties[0] if unit.parties else "")
        after = decision.after or (unit.parties[-1] if unit.parties else "")
        if not before or not after or before == after:
            return None, set(), [], None
        state = UnitState(
            unit=unit,
            action="dependent",
            source="decision",
            severity="info",
            code="merge.decision",
            message=f"已声明依赖：{after} 排在 {before} 之后，并接管这一处",
            decision=decision,
        )
        return state, set(), [(before, after)], None

    return None, set(), [], None


def disabled_parties(
    items: Iterable[tuple[str, dict]],
    *,
    store: decisions_mod.Store | None = None,
    line: str = "4.6",
) -> set[str]:
    """只算「整包停用」的决定，供启动前过滤；不需要顺序。"""
    pairs = [(str(pid), dict(data or {})) for pid, data in items if pid]
    meta = {pid: data for pid, data in pairs}
    out: set[str] = set()
    if store is None:
        return out
    for unit in units_mod.discover(pairs):
        decision = store.match(line, unit.target, unit.locator, unit.parties, meta)
        if decision is not None and decision.action == "disable":
            party = decision.party or (unit.parties[-1] if unit.parties else "")
            if party in unit.parties:
                out.add(party)
    return out


def plan(
    items: Iterable[tuple[str, dict]],
    order: list[str],
    *,
    mode: str,
    store: decisions_mod.Store | None = None,
    line: str = "4.6",
) -> MergePlan:
    """算出这次合成的全部处置。纯计算，不写盘。"""
    pairs = [(str(pid), dict(data or {})) for pid, data in items if pid]
    meta = {pid: data for pid, data in pairs}
    found = units_mod.discover(pairs)
    result = MergePlan()
    # 完全校验：未处置的位置冲突直接算 error，交给 gate 做剔除。
    # 宽松校验：同一批冲突降级为 warn，照常按顺序取舍，只在报告里说清
    # 「谁和谁撞了」——宽松不是「不查」，是「查了但放行」。
    verify = cfg_mod.is_strict(mode)

    for unit in found:
        unit.parties = _ordered_parties(unit, order)

        ok, reason = units_mod.authorized(unit, meta)
        if ok:
            result.states.append(
                UnitState(
                    unit=unit,
                    action="override",
                    source="declared",
                    severity="info",
                    code="merge.override",
                    message=f"{unit.parties[-1]} 已声明接管 {units_mod.describe(unit)}",
                )
            )
            continue

        decision = None
        if store is not None:
            decision = store.match(line, unit.target, unit.locator, unit.parties, meta)
        if decision is not None:
            state, disabled, deps, synthetic = _decision_state(unit, decision)
            if state is not None:
                result.states.append(state)
                result.disabled.update(disabled)
                result.extra_deps.extend(deps)
                if synthetic:
                    result.synthetic.append(synthetic)
                continue

        where = units_mod.describe(unit)
        code = "conflict.files" if unit.kind == "files" else "conflict.location"
        if verify:
            state = UnitState(
                unit=unit,
                action="pending",
                source="default",
                severity="error",
                code=code,
                message=f"{'、'.join(unit.parties)} 都改了 {where}，还没有处置（{reason}）",
            )
            result.pending.append(state)
        else:
            state = UnitState(
                unit=unit,
                action="degraded",
                source="default",
                severity="warn",
                code=code,
                message=(
                    f"{'、'.join(unit.parties)} 都改了 {where}，"
                    "已按顺序自动取舍；相关功能可能不完整"
                ),
            )
            result.degraded.append(state)
        result.states.append(state)

    # 依赖处置会改变顺序：注入排序边后由 resolver 重新拓扑
    return result
