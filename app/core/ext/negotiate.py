"""自定义线能力协商：按 ``line.json`` 的 capabilities 决定哪些扩展能挂。

为什么不复用 ``enable.build_for_launch``
----------------------------------------
那条链里 ``enabled_mods`` 按 ``lines[line]`` 裁剪（自定义线永远取不到东西）、
``resolver`` / ``readiness`` / ``mods.json`` 按内置引擎线硬编码、插桩层依赖
``baseline/sources-4.7.zip`` 正典源码。把 ``custom:*`` 硬塞进去只会得到
「扩展装错线」。这里独立做一层**装载期协商**，只交付三样东西：

- 能挂的 overlay 条目（追加到自定义线的 packset）；
- 给底座 ModHost 的 ``mods.json``；
- 一份说清「谁挂了、谁没挂、为什么」的 ``negotiation.json``。

准入边界（不可放宽）
--------------------
- 只有 ``pck`` 形态 + ``access_tier == full`` 才协商。``exe`` 形态自带
  运行时，启动器注入不了 ``_compat`` / ``--packset``，永远只能仅启动。
- 底座引擎**自动**提供 ``overlay.mount`` / ``modhost`` / ``mods.json``。
  其余能力必须由创作者在 line.json 声明并通过 ``line_desc`` 一致性校验。
- ``method_hooks`` 需要插桩层，而自定义线没有正典源码可插桩。**一律跳过
  并写明原因**，绝不静默挂载——战力 mod 在 4.7「装载全通、数字没人消费」
  就是没把这件事说清楚。

能力命名
--------
``overlay.mount`` / ``modhost`` / ``mods.json`` 是底座内建；
``method:<target>:<symbol>``、``signal:<target>:<symbol>``、
``instrument:<target>:<symbol>`` 与 line.json 的 capabilities 对应；
创作者声明的 ``id``（如 ``Game.combat_power``）也可直接作为能力名被 mod 引用。
mod 侧可用清单里的 ``requires`` 显式声明需求，缺省按清单内容推断。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .. import config as cfg_mod
from .. import line_desc
from ..fsutil import atomic_write_json
from . import enable as enable_mod

#: 底座引擎（pck 形态）自己提供的能力，不需要创作者声明
BUILTIN_CAPABILITIES = frozenset({"overlay.mount", "modhost", "mods.json"})

_MOUNT = "mount"
_SKIP = "skip"


@dataclass
class ModDecision:
    id: str
    action: str
    reason: str = ""
    needs: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    entry: str = ""
    overlay: str = ""


@dataclass
class Negotiation:
    """一次协商的完整结果，可直接落盘给界面与排障用。"""

    line_id: str
    tier: str = ""
    kind: str = ""
    engine_line: str = ""
    mode: str = "loose"
    baseline_ready: bool = False
    available: list[str] = field(default_factory=list)
    decisions: list[ModDecision] = field(default_factory=list)
    packs: list[dict] = field(default_factory=list)
    mods: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def mounted_ids(self) -> list[str]:
        return [d.id for d in self.decisions if d.action == _MOUNT]

    @property
    def skipped_ids(self) -> list[str]:
        return [d.id for d in self.decisions if d.action == _SKIP]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["mounted"] = self.mounted_ids
        payload["skipped"] = self.skipped_ids
        return payload

    def write_artifacts(self, out_dir: str | Path) -> tuple[Path | None, Path]:
        """写 mods.json（给底座）与 negotiation.json（给界面/排障）。

        ``mods.json`` 只有在确实有 mod 时才生成；没有也写一份空的，避免
        上一次启动留下的旧文件被底座读走，挂上不属于这条线的扩展。
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        report_path = atomic_write_json(out / "negotiation.json", self.as_dict())
        mods_path = atomic_write_json(
            out / "mods.json",
            {
                "schema": 2,
                "line": self.line_id,
                "mode": self.mode,
                "hooks": [],  # 自定义线不生成插桩层
                "mods": self.mods,
                "peer_expose": {},
            },
        )
        return mods_path, report_path


def _empty(line_id: str, reason: str, **kw) -> Negotiation:
    return Negotiation(
        line_id=line_id, warnings=[reason],
        tier=str(kw.get("tier") or ""), kind=str(kw.get("kind") or ""),
        engine_line=str(kw.get("engine_line") or ""),
    )


