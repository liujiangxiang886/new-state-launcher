"""启动期组装：把已启用扩展的钩子插桩成一份覆盖层，并产出 mods.json。

启动时的挂载顺序（后者覆盖前者）：

    1. 原版基线包
    2. <扩展 A>/overlay.pck
    3. <扩展 B>/overlay.pck
    ...
    4. instrumented.pck  ← 插桩后的脚本，**必须最后挂**

为什么插桩要单独一层：多个扩展可能钩同一个方法，只能插一次；而且插桩的
永远是**原版脚本**，与具体扩展无关。这样扩展之间不会互相踩，顺序也可控。

为什么插桩层必须排在 overlay **之后**（踩过的坑）：overlay 里也可能包含
插桩目标脚本——实测 4.6 线上 whf.all_instructors / qt6663.exp_pet_exp 都带
``scripts/game.gd``，qt6663.exp_system 带 ``slice_overlay.gd``+``slice_run.gd``。
若插桩层先挂，这些 overlay 会把插桩结果整片盖掉，10 个钩子全部失效，
表现为「扩展装了但功能没反应」。

反过来，插桩层最后挂又必须**基于 overlay 合并后的内容**去插桩，否则会丢掉
overlay 做的补丁（例如 whf 注入的 11 个成员声明）。所以顺序是：

    baseline → overlay×N → instrumented（内容 = 基线脚本 + 各 overlay 改动 + 钩子）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config as cfg_mod
from .. import paths
from ..fsutil import atomic_write_json, read_json
from ..pck.writer import PckWriter
from . import hooks as hooks_mod
from . import netguard
from .service import ExtensionService

#: 需要注入社交指令注册表的脚本
SOCIAL_SCRIPT = "scripts/hud.gd"

#: 插桩缓存格式版本。改插桩算法 / 派发契约 / 缓存键算法时 +1，旧缓存自动作废。
#: 1 → 2：缓存键加入 overlay 覆盖签名（v1 的缓存没算 overlay，会漏掉补丁改动）。
#: 插桩产物的格式版本。**改动插桩算法或 context 契约时必须 +1**：
#: 旧缓存的内容其实已经不对，但输入签名可能恰好没变，加版本号后
#: 全部旧缓存自动作废。
#:
#: 3 = context 契约加 instance / arguments（M0-3）。
#: 4 → 5：before 相位对值类型（int / bool / float / String / Array / Dictionary）
#:        开始生成 skip_original 守卫。旧缓存的插桩结果里没有这段守卫，
#:        扩展设了 skip_original 也会被原逻辑盖掉（技能解锁、副本禁技能都受影响）。
#: 5 → 6：before 相位把钩子改过的 `context.arguments` 回写到函数形参。
#:        旧缓存里没有回写，钩子「调整调用参数」的用法全部无效
#:        （浮游炮大招改 pose → 护甲层缺 motion → 角色消失）。
#: 6 → 7：社交注入不再覆盖方法钩子插桩。旧缓存里 ``scripts/hud.gd``
#:        只有 dispatch_social、没有任何 dispatch_after —— 声明在 hud.gd 上的
#:        方法钩子（_ready / refresh / modal_open）运行时全部不触发，
#:        「装载成功但功能完全没效果」正是这个原因。
CACHE_FORMAT = 7


@dataclass
class LaunchMods:
    packs: list[dict] = field(default_factory=list)
    mods_json: Path | None = None
    hooks: list[dict] = field(default_factory=list)
    mods: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lock: dict = field(default_factory=dict)
    #: 开发者模式的剔除裁决（``ext.gate.GatePlan``）；其它模式为 None
    gate: object | None = None
    #: 本次位置级处置（``ext.policy.MergePlan``）
    plan: object | None = None
    #: 合并层锚点失配导致「某包的改动没生效」的结构化清单。
    #:
    #: 2026-11-05 主城灰屏事故：这类失配原先只进 ``warnings``，装包显示成功，
    #: 进游戏才发现主城起不来。现在单独成字段，供 UI 标红。
    skipped_patches: list[dict] = field(default_factory=list)
    #: 本次真正插桩成功的脚本（res:// 相对路径）。
    #:
    #: 用来回答「这个包的钩子到底落上了没有」：只有它声明的钩子目标全部
    #: 出现在这里，才算「插桩完成」。``mods.json`` 里列的是**声明**，
    #: 源码缺失时插桩会被跳过，两者不能混为一谈。
    instrumented_scripts: set[str] = field(default_factory=set)
    #: 本次启动后可以翻成「已就绪」的扩展 id（钩子目标全部插桩成功）。
    #:
    #: 首次导入的「待插桩」不是故障，是还没启动过。启动组装成功后由界面
    #: 调用 :meth:`ExtensionService.mark_launched` 持久化这个结论。
    ready_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "packs": [p.get("path") for p in self.packs],
            "mods": [m.get("id") for m in self.mods],
            "hooks": len(self.hooks),
            "order": [e.get("id") for e in (self.lock.get("resolution_order") or [])],
            "warnings": self.warnings,
            "dropped": getattr(self.gate, "dropped_ids", []),
            "skipped_patches": self.skipped_patches,
            "instrumented_scripts": sorted(self.instrumented_scripts),
            "ready_ids": self.ready_ids,
            "degraded": [
                s.unit.as_dict() for s in getattr(self.plan, "degraded", []) or []
            ],
        }


def _manifest_path(ext_id: str, line: str = "") -> Path:
    """扩展清单路径。

    多线安装后每条线有一份自己的清单（``manifest-47.v2.json``），
    因为补丁是按那条线的原版脚本算出来的。旧记录没有带线的文件，
    回落到 ``manifest.v2.json``。
    """
    root = paths.PACKS / ext_id
    if line:
        tagged = root / f"manifest-{paths.line_tag(line)}.v2.json"
        if tagged.exists():
            return tagged
    legacy = root / "manifest.v2.json"
    if legacy.exists():
        return legacy
    return root / f"manifest-{paths.line_tag(line)}.v2.json" if line else legacy


def _read_manifest(ext_id: str, line: str = "") -> dict | None:
    return _cached_manifest(_manifest_path(ext_id, line))


#: ``(路径, mtime_ns, size) -> 解析结果``。清单是纯只读数据（下游各检查器
#: 只读不写，见 ``resolver`` / ``units`` / ``gate``），可以放心复用同一份对象。
#:
#: 为什么需要它：清单总量 10.6 MB，其中 ``qt6663.exp_equipment`` 单份
#: 8.5 MB，光 ``json.loads`` 就要 65 ms。而一次 ``readiness.evaluate``
#: 里 ``enabled_manifests`` 会被调**两次**（``gate.plan`` 一次、
#: ``readiness`` 自己一次），``enabled_mods`` 里的 ``_fits_launcher``
#: 还会逐包再读一遍 —— 实测同一份数据被解析 3 轮共 340 ms。
#:
#: 键带 mtime + size：装/卸/改扩展都会改写清单文件，缓存自动失效，
#: 不需要谁记得来清。
_MANIFEST_CACHE: dict[tuple[str, int, int], dict] = {}

#: 上限只是防御：正常装机量几十份，超过就整体丢掉重建。
_MANIFEST_CACHE_MAX = 512


def _cached_manifest(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _MANIFEST_CACHE.get(key)
    if hit is not None:
        return hit
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        return None
    if len(_MANIFEST_CACHE) >= _MANIFEST_CACHE_MAX:
        _MANIFEST_CACHE.clear()
    _MANIFEST_CACHE[key] = data
    return data


def invalidate_manifest_cache() -> None:
    """装/卸扩展后手动作废。

    正常不需要调 —— mtime + size 已经能覆盖文件被改写的情况。留这个入口
    是给「同一纳秒内改写、size 又恰好相同」这种极端场景兜底，以及给测试
    一个干净起点。
    """
    _MANIFEST_CACHE.clear()


def _fits_launcher(item: dict, line: str) -> tuple[bool, str]:
    """这个包还能不能被本启动器装载。

    这里**不做**任何质量校验（补丁命中、钩子目标、文件清单都不看），
    只回答一个问题：装上也跑不起来的那类「不适配」，要不要直接剔除。

    判据只有三条：
    - 清单读得出来（注册表里记的 manifest 文件真的在）；
    - 清单 schema 是启动器认识的版本；
    - 覆盖层文件还在（用户在资源管理器里删了 packs/ 里的东西就会缺）。

    这三条**两档都执行**：宽松档放行的是「内容有冲突」，不是「根本挂不上」。
    把挂不上的包硬塞进去，玩家看到的就是黑屏 —— 那不是宽松，那是坏。
    """
    data = _read_manifest(str(item.get("id", "")), line)
    if not isinstance(data, dict):
        return False, "缺少可读的扩展清单"
    try:
        schema = int(data.get("schema", 1) or 1)
    except (TypeError, ValueError):
        schema = 0
    if schema not in (1, 2, 3):
        return False, f"清单 schema {schema} 不受支持"
    overlay = str(item.get("overlay") or "")
    if overlay:
        path = cfg_mod.resolve_path(overlay)
        if path is None or not path.exists():
            return False, "覆盖层文件缺失"
    return True, ""


def enabled_mods(cfg: dict, line: str) -> list[dict]:
    """已安装、已启用、且在 ``line`` 这条线上有覆盖层的扩展。

    1.4 起只有一条内置线；``lines`` 仍按线存，是为了兼容老数据与自定义线。
    这里返回的是**按线裁剪后**的视图：顶层字段换成该线的那份，
    这样下游（插桩、挂载、锁文件）不用关心多线的存在。

    玩家版普通模式额外过滤「不适配启动器」的条目：直接在生效集合里剔除，
    不弹提示、不拦启动——玩家不需要理解清单版本和覆盖层路径。
    """
    items = ExtensionService(cfg).installed()
    # 「不适配当前启动器」的条目（清单 schema 不认识、覆盖层文件被删）
    # 在**两档**下都要从生效集合里剔除：它不是「内容冲突」，是「这个包
    # 根本没法挂载」。宽松档只意味着「内容问题放行」，不意味着「挂不上
    # 的也硬挂」——硬挂的结果就是玩家看到黑屏。
    drop_unfits = True

    # 自动兜底写下的**临时停用**名单。
    #
    # 为什么单独一层而不是直接改注册表的 ``enabled``：那是玩家的选择。
    # 启动器自己把某个包标成「停用」，玩家回头看扩展页会以为是自己关的，
    # 而且在界面上重新打开时会和兜底逻辑打架。临时名单只在本次启动生效，
    # 界面照实显示「已启用」，另有一行说明「上次因启动失败被临时跳过」。
    temp_off = _temp_disabled(line)

    out: list[dict] = []
    for item in items:
        pid = str(item.get("id") or "")
        if pid in temp_off:
            continue
        lines = item.get("lines")
        if isinstance(lines, dict) and lines:
            picked = lines.get(line)
            if not isinstance(picked, dict):
                continue      # 这条线上没装
            # **按线启用**：4.6 关掉不影响 4.7。顶层 ``enabled`` 只是镜像，
            # 老数据（lines 里没有 enabled 字段）按启用处理，并回落到顶层。
            if not bool(picked.get("enabled", item.get("enabled", True))):
                continue
            merged = dict(item)
            merged.update(picked)
            merged["engine_line"] = line
            if drop_unfits and not _fits_launcher(merged, line)[0]:
                continue
            out.append(merged)
            continue
        # 旧格式：只有一条线
        if not item.get("enabled", True):
            continue
        if str(item.get("engine_line", "")) == line:
            merged = dict(item)
            if drop_unfits and not _fits_launcher(merged, line)[0]:
                continue
            out.append(merged)
    return out


def _source_root(cfg: dict, line: str) -> Path | None:
    meta = (cfg.get("runtime_sources") or {}).get(line)
    if not meta:
        return None
    root = Path(meta["project_godot"]).parent
    return root if root.exists() else None


# ---------------------------------------------------------------- 临时停用


#: 自动兜底的临时停用名单。**不是**玩家配置，放 CACHE 而不是 CONFIG：
#: 换台机器不该带着「上次崩溃时停了谁」这个状态。
_TEMP_OFF = "auto-disabled.json"


def _temp_off_path() -> Path:
    return paths.CACHE / _TEMP_OFF


def _temp_disabled(line: str) -> set[str]:
    """读这条线的临时停用名单。文件不存在返回空集。"""
    data = read_json(_temp_off_path(), default=None)
    if not isinstance(data, dict):
        return set()
    slot = (data.get("by_line") or {}).get(str(line))
    if not isinstance(slot, dict):
        return set()
    return {str(x) for x in (slot.get("ids") or []) if x}


def set_temp_disabled(line: str, ids: list[str], reason: str = "") -> Path:
    """记录「这些扩展本次启动先跳过」，并让生效视图缓存失效。

    只影响启动，**不改玩家的启用状态**。玩家在扩展页看到的仍是「已启用」，
    另有一行说明为什么这次没生效。
    """
    data = read_json(_temp_off_path(), default={}) or {}
    if not isinstance(data, dict):
        data = {}
    slot = data.setdefault("by_line", {}).setdefault(str(line), {})
    slot["ids"] = sorted({str(x) for x in ids})
    slot["reason"] = reason
    slot["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path = atomic_write_json(_temp_off_path(), data)

    # 生效视图是按「注册表 mtime + packs mtime」缓存的，改名单不会动这两处，
    # 必须显式作废 —— 否则兜底重启后会读到「上一轮的旧集合」，等于没停。
    try:
        from . import effective as effective_mod

        effective_mod.invalidate(line)
    except Exception:  # noqa: BLE001
        pass
    return path


def clear_temp_disabled(line: str = "") -> None:
    """清掉临时停用记录。

    三处调用：玩家在界面上手动「恢复」、游戏成功启动一段时间后自动清、
    以及卸载扩展时。
    """
    if not line:
        try:
            _temp_off_path().unlink(missing_ok=True)
        except OSError:
            pass
        return
    data = read_json(_temp_off_path(), default=None)
    if not isinstance(data, dict):
        return
    slot = data.get("by_line") or {}
    slot.pop(str(line), None)
    try:
        atomic_write_json(_temp_off_path(), data)
    except OSError:
        return
    try:
        from . import effective as effective_mod

        effective_mod.invalidate(line)
    except Exception:  # noqa: BLE001
        pass


def temp_disabled(line: str) -> list[str]:
    """公开读取接口（界面要展示「上次因启动失败跳过了谁」）。"""
    return sorted(_temp_disabled(line))


def _baseline_signature(cfg: dict, line: str) -> str:
    """基线的签名（路径 + 大小 + mtime），给插桩缓存做键的一部分。

    没有基线文件时返回空串——「没有基线」本身也是有效状态，缓存键照常可用。
    用 stat 而不是算 sha256：3 GB 的包每次都哈希会让每次启动多等十几秒，
    而路径+大小+mtime 已经足够区分「换了一个本体」。
    """
    bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
    meta = (cfg.get("baselines") or {}).get(bid) or {}
    raw = meta.get("path") or ""
    if not raw:
        return ""
    path = cfg_mod.resolve_path(str(raw))
    if path is None:
        return ""
    try:
        stat = path.stat()
    except OSError:
        return f"{path}:missing"
    base = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    # 剧情覆盖层也是插桩基底的一部分：换了剧情包（或它被改过），插桩产物
    # 就必须重算，否则会命中「按旧剧情源码算出来的」缓存。
    from .basecontent import story_overlay_of

    story = story_overlay_of(cfg, line)
    if story is not None:
        try:
            sst = story.stat()
            base += f"|story:{story}:{sst.st_size}:{sst.st_mtime_ns}"
        except OSError:
            base += "|story:missing"
    return base


def _hooks_of_item(item: dict, line: str) -> tuple[list[dict], list[str]]:
    """取单个扩展的钩子。任一扩展出错都只影响它自己，不波及其它扩展。

    **插桩点的取值原则：宁多不少。**

    这里有四份可能的数据源，完整度和可信度各不相同：

    ==================  ==========================================
    来源                 问题
    ==================  ==========================================
    ``runtime_hooks``    安装时扫描的，最准但**可能不全**
                         （实测 rose.dimension_ruins 只有 8 条，
                          源码实际 10 条，漏的 2 个钩子从来没插上）
    清单 ``method_hooks`` 相位常与源码不一致
                         （浮游炮清单写 after，源码注册 before+after）
    源码反查             依赖 ``builds/_install_*/src`` 还在，包升级/清理后没有
    ==================  ==========================================

    所以策略是**取并集**：``runtime_hooks`` ∪ 清单双插 ∪ 源码反查。
    多插一个点在运行期只是多派发一次空回调，代价极小；
    少插一个点是 ``add_method_hook`` 返回 false，而扩展普遍写成
    ``if not add_method_hook(...): return false`` —— **后面所有钩子全部丢失**。
    2026-11-05 的浮游炮卡死就是这么来的。
    """
    ext_id = str(item.get("id", ""))
    hooks: dict[tuple[str, str, str], dict] = {}
    warnings: list[str] = []

    def add(script: str, method: str, phase: str) -> None:
        script = str(script or "").replace("res://", "")
        method = str(method or "")
        phase = str(phase or "").strip().lower()
        if not script or not method:
            return
        if phase not in ("before", "after"):
            phase = "after"
        hooks.setdefault((script, method, phase), {
            "script": script, "method": method, "phase": phase,
        })

    # 1) 安装时扫描的真实相位
    for h in item.get("runtime_hooks") or []:
        if isinstance(h, dict):
            add(h.get("script", ""), h.get("method", ""), h.get("phase", "before"))

    # 2) 清单：声明了哪些方法就一定插（相位双插，见上面的取舍说明）
    data = _read_manifest(ext_id, line)
    declared = 0
    if isinstance(data, dict):
        for h in data.get("method_hooks") or []:
            if not isinstance(h, dict):
                continue
            method = str(h.get("method", ""))
            target = str(h.get("target", ""))
            if not method or not target:
                continue
            declared += 1
            add(target, method, "before")
            add(target, method, "after")

    # 3) 源码反查：补上 runtime_hooks 与清单都漏掉的
    tag = paths.line_tag(line)
    src_root = paths.BUILDS / f"_install_{ext_id}_{tag}" / "src"
    scanned = 0
    if src_root.is_dir():
        try:
            from . import hook_scan as scan_mod

            got = scan_mod.scan_package(src_root)
            for site in got.sites:
                scanned += 1
                add(site.script, site.method, site.phase)
        except Exception as exc:  # noqa: BLE001 - 反查失败不影响其它来源
            warnings.append(f"{ext_id}：源码钩子反查失败（{exc}）")

    if not hooks:
        # 只有「确实声明过钩子」的包才值得警告。
        # 大量扩展根本不用钩子机制（ai_act.combat_workbench 就是用 text_patches
        # 直接改原版脚本，清单里 method_hooks 是空的），对它们发警告纯属噪音。
        if declared:
            warnings.append(f"{ext_id}：声明了 {declared} 个钩子但都没有目标，跳过")
        return [], warnings
    return list(hooks.values()), warnings


def _collect_hooks(
    cfg: dict, mods: list[dict], line: str = ""
) -> tuple[list[dict], list[str], dict[str, list[dict]]]:
    """汇总所有扩展声明的方法钩子（去重），并保留「每个包声明了哪些」。

    返回 ``(全部钩子, 警告, 按包分组)``。第三项给启动后的状态流转用：
    只有某个包声明的钩子目标全部插桩成功，才能把它的「待插桩」翻成
    「已就绪」，否则就是骗人。

    优先用安装时扫描出来的 ``runtime_hooks``（源码里的真实相位）；
    老记录没有这一项时，退化为「清单里的方法 + before/after 两种相位都插」——
    多派发一次没有副作用，但能保证运行期注册一定命中。

    隔离原则：某个扩展的钩子读不出来/格式不对，只记一条警告并跳过它，
    绝不让「一个坏包」变成「整个游戏起不来」。
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    warnings: list[str] = []
    by_mod: dict[str, list[dict]] = {}
    for item in mods:
        ext_id = str(item.get("id", ""))
        try:
            hooks, warns = _hooks_of_item(item, line)
        except Exception as exc:  # noqa: BLE001 - 单扩展异常必须被隔离
            warnings.append(f"{ext_id}：钩子收集失败，已跳过（{exc}）")
            continue
        warnings.extend(warns)
        by_mod[ext_id] = list(hooks)
        for h in hooks:
            key = (h["script"], h["method"], h["phase"])
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
    return out, warnings, by_mod


