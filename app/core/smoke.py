"""冒烟测试：把「基线 + 覆盖层」真挂起来跑一次 headless。

计划 §4.5 给判据：退出码 0、stdout 含 ``"smoke":"ok"``、实例化节点数 > 3。
本模块把这三条做成可复用的函数，供「扩展安装」「剧情导入」「发布包校验」
「诊断页」共用——四处都要跑同一种冒烟，判据必须只有一份。

设计上刻意不缓存结果：冒烟就是验证"现在这套组合能不能跑"，缓存会让它失去意义。
一次冒烟约 20 秒（GB 级包），可以接受。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import profiles as profiles_mod
from . import runner

#: 判定节点数下限（start_page 实测 5 = 根 + 4 个子节点，留一点余量）
MIN_NODES = 4

#: 启动期脚本错误标记：出现在 stderr 即视为冒烟失败。
#: Godot 在脚本解析/运行时出错时退出码仍可能是 0（或进程被底座吞掉），
#: 但游戏实际不可用。过去「全部五位教官」扩展就是这种——装上时退出码 0、
#: 节点数也够，真启动却整条线崩。这里把 stderr 里的硬性报错也纳入判据。
SCRIPT_ERROR_MARKERS = ("SCRIPT ERROR", "Parse Error", "ERROR: Script")


@dataclass
class SmokeResult:
    ok: bool
    line: str
    seconds: float = 0.0
    returncode: int = 0
    nodes: int = 0
    scene: str = ""
    mounted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    run_dir: str = ""
    packs: list[str] = field(default_factory=list)
    #: mod 装载明细（来自 ModHost 写出的 mod_report.json）
    mods_loaded: list[str] = field(default_factory=list)
    mods_failed: list[dict] = field(default_factory=list)
    #: ModHost 真正挂上的 game.* 信号名（不含 "game." 前缀）
    game_events: list[str] = field(default_factory=list)
    #: 装上了但部分功能未生效的扩展（软警告，不影响 ok）
    mods_warnings: list[str] = field(default_factory=list)
    #: 越界访问记录（严格档下被拦下的读写）
    mods_violations: list[dict] = field(default_factory=list)
    #: 钩子点上「后面的扩展覆盖前面结果」的记录
    mods_hook_writes: list[dict] = field(default_factory=list)
    #: 本次运行的扩展隔离模式（loose / strict）
    mods_mode: str = "loose"
    #: HookRuntime 上真正注册的钩子数
    mods_hooks: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "line": self.line,
            "seconds": round(self.seconds, 1),
            "returncode": self.returncode,
            "nodes": self.nodes,
            "scene": self.scene,
            "mounted": len(self.mounted),
            "errors": self.errors[:5],
            "reasons": self.reasons,
            "run_dir": self.run_dir,
            "mods_loaded": self.mods_loaded,
            "mods_failed": self.mods_failed,
            "game_events": self.game_events,
            "mods_warnings": self.mods_warnings,
            "mods_violations": self.mods_violations,
            "mods_hook_writes": self.mods_hook_writes,
            "mods_mode": self.mods_mode,
            "mods_hooks": self.mods_hooks,
        }

    def summary(self) -> str:
        if self.ok:
            return f"冒烟通过（{self.nodes} 节点，{self.seconds:.0f} 秒）"
        return "冒烟未通过：" + "；".join(self.reasons or self.errors[:2] or ["未知原因"])


def default_baseline(line: str) -> str:
    """该引擎线默认用哪个基线。

    **必须按 line 查，不能写死内置线**：1.4 单线化时这里返回的是
    ``BUILTIN_BASELINE``，4.7 恢复后没跟着改，于是「导入 4.7 本体」之后的
    冒烟跑的其实是 1.45 的基线——在只装了 4.7 的机器上直接报
    ``基线文件缺失：game/mchGame.exe (baseline='jjxf.v1.45')``，
    玩家看到「导入后续校验失败」却不知道跟 4.7 没关系（2026-11-13 实测）。

    唯一真源是 :data:`app.core.config.BASELINE_OF`。
    """
    return cfg_mod.BASELINE_OF.get(str(line), cfg_mod.BUILTIN_BASELINE)


def script_errors_from_stderr(stderr: str, limit: int = 5) -> list[str]:
    """从 Godot stderr 里抽出脚本错误行（SCRIPT ERROR / Parse Error）。"""
    out: list[str] = []
    for raw in (stderr or "").splitlines():
        line = raw.strip()
        if any(m in line for m in SCRIPT_ERROR_MARKERS):
            out.append(line)
            if len(out) >= limit:
                break
    return out


def judge(result: runner.LaunchResult) -> tuple[bool, list[str]]:
    """按计划 §4.5 的三条判据判定，返回 (通过, 不通过原因)。

    额外加入 stderr 脚本错误判据：Godot 脚本解析/运行时报错时退出码可能仍为
    0，但游戏不可用，这类「静默坏包」必须在这里拦下，否则会像
    ``whf.all_instructors`` 那样装上时验过、真启动才崩。
    """
    reasons: list[str] = []
    if result.returncode != 0:
        reasons.append(f"退出码 {result.returncode}")
    # stderr 脚本错误：即便退出码 0 也判失败
    err_text = result.stderr or ""
    for marker in SCRIPT_ERROR_MARKERS:
        idx = err_text.find(marker)
        if idx >= 0:
            # 取该行作为可读原因，避免把整段 stderr 塞进 reasons
            line_start = err_text.rfind("\n", 0, idx) + 1
            line_end = err_text.find("\n", idx)
            snippet = err_text[line_start:line_end if line_end >= 0 else None].strip()
            reasons.append(f"脚本错误：{snippet[:120]}")
            break
    smoke = result.smoke_report or {}
    if smoke.get("smoke") != "ok":
        reasons.append(f"smoke 标记为 {smoke.get('smoke')!r}")
    nodes = int(smoke.get("nodes") or 0)
    if nodes < MIN_NODES:
        reasons.append(f"节点数 {nodes} < {MIN_NODES}")
    mount = result.mount_report or {}
    if not mount.get("ok"):
        reasons.append("挂载报告 ok=false")
    for err in (mount.get("errors") or [])[:2]:
        reasons.append(str(err))
    return (not reasons), reasons


def run(
    line: str = cfg_mod.BUILTIN_LINE,
    *,
    packs: list[dict] | None = None,
    baseline_id: str | None = None,
    profile: str = "smoke",
    main_scene: str = "res://scenes/start_page.tscn",
    timeout: float | None = None,
    cfg: dict | None = None,
    mods_json: str | Path | None = None,
) -> SmokeResult:
    """跑一次冒烟。

    ``packs`` 省略时只挂基线；给了就按顺序附在基线之后（覆盖层在最后）。
    ``profile`` 默认用独立的 ``smoke`` 档案，不碰玩家的真实存档。

    ``mods_json`` 给了就把扩展清单一起传给游戏，**让 ModHost 真的去装载入口**。
    这是"安装期验证 mod 可运行"的关键：只挂覆盖层永远发现不了
    "脚本能装起来但 install() 报错"这类问题。
    """
    cfg = cfg or cfg_mod.load()
    bid = baseline_id or default_baseline(line)
    rd = runner.run_dir_for(f"smoke{line.replace('.', '')}")
    base = runner.baseline_packs(cfg, bid)
    all_packs = [*base, *(packs or [])]

    packset = runner.make_packset(
        rd / "packset.json",
        packset_id=f"{bid}-{line}-smoke",
        line=line,
        packs=all_packs,
        baseline_id=bid,
    )

    profiles_mod.ensure(profile)

    # mods.json 必须放到本次运行目录里：ModHost 会把装载报告写在
    # mods.json 同目录下（mod_report.json），放进 run_dir 才能就近读到，
    # 也避免多次运行互相覆盖。
    probe_mods: Path | None = None
    if mods_json is not None:
        probe_mods = rd / "mods.json"
        try:
            src = Path(mods_json)
            if src.resolve() != probe_mods.resolve():
                probe_mods.write_bytes(src.read_bytes())
        except OSError as exc:
            return SmokeResult(
                ok=False,
                line=line,
                reasons=[f"无法准备 mods.json：{exc}"],
                run_dir=rd.as_posix(),
            )
        # 上一轮的报告必须清掉，否则会读到过期结果
        stale = rd / "mod_report.json"
        try:
            if stale.exists():
                stale.unlink()
        except OSError:
            pass

    result = runner.launch(
        cfg,
        line,
        packset=packset,
        main_scene=main_scene,
        smoke=True,
        headless=True,
        profile=profile,
        run_dir=rd,
        timeout=timeout,
        mods_path=probe_mods,
    )
    ok, reasons = judge(result)
    smoke = result.smoke_report or {}
    mount = result.mount_report or {}
    stderr_errs = script_errors_from_stderr(result.stderr)

    # --- mod 装载结果（安装期校验的核心判据）
    mods_loaded: list[str] = []
    mods_failed: list[dict] = []
    game_events: list[str] = []
    mods_warnings: list[str] = []
    mods_violations: list[dict] = []
    mods_hook_writes: list[dict] = []
    mods_mode = "loose"
    mods_hooks = 0
    if mods_json is not None:
        report = read_mod_report_full(rd / "mod_report.json")
        mods_loaded = report.loaded
        mods_failed = report.failed
        game_events = report.game_events
        mods_warnings = report.warnings
        mods_violations = report.violations
        mods_hook_writes = report.hook_writes
        mods_mode = report.mode
        mods_hooks = report.hooks
        # 报告缺失/解析失败 = 我们无法确认 mod 装起来了 → 必须判不通过。
        # 这正是"安装成功即视为可用"的老毛病：没证据就当通过。
        if report.reasons:
            ok = False
            reasons = [*reasons, *report.reasons]
        if mods_failed:
            ok = False
            reasons = [*reasons, *mod_failed_reasons(mods_failed)]
        # 越界记录不算失败（扩展被拦下了、没崩），但必须进 reasons 之外的
        # 独立字段 —— 界面要单独用红条展示「谁想改谁」。
        # 完全校验档下越界意味着「本该在安装期就被拒绝的扩展混进来了」，
        # 这时判不通过更诚实：说明闸门没拦住。
        if mods_violations and cfg_mod.is_strict(mods_mode):
            ok = False
            reasons = [
                *reasons,
                f"严格校验模式下仍有 {len(mods_violations)} 次越界访问 —— "
                "说明该扩展不应通过安装校验，请重新体检",
            ]

    return SmokeResult(
        ok=ok,
        line=line,
        seconds=result.seconds,
        returncode=result.returncode,
        nodes=int(smoke.get("nodes") or 0),
        scene=str(smoke.get("scene") or main_scene),
        mounted=[str(x) for x in (mount.get("mounted") or [])],
        errors=[str(x) for x in (mount.get("errors") or [])] + stderr_errs,
        reasons=reasons,
        run_dir=rd.as_posix(),
        packs=[str(p.get("path")) for p in all_packs],
        mods_loaded=mods_loaded,
        mods_failed=mods_failed,
        game_events=game_events,
        mods_warnings=mods_warnings,
        mods_violations=mods_violations,
        mods_hook_writes=mods_hook_writes,
        mods_mode=mods_mode,
        mods_hooks=mods_hooks,
    )


@dataclass
class ModReport:
    """``mod_report.json`` 的结构化视图。

    字段对应 ``runtime/_compat/mod_host.gd`` 的 ``_write_report()``。
    ``warnings`` 是「装上了但部分功能未生效」，``violations`` 是
    「越界访问被拦下的每一次」——两者都不算装载失败，但都必须让玩家看见。
    """

    loaded: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    game_events: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    #: 钩子覆盖记账：同一个点上后面的扩展改掉了前面写的结果
    hook_writes: list[dict] = field(default_factory=list)
    mode: str = "loose"
    hooks: int = 0
    ready_state: str = ""
    services: list[str] = field(default_factory=list)
    pending_events: int = 0

    @property
    def is_strict(self) -> bool:
        return cfg_mod.is_strict(self.mode)


def read_mod_report(path: str | Path) -> tuple[list[str], list[dict], list[str], list[str]]:
    """读取 ModHost 写出的 ``mod_report.json``。

    返回 ``(已装载 id 列表, 失败明细, 判不过的原因, 已订阅的游戏信号)``。

    报告缺失本身是一种失败信号：说明 ModHost 根本没跑到写报告那一步
    （比如 entry 阶段就崩了），这时不能当作"没失败"放过。

    需要 ``warnings`` / ``violations`` / ``mode`` 时用 :func:`read_mod_report_full`。
    这个 4 元组签名保留是因为它是既有调用面，改签名会波及测试与界面。
    """
    report = read_mod_report_full(path)
    return report.loaded, report.failed, report.reasons, report.game_events


def read_mod_report_full(path: str | Path) -> ModReport:
    """读取完整装载报告（含隔离模式、越界记录、软警告）。

    为什么要分开：``warnings`` 与 ``violations`` 是 **GDScript 侧早就写进
    报告、Python 侧一直没读** 的字段。结果是「扩展越界被拦下了，但玩家和
    作者都看不到」—— 严格模式等于白开。这里把它们正式接出来。
    """
    p = Path(path)
    if not p.is_file():
        return ModReport(
            loaded=[],
            failed=[],
            reasons=["未生成 mod 装载报告（ModHost 未运行或启动即崩溃）"],
            game_events=[],
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ModReport(
            loaded=[], failed=[], reasons=[f"mod 装载报告无法解析：{exc}"], game_events=[]
        )
    if not isinstance(data, dict):
        return ModReport(loaded=[], failed=[], reasons=["mod 装载报告格式异常"], game_events=[])

    reasons: list[str] = []
    if data.get("ok") is False:
        reasons.append("mod 装载报告标记 ok=false")
    return ModReport(
        loaded=[str(x) for x in (data.get("loaded") or [])],
        failed=[x for x in (data.get("failed") or []) if isinstance(x, dict)],
        reasons=reasons,
        game_events=[str(x) for x in (data.get("game_events") or [])],
        warnings=[str(x) for x in (data.get("warnings") or [])],
        violations=[x for x in (data.get("violations") or []) if isinstance(x, dict)],
        hook_writes=[x for x in (data.get("hook_writes") or []) if isinstance(x, dict)],
        mode=str(data.get("mode") or "loose"),
        hooks=int(data.get("hooks") or 0),
        ready_state=str(data.get("ready_state") or ""),
        services=[str(x) for x in (data.get("services") or [])],
        pending_events=int(data.get("pending_events") or 0),
    )


def violation_lines(violations: list[dict], limit: int = 8) -> list[str]:
    """把越界记录转成人能读的一行行。"""
    out: list[str] = []
    for item in violations[:limit]:
        who = str(item.get("from") or "?")
        target = str(item.get("target") or "?")
        detail = str(item.get("detail") or "")
        out.append(f"{who} → {target}：{detail}" if detail else f"{who} → {target}")
    if len(violations) > limit:
        out.append(f"还有 {len(violations) - limit} 条越界记录")
    return out


def overwrite_lines(writes: list[dict], limit: int = 8) -> list[str]:
    """把钩子覆盖记账转成人能读的一行行。

    ``hook`` 形如 ``scripts/x.gd::character_attributes::after``，
    报告里只留最后两段够用了（前一段是完整路径，太长）。
    """
    out: list[str] = []
    for item in writes[:limit]:
        hook = str(item.get("hook") or "?")
        parts = hook.split("::")
        where = "::".join(parts[-2:]) if len(parts) >= 2 else hook
        by = str(item.get("by") or "（未知扩展）")
        out.append(f"{where}：{by} 覆盖了 result（{item.get('before')} → {item.get('after')}）")
    if len(writes) > limit:
        out.append(f"还有 {len(writes) - limit} 条覆盖记录")
    return out


def mod_failed_reasons(failed: list[dict], limit: int = 5) -> list[str]:
    """把失败明细转成人能读的原因行。"""
    out: list[str] = []
    for item in failed[:limit]:
        mid = str(item.get("id") or "?")
        err = str(item.get("error") or "未知原因")
        out.append(f"{mid} 装载失败：{err}")
    if len(failed) > limit:
        out.append(f"还有 {len(failed) - limit} 个扩展装载失败")
    return out


def run_overlay(
    line: str,
    overlay: str | Path,
    *,
    baseline_id: str | None = None,
    timeout: float | None = None,
    mods_json: str | Path | None = None,
) -> SmokeResult:
    """验证「基线 + 覆盖层 [+ mods.json]」能不能跑。

    扩展安装必须带上 ``mods_json``：只挂覆盖层的话 mod 脚本压根不会被载入，
    "安装成功"就成了空话——这正是改造前「装了却用不了」的根因。
    剧情导入只需要覆盖层，``mods_json`` 传 ``None`` 即可。
    """
    path = Path(overlay)
    if not path.exists():
        return SmokeResult(
            ok=False,
            line=line,
            reasons=[f"覆盖层不存在：{path}"],
        )
    return run(
        line,
        packs=[{"path": path.as_posix(), "offset": 0, "role": "overlay"}],
        baseline_id=baseline_id,
        profile="smoke",
        timeout=timeout,
        mods_json=mods_json,
    )


def run_many(
    line: str,
    overlays: list[str | Path],
    *,
    baseline_id: str | None = None,
    timeout: float | None = None,
) -> SmokeResult:
    """一次验证多个覆盖层叠加（顺序即挂载顺序）。"""
    packs = []
    for item in overlays:
        path = Path(item)
        if not path.exists():
            return SmokeResult(ok=False, line=line, reasons=[f"覆盖层不存在：{path}"])
        packs.append({"path": path.as_posix(), "offset": 0, "role": "overlay"})
    return run(line, packs=packs, baseline_id=baseline_id, profile="smoke", timeout=timeout)


def quick_check(line: str = cfg_mod.BUILTIN_LINE) -> dict:
    """给诊断页用：跑一次裸基线冒烟并返回可序列化结果。"""
    started = time.time()
    result = run(line)
    data = result.as_dict()
    data["elapsed"] = round(time.time() - started, 1)
    return data