def needs_of(manifest: dict) -> list[str]:
    """推断一个扩展需要哪些能力；显式 ``requires`` 优先。

    缺省推断只覆盖「挂上去确实会用到」的东西，不猜补丁锚点——
    补丁在安装期已经烘进 overlay pck，运行期不再需要源码。
    """
    explicit = manifest.get("requires")
    if isinstance(explicit, list) and explicit:
        return [str(x) for x in explicit if str(x).strip()]

    needs = ["overlay.mount"]
    scripts = manifest.get("scripts") or {}
    if isinstance(scripts, dict) and str(scripts.get("entry") or ""):
        needs += ["modhost", "mods.json"]
    for hook in manifest.get("method_hooks") or []:
        if not isinstance(hook, dict):
            continue
        target = str(hook.get("target") or "").replace("res://", "")
        method = str(hook.get("method") or "")
        needs.append(f"instrument:{target}:{method}")
    return needs


def declared_capabilities(desc: dict) -> set[str]:
    """把 line.json 的 capabilities 展开成可匹配的标识集合。"""
    out: set[str] = set()
    for cap in desc.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        cid = str(cap.get("id") or "")
        if cid:
            out.add(cid)
        kind = str(cap.get("kind") or "")
        target = str(cap.get("target") or "").replace("res://", "")
        symbol = str(cap.get("symbol") or "")
        if kind == "method" and target and symbol:
            out.add(f"method:{target}:{symbol}")
            out.add(f"{target}:{symbol}")
        elif kind == "signal" and target and symbol:
            out.add(f"signal:{target}:{symbol}")
        if kind == "resource":
            out.add("overlay.mount")
    return out


def _pick_for_engine(item: dict, engine: str) -> dict | None:
    """按改版所属引擎线取该扩展的线内字段（overlay / manifest 等）。"""
    lines = item.get("lines")
    if isinstance(lines, dict) and lines:
        picked = lines.get(engine)
        if not isinstance(picked, dict):
            return None
        merged = dict(item)
        merged.update(picked)
        merged["engine_line"] = engine
        return merged
    if str(item.get("engine_line") or "") == engine:
        return dict(item)
    return None


def _scan_overlay_protocols(overlay: Path) -> list[str]:
    """扫 overlay pck 里的文本条目，找 netguard 定义的联机协议痕迹。

    pck 不是 zip，``scan_package`` 用不上；这里按条目逐个把文本喂给
    ``netguard.scan_bytes``，口径与安装期闸门完全一致。
    """
    from ..pck.reader import PckReader
    from . import netguard as netguard_mod

    hits: list[str] = []
    try:
        reader = PckReader.open(overlay)
        entries = list(reader.iter_entries())
        for entry in entries:
            path = str(getattr(entry, "path", "") or "").replace("res://", "")
            if not path.lower().endswith(netguard_mod.TEXT_SUFFIXES):
                continue
            try:
                data = reader.read_bytes(path)
            except Exception:  # noqa: BLE001 - 单条目读失败不该拦别的
                continue
            report = netguard_mod.scan_bytes(data, path)
            for hit in report.blocks:
                hits.append(f"{path}: {hit.reason}")
                break
    except Exception:  # noqa: BLE001 - 打不开就交回「无命中」，由别的环节报错
        return []
    return hits