def _build_instrumented(
    cfg: dict,
    line: str,
    run_dir: Path,
    hooks: list[dict],
    overlay_targets: dict[str, list[Path]] | None = None,
    merged_text: dict[str, str] | None = None,
) -> tuple[Path | None, list[str], set[str]]:
    """插桩原版脚本并打成覆盖层。返回 ``(pck 路径, 警告, 实际插桩的脚本集)``。

    第三项是「**真的**插进去了哪些脚本」，用于启动后判定某个扩展的钩子
    是否全部落地。``mods.json`` 里的 ``hooks`` 是声明，不是结果——源码缺失
    时对应脚本会被跳过，两者必须分开。

    ``overlay_targets`` 是「脚本路径 → 含该脚本的 overlay 列表（按挂载顺序）」。
    插桩层排在所有 overlay 之后，所以插桩的基底必须是**最后一个 overlay 的版本**
    而不是源码原文——否则 overlay 里做的补丁（新增成员声明、改方法体）会被
    插桩层覆盖回旧内容。没有 overlay 覆盖的脚本才回落到源码原文。

    ``merged_text`` 是合并层（``ext.compose``）已经叠加好的脚本文本。
    命中时**直接用它当基底、跳过 diff 合并**：它已经包含所有扩展对这些脚本的
    改动，再按 overlay 逐层 diff 一次就等于把同一批补丁打两遍。
    """
    root = _source_root(cfg, line)
    warnings: list[str] = []
    overlay_targets = overlay_targets or {}
    merged_text = merged_text or {}
    # 插桩基底统一从 BaseContent 取（它按「反解工程 → 基线 pck → 已装扩展」
    # 的优先级找原文）。过去这里直接读 ``root/script``，绕过了基线回落。
    from .basecontent import BaseContent, canonical_signature

    base_reader = BaseContent(cfg, line, include_installed=False)
    # 随包反解源码（baseline/canonical_sources.zip）也算可用基底：1.45.1 就是
    # 这么做的。只有「既没有反解工程、又没有随包源码、还没有基线」才放弃插桩。
    canon_sig = canonical_signature(line, cfg)
    base_sig = "|".join(x for x in (_baseline_signature(cfg, line), f"canon:{canon_sig}" if canon_sig else "") if x)
    if root is None and not base_sig:
        if hooks:
            warnings.append(f"{line} 没有反解源码目录，无法插桩（钩子将不生效）")
        return None, warnings, set()

    # 是否往 context 里多注入 instance / arguments。
    #
    # 这两个键是扩展普遍在读的（实测 ai_act 有 65 处读 instance、45 处读
    # arguments），而原来注入的 context 只有 script/method/args 三个 ——
    # 那些代码是**死代码**（is_instance_valid(null) 恒假，回调第一行就 return）。
    #
    # 默认**开**：现状是「扩展作者的钩子代码从来没执行过」，那是缺陷不是特性。
    # 用户可以关掉回退（extensions.hook_ctx_extended=false）。
    extended_ctx = bool((cfg.get("extensions") or {}).get("hook_ctx_extended", True))

    by_script: dict[str, list[hooks_mod.Hook]] = {}
    for h in hooks:
        by_script.setdefault(h["script"], []).append(
            hooks_mod.Hook(script=h["script"], method=h["method"], phase=h["phase"])
        )

    # 缓存键必须把「overlay 对这些脚本的覆盖」算进去：overlay 内容变了
    # （例如扩展升级后补丁不同），插桩基底就不同，产物也不同。
    overlay_sig = _overlay_signature(by_script, overlay_targets, merged_text)

    # 插桩层内容缓存：线 + 钩子规格 + 源文件内容 + overlay 覆盖签名都不变才复用。
    cache_key = _instrumented_cache_key(
        line, by_script, root, overlay_sig, extended_ctx, base_sig=base_sig
    )
    cached = paths.CACHE / "instrumented" / f"{cache_key}.pck"
    if cached.exists():
        # 缓存命中也得知道「上次实际插了哪些脚本」，否则启动后无法判断
        # 某个包能不能翻成已就绪。元数据里有 applied（老缓存没有就按
        # 本次请求的脚本集算——缓存是按这套请求生成的）。
        applied: set[str] = set()
        try:
            meta = read_json(cached.with_suffix(".json"), default={}) or {}
            raw = meta.get("applied")
            if isinstance(raw, list):
                applied = {str(x) for x in raw if x}
        except Exception:  # noqa: BLE001 - 元数据坏了按全量算，只影响状态提示
            applied = set()
        if not applied:
            applied = set(by_script)
        return cached, ["（命中插桩缓存，跳过重插桩）"], applied

    writer = PckWriter(run_dir / "instrumented.pck", line=line, verify=False)
    written = 0

    # 先攒住「每个脚本的最终文本」，最后统一落盘。
    #
    # 不能边算边 ``writer.add_text``：``scripts/hud.gd`` 既是方法钩子目标，
    # 又是社交注入目标，而 PckWriter 是同名覆盖（``_items[path] = ...``）。
    # 过去社交注入拿的是**未插桩的基底**再写一次同一路径，把刚写进去的方法
    # 钩子插桩整段冲掉：hud.gd 上声明的钩子（_ready / refresh / modal_open）
    # 在运行时一个都不触发。扩展侧只看到 ``add_method_hook`` 返回 true，
    # 界面显示「装载成功」，实际钩子全是死代码 —— 排查时极难定位。
    pending: dict[str, str] = {}

    for script, specs in by_script.items():
        base_text = merged_text.get(script)
        if base_text is None:
            base_text = _script_base_text(script, root, overlay_targets, base_reader)
        if base_text is None:
            warnings.append(f"找不到 {script} 的源码，跳过插桩")
            continue
        try:
            updated, warns = hooks_mod.instrument_script(
                base_text, specs, script, extended_ctx=extended_ctx
            )
        except Exception as exc:  # noqa: BLE001 - 单个脚本插桩失败不影响其它脚本
            warnings.append(f"{script} 插桩失败，已跳过（{exc}）")
            continue
        warnings.extend(warns)
        if updated == base_text:
            warnings.append(f"{script} 未发生变化（方法名可能不对）")
            continue
        pending[script] = updated

    # 社交指令注册表：有扩展时要注入，否则 register_social_command 永远不会被调用。
    # 基底优先取**已经插过方法钩子**的文本，同路径只写一次（见 pending 的说明）。
    hud_base = pending.get(SOCIAL_SCRIPT)
    if hud_base is None:
        hud_base = merged_text.get(SOCIAL_SCRIPT)
    if hud_base is None:
        hud_base = _script_base_text(SOCIAL_SCRIPT, root, overlay_targets, base_reader)
    if hud_base is not None:
        try:
            updated, warns = hooks_mod.instrument_social(hud_base, SOCIAL_SCRIPT)
            warnings.extend(warns)
            if updated != hud_base:
                pending[SOCIAL_SCRIPT] = updated
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{SOCIAL_SCRIPT} 社交注入失败，已跳过（{exc}）")

    for script, text in pending.items():
        writer.add_text(script, text)
        writer.add_remap(script)   # 关键：中和基线的 x.gd -> x.gdc
        written += 1

    if written == 0:
        return None, warnings, set()

    writer.commit()
    built = run_dir / "instrumented.pck"
    # 只有方法钩子的目标脚本才算「插桩完成」；hud.gd 可能只是为了社交注入
    # 被写进去，那是另一条链路，不能拿来证明某个包的钩子成功了。
    applied = {s for s in pending if s in by_script}
    # 落一份内容缓存：钩子规格 + 源文件内容 + overlay 覆盖都没变时，下次直接复用
    _cache_instrumented(
        line, by_script, root, built, overlay_sig, extended_ctx, base_sig=base_sig,
        applied=applied,
    )
    return built, warnings, applied


