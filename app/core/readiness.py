"""启动就绪度：把「现在能不能启动」从界面里抽出来单独判定。

为什么单独一层：
- 界面以前只判断「进程是否在跑」，扩展冲突/基线缺失这类情况一律放过去，
  结果就是点了启动没反应（BUG-1 的表现之一）；
- 命令行与界面应当共用同一套判断，否则会出现「界面说能启动、CLI 说不能」。

评估是**只读**的：不写文件、不改配置、不启动进程，可以放心在每次界面刷新时调用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import config as cfg_mod, paths, runner
from .ext import decisions as decisions_mod
from .ext import enable as ext_enable

#: 引擎线 -> 基线 id。真源在 :data:`config.BASELINE_OF`，这里直接引用，
#: 不再各存一份——1.4 之前是两份各自维护，移除一条线时差点只改了一边。
BASELINE_OF = cfg_mod.BASELINE_OF

#: 需要在启动前弹一次确认的 code。只有「两个包改同一份内容」这类问题
#: 才有意义让用户选「仍然启动」——覆盖层缺失就算确认了也没得选，弹窗只是噪音。
#: 宽松档启动前值得提一句的问题类型。
#:
#: **只收真正的同位置冲突**（两个扩展改同一处内容，运行时按顺序取了后者），
#: 因为这种情况下「相关功能可能不完整」是事实。
#:
#: 不收 ``overlap.*``：那类问题是「同一文件、位置不重叠，会自动合并」，
#: 它是正常情况。旧的三档制里把它当提示，是因为 ``strict_warn`` 那一档
#: 的用途是给作者做灰度体检、要看全量影响面；那个档删掉之后，宽松档成了
#: 玩家的默认档，把正常合并当提示刷给玩家只会淹没真正该看的东西。
CONFIRM_CODES = frozenset({"conflict.location", "conflict.files"})


def baseline_id(line: str) -> str:
    return BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)


@dataclass
class Readiness:
    ok: bool = True
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mods: list[dict] = field(default_factory=list)
    #: 结构化问题（``Problem.as_dict()`` + 覆盖层缺失项），带稳定的 ``code``。
    #: 界面靠 code 区分「要不要弹确认」，不能靠文案——文案随时会改。
    problems: list[dict] = field(default_factory=list)
    #: 开发者模式下将被剔除的扩展（``GateDrop.as_dict()``）。其它模式恒为空。
    dropped: list[dict] = field(default_factory=list)
    #: 被剔除的 id（去重）。``dropped`` 是「一个问题一条」，会有重复。
    dropped_ids: list[str] = field(default_factory=list)
    #: 已启用扩展的全量（含被剔除的），给需要「总数」的界面用。
    all_mods: list[dict] = field(default_factory=list)
    #: 生效视图（``ext.effective.Effective``）：剔除原因、生效集合的唯一来源。
    effective: object | None = None
    #: 不阻断、也不属于警告的说明（例如「这条线暂时没有可用扩展」）。
    infos: list[str] = field(default_factory=list)
    #: 当前校验档：loose（宽松）/ strict（完全）
    mode: str = "loose"
    #: 位置级冲突的处置状态（policy.UnitState.as_dict()）
    units: list[dict] = field(default_factory=list)
    #: 普通模式下按顺序自动取舍的位置
    degraded: list[dict] = field(default_factory=list)
    #: 严格档下尚未处置的位置
    pending: list[dict] = field(default_factory=list)

    def drop_summary(self) -> str:
        if not self.dropped_ids:
            return ""
        return (
            f"开发者模式将剔除 {len(self.dropped_ids)} 个，"
            f"剩 {len(self.mods)} 个可启动"
        )

    def drop_lines(self) -> list[str]:
        """一行一个包：``id —— 撞的目标1；目标2``（不去重会念 26 遍同名字）。"""
        view = self.effective
        plan = getattr(view, "plan", None)
        if plan is not None:
            return plan.drop_lines()
        return [
            f"{'、'.join(str(x) for x in (item.get('ids') or []))} —— {item.get('reason', '')}"
            for item in self.dropped
        ]

    def drop_groups(self) -> list[str]:
        """按目标分组：``目标 → 涉及的包``。"""
        plan = getattr(self.effective, "plan", None)
        return plan.drop_groups() if plan is not None else []

    def reason(self) -> str:
        """给按钮 tooltip / 提示条用的一句话原因。"""
        if self.ok:
            return "可以启动"
        return self.blockers[0] if self.blockers else "暂不可启动"

    def detail(self) -> str:
        lines = list(self.blockers) + [f"（提示）{w}" for w in self.warnings]
        return "\n".join(lines)

    # ------------------------------------------------------------ 确认项

    def confirm_items(self) -> list[dict]:
        """需要用户点头才能继续的项。

        **只有宽松档会返回内容**：把组合层面的影响面列出来让玩家知道
        「这局可能有功能不全」。

        完全档返回空列表 —— 它不靠「确认」放行，而是直接按验证标志与
        检查结果剔除问题包（见 ``gate.plan``）。把它也塞进确认流程等于
        「玩家点一下就能绕过校验」，与完全校验的语义冲突。
        """
        if cfg_mod.is_strict(self.mode):
            return []
        return [
            p
            for p in self.problems
            if str(p.get("code") or "") in CONFIRM_CODES and str(p.get("level")) == "warn"
        ]

    def needs_confirm(self) -> bool:
        return bool(self.confirm_items())

    def needs_disposition(self) -> bool:
        return bool(self.pending)

    def fingerprint(self) -> str:
        """这组确认项的指纹：只看 code + 涉及的包，不看文案。

        为什么要指纹：弹窗是「每次启动都弹」的，但用户装完包之后组合没变
        就没必要反复打扰。用指纹记住「这套组合已经确认过」，比记住整段
        文案稳——文案改一个字就重新弹窗是很烦人的。
        """
        items = self.confirm_items()
        if not items:
            return ""
        payload = "|".join(
            f"{i.get('code')}:{','.join(sorted(str(x) for x in (i.get('ids') or [])))}"
            for i in sorted(
                items,
                key=lambda x: (str(x.get("code")), sorted(str(y) for y in (x.get("ids") or []))),
            )
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def classify(
    problems: list[dict], mode: str = "loose"
) -> tuple[list[str], list[str], list[dict]]:
    """把结构化问题分成 ``(阻断原因, 提示, 需要确认的项)``。

    抽成纯函数是为了能单测：过去这段逻辑埋在 ``evaluate`` 里，只能靠
    「造一个真实项目根 + 真实注册表」才能验证分级对不对。
    """
    blockers: list[str] = []
    warnings: list[str] = []
    confirm: list[dict] = []
    for item in problems or []:
        level = str(item.get("level") or "")
        text = str(item.get("message") or "")
        if level == "error":
            if cfg_mod.is_strict(mode):
                # 完全校验：有 error 级问题不放行，交给 gate 的剔除逻辑
                # 决定是「剔除该包」（带 ids）还是「整组拒绝」（无 ids）。
                blockers.append(f"扩展冲突：{text}")
            elif item.get("ids"):
                # 宽松档：启动时会跳过这些包，不拦整个游戏
                warnings.append(f"将跳过：{text}")
            else:
                blockers.append(f"扩展冲突：{text}")
        elif level == "warn":
            warnings.append(text)
            if str(item.get("code") or "") in CONFIRM_CODES:
                confirm.append(item)
    return blockers, warnings, confirm


def _layered_check(cfg: dict, line: str, result: "Readiness"):
    """有 identity 就做分层校验，并决定要不要阻断启动。

    返回 ``IdentityCheck`` 或 ``None``（没有 identity → 调用方走老的 size 判据）。

    阻断规则按线区分：

    - **4.7**：分层校验不通过 → **拒启动**。剧情层的存在使得「本体大小
      对了」完全不能说明问题；而且代码层变动会让所有扩展锚点失效，
      放进去只会让玩家看到一堆「装了没反应」。
    - **4.6**：不阻断，只警告。4.6 内容冻结、玩家群体里存在自制整合包，
      硬拦会让一批人打不开游戏 —— 那是比「校验不严」更糟的结果。

    无论哪条线，**代码层变动的具体文件**都会被列出来（最多 20 个），
    这比「整包不匹配」有用得多。
    """
    from . import baseline_identity

    try:
        check = baseline_identity.verify(cfg, line)
    except Exception as exc:  # noqa: BLE001 - 校验器自身出错不该拦住启动
        result.warnings.append(f"基线指纹校验未完成（已放行）：{exc}")
        return None

    if not check.layers:
        # 有 identity 但一层都没查到（文件全不在），交给老的健康判定去说
        return None

    if check.ok:
        return check

    strict_line = str(line) == "4.7"
    note = check.message or "基线指纹与登记不符"

    codes = {
        "body": "baseline.body_mismatch",
        "exe": "baseline.exe_mismatch",
        "code": "baseline.code_mismatch",
    }
    for layer in check.failing():
        detail = note
        if layer.name == "code" and (layer.changed or layer.added or layer.removed):
            sample = (layer.changed or layer.added or layer.removed)[:5]
            detail = (
                f"代码层有 {len(layer.changed) + len(layer.added) + len(layer.removed)} "
                f"个文件变动（{'、'.join(sample)}）；扩展的补丁锚点是按旧代码算的，"
                "多半会失配"
            )
        elif layer.reason:
            detail = layer.reason

        if strict_line:
            result.blockers.append(detail)
            result.ok = False
        else:
            result.warnings.append(detail)

        result.problems.append(
            {
                "level": "error" if strict_line else "warn",
                "code": codes.get(layer.name, "baseline.mismatch"),
                "message": detail,
                "ids": [],
            }
        )
    return check


def _check_baseline_verify(cfg: dict, baseline_id: str, result: "Readiness") -> bool:
    """本体校验门禁：导入后没校验通过的一律不放行。

    **为什么要有这一层**（2026-11-16 玩家事故）：旧流程是「先登记 → 再
    可选跑冒烟」，于是导入完成那一秒按钮就亮了。玩家在 3 GB 的包还没
    校验完时点了启动，游戏进程读到半成品 pck，26 秒后 0xC0000005
    崩掉（日志 ``launcher-20261116-113827.log``）。导入与可启动之间必须
    隔一道校验，而不是靠界面的勾选框自律。

    **字段缺失不拦**：1.5.8x 之前的配置没有 ``verify_state``，升级后
    一律判待校验会让所有老玩家一开就「无法启动」。迁移策略见
    :data:`app.core.baseline_ops.VERIFY_PENDING` 的注释。

    返回 ``True`` 表示已被拦下。
    """
    from . import baseline_ops

    state = baseline_ops.verify_state(cfg, baseline_id)
    if state not in baseline_ops.BLOCKING_VERIFY_STATES:
        return False

    label = baseline_ops.VERIFY_STATE_LABELS.get(state, state)
    meta = (cfg.get("baselines") or {}).get(baseline_id) or {}
    note = str(meta.get("verify_note") or "").strip()

    if state == baseline_ops.VERIFY_PENDING:
        how = (
            "游戏本体已登记，但还没完成校验。"
            "点「游戏库 → 校验本体」跑一次冒烟，通过后才能启动。"
        )
    else:
        how = note or (
            "游戏本体校验未通过，请重新导入本体；"
            "也可到「诊断」页重跑校验看具体原因。"
        )

    message = f"本体{label}：{how}"
    result.blockers.append(message)
    result.ok = False
    result.problems.append(
        {
            "level": "error",
            "code": f"baseline.verify_{state}",
            "message": message,
            "ids": [],
        }
    )
    return True


def evaluate(cfg: dict, line: str) -> Readiness:
    """判断 ``line`` 这条引擎线现在能否启动。

    判定顺序即优先级：校验状态 -> 基线 -> 运行时 -> 扩展。
    扩展里只有**硬冲突**算阻断，其余问题（缺清单、覆盖层缺失、插桩跳过）
    一律降级为警告——单个扩展的毛病不该让整个游戏起不来。
    """
    result = Readiness(mode=cfg_mod.extension_mode(cfg))
    bid = baseline_id(line)

    # 0) 本体校验状态。放最前面：没校验过的本体，后面所有判据都不作数。
    _check_baseline_verify(cfg, bid, result)

    # 1) 基线本体
    #
    # 两条线用**不同的判据**，这是有意的：
    #
    # | 线 | 判据 | 不符时的处置 |
    # |---|---|---|
    # | 4.6 | 老流程（size 比对） | 只警告，照样能启动 |
    # | 4.7 | 分层指纹（body/exe/code） | 拒启动 |
    #
    # 为什么 4.7 更严：4.7 有剧情层，玩家换的是 160 MB 的子集而不是整个
    # 2.9 GB 本体，`size` 完全发现不了；而扩展的锚点是按代码层算的，
    # 代码层一变旧扩展就失配。这种情况下放行等于让玩家进游戏后看到
    # 一堆「装了没反应」。
    layered = _layered_check(cfg, line, result)
    if layered is None:
        # 没有 identity（老基线 / 4.6）→ 走原有 size 判据
        state, note = cfg_mod.baseline_health(cfg, bid)
        if state in ("missing", "unregistered"):
            result.blockers.append(f"基线不可用：{note}")
            result.ok = False
        elif state == "size_mismatch":
            # 换了版本不再一律判「不可启动」：文件确实在，能不能跑交给引擎。
            # 但必须让人看见——否则「导入完就提示文件缺失」的老问题会变成静默异常。
            result.warnings.append(note)
            result.problems.append(
                {
                    "level": "warn",
                    "code": "baseline.size_mismatch",
                    "message": note,
                    "ids": [],
                }
            )

    # 2) 运行时（host exe + 底座 pck）
    try:
        exe, pck = runner.host_paths(line)
        if not exe.exists() or not pck.exists():
            result.blockers.append(f"{line} 运行时缺失（{exe.parent}）")
            result.ok = False
    except Exception as exc:  # noqa: BLE001 - 运行时缺失本身就是结论
        result.blockers.append(f"{line} 运行时缺失：{exc}")
        result.ok = False

    # 3) 扩展：冲突阻断，其余只警告；开发者模式先过 gate
    try:
        from .ext import effective as effective_mod
        from .ext import resolver as resolver_mod

        # 唯一真相源：剔除结论与生效集合都从 effective 取，
        # 保证「界面显示 / 就绪度判定 / 实际组装」三处说的是同一件事。
        view = effective_mod.of(cfg, line)
        decision = view.plan
        result.effective = view

        mods = ext_enable.enabled_mods(cfg, line)
        result.all_mods = mods
        if view.dropped:
            result.mods = [m for m in mods if str(m.get("id") or "") in view.keep_set]
        else:
            result.mods = mods

        # 走 enabled_manifests：内部按线取 manifest-<line>.v2.json，
        # 不再出现「界面读 manifest.v2.json、启动读带线清单」两套口径。
        pairs = ext_enable.enabled_manifests(cfg, line)
        if view.dropped:
            # 开发者模式：把未通过的那一组整体剔掉，再拿剩余集合判冲突。
            # 这里必须与 build_for_launch 用同一个 gate 结论，否则会出现
            # 「按钮说能开、点下去被拒」。
            result.dropped = [d.as_dict() for d in decision.dropped]
            result.dropped_ids = list(view.dropped)
            pairs = [(pid, data) for pid, data in pairs if pid in view.keep_set]
            result.warnings.append(view.summary())
            result.problems.append(
                {
                    "level": "warn",
                    "code": "gate.dropped",
                    "message": view.summary(),
                    "ids": list(view.dropped),
                }
            )
        if not decision.ok:
            result.blockers.extend(f"扩展冲突：{msg}" for msg in decision.errors)
            result.ok = False

        resolution = resolver_mod.resolve(
            pairs,
            compose=cfg_mod.extension_compose(cfg),
            mode=cfg_mod.extension_mode(cfg),
            line=line,
            store=decisions_mod.Store.load(paths.CONFIG_DIR / "decisions.json"),
        )
        problems = [p.as_dict() for p in resolution.problems]
        blockers, warnings, _confirm = classify(problems, cfg_mod.extension_mode(cfg))
        result.blockers.extend(blockers)
        result.warnings.extend(warnings)
        result.problems.extend(problems)
        plan = getattr(resolution, "plan", None)
        if plan is not None:
            result.units = [s.as_dict() for s in getattr(plan, "states", [])]
            result.degraded = [s.as_dict() for s in getattr(plan, "degraded", [])]
            result.pending = [s.as_dict() for s in getattr(plan, "pending", [])]
        if blockers:
            result.ok = False

        # 覆盖层缺失：装载失败的典型症状，启动时会被跳过，这里提前说清。
        # 它是 warn 但**不是**确认项——code 不在 CONFIRM_CODES 里，界面就不会弹窗。
        # 遍历的是已剔除被剔除项的 result.mods，不必再单独判 keep。
        for item in result.mods:
            raw = str(item.get("overlay") or "")
            overlay = cfg_mod.resolve_path(raw)
            if raw and (overlay is None or not overlay.exists()):
                message = f"{item.get('id')} 覆盖层缺失，启动时将跳过"
                result.warnings.append(message)
                result.problems.append(
                    {
                        "level": "warn",
                        "code": "overlay.missing",
                        "message": message,
                        "ids": [str(item.get("id") or "")],
                    }
                )
    except Exception as exc:  # noqa: BLE001 - 检查本身出错不该阻断启动判断
        message = f"扩展检查未完成：{exc}"
        result.warnings.append(message)
        result.problems.append(
            {"level": "warn", "code": "ext.check_failed", "message": message, "ids": []}
        )

    return result
