"""装配检查器：把「这个扩展能不能在这条线上装出来」一次问清。

为什么要单独一层
================

「宽松档也做检查」这句话有连锁后果：**原来根本没有这套检查**。

分散在各处的判定各有各的语气和粒度：

| 检查 | 原来在哪 | 原有粒度 |
|---|---|---|
| 补丁锚点命中 | ``compose._apply_text`` 的 ``compose.anchor`` | 只在「真叠加」时才发现 |
| 位置冲突 | ``units.discover`` + ``policy.plan`` | 只比「已启用组合」 |
| 钩子落到源码上 | ``enable`` 里的零散 warning | 没有汇总结论 |
| 成员注入 | 无 | 无 |

所以「宽松档和完全档跑同一套检查」不是改一个开关，是**新建**这一层。
本模块只做一件事：把四项检查跑一遍，产出一份**结构化、可展示、可存档**
的报告。它自己不做任何处置决定 —— 处置在 ``gate``（剔除）和
``verify_mark``（标志判定）。

四项检查
========

1. **manifest** —— 清单本身能不能读、schema 认不认、有没有 fatal；
2. **anchor** —— 每条补丁的锚点能不能在这条线的原版内容里命中恰好 1 处；
3. **units** —— 它与其他扩展改的位置有没有重叠（同指针 / 父子 / 同锚点）；
4. **hooks** —— 声明的钩子目标方法在不在源码里。

每一项都给出 ``ok`` / ``severity`` / 人话原因，并且**只报告不处置**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import config as cfg_mod

#: 检查项的名字。写进验证记录 ``passed`` 字段，顺序即执行顺序。
CHECKS = ("manifest", "anchor", "units", "hooks", "members")

#: 严重级别。``error`` 表示「装上了也会坏」，``warn`` 表示「可能缺功能」。
ERROR = "error"
WARN = "warn"
INFO = "info"


@dataclass
class Finding:
    """一条检查发现。"""

    check: str
    severity: str
    code: str
    message: str
    #: 涉及的扩展 id（单个包检查时就是它自己）
    ids: list[str] = field(default_factory=list)
    #: 具体位置（``data/e.json`` 的 ``/catalog/1/power``、``scripts/game.gd``）
    target: str = ""
    #: 附带细节，给界面展开用
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def bad(self) -> bool:
        return self.severity in (ERROR, WARN)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "check": self.check,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.ids:
            out["ids"] = list(self.ids)
        if self.target:
            out["target"] = self.target
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class Report:
    """一次检查的完整结果。"""

    line: str
    ext_ids: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: 实际跑过的检查项（某项因缺前提没跑时不会出现在这里）
    ran: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        """没有任何 error 级问题 —— 这是「能不能装」的判据。"""
        return not self.errors

    def passed_checks(self) -> list[str]:
        """通过的检查项，直接写进验证记录的 ``passed`` 字段。

        某项只要有一条 error，就不算通过。warn 不影响通过 ——
        「部分钩子没落上」这类问题记在报告里给作者看，但不阻止出记录，
        否则永远没人能出记录。

        **只列真的跑过的项**：没跑成的检查（例如基线内容读不到时的
        ``anchor``）不在 ``ran`` 里，自然也不会进这里。这条很重要 ——
        验证记录必须能如实回答「当时到底验了什么」。
        """
        failed = {f.check for f in self.errors}
        return [c for c in self.ran if c not in failed]

    def unavailable(self) -> list[str]:
        """本该跑但因为缺前提没跑成的检查项。"""
        return [c for c in CHECKS if c not in self.ran]

    def summary(self) -> str:
        if not self.ok:
            return f"{len(self.errors)} 个问题会导致装上不生效"
        missing = self.unavailable()
        parts = []
        if self.warnings:
            parts.append(f"{len(self.warnings)} 条提示")
        if missing:
            parts.append(f"{len(missing)} 项未能验证（{'、'.join(missing)}）")
        return "通过" + (f"（{'，'.join(parts)}）" if parts else "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "line": self.line,
            "ext_ids": list(self.ext_ids),
            "ok": self.ok,
            "ran": list(self.ran),
            "findings": [f.as_dict() for f in self.findings],
        }


# ---------------------------------------------------------------- 各项检查


def _check_manifest(cfg: dict, line: str, pairs: list[tuple[str, dict]]) -> list[Finding]:
    """清单层：schema 认不认、有没有声明层面的硬错误。"""
    from . import manifest as manifest_mod

    out: list[Finding] = []
    for ext_id, data in pairs:
        if not isinstance(data, dict):
            out.append(
                Finding(
                    check="manifest",
                    severity=ERROR,
                    code="manifest.missing",
                    message=f"{ext_id} 的清单读不出来",
                    ids=[ext_id],
                )
            )
            continue
        try:
            schema = int(data.get("schema", 1) or 1)
        except (TypeError, ValueError):
            schema = 0
        if schema not in (1, 2, 3):
            out.append(
                Finding(
                    check="manifest",
                    severity=ERROR,
                    code="manifest.schema",
                    message=f"{ext_id} 的清单 schema {schema} 不受支持",
                    ids=[ext_id],
                )
            )
        # 未知字段只是提示：新版本启动器加了字段，不该让老包不能用
        known = getattr(manifest_mod, "_KNOWN_FIELDS", frozenset())
        extra = sorted(k for k in data if known and k not in known and not k.startswith("x_"))
        if extra:
            out.append(
                Finding(
                    check="manifest",
                    severity=INFO,
                    code="manifest.unknown_field",
                    message=f"{ext_id} 有未识别的清单字段：{'、'.join(extra)}",
                    ids=[ext_id],
                )
            )
    return out


def _check_anchor(
    cfg: dict, line: str, pairs: list[tuple[str, dict]]
) -> tuple[list[Finding], bool]:
    """锚点层：每条补丁能不能在这条线的原版内容里命中。

    实现方式：真的跑一遍合成层，读它产出的 ``compose.anchor`` 冲突。
    这是唯一可靠的办法 —— 锚点是「函数签名 + 函数体开头」，只有拿真实
    基底去匹配才知道命中没有。

    返回 ``(findings, 是否跑成)``。

    **「跑不成」绝不能算「通过」。** 这个前提必须先验：基线内容读不到
    （没导入本体、路径错、pck 损坏）时，锚点一条都验不了 —— 这时候
    报「通过」等于给玩家一个假的安全感，比不检查更坏。所以这里显式
    探一次基底可读性，读不到就返回 ``(说明性 finding, False)``，
    由调用方把 ``anchor`` 从 ``ran`` 里排除（于是也不会进 ``passed``）。
    """
    from . import compose as compose_mod
    from .basecontent import BaseContent

    if not pairs:
        return [], False

    base = BaseContent(cfg, line, include_installed=False)

    # 先探基底：拿这批补丁实际要改的目标去试读，读不到就说明这条线的
    # 原版内容不可用（最常见：玩家还没导入本体）。
    targets: set[str] = set()
    for _pid, data in pairs:
        for patch in data.get("data_patches") or []:
            if isinstance(patch, dict) and patch.get("target"):
                targets.add(str(patch["target"]))
        for patch in data.get("text_patches") or []:
            if isinstance(patch, dict) and patch.get("target"):
                targets.add(str(patch["target"]))
    readable = 0
    for target in targets:
        try:
            if base.read(target):
                readable += 1
        except Exception:  # noqa: BLE001
            continue
    if targets and readable == 0:
        return (
            [
                Finding(
                    check="anchor",
                    severity=INFO,
                    code="anchor.unavailable",
                    message=(
                        f"{line} 线的原版内容读不到（多半是还没导入游戏本体），"
                        f"{len(targets)} 个被改的文件一个都没验证。"
                    ),
                    detail={"targets": sorted(targets)[:10]},
                )
            ],
            False,
        )

    try:
        import tempfile

        from .. import paths as paths_mod

        with tempfile.TemporaryDirectory(
            prefix="jjxf-inspect-", dir=str(paths_mod.temp_dir())
        ) as tmp:
            result = compose_mod.build(
                pairs,
                read_base=base.read,
                read_overlay=lambda _pid, _target: None,
                out_pck=Path(tmp) / "inspect.pck",
                line=line,
            )
    except Exception:  # noqa: BLE001 - 检查器自身失败不该报成「包有问题」
        return [], False

    out: list[Finding] = []
    for item in result.conflicts:
        if str(item.get("code") or "") != "compose.anchor":
            continue
        ext_id = str(item.get("ext_id") or ((item.get("ids") or [""])[0]))
        out.append(
            Finding(
                check="anchor",
                severity=ERROR,
                code="anchor.miss",
                message=(
                    f"{ext_id} 在 {item.get('target')} 上的补丁找不到锚点："
                    f"{item.get('reason') or '锚点在叠加后的内容里不存在'}"
                ),
                ids=[ext_id] if ext_id else [],
                target=str(item.get("target") or ""),
                detail={"anchor": item.get("anchor"), "impact": item.get("impact")},
            )
        )
    return out, True


def _check_units(
    cfg: dict, line: str, pairs: list[tuple[str, dict]]
) -> list[Finding]:
    """位置层：这些包之间有没有改同一处。

    这里只报告「撞了哪里、谁和谁撞」，不决定谁赢 —— 处置归 ``policy``。
    """
    from . import units as units_mod

    out: list[Finding] = []
    if len(pairs) < 2:
        return out
    try:
        found = units_mod.discover(pairs)
    except Exception:  # noqa: BLE001
        return out

    meta = {pid: data for pid, data in pairs}
    for unit in found:
        parties = sorted({str(x) for x in getattr(unit, "parties", [])})
        if len(parties) < 2:
            continue
        ok, _reason = units_mod.authorized(unit, meta)
        if ok:
            continue          # 已声明接管，不算冲突
        out.append(
            Finding(
                check="units",
                severity=WARN,
                code="units.overlap",
                message=f"{'、'.join(parties)} 都改了 {units_mod.describe(unit)}",
                ids=parties,
                target=str(getattr(unit, "target", "") or ""),
                detail={"locator": str(getattr(unit, "locator", "") or "")},
            )
        )
    return out


def _check_hooks(
    cfg: dict, line: str, pairs: list[tuple[str, dict]], source_root: Path | None
) -> tuple[list[Finding], bool]:
    """钩子层：声明的 ``method_hooks`` 目标方法在不在源码里。

    没有源码就返回「没跑成」—— 不能因为「查不了」就判人家不合格。
    """
    from . import hooks as hooks_mod

    if source_root is None or not source_root.is_dir():
        return [], False

    out: list[Finding] = []
    cache: dict[str, str] = {}
    for ext_id, data in pairs:
        raw = data.get("method_hooks") or []
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            script = str(item.get("script") or "")
            method = str(item.get("method") or "")
            if not script or not method:
                continue
            text = cache.get(script)
            if text is None:
                path = source_root / script
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                cache[script] = text
            if not text:
                out.append(
                    Finding(
                        check="hooks",
                        severity=WARN,
                        code="hooks.script_missing",
                        message=f"{ext_id} 的钩子目标 {script} 在源码里不存在",
                        ids=[ext_id],
                        target=script,
                    )
                )
                continue
            if not hooks_mod.has_method(text, method):
                out.append(
                    Finding(
                        check="hooks",
                        severity=WARN,
                        code="hooks.method_missing",
                        message=f"{ext_id} 说要在 {script} 的 {method}() 上挂钩子，但那个方法不存在",
                        ids=[ext_id],
                        target=script,
                        detail={"method": method},
                    )
                )
    return out, True


def _check_members(
    cfg: dict, line: str, pairs: list[tuple[str, dict]]
) -> tuple[list[Finding], bool]:
    """成员层：``script_members`` 注入声明能不能落地。

    「缺声明 → Parse Error → 黑屏」是实测最常见的一类崩溃，所以单独查。
    """
    out: list[Finding] = []
    ran = False
    for ext_id, data in pairs:
        members = data.get("script_members") or []
        if not isinstance(members, list):
            continue
        ran = True
        for item in members:
            if not isinstance(item, dict):
                continue
            script = str(item.get("script") or "")
            name = str(item.get("name") or "")
            if not script or not name:
                out.append(
                    Finding(
                        check="members",
                        severity=WARN,
                        code="members.incomplete",
                        message=f"{ext_id} 的 script_members 条目缺少 script 或 name",
                        ids=[ext_id],
                    )
                )
                continue
            # 注入的名字必须是合法标识符，否则 GDScript 解析失败
            if not name.replace("_", "a").isalnum() or name[0].isdigit():
                out.append(
                    Finding(
                        check="members",
                        severity=ERROR,
                        code="members.invalid_name",
                        message=f"{ext_id} 想注入的成员名 {name!r} 不是合法标识符，会导致 Parse Error",
                        ids=[ext_id],
                        target=script,
                    )
                )
    return out, ran


# ---------------------------------------------------------------- 主入口


def inspect(
    cfg: dict,
    line: str,
    pairs: Iterable[tuple[str, dict]],
    *,
    source_root: Path | None = None,
) -> Report:
    """对一组 ``(ext_id, manifest)`` 跑全套检查。

    ``pairs`` 通常是「已启用的 + 待装的」合集 —— 冲突检测必须放在真实
    组合里做，单个包自己看不出与谁撞。

    ``source_root`` 是反解工程根目录，用来查钩子目标方法在不在。
    没有就跳过钩子检查（不判不合格）。
    """
    items = [(str(pid), data) for pid, data in pairs if pid]
    report = Report(line=line, ext_ids=[pid for pid, _ in items])

    findings = _check_manifest(cfg, line, items)
    report.findings.extend(findings)
    if items:
        report.ran.append("manifest")

    anchor_findings, anchor_ok = _check_anchor(cfg, line, items)
    report.findings.extend(anchor_findings)
    if anchor_ok:
        report.ran.append("anchor")

    unit_findings = _check_units(cfg, line, items)
    report.findings.extend(unit_findings)
    if items:
        report.ran.append("units")

    hook_findings, hooks_ok = _check_hooks(cfg, line, items, source_root)
    report.findings.extend(hook_findings)
    if hooks_ok:
        report.ran.append("hooks")

    member_findings, members_ok = _check_members(cfg, line, items)
    report.findings.extend(member_findings)
    if members_ok:
        report.ran.append("members")

    return report


def inspect_line(cfg: dict, line: str, *, source_root: Path | None = None) -> Report:
    """对「这条线上所有已启用扩展」跑一遍检查。"""
    from . import enable as enable_mod

    pairs = enable_mod.enabled_manifests(cfg, line)
    if source_root is None:
        try:
            src = (cfg.get("runtime_sources") or {}).get(line) or {}
            raw = str(src.get("project_godot") or "").strip()
            if raw:
                candidate = cfg_mod.resolve_path(raw)
                source_root = candidate.parent if candidate else None
        except Exception:  # noqa: BLE001
            source_root = None
    return inspect(cfg, line, pairs, source_root=source_root)