def _script_base_text(
    script: str,
    root: Path | None,
    overlay_targets: dict[str, list[Path]],
    base_reader: object | None = None,
) -> str | None:
    """取插桩基底：**原版内容** + 所有覆盖该脚本的 overlay 改动。

    为什么不是「取最后一个 overlay」：每个 overlay 都是**各自基于源码原文**
    打的补丁（实测 game.gd：源码 64704 字节，qt6663.exp_pet_exp 的 overlay
    64861，whf.all_instructors 的 overlay 66306，三者互不包含）。按「后挂覆盖
    先挂」的原始规则，最终生效的只有最后一份，前面那些扩展的补丁全部丢失。

    要保留每一份的改动，就得按「源码原文 → 用每个 overlay 的版本依次打补丁」
    的顺序累积。这里用序列差分还原每份 overlay 的改动：把 overlay 的内容与
    **它当时的基底**比对，取差集再应用到当前累积结果上。

    差分太脆弱（不同 overlay 的上下文行会互相干扰），因此改用更直接的做法：
    以源码为共同基底，对每份 overlay 求「相对于源码新增/修改的片段」，逐份
    应用。实现放在 :func:`_merge_overlay_scripts`，逻辑简单且可解释：
    只要某个 overlay 的某个替换片段在累积结果里找不到，就跳过它并记警告，
    而不是静默丢掉（静默丢正是这次 bug 难查的原因）。
    """
    sources: list[str] = []
    base_src = None
    if base_reader is not None:
        try:
            raw = base_reader.read(script)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 取不到就走源码回落
            raw = None
        if raw is not None:
            base_src = raw.decode("utf-8", "replace")
    src = root / script if root is not None else None
    if base_src is None and src is not None and src.exists():
        try:
            base_src = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            base_src = None
    if base_src is not None:
        sources.append(base_src)

    for overlay in overlay_targets.get(script) or []:
        try:
            from ..pck.reader import PckReader

            reader = PckReader.open(overlay)
            entry = reader.get(script)
            if entry is not None:
                sources.append(reader.read_bytes(entry).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - 读不到就跳过这一层
            continue

    if not sources:
        return None
    if len(sources) == 1:
        return sources[0]
    return _merge_script_layers(base_src or sources[0], sources[1:])


def _merge_script_layers(base: str, layers: list[str]) -> str:
    """把多层 overlay 的脚本改动累积到 ``base`` 上。

    两个实测踩过的坑，决定了这里的规则：

    1. **同区间「替换」必须后者获胜**。两层都把 ``var flash_actions`` 换成自己的
       preload 时，若都保留就会插出两条同名声明，引擎报
       ``Variable "flash_actions" has the same name as a previously declared variable``。

    2. **同位置的「纯插入」必须合并，不能覆盖**。扩展的成员声明块都插在
       ``extends`` 之后的同一行（``base[1:1]``），若按「后者获胜」处理，
       后挂载那份的声明会把前面的整块顶掉——表现为某扩展的常量「明明装了却
       未声明」，例如 ``PET_EXP_DIVISOR not declared``。

    所以按「区间 + 是否纯插入」区分：纯插入（``i1 == i2``）逐层拼接，
    真替换（``i1 < i2``）后者覆盖。
    """
    import difflib

    #: key = (i1, i2)；value = (层序号, 该层在这一区间的行)
    replacements: dict[tuple[int, int], tuple[int, list[str]]] = {}
    insertions: dict[int, list[tuple[int, list[str]]]] = {}

    for index, layer in enumerate(layers):
        if not layer or layer == base:
            continue
        matcher = difflib.SequenceMatcher(None, base.splitlines(True), layer.splitlines(True))
        layer_lines = layer.splitlines(True)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            seg = layer_lines[j1:j2]
            if i1 == i2:
                # 纯插入：按层序累积，全部保留
                insertions.setdefault(i1, []).append((index, seg))
            else:
                prev = replacements.get((i1, i2))
                if prev is None or index > prev[0]:
                    replacements[(i1, i2)] = (index, seg)

    if not replacements and not insertions:
        return base

    base_lines = base.splitlines(True)
    out: list[str] = []
    cursor = 0
    points = sorted(set(insertions) | {i1 for (i1, _i2) in replacements})
    for point in points:
        if point < cursor:
            continue
        out.extend(base_lines[cursor:point])
        # 先写该点的插入（按层序），再处理以该点为起点的替换
        for _idx, seg in sorted(insertions.get(point, [])):
            out.extend(seg)
        hit = [(k, v) for k, v in replacements.items() if k[0] == point]
        for (i1, i2), (_idx, seg) in sorted(hit):
            out.extend(seg)
            cursor = max(cursor, i2)
        cursor = max(cursor, point)
    out.extend(base_lines[cursor:])
    return "".join(out)


def _merged_cache_key(
    cfg: dict,
    line: str,
    pairs: list[tuple[str, dict]],
    overlays: dict[str, Path],
    plan: object | None,
) -> str:
    """合并层缓存键：参与方 + 各自 overlay 的 stat + 基线签名 + 计划指纹。

    用 ``stat``（路径 + 大小 + mtime）而不是内容哈希：overlay 有几十 MB，
    每次启动都哈希等于把「省下来的重建时间」又花回读盘上。overlay 变了
    mtime 一定变，这就够区分了。

    计划（``plan``）也要进键：同一批扩展，处置结果不同（谁覆盖谁、
    谁被停用）会产出不同的合并结果。
    """
    parts: list[str] = [f"line={line}", f"schema=1"]
    for ext_id, _data in sorted(pairs):
        path = overlays.get(ext_id)
        if path is None:
            parts.append(f"{ext_id}:nooverlay")
            continue
        try:
            st = path.stat()
            parts.append(f"{ext_id}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{ext_id}:missing")
    # 基线/剧情层变了，合并的「原版基底」就变了
    parts.append(f"base={_baseline_signature(cfg, line)}")
    if plan is not None:
        # 只取影响合并结果的部分：停用集合 + 每个目标被谁接管
        disabled = sorted(str(x) for x in (getattr(plan, "disabled", set()) or set()))
        parts.append("disabled=" + ",".join(disabled))
    blob = "\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _build_merged(
    cfg: dict, line: str, run_dir: Path, mods: list[dict], plan: object | None = None
) -> tuple[object | None, dict[str, str]]:
    """合并层：被多个扩展同时修改的文件，按解析顺序叠加成 ``merged.pck``。

    返回 ``(compose.Result | None, 脚本文本表)``。第二个值给插桩层当基底用。

    为什么要读「已装扩展的整文件副本」而不是源码：``rose.dimension_ruins``
    的 ``mods/rose.dimension_ruins/dimension_ruins.gd`` 在原版里根本不存在，
    只有它自己的覆盖层里有。而 P01 的补丁正是打在这份文件上的——所以
    合并时的基底要么取原版、要么取那个扩展的整文件副本，二者都不会是
    「另一个扩展补丁后的结果」。

    **内容缓存**：``run_dir`` 按时间戳每次新建，所以合并产物原先**永远
    缓存 miss**（插桩层有缓存，合并层没有 —— 这是个疏漏）。同一套扩展
    第二次启动仍要把每个重叠目的大文件读一遍再叠一遍。命中缓存时直接
    把缓存 pck 复制进本次 run_dir，省掉整个重建过程。
    """
    from . import compose as compose_mod
    from .basecontent import BaseContent

    pairs: list[tuple[str, dict]] = []
    overlays: dict[str, Path] = {}
    for item in mods:
        ext_id = str(item.get("id") or "")
        data = _manifest_of(item, line)
        if not isinstance(data, dict):
            continue
        pairs.append((ext_id, data))
        resolved = cfg_mod.resolve_path(str(item.get("overlay") or ""))
        if resolved is not None:
            overlays[ext_id] = resolved
    if len(pairs) < 2:
        return None, {}

    # 读扩展覆盖层的统一入口。reader 按 ext_id 缓存（同一个包只开一次），
    # 这是必要的：一个扩展改 10 个脚本就会调 10 次，每次都重新开 pck
    # 等于把文件头解析做 10 遍。
    readers: dict[str, object] = {}

    def read_overlay(ext_id: str, target: str) -> bytes | None:
        path = overlays.get(ext_id)
        if path is None or not path.exists():
            return None
        reader = readers.get(ext_id)
        if reader is None:
            try:
                from ..pck.reader import PckReader

                reader = PckReader.open(path)
            except Exception:  # noqa: BLE001 - 坏包按「取不到」处理
                reader = False
            readers[ext_id] = reader
        if reader is False:
            return None
        try:
            entry = reader.get(target)  # type: ignore[union-attr]
            return reader.read_bytes(entry) if entry is not None else None  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None

    out_pck = run_dir / "merged.pck"
    cache_key = _merged_cache_key(cfg, line, pairs, overlays, plan)
    cached = paths.CACHE / "merged" / f"{cache_key}.pck"
    meta_path = paths.CACHE / "merged" / f"{cache_key}.json"

    if cached.is_file() and meta_path.is_file():
        try:
            meta = read_json(meta_path, default={}) or {}
            shutil.copy2(cached, out_pck)
            result = compose_mod.Result(
                entries=dict(meta.get("entries") or {}),
                notes=list(meta.get("notes") or []),
                conflicts=list(meta.get("conflicts") or []),
                degraded=list(meta.get("degraded") or []),
                dispositions=list(meta.get("dispositions") or []),
                pck=out_pck,
                sha256=str(meta.get("sha256") or ""),
                size=int(meta.get("size") or 0),
            )
            # entries 里存的是 "路径→文本"，这里换回 bytes 以对齐正常路径
            texts = result.entries
            result.entries = {
                k: (v.encode("utf-8") if isinstance(v, str) else v)
                for k, v in texts.items()
            }
            result.notes = [*result.notes, "（命中合并层缓存，跳过重建）"]
            return result, result.texts()
        except Exception:  # noqa: BLE001 - 缓存坏了就当没命中，正常重建
            pass

    base = BaseContent(cfg, line, include_installed=False)
    merged = compose_mod.build(
        pairs,
        read_base=base.read,
        read_overlay=read_overlay,
        out_pck=out_pck,
        line=line,
        origin_base=base.origin,
        plan=plan,
    )

    # 只在真正产出了合并文件时才写缓存：没有重叠时 compose 不落盘，
    # 缓存一份空结果没有意义，还会让下次误以为「有合并层」。
    if merged.pck is not None and merged.entries:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(merged.pck, cached)
            atomic_write_json(
                meta_path,
                {
                    "schema": 1,
                    "line": line,
                    "sha256": merged.sha256,
                    "size": merged.size,
                    # entries 存文本（合并层只处理脚本），字节由文本还原
                    "entries": {
                        k: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                        for k, v in merged.entries.items()
                    },
                    "notes": merged.notes,
                    "conflicts": merged.conflicts,
                    "degraded": merged.degraded,
                    "dispositions": merged.dispositions,
                },
            )
        except Exception:  # noqa: BLE001 - 缓存写失败不影响本次启动
            pass

    return merged, merged.texts() if merged.entries else {}


