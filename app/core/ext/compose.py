"""合并层：把多个扩展对同一份文件的改动按顺序叠加成一份。

为什么需要
----------

覆盖层是「一个扩展一个 ``overlay.pck``」，同一路径在引擎里只有一个赢家——
后挂载的整份替换先挂载的。于是两个扩展都改 ``data/equipment.json`` 时，
最终只有一份生效，另一份的改动凭空消失。而这两份改动在**内容**上往往
并不冲突：一个往 ``/catalog`` 里加装备、一个调 ``/catalog/*/attributes``
的数值，完全可以叠加。

实测这 19 个扩展：3 个数据文件、6 个脚本共 11 个目标被 2~5 个包同时
修改，但

* 数据补丁的 JSON 指针**两两不相交**；
* 文本补丁的锚点**两两不相交**（按解析顺序逐条应用，19 个包 0 次失配）。

也就是说它们本来就该共存，是「整文件挂载」这个实现把它们判成了冲突。

判定标准（与 :mod:`resolver` 对齐）
-----------------------------------

======================  ==========================================  ==========
情形                    处理                                        依据
======================  ==========================================  ==========
同目标、两份整文件副本   不可合并，交 gate 剔除                     内容完全不同源
同目标、同一 JSON 指针   不可合并，交 gate 剔除                     后者会覆盖前者
同目标、锚点互相包含     不可合并，交 gate 剔除                     替换区域重叠
同目标、区域不相交       **本模块叠加**，按解析顺序逐层应用          增量可交换
======================  ==========================================  ==========

产物是 ``merged.pck``：挂在所有扩展覆盖层**之后**、插桩层**之前**。
这样各扩展自己的 overlay 原样保留（关掉合并层就回到旧行为），
而合并后的版本在最后把整份内容顶上去。

顺序由 ``requires`` / ``load_after`` / ``priority`` 决定（``resolver`` 已经
排好），所以「补丁打在别人整文件覆盖之上」这种组合也是确定的。例如
``ai_act.dimension_performance`` 依赖 ``rose.dimension_ruins``，它的补丁
一定在次元裂缝的整文件副本**之后**应用。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .. import config as cfg_mod
from ..pck.writer import PckWriter
from . import manifest as manifest_mod
from . import patcher

#: 需要自指 remap 的后缀（与 ``apply.py`` 保持一致）
_REMAP_SUFFIXES = (".gd", ".tscn", ".tres", ".res")

#: 基底来源的人话标注（剧情层的 ``"story"`` 标签随剧情功能一起移除）。
_BASE_LABELS = {
    "baseline": "基线 pck",
    "source": "源码",
    "extension": "扩展整文件",
}


def _norm(target: object) -> str:
    text = str(target or "").strip().replace("\\", "/")
    if text.startswith("res://"):
        text = text[len("res://"):]
    return text.lstrip("/")


@dataclass
class Layer:
    """一个扩展对某个目标的全部改动。"""

    ext_id: str
    whole: bool = False              # 是否自带整文件副本（files[]）
    data: list[dict] = field(default_factory=list)
    text: list[dict] = field(default_factory=list)
    members: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.whole and not self.data and not self.text and not self.members


@dataclass
class Result:
    entries: dict[str, bytes] = field(default_factory=dict)
    #: 人话说明：哪个文件由哪几个包叠加、叠了多少条
    notes: list[str] = field(default_factory=list)
    #: 叠加失败（取不到原文 / 锚点失配 / 指针冲突），交调用方写日志与界面
    conflicts: list[dict] = field(default_factory=list)
    #: 普通模式下「按顺序自动取舍」的位置，只记录、不阻断
    degraded: list[dict] = field(default_factory=list)
    #: 每个目标应用了哪些处置（override/compatible/dependent/degraded）
    dispositions: list[dict] = field(default_factory=list)
    pck: Path | None = None
    sha256: str = ""
    size: int = 0

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def texts(self) -> dict[str, str]:
        """只取脚本目标的文本（插桩层要用它当基底）。"""
        return {
            path: data.decode("utf-8", "replace")
            for path, data in self.entries.items()
            if path.endswith(".gd")
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": 1,
            "pck": str(self.pck or ""),
            "sha256": self.sha256,
            "entries": sorted(self.entries),
            "notes": self.notes,
            "conflicts": self.conflicts,
            "degraded": self.degraded,
            "dispositions": self.dispositions,
        }


# ---------------------------------------------------------------- 收集


def layers_of(manifest: dict) -> dict[str, Layer]:
    """从一个清单里取出「它对每个目标做了什么」。"""
    ext_id = str(manifest.get("id") or "")
    out: dict[str, Layer] = {}

    def slot(target: str) -> Layer:
        return out.setdefault(target, Layer(ext_id=ext_id))

    for rec in manifest.get("files") or []:
        if isinstance(rec, dict):
            target = _norm(rec.get("target"))
            if target:
                slot(target).whole = True

    for patch in manifest.get("data_patches") or []:
        if isinstance(patch, dict):
            target = _norm(patch.get("target"))
            if target:
                slot(target).data.append(patch)

    for patch in manifest.get("text_patches") or []:
        if isinstance(patch, dict):
            target = _norm(patch.get("target"))
            if target:
                slot(target).text.append(patch)

    scripts = manifest.get("scripts") or {}
    for rec in (scripts.get("members") or []) if isinstance(scripts, dict) else []:
        if not isinstance(rec, dict):
            continue
        target = _norm(rec.get("target"))
        members = rec.get("members")
        if target and isinstance(members, dict) and members:
            slot(target).members.update({str(k): str(v) for k, v in members.items()})
    return out


def shared_targets(
    ordered: Iterable[tuple[str, dict]]
) -> dict[str, list[Layer]]:
    """``目标 -> 有序层``，只保留被 2 个及以上扩展触及的目标。"""
    grouped: dict[str, list[Layer]] = {}
    for _ext_id, manifest in ordered:
        for target, layer in layers_of(manifest).items():
            if not layer.empty:
                grouped.setdefault(target, []).append(layer)
    return {
        target: layers
        for target, layers in grouped.items()
        if len({layer.ext_id for layer in layers}) >= 2
    }


# ---------------------------------------------------------------- 叠加

#: GDScript 顶层函数声明行（锚点重定向用）
_FUNC_HEAD_RE = re.compile(r"^func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.M)


def _apply_data(target: str, base: bytes, layers: list[Layer]) -> tuple[bytes, dict[str, int]]:
    """按层序把 JSON 操作叠加到同一份文档上，最后统一序列化一次。

    ``doc`` 是本次调用自己解析出来的，且任何一步失败都会整体作废，
    所以传给 ``apply_json`` 时用 ``inplace=True``：省掉每条补丁一次
    整份深拷贝（几 MB 的 items.json 上实测每条 ~0.8 秒）。
    """
    doc = json.loads(base.decode("utf-8"))
    counts: dict[str, int] = {}
    for layer in layers:
        for patch in layer.data:
            doc, applied, _ = patcher.apply_json(
                doc, manifest_mod.ops_of(patch), target=target, inplace=True
            )
            counts[layer.ext_id] = counts.get(layer.ext_id, 0) + applied
    return json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"), counts


def _native_alias(target: str, find: str, text: str) -> tuple[str, str] | None:
    """把「函数头」锚点改指向 ``*native_<name>_<hash>``，返回改写后的 find。

    背景（2026-11-05 线上事故）
    ---------------------------
    replace 型钩子把原函数体改名搬走，原位置只留一行转发：

        func release_caught_targets() -> void :
            preload(...).invoke_method(...)
        func ai_act_wb_native_release_caught_targets_a03a4c8459() -> void :
            <原函数体>

    后装的包若拿「函数头 + 函数体头几行」当锚点，就永远匹配不上——它想改的
    其实正是那段被搬走的函数体。这里把锚点首行换成 native 函数签名再试一次，
    语义上等价于「改原函数体」，正是作者本意。

    只在能**唯一**定位到 native 函数时才改写；有歧义就返回 None，交回原路径
    按失配处理。
    """
    match = _FUNC_HEAD_RE.match(find)
    if not match:
        return None
    name = match.group(1)
    pattern = re.compile(
        r"^func\s+\w*native_" + re.escape(name) + r"_[0-9a-f]{6,}\s*\([^)]*\)[^\n:]*:",
        re.M,
    )
    hits = pattern.findall(text)
    if len(hits) != 1:
        return None
    lines = find.split("\n")
    lines[0] = hits[0]
    return "\n".join(lines), name


def _apply_text(target: str, base: bytes, layers: list[Layer]) -> tuple[bytes, dict[str, int], list[dict]]:
    """按层序应用文本补丁。

    锚点失配时**不再静默跳过**：记一条 ``compose.anchor`` 冲突，由调用方决定
    是硬失败还是降级提示。2026-11-05 的灰屏就是「跳过 + 只发一条警告」造成的：
    更好的体验有 8 处补丁被丢掉，`slice_monster.gd` 少了 3 处改动，连带
    `training_monster.gd` 的父类解析失败，最终主城一片灰。装的时候看着是
    「安装成功」，进游戏才发现问题，排查成本极高。
    """
    text = base.decode("utf-8", "replace")
    counts: dict[str, int] = {}
    problems: list[dict] = []
    for layer in layers:
        for patch in layer.text:
            new_text, count, issues = patcher.apply_text(text, patch, target=target)
            if count == 0:
                # 先试「锚点重定向到 replace 钩子的 native 函数」
                find = str(patch.get("find") or "")
                alias = _native_alias(target, find, text) if find else None
                if alias is not None:
                    alias_find, name = alias
                    retargeted = dict(patch)
                    retargeted["find"] = alias_find
                    replace = str(patch.get("replace") or "")
                    head = _FUNC_HEAD_RE.match(replace)
                    if head and head.group(1) == name:
                        lines = replace.split("\n")
                        lines[0] = alias_find.split("\n")[0]
                        retargeted["replace"] = "\n".join(lines)
                    new_text, count, issues = patcher.apply_text(
                        text, retargeted, target=target
                    )
                    if count:
                        # 救回后按正常命中记数，但留一条 note 方便排查
                        counts[layer.ext_id] = counts.get(layer.ext_id, 0) + count
                        text = new_text
                        continue
            expect = int(patch.get("expect_count", 1) or 0)
            if count == 0 and expect != 0:
                first = (str(patch.get("find") or "").split("\n") or [""])[0]
                problems.append(
                    {
                        "target": target,
                        "code": "compose.anchor",
                        "ids": [layer.ext_id],
                        "reason": (issues or ["锚点在叠加后的内容里找不到"])[0],
                        # 给界面用的人话：哪个包、哪段锚点、后果是什么
                        "ext_id": layer.ext_id,
                        "anchor": first[:120],
                        "impact": (
                            "该包的这条改动不会生效，相关功能会静默缺失；"
                            "若它改的是被继承的父类脚本，还可能让子类解析失败导致主城灰屏。"
                        ),
                    }
                )
                continue
            counts[layer.ext_id] = counts.get(layer.ext_id, 0) + count
            text = new_text
    return text.encode("utf-8"), counts, problems


def _apply_members(target: str, base: bytes, layers: list[Layer]) -> bytes:
    text = base.decode("utf-8", "replace")
    for layer in layers:
        if layer.members:
            text, _issues = patcher.inject_members(text, layer.members, target=target)
    return text.encode("utf-8")


def _apply_synthetic(
    target: str, base: bytes, entries: list[dict]
) -> tuple[bytes, int, list[str]]:
    """把「兼容处置」算出的最终值写到合成结果上。

    这些值来自玩家/作者的逐项处置，必须最后写——前面的补丁都应用完，
    它才是这一处的最终结果。
    """
    issues: list[str] = []
    applied = 0
    current = base
    for item in entries:
        kind = str(item.get("kind") or "data")
        locator = str(item.get("locator") or "/")
        value = item.get("value")
        if kind == "files":
            issues.append("整份文件不能填兼容值，请改为停用一方或声明依赖")
            continue
        if kind == "text":
            find = str((item.get("payload") or {}).get("find") or "")
            if not find:
                issues.append("文本兼容缺少原始锚点")
                continue
            patch = {
                "find": find,
                "replace": "" if value is None else str(value),
                "mode": str((item.get("payload") or {}).get("mode") or "literal"),
                "expect_count": 0,
            }
            text, count, problems = patcher.apply_text(
                current.decode("utf-8", "replace"), patch, target=target
            )
            if problems:
                issues.extend(problems)
                continue
            current = text.encode("utf-8")
            applied += count
            continue
        try:
            doc = json.loads(current.decode("utf-8"))
            doc, count, _ = patcher.apply_json(
                doc,
                [{"op": "replace", "path": locator, "value": value}],
                target=target,
                inplace=True,
            )
            current = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
            applied += count
        except (UnicodeDecodeError, json.JSONDecodeError, patcher.PatchError) as exc:
            issues.append(str(exc))
    return current, applied, issues


def build(
    ordered: list[tuple[str, dict]],
    *,
    read_base: Callable[[str], bytes | None],
    read_overlay: Callable[[str, str], bytes | None],
    out_pck: str | Path,
    line: str = cfg_mod.BUILTIN_LINE,
    origin_base: Callable[[str], str] | None = None,
    plan: object | None = None,
) -> Result:
    """把重叠目标叠加成 ``out_pck``；没有重叠时不产出任何文件。

    ``ordered`` 必须**已按解析顺序排好**（``resolver.resolve().order``），
    叠加顺序就是它。``read_base`` 只应读「原版内容」（剧情覆盖层 / 基线 pck /
    源码），不能再回落到已装扩展，否则会把上一轮的结果当成原文重复叠加。

    ``origin_base`` 只用于生成 ``notes`` 里的「基底取自哪里」——历史上这条线的基底
    常是剧情覆盖层，写成「原版」会让人误以为拿到的是 demo 源码。取不到标注
    时就按整文件副本 / 原版二分。
    """
    result = Result()
    groups = shared_targets(ordered)
    if not groups:
        return result

    # 兼容处置的最终值：按目标归组，合成完所有补丁后最后写入
    synthetic_by_target: dict[str, list[dict]] = {}
    if plan is not None:
        for item in getattr(plan, "synthetic", []) or []:
            synthetic_by_target.setdefault(str(item.get("target") or ""), []).append(item)

    writer = PckWriter(out_pck, line=line, verify=False)
    for target in sorted(groups):
        layers = groups[target]
        if plan is not None:
            disabled = set(getattr(plan, "disabled", set()) or set())
            layers = [layer for layer in layers if layer.ext_id not in disabled]
        ids = [layer.ext_id for layer in layers]
        if len({layer.ext_id for layer in layers}) < 2:
            # 停用后只剩一方，它自己的覆盖层已经挂了，不必再合成一份
            continue

        if plan is not None:
            for state in getattr(plan, "for_target", lambda _t: [])(target):
                result.dispositions.append(state.as_dict())
                if state.action == "degraded":
                    result.degraded.append(state.as_dict())

        whole = [layer for layer in layers if layer.whole]
        if len(whole) > 1:
            if plan is None:
                result.conflicts.append(
                    {
                        "target": target,
                        "code": "compose.files",
                        "ids": sorted({layer.ext_id for layer in whole}),
                        "reason": "两个扩展都自带整文件副本，无法合并（内容完全不同源）",
                    }
                )
                continue
            # 有处置计划：普通档保留第一个提供者，其余记降级；严格档在
            # launch 前就被 pending 拦住了，不会走到这里。
            keep = whole[0]
            result.degraded.append(
                {
                    "target": target,
                    "code": "conflict.files",
                    "kept": keep.ext_id,
                    "dropped": sorted({x.ext_id for x in whole[1:]}),
                    "reason": "整份文件只能由一个扩展提供",
                }
            )
            whole = [keep]

        # 基底：有整文件副本就用它的（别人在它之上打补丁），否则取原版内容
        base = read_overlay(whole[0].ext_id, target) if whole else read_base(target)
        if base is None:
            result.conflicts.append(
                {
                    "target": target,
                    "code": "compose.base",
                    "ids": ids,
                    "reason": "取不到原文（源码根、基线 pck、整文件副本都没有）",
                }
            )
            continue

        current = base
        counts: dict[str, int] = {}
        try:
            if target.lower().endswith(".json") and any(layer.data for layer in layers):
                current, counts = _apply_data(target, current, layers)
            current, text_counts, problems = _apply_text(target, current, layers)
            for ext_id, count in text_counts.items():
                counts[ext_id] = counts.get(ext_id, 0) + count
            if problems:
                result.conflicts.extend(problems)
                continue
            current = _apply_members(target, current, layers)
            synthetic = synthetic_by_target.get(target) or []
            if synthetic:
                current, syn_count, issues = _apply_synthetic(target, current, synthetic)
                if issues:
                    result.conflicts.append(
                        {
                            "target": target,
                            "code": "compose.decision",
                            "ids": [str((s.get("parties") or [""])[-1]) for s in synthetic],
                            "reason": "；".join(issues[:2]),
                        }
                    )
                    continue
                counts["__decision__"] = syn_count
        except (UnicodeDecodeError, json.JSONDecodeError, patcher.PatchError) as exc:
            result.conflicts.append(
                {
                    "target": target,
                    "code": "compose.patch",
                    "ids": ids,
                    "reason": str(exc),
                }
            )
            continue

        if current == base:
            continue
        writer.add_bytes(target, current)
        if target.endswith(_REMAP_SUFFIXES) and read_base(target) is not None:
            # 覆盖基线脚本必须补自指 remap，否则引擎顺着旧 .gd.remap 读回 .gdc
            writer.add_remap(target)
        result.entries[target] = current
        base_label = "扩展整文件" if whole else _BASE_LABELS.get(
            (origin_base(target) if origin_base else ""), "原版"
        )
        result.notes.append(
            f"{target} ← {' → '.join(ids)}"
            + (f"（{sum(counts.values())} 处改动，基底取自{base_label}）" if counts else "")
        )

    if not result.entries:
        return result

    committed = writer.commit()
    result.pck = Path(out_pck)
    result.sha256 = committed.sha256
    result.size = committed.size
    return result


def signature(result: Result) -> list[tuple[str, str]]:
    """合并层内容签名，供插桩缓存键使用。"""
    return [
        (target, hashlib.sha256(data).hexdigest())
        for target, data in sorted(result.entries.items())
    ]