def negotiate(cfg: dict, line) -> Negotiation:
    """对一条自定义线做装载期能力协商。任何异常都降级、不抛、不阻断启动。"""
    line_id = str(getattr(line, "id", "") or "")
    tier = str(getattr(line, "access_tier", "") or "")
    kind = str(getattr(line, "kind", "") or "")
    engine = str(getattr(line, "identity_engine_line", "") or getattr(line, "engine_line", "") or "")

    if tier != line_desc.TIER_FULL:
        return _empty(line_id, "未提供通过一致性校验的 line.json：仅启动，不协商扩展",
                      tier=tier, kind=kind, engine_line=engine)
    if kind != "pck":
        return _empty(line_id, "exe 形态自带运行时，启动器无法注入底座：仅启动，不协商扩展",
                      tier=tier, kind=kind, engine_line=engine)

    game_file = Path(str(getattr(line, "path", "") or ""))
    desc_path = line_desc.desc_path_for(game_file)
    try:
        desc = line_desc.load_desc(desc_path)
        verdict = SimpleNamespace(
            identity_hash=str(getattr(line, "identity_hash", "") or ""),
            engine_line=engine,
            baseline=str(getattr(line, "identity_baseline", "") or ""),
        )
        check = line_desc.validate_desc(desc, verdict, desc_path.parent)
    except (line_desc.LineDescError, OSError, ValueError) as exc:
        return _empty(line_id, f"line.json 不可用，已降级为仅启动：{exc}",
                      tier=tier, kind=kind, engine_line=engine)
    if not check.ok:
        return _empty(line_id, "line.json 一致性校验未通过：" + "；".join(check.errors[:2]),
                      tier=tier, kind=kind, engine_line=engine)

    available = set(BUILTIN_CAPABILITIES) | declared_capabilities(desc)
    result = Negotiation(
        line_id=line_id, tier=tier, kind=kind, engine_line=engine,
        mode=cfg_mod.extension_mode(cfg), available=sorted(available),
    )

    # 创作者是否已按《自定义基线导入准则》交了可验收的反解源码。
    # 这是插桩层的前置条件，但**不等于**插桩层已经能生成——见下面 hooks 分支。
    from .. import custom_baseline as cb_mod

    baseline_status = cb_mod.status(line_id, str(getattr(line, "identity_hash", "") or ""))
    result.baseline_ready = bool(baseline_status.get("ready"))
    if result.baseline_ready:
        result.warnings.append(
            f"已导入可插桩基线（{baseline_status.get('scripts')} 个脚本）"
        )

    from . import netguard as netguard_mod

    try:
        items = enable_mod.ExtensionService(cfg).installed()
    except Exception as exc:  # noqa: BLE001
        result.warnings.append(f"扩展清单读不出来，本次不挂载：{exc}")
        return result

    for item in items:
        ext_id = str(item.get("id") or "")
        if not ext_id or item.get("enabled") is False:
            continue
        picked = _pick_for_engine(item, engine)
        if picked is None:
            result.decisions.append(ModDecision(
                ext_id, _SKIP, f"这个扩展没有 {engine} 线的安装记录",
            ))
            continue

        manifest = enable_mod._read_manifest(ext_id, engine)
        if not isinstance(manifest, dict):
            result.decisions.append(ModDecision(ext_id, _SKIP, f"缺少 {engine} 线的扩展清单"))
            continue

        overlay_raw = str(picked.get("overlay") or "")
        overlay = cfg_mod.resolve_path(overlay_raw) if overlay_raw else None
        if overlay is None or not overlay.exists():
            result.decisions.append(ModDecision(ext_id, _SKIP, "覆盖层文件缺失", overlay=overlay_raw))
            continue

        # 联机禁入：自定义线不能成为绕过 R2 的口子。安装期闸门只管扩展包，
        # 这里在装载期再过一遍「清单声明 + overlay 正文」两道。
        net = netguard_mod.scan_manifest(manifest)
        if net.blocked:
            result.decisions.append(ModDecision(
                ext_id, _SKIP, net.message(), overlay=overlay.as_posix(),
            ))
            continue
        proto = _scan_overlay_protocols(overlay)
        if proto:
            result.decisions.append(ModDecision(
                ext_id, _SKIP,
                "覆盖层含联机协议痕迹：" + "；".join(proto[:2]),
                overlay=overlay.as_posix(),
            ))
            continue

        needs = needs_of(manifest)
        hooks = [n for n in needs if n.startswith("instrument:")]
        if hooks:
            # 没有插桩层就别挂：挂上去也只是「装载成功、钩子没人调用」。
            # 基线已导入时把状态说清，让创作者知道材料已经验收、差的只是生成器。
            if result.baseline_ready:
                reason = ("基线已导入且校验有效，但插桩层生成器尚未接入自定义线；"
                          "为避免静默失效，本次跳过")
            else:
                reason = ("含方法钩子，自定义线无正典源码、不生成插桩层；"
                          "请按《自定义基线导入准则》导入反解源码")
            result.decisions.append(ModDecision(
                ext_id, _SKIP, reason,
                needs=needs, missing=hooks, overlay=overlay.as_posix(),
            ))
            continue
        missing = [n for n in needs if n not in available]
        if missing:
            result.decisions.append(ModDecision(
                ext_id, _SKIP, "改版未提供所需能力：" + "、".join(missing),
                needs=needs, missing=missing, overlay=overlay.as_posix(),
            ))
            continue

        scripts = manifest.get("scripts") or {}
        entry = str(scripts.get("entry") or "") if isinstance(scripts, dict) else ""
        result.decisions.append(ModDecision(
            ext_id, _MOUNT, "能力齐备", needs=needs, entry=entry, overlay=overlay.as_posix(),
        ))
        result.packs.append({"path": overlay.as_posix(), "offset": 0, "role": "mod", "id": ext_id})
        if entry:
            result.mods.append({"id": ext_id, "entry": entry})

    result.warnings.append(
        f"能力协商：挂载 {len(result.mounted_ids)} 个、跳过 {len(result.skipped_ids)} 个"
        f"（可用能力 {len(available)} 项）"
    )
    for d in result.decisions:
        if d.action == _SKIP:
            result.warnings.append(f"跳过 {d.id}：{d.reason}")
    return result