def _overlay_signature(
    by_script: dict[str, list[hooks_mod.Hook]],
    overlay_targets: dict[str, list[Path]],
    merged_text: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """对「覆盖插桩目标的 overlay 文件」做内容签名，用于缓存键。

    合并层（``merged.pck``）的内容也要进签名：它才是这些脚本的最终基底，
    扩展版本一变、合并结果就变，只算 overlay 会命中过期缓存。

    **同一个 overlay 只读一次**。这是启动耗时的主要来源之一：
    ``overlay_targets`` 是「脚本 → 覆盖它的 overlay」的倒排索引，同一个
    overlay 会因为改了 10 个脚本而出现 10 次。原实现对每次出现都
    ``read_bytes()`` 整个 pck（几十 MB），19 个扩展就是几十次全量读盘 ——
    而这个函数算出来的只是缓存键，读盘成本全白花。

    签名按 ``(stat → sha256)`` 两层做：先看 mtime+size，命中就复用上次
    算好的哈希（进程内 memo），避免「同一个文件在同一轮里被哈希多次」。
    """
    digests: dict[Path, str] = {}

    def digest_of(path: Path) -> str:
        got = digests.get(path)
        if got is not None:
            return got
        try:
            got = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            got = "missing"
        digests[path] = got
        return got

    out: list[tuple[str, str]] = []
    names = set(by_script.keys()) | {SOCIAL_SCRIPT}
    for script in sorted(names):
        for overlay in overlay_targets.get(script) or []:
            out.append((f"{script}@{overlay.name}", digest_of(overlay)))
        if merged_text and script in merged_text:
            out.append(
                (f"{script}@merged", hashlib.sha256(merged_text[script].encode("utf-8")).hexdigest())
            )
    return out


def _instrumented_cache_key(
    line: str,
    by_script: dict[str, list[hooks_mod.Hook]],
    root: Path | None,
    overlay_sig: list[tuple[str, str]] | None = None,
    extended_ctx: bool = False,
    base_sig: str = "",
) -> str:
    """插桩层的缓存键：只依赖「引擎线 + 钩子规格 + 源文件内容 + overlay 覆盖 + context 契约」。

    ``PckWriter`` 是确定性的（同输入同字节），所以这几项不变，产物字节就不变。
    基线/源码/overlay 任一改动，缓存自动失效。

    ``CACHE_FORMAT`` 是保险：插桩算法或派发契约改了（例如 hooks.py 调整了
    模板），旧缓存内容其实已经不对，但输入签名可能恰好没变。加版本号后
    这类改动只需把常量 +1，全部旧缓存自动作废。

    ``extended_ctx`` 必须进键：它决定 context 里有没有 ``instance`` /
    ``arguments``，两者产物字节**完全不同**，共用缓存会拿到错的那份。
    """
    h = hashlib.sha256()
    h.update(str(CACHE_FORMAT).encode("utf-8"))
    h.update(line.encode("utf-8"))
    h.update(b"ctx2" if extended_ctx else b"ctx1")
    # 基线 pck 的签名进键：换基线（导入新本体）后插桩基底就变了，缓存必须失效。
    h.update(base_sig.encode("utf-8"))
    specs: list[tuple[str, str, str]] = []
    for script, items in by_script.items():
        for spec in items:
            specs.append((script, spec.method, spec.phase))
    h.update(json.dumps(sorted(specs), ensure_ascii=False).encode("utf-8"))
    files = sorted(set(by_script.keys()) | {SOCIAL_SCRIPT})
    sigs: list[tuple[str, str]] = []
    for rel in files:
        src = root / rel if root is not None else None
        if src is not None and src.exists():
            sigs.append((rel, hashlib.sha256(src.read_bytes()).hexdigest()))
    h.update(json.dumps(sigs, ensure_ascii=False).encode("utf-8"))
    if overlay_sig:
        h.update(json.dumps(overlay_sig, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:32]


def _metadata_of(cache_dir: Path, pck: Path) -> dict:
    """读某个缓存 pck 的元数据（同名 .json）；没有就返回空表。"""
    meta_path = cache_dir / f"{pck.stem}.json"
    data = read_json(meta_path, default=None)
    return data if isinstance(data, dict) else {}


def _purge_instrumented_cache(line: str = "") -> int:
    """删除插桩缓存。``line`` 为空时删全部，否则只删该线。

    只删派生物（``.pck`` 与其 ``.json`` 元数据），不碰 ``packs/`` 与扩展本体。
    对于「没有元数据的旧缓存」，无法判断属于哪条线，``line`` 非空时也一并清掉——
    这些是历史遗留（CACHE_FORMAT 升级前产生的），留着只会干扰排查。
    """
    cache_dir = paths.CACHE / "instrumented"
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for pck in list(cache_dir.glob("*.pck")):
        meta = _metadata_of(cache_dir, pck)
        if line and meta and str(meta.get("line") or "") not in ("", line):
            continue
        try:
            pck.unlink()
            removed += 1
        except OSError:
            continue
        meta_path = cache_dir / f"{pck.stem}.json"
        if meta_path.exists():
            try:
                meta_path.unlink()
            except OSError:
                pass
    return removed


def instrumented_cache_info() -> list[dict]:
    """列出当前所有插桩缓存，供界面展示「这份缓存是怎么来的」。"""
    cache_dir = paths.CACHE / "instrumented"
    if not cache_dir.is_dir():
        return []
    out: list[dict] = []
    for pck in sorted(cache_dir.glob("*.pck")):
        meta = _metadata_of(cache_dir, pck)
        out.append(
            {
                "key": pck.stem,
                "path": str(pck),
                "size": pck.stat().st_size,
                "line": meta.get("line", ""),
                "format": meta.get("format"),
                "created_at": meta.get("created_at", ""),
                "hook_count": len(meta.get("hooks") or []),
                "overlay_count": len(meta.get("overlay_sig") or []),
            }
        )
    return out


def _cache_instrumented(
    line: str,
    by_script: dict,
    root: Path | None,
    built: Path,
    overlay_sig: list[tuple[str, str]] | None = None,
    extended_ctx: bool = False,
    base_sig: str = "",
    applied: set[str] | None = None,
) -> None:
    """把刚生成的插桩层复制到内容缓存，供后续启动命中。

    同时写一份同名 ``.json`` 元数据，记录线、格式版本、来源脚本与 overlay 签名。
    元数据不参与命中判定（键已经包含全部输入），只用于「这份缓存是什么时候、
    由什么产生的」这类排障问题，以及按线清理时识别归属。
    """
    if not built.exists():
        return
    try:
        key = _instrumented_cache_key(
            line, by_script, root, overlay_sig, extended_ctx, base_sig=base_sig
        )
        cache_dir = paths.CACHE / "instrumented"
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / f"{key}.pck"
        if not dest.exists():
            tmp = cache_dir / f".{key}.tmp"
            tmp.write_bytes(built.read_bytes())
            os.replace(tmp, dest)
        # 插桩缓存是纯派生数据（线 + 钩子 + 源文件 + overlay 签名决定），
        # 同一时刻每线只留最新一份就够。不清会随钩子组合变化无限堆积，
        # 而且旧的一份永远不可能再被命中。
        for stale in cache_dir.glob("*.pck"):
            if stale.stem == key:
                continue
            meta = _metadata_of(cache_dir, stale)
            if str(meta.get("line") or "") not in ("", line):
                continue
            try:
                stale.unlink()
                old_meta = cache_dir / f"{stale.stem}.json"
                if old_meta.exists():
                    old_meta.unlink()
            except OSError:
                continue
        atomic_write_json(
            cache_dir / f"{key}.json",
            {
                "key": key,
                "format": CACHE_FORMAT,
                "line": line,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "hooks": [
                    [s, h.method, h.phase] for s, items in by_script.items() for h in items
                ],
                "overlay_sig": [{"target": t, "sha256": d} for t, d in (overlay_sig or [])],
                # 实际插桩成功的脚本。命中缓存时靠它判断某包的钩子有没有落地，
                # 不能用请求集代替——源码缺失的脚本会被跳过。
                "applied": sorted(applied if applied is not None else set(by_script)),
            },
        )
    except OSError:
        # 缓存只是加速，失败绝不影响启动
        pass


def _manifest_of(item: dict, line: str = "") -> dict | None:
    return _read_manifest(str(item.get("id", "")), line or str(item.get("engine_line", "")))


def manifest_of(item: dict, line: str = "") -> dict | None:
    """公开取「这条线上生效的清单」。

    为什么要公开：界面红条（``pages/extensions.py``）和 CLI 的 ``lock``
    子命令过去都硬编码 ``packs/<id>/manifest.v2.json``，而安装实际写的是
    ``manifest-46.v2.json``——两条路读的不是同一份文件，于是「包里只有
    带线清单」的扩展会被静默漏掉（``packs/test.member`` 就是这种）。
    统一走这里，UI、CLI、启动三处的结论才可能一致。
    """
    return _manifest_of(item, line)


def enabled_manifests(cfg: dict, line: str) -> list[tuple[str, dict]]:
    """该线上「已启用 + 能读到清单」的扩展，可直接喂给 ``resolver.resolve``。

    和 ``enabled_mods`` 的区别：那个返回注册表条目（含 overlay、钩子等），
    这个只返回解析冲突需要的 ``(id, manifest.data)``。缺清单的包会被跳过——
    它连声明都没有，参与不了冲突判定，跳过的理由由调用方的 warnings 说明。
    """
    pairs: list[tuple[str, dict]] = []
    for item in enabled_mods(cfg, line):
        data = _manifest_of(item, line)
        if isinstance(data, dict):
            pairs.append((str(item.get("id", "")), data))
    return pairs

def resolve_order(
    cfg: dict, line: str, mods: list[dict], run_dir: Path, plan: object | None = None
) -> tuple[list[dict], dict, list[str], object | None]:
    """按 requires / load_after / priority 排序，判定位置冲突，落 lock.json。

    返回 ``(排好序的 mods, lock, 警告, 处置计划)``。

    只有开发者模式才会因为冲突拒绝启动；普通/宽松档把「真装不了的包」
    剔除后继续，避免一个坏包让整个游戏起不来。
    """
    from ..errors import LauncherError
    from . import decisions as decisions_mod
    from . import policy as policy_mod
    from . import resolver as resolver_mod

    mode = cfg_mod.normalize_mode(cfg_mod.extension_mode(cfg))
    store = decisions_mod.Store.load(paths.CONFIG_DIR / "decisions.json")
    warnings: list[str] = []
    excluded: set[str] = set()

    def collect(current: list[dict]) -> tuple[list[tuple[str, dict]], dict[str, dict]]:
        got: list[tuple[str, dict]] = []
        packages: dict[str, dict] = {}
        for item in current:
            ext_id = str(item.get("id", ""))
            data = _manifest_of(item, line)
            if data is None:
                warnings.append(f"{ext_id}：缺少 manifest.v2.json，不参与顺序解析")
                continue
            # 已装的老扩展也要过联机闸门：安装时可能还没有这道检查。
            net = netguard.audit_manifest_only(data)
            if net.blocked:
                if cfg_mod.netplay_open(cfg):
                    warnings.append(
                        f"{ext_id}：联机协议已开放，放行（{net.blocks[0].reason}）"
                    )
                else:
                    warnings.append(f"{ext_id}：{net.message()}")
                    excluded.add(ext_id)
                    continue
            got.append((ext_id, data))
            packages[ext_id] = {
                "version": item.get("version", ""),
                "kind": data.get("kind", "extension"),
                "priority": int(data.get("priority", 50) or 50),
                "overlay": item.get("overlay", ""),
                "sha256": item.get("sha256", ""),
                "entries": item.get("entry_count", 0),
            }
        return got, packages

    items, packages = collect(mods)
    resolution = resolver_mod.resolve(
        items, mode=mode, line=line, store=store, plan=plan
    )

    if not resolution.ok and not cfg_mod.is_strict(mode):
        bad_ids = sorted(
            {pid for p in resolution.problems if p.level == "error" for pid in (p.ids or [])}
        )
        if bad_ids:
            excluded.update(bad_ids)
            mods = [m for m in mods if str(m.get("id")) not in bad_ids]
            warnings.append(
                f"已跳过 {len(bad_ids)} 个无法与当前组合共存的扩展：{'、'.join(bad_ids)}"
            )
            items, packages = collect(mods)
            resolution = resolver_mod.resolve(
                items, mode=mode, line=line, store=store, plan=None
            )

    bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
    meta = (cfg.get("baselines") or {}).get(bid) or {}
    lock = resolver_mod.build_lock(
        resolution,
        packages,
        line=line,
        baseline={
            "id": bid,
            "sha256": meta.get("sha256", ""),
            "entry_count": meta.get("entry_count", 0),
            "size": meta.get("size", 0),
        },
    )
    atomic_write_json(run_dir / "lock.json", lock)

    if not resolution.ok and cfg_mod.is_strict(mode):
        detail = "；".join(p.message for p in resolution.problems if p.level == "error")
        raise LauncherError(
            f"扩展组合有冲突，已拒绝启动：{detail}", path=str(run_dir / "lock.json")
        )

    position = {pid: index for index, pid in enumerate(resolution.order)}
    ordered = [
        m
        for m in sorted(mods, key=lambda m: position.get(str(m.get("id")), 999))
        if str(m.get("id")) not in excluded
    ]
    warnings.extend(p.message for p in resolution.problems if p.level == "warn")
    return ordered, lock, warnings, resolution.plan


def build_for_launch(cfg: dict, line: str, run_dir: str | Path) -> LaunchMods:
    """产出「插桩覆盖层 + 各扩展覆盖层 + mods.json + lock.json」。"""
    rd = Path(run_dir)
    rd.mkdir(parents=True, exist_ok=True)
    result = LaunchMods()

    mods = enabled_mods(cfg, line)
    if not mods:
        return result

    # 开发者模式：先整组剔除未通过的包，再排顺序、收集钩子。
    # 顺序很关键——hooks 是从 mods 收集的，先过滤才不会给被剔除的包插桩。
    from . import effective as effective_mod
    from . import gate as gate_mod

    # 启动这条路径必须拿最新结论，不吃缓存：界面刚改完启停、配置还没落盘时，
    # 缓存里的旧集合会让「点了启动却少装一个包」。
    view = effective_mod.of(cfg, line, refresh=True)
    decision = view.plan
    if view.dropped:
        mods = [m for m in mods if str(m.get("id") or "") in view.keep_set]
        # 与 readiness 的文案错开：两处都写「剔除 N 个、剩 M 个」时，
        # 启动日志会连着出现两条一模一样的话，看着像重复执行了两次。
        result.warnings.append(
            f"实际挂载 {len(mods)} 个（开发者模式整组剔除 {len(view.dropped)} 个）"
        )
        gate_mod.write_report(decision, rd)
    result.gate = decision

    if not mods:
        # 全剔光：裸启动。仍然写一份 lock.json，方便排查「为什么一个都没挂」。
        from . import resolver as resolver_mod

        lock = resolver_mod.build_lock(
            resolver_mod.resolve([]),
            {},
            line=line,
            baseline={"id": cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)},
        )
        result.lock = lock
        atomic_write_json(rd / "lock.json", lock)
        result.mods_json = atomic_write_json(
            rd / "mods.json",
            {
                "schema": 2,
                "line": line,
                "mode": cfg_mod.extension_mode(cfg),
                "hooks": [],
                "mods": [],
                "peer_expose": {},
            },
        )
        return result

    # 本地处置：整包停用的先摘掉，再排序；位置级的留给 compose。
    from . import decisions as decisions_mod
    from . import policy as policy_mod

    store = decisions_mod.Store.load(paths.CONFIG_DIR / "decisions.json")
    pairs_all = [
        (str(m.get("id", "")), _manifest_of(m, line) or {}) for m in mods
    ]
    disabled = policy_mod.disabled_parties(pairs_all, store=store, line=line)
    if disabled:
        mods = [m for m in mods if str(m.get("id")) not in disabled]
        result.warnings.append(f"按本地处置停用：{'、'.join(sorted(disabled))}")

    mods, lock, warns, plan = resolve_order(cfg, line, mods, rd)
    result.lock = lock
    result.plan = plan
    result.warnings.extend(warns)
    if plan is not None:
        for state in getattr(plan, "degraded", []):
            result.warnings.append(state.message)

    hooks, warns, hooks_by_mod = _collect_hooks(cfg, mods, line)
    result.hooks = hooks
    result.warnings.extend(warns)

    # 各扩展的 peer_expose 汇总（顶层一个表，游戏侧查表用）
    peer_expose: dict[str, dict] = {}

    # 先把 overlay 按解析好的顺序排进 packs，再生成插桩层。
    # 顺序很关键：插桩层必须排最后（见模块 docstring），而它的内容又需要
    # 基于 overlay 合并后的脚本，所以要在收集 overlay 之后再构建。
    overlay_targets: dict[str, list[Path]] = {}
    for item in mods:
        ext_id = str(item.get("id", ""))
        try:
            # 清单里存的可能是包内相对路径（整个目录搬走后仍有效），
            # 这里必须按项目根展开，否则 Path() 会按 cwd 解析成别的地方。
            overlay = cfg_mod.resolve_path(str(item.get("overlay") or ""))
            if overlay is None or not overlay.exists():
                result.warnings.append(f"{ext_id} 的覆盖层缺失：{overlay}")
                continue
            result.packs.append({"path": overlay.as_posix(), "offset": 0, "role": "mod", "id": ext_id})
            for rel in _overlay_script_paths(overlay):
                overlay_targets.setdefault(rel, []).append(overlay)
            entry = _entry_of(item, line)
            if entry:
                mod_entry = {"id": ext_id, "entry": entry}
                # peer_access / peer_expose：跨扩展读写的**双向**声明。
                # 消费方声明意图、提供方声明许可，游戏侧取交集。
                decl = _peer_decl(item, line)
                if decl.get("peer_access"):
                    mod_entry["peer_access"] = decl["peer_access"]
                if decl.get("peer_expose"):
                    mod_entry["peer_expose"] = decl["peer_expose"]
                    peer_expose[ext_id] = decl["peer_expose"]
                result.mods.append(mod_entry)
            else:
                # 纯补丁包（只有 data/text_patch 或纯文件覆盖）没有运行期入口，
                # 只需挂载覆盖层即可，不必让 ModHost 去实例化一个空路径。
                result.warnings.append(f"{ext_id}：没有 scripts.entry，按纯覆盖层挂载")
        except Exception as exc:  # noqa: BLE001 - 单扩展装载失败只跳过它自己
            result.warnings.append(f"{ext_id} 装载失败，已跳过（{exc}）")
            continue

    # 合并层：被多个扩展同时修改的文件先叠加成一份，再挂上去。
    # 它必须排在所有扩展覆盖层之后——合并结果是「最终版」，要能盖住各扩展
    # 自己那份只含单方改动的副本。
    merged, merged_text = _build_merged(cfg, line, rd, mods, plan)
    if merged is not None:
        if merged.pck is not None and merged.entries:
            result.packs.append(
                {"path": merged.pck.as_posix(), "offset": 0, "role": "merged"}
            )
            result.warnings.append(
                f"合并层：{len(merged.entries)} 个文件由多个扩展叠加"
                f"（{'、'.join(sorted(merged.entries))}）"
            )
        for conflict in merged.conflicts:
            code = str(conflict.get("code") or "")
            if code == "compose.anchor":
                # 锚点失配 = 某个包的改动整条丢失，不能只当警告。
                # 单独收进 dropped，界面要标红；若是改父类脚本，还会连带灰屏。
                result.skipped_patches.append(
                    {
                        "target": conflict.get("target"),
                        "ext_id": conflict.get("ext_id")
                        or (conflict.get("ids") or [""])[0],
                        "reason": conflict.get("reason"),
                        "anchor": conflict.get("anchor"),
                        "impact": conflict.get("impact"),
                    }
                )
                result.warnings.append(
                    f"合并层跳过 {conflict.get('target')}："
                    f"{conflict.get('ext_id') or ''} 的改动未生效"
                    f"（锚点 {str(conflict.get('anchor') or '')[:60]}）"
                )
                continue
            result.warnings.append(
                f"合并层跳过 {conflict.get('target')}：{conflict.get('reason')}"
            )
        atomic_write_json(rd / "compose.json", merged.as_dict())

    # 插桩层最后挂：既不会被 overlay 盖掉，又能带上 overlay 的改动。
    instrumented, warns, applied = _build_instrumented(
        cfg, line, rd, hooks, overlay_targets, merged_text
    )
    result.warnings.extend(warns)
    result.instrumented_scripts = set(applied)
    if hooks and instrumented is None:
        # 有钩子却一个脚本都没插成：先判断是不是「根本没有可用源码」。
        # 这种情况必须硬失败——继续启动只会得到「扩展显示已启用、进游戏
        # 完全没效果」，那是最难排查的一类故障（过去正是这么静默的）。
        from ..errors import LauncherError
        from . import basecontent

        ok_src, why = basecontent.source_ready(cfg, line)
        if not ok_src:
            raise LauncherError(
                "无法为已启用的扩展插桩，启动已中止。\n\n" + why
            )
    if instrumented is not None:
        result.packs.append({"path": instrumented.as_posix(), "offset": 0, "role": "instrumented"})

    # 哪些包的钩子**全部**落地了？
    #
    # 这是「待插桩 → 已就绪」的唯一依据：声明了钩子、且它涉及的每个脚本
    # 都真的被插桩，启动成功后才能翻状态。只按「有没有声明」算会把
    # 「目标脚本不存在、插桩整段跳过」的包也标成已就绪——那正是过去
    # 「显示已启用、进游戏没反应」的翻版。
    for item in mods:
        ext_id = str(item.get("id", ""))
        declared = hooks_by_mod.get(ext_id) or []
        if not declared:
            continue
        targets = {str(h.get("script") or "") for h in declared if h.get("script")}
        if targets and targets <= result.instrumented_scripts:
            result.ready_ids.append(ext_id)

    payload = {
        "schema": 2,
        "line": line,
        # 扩展隔离模式。游戏侧的 ModHost 读它决定要不要启用严格行为。
        # 走 mods.json 而不加引擎命令行参数：mods.json 已经是
        # 「启动器 → 游戏」的既有契约（bootstrap.gd --mods=），
        # 加参数要同时改 bootstrap.gd / runner.py / smoke.py / service.py 四处。
        "mode": cfg_mod.extension_mode(cfg),
        "hooks": hooks,
        "mods": result.mods,
        # 各扩展「允许谁读我」的许可汇总。游戏侧按提供方 id 查表。
        "peer_expose": peer_expose,
    }
    result.mods_json = atomic_write_json(rd / "mods.json", payload)

    # 把「哪些包的改动没生效」单独落一份文件。
    #
    # 为什么不能只留在内存 / warnings 里：启动组装跑完，LaunchMods 对象就
    # 丢了，界面此时才刷新 —— 它拿不到这个字段。而玩家看到的「已启用」
    # 和真实生效状态是两回事：
    #
    #   overlay-47.pck 存在         → 界面显示「已启用」
    #   但里面补丁在合成时锚点失配 → 那条改动根本没进 merged.pck
    #
    # 实测反馈就是「显示挂载成功，但游戏里没变化」。写一份文件，界面与
    # 诊断页都能读，才能把「已启用但没生效」这件事故意暴露出来。
    try:
        atomic_write_json(
            rd / "effect-report.json",
            {
                "schema": 1,
                "line": line,
                "failed_patches": result.skipped_patches,
                "warnings": result.warnings,
            },
        )
    except OSError:
        # 写不出来不该让启动失败 —— 它只是诊断材料
        result.warnings.append("effect-report.json 写入失败（不影响启动）")

    return result


#: 插桩层需要感知的脚本后缀——只有脚本会被插桩，场景/JSON 不参与
_SCRIPT_SUFFIXES = (".gd",)


def _overlay_script_paths(overlay: Path) -> list[str]:
    """列出 overlay 里包含的脚本路径（不含 .remap）。失败时返回空表。"""
    try:
        from ..pck.reader import PckReader

        reader = PckReader.open(overlay)
    except Exception:  # noqa: BLE001 - 读不了就当它没有脚本，不影响启动
        return []
    return [
        e.path
        for e in reader.entries
        if e.path.endswith(_SCRIPT_SUFFIXES) and not e.path.endswith(".remap")
    ]


def _peer_decl(item: dict, line: str) -> dict:
    """取一个扩展的 ``peer_access`` / ``peer_expose`` 声明。

    数据来源：装��时写进注册表的 ``lines[line]`` 里（与 ``runtime_hooks``
    同级）。没声明过就返回空表 —— 那是绝大多数扩展的情况。
    """
    ext_id = str(item.get("id", ""))
    lines_meta = item.get("lines")
    entry = None
    if isinstance(lines_meta, dict):
        entry = lines_meta.get(line)
        if not isinstance(entry, dict):
            entry = lines_meta.get(str(item.get("engine_line") or ""))
    if not isinstance(entry, dict):
        return {}
    out: dict = {}
    access = entry.get("peer_access")
    if isinstance(access, dict) and access:
        out["peer_access"] = access
    expose = entry.get("peer_expose")
    if isinstance(expose, dict) and expose:
        out["peer_expose"] = expose
    return out


def _entry_of(item: dict, line: str = "") -> str:
    """从安装目录的清单里取入口路径。"""
    ext_id = str(item.get("id", ""))
    data = _read_manifest(ext_id, line or str(item.get("engine_line", "")))
    if isinstance(data, dict):
        entry = ((data.get("scripts") or {}).get("entry") or "")
        if entry:
            return str(entry)
    return ""



def declared_line_of(item: dict) -> str:
    """这个扩展的清单声明的适配引擎线。

    为什么不能读 ``lines[L].baseline``：安装时 ``service.install`` 会把
    baseline 改写成目标线的 id，于是「装在哪条线」看起来就像「声明哪条线」。
    唯一可信的是包内清单 ``baseline.engine.line``。

    1.4 起只剩内置线；老数据里声明已下线线的包仍会返回它自己的声明值，
    调用方据此判定「不适配」，不会被误当成可用。
    """
    from . import manifest as manifest_mod

    lines = item.get("lines") if isinstance(item.get("lines"), dict) else {}
    candidates = [str(k) for k in lines if k]
    top = str(item.get("engine_line") or "")
    if top and top not in candidates:
        candidates.append(top)
    for line in candidates:
        data = _manifest_of(item, line)
        if not isinstance(data, dict):
            continue
        value = str(((data.get("baseline") or {}).get("engine") or {}).get("line") or "")
        if value:
            return value

    bid = str(item.get("baseline") or "")
    mapped = manifest_mod.BASELINE_ALIAS.get(bid)
    if mapped:
        return mapped[1]
    return cfg_mod.BUILTIN_LINE


