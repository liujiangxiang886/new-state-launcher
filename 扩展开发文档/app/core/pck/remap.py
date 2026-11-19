"""remap 生成与判定——整条覆盖链路最关键的一块。

Godot 导出时把资源编译成二进制并留下一个 ``.remap`` 指过去：

    scripts/game.gd   → 导出为 scripts/game.gdc + scripts/game.gd.remap
                        （remap 内容 path="res://scripts/game.gdc"）

所以**覆盖源码时必须同时写自指 remap**，否则引擎顺着基线的 remap 读到旧
``.gdc``，表现为「文件挂上了、改了完全没反应」。这个坑在本项目里踩过三次
（剧情库、扩展 files、扩展 patches），本模块把它收敛成唯一实现。

本模块只做纯逻辑判断与内容生成，不碰写入器（写由
:meth:`app.core.pck.writer.PckWriter.add_remap` 完成）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 覆盖这些后缀需要自指 remap
REMAP_SUFFIXES: tuple[str, ...] = (".gd", ".tscn", ".tres", ".res")

#: 导出后编译成别的后缀、且原路径只留 remap 的类型
COMPILED_SUFFIXES: tuple[str, ...] = (".gd", ".tscn")


@dataclass(frozen=True)
class RemapPlan:
    target: str
    needed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"target": self.target, "needed": self.needed, "reason": self.reason}


def self_remap_text(target: str) -> str:
    """自指 remap 的内容。``target`` 用包内相对路径（不带 ``res://``）。"""
    rel = target[len("res://"):] if target.startswith("res://") else target
    return f'[remap]\n\npath="res://{rel}"\n'


def redirect_remap_text(target: str, to: str) -> str:
    """指向别处的 remap（把一个路径映射到另一个编译产物）。"""
    rel_t = target[len("res://"):] if target.startswith("res://") else target
    rel_to = to[len("res://"):] if to.startswith("res://") else to
    return f'[remap]\n\npath="res://{rel_to}"\n'


def needs_remap(target: str, baseline_paths: frozenset[str] | None) -> RemapPlan:
    """判断覆盖 ``target`` 时是否需要写自指 remap。

    三条规则，按顺序：

    1. 后缀不在 :data:`REMAP_SUFFIXES` 里 → 不需要（json/贴图与 remap 无关）；
    2. ``target`` 本身在基线里 → 需要（基线有同名资源，可能带 remap）；
    3. ``target + ".remap"`` 在基线里 → 需要（导出包只有 ``x.gdc`` + ``x.gd.remap``，
       **没有** ``x.gd``，这是最常见的情况，也是踩坑点）。

    没有基线索引（``baseline_paths is None``）时一律认为需要——多写一个自指
    remap 是安全的，漏写则静默失效。
    """
    rel = target[len("res://"):] if target.startswith("res://") else target.replace("\\", "/")
    if not rel.endswith(REMAP_SUFFIXES):
        return RemapPlan(rel, False, "后缀不需要 remap")

    if baseline_paths is None:
        return RemapPlan(rel, True, "无基线索引，按需要处理（多写安全）")
    if rel in baseline_paths:
        return RemapPlan(rel, True, "基线存在同名资源")
    if f"{rel}.remap" in baseline_paths:
        return RemapPlan(rel, True, "基线存在同名 .remap（导出包只有编译产物）")
    return RemapPlan(rel, False, "基线里没有这个路径，属于新增文件")


def is_override(target: str, baseline_paths: frozenset[str] | None) -> bool:
    """目标是否覆盖了基线里的既有资源（用于「覆盖 / 新增」分类与 remap 决策）。

    和 :func:`needs_remap` 的区别：这里不限制后缀，任何在基线里存在的路径都算覆盖。
    """
    if not baseline_paths:
        return False
    rel = target[len("res://"):] if target.startswith("res://") else target.replace("\\", "/")
    if rel in baseline_paths:
        return True
    return rel.endswith(REMAP_SUFFIXES) and f"{rel}.remap" in baseline_paths


def plan_for(
    targets: list[str], baseline_paths: frozenset[str] | None
) -> tuple[list[RemapPlan], list[str]]:
    """批量给出 remap 计划，返回 ``(计划列表, 提醒文案)``。"""
    plans = [needs_remap(t, baseline_paths) for t in targets]
    notes: list[str] = []
    covered = [p for p in plans if p.needed]
    if covered:
        notes.append(
            f"{len(covered)} 个条目的覆盖需要自指 remap（否则引擎会读回基线字节）"
        )
    return plans, notes