"""从扩展源码反查「真实注册的钩子相位」。

为什么需要它
------------
插桩层为了保证「运行期注册一定命中」，对缺 ``runtime_hooks`` 的老记录采用
``before`` + ``after`` 双插。这个策略是对的（见 :func:`app.core.ext.enable._hooks_of_item`），
但它让**钩子撞车检查**失准：

* 插桩表里一个只声明 ``after`` 的包，会同时出现 ``before``；
* 别人的 ``before`` 回调于是和它"撞车"——其实那个包根本没注册 before。

2026-11-05 实测：17 个包里 15 个缺 ``runtime_hooks``，玩家版因此多报 2 条假撞车。

另一个方向的坑（同一天踩到）
----------------------------
``jjxf.floating_cannons`` 的清单只写了 ``after``，源码里却注册了
``before`` + ``after``。若按清单插桩，``before`` 点不存在 →
``add_method_hook`` 返回 false → 它 ``install()`` 里 fail-fast 直接 return false →
**后面 16 个钩子全部丢失**，浮游炮卡在原地不动。

所以两件事必须分开：

===========  ==========================================
用途          数据源
===========  ==========================================
插桩          双插（保证命中，功能优先）
撞车检查      真实相位（避免虚报）
===========  ==========================================

本模块负责后者：从 ``builds/_install_<id>_<line>/src`` 的源码里解析
``add_method_hook(script, method, phase, callback, ...)``，拿到**作者真正注册
的相位**。拿不到时返回空，由调用方决定退回什么口径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: `脚本路径` 参数既可能是字面量，也可能是常量名
_CONST_STR_RE = re.compile(
    r'^(?:const|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*\w+\s*)?:?=\s*"([^"]*)"',
    re.M,
)

#: `api.add_method_hook("scripts/x.gd", "method", "phase", _callback, prio)`
_ADD_HOOK_RE = re.compile(
    r"add_method_hook\s*\(\s*"
    r"(?P<script>\"[^\"]*\"|[A-Za-z_][\w.]*)\s*,\s*"
    r"(?P<method>\"[^\"]*\"|[A-Za-z_][\w.]*)\s*,\s*"
    r"(?P<phase>\"[^\"]*\"|[A-Za-z_][\w.]*)\s*,\s*"
    r"(?P<cb>[A-Za-z_][\w.]*)"
    # 第五个参数是优先级，可省。只认字面量：变量形式留给调用方判「未知」。
    r"(?:\s*,\s*(?P<prio>\d+))?"
)


@dataclass
class HookSite:
    """一个真实注册的钩子。"""

    script: str
    method: str
    phase: str
    callback: str = ""
    source: str = ""
    #: 注册时给的优先级；表驱动/变量形式认不出就是 None。
    #: 顺序判定要用它：小数字先跑（hook_runtime.register 升序排序）。
    priority: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.script, self.method, self.phase)


@dataclass
class ScanResult:
    sites: list[HookSite] = field(default_factory=list)
    #: 认不出的调用（参数是变量且常量表里查不到等）
    unresolved: int = 0

    def phases_of(self, script: str, method: str) -> set[str]:
        return {
            s.phase for s in self.sites if s.script == _norm(script) and s.method == method
        }


def _norm(path: str) -> str:
    return str(path or "").replace("res://", "").strip()


def _strip_comments(text: str) -> str:
    """去掉行尾注释，避免把注释里的示例当成真代码。"""
    out: list[str] = []
    for line in text.splitlines():
        idx = line.find("#")
        while idx != -1:
            head = line[:idx]
            if head.count('"') % 2 == 0 and head.count("'") % 2 == 0:
                line = head
                break
            idx = line.find("#", idx + 1)
        out.append(line)
    return "\n".join(out)


def _literal(raw: str, consts: dict[str, str]) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return consts.get(raw, "")


#: `for hook in hooks: api.add_method_hook(hook[0], hook[1], hook[2], hook[3], 50)`
#:
#: 单条调用解析不出来——参数是数组元素。必须先找到 `var hooks: Array = [[...]]`
#: 那张数据表，把每个元素当一条记录读。浮游炮就是这么写的。
_FOR_HOOK_TABLE_RE = re.compile(
    r"for\s+(\w+)\s+in\s+(\w+)\s*:\s*\n[^\n]*add_method_hook\s*\(\s*"
    r"\1\s*\[\s*0\s*\]\s*,\s*\1\s*\[\s*1\s*\]\s*,\s*\1\s*\[\s*2\s*\]"
    r"(?:\s*,\s*\1\s*\[\s*3\s*\])?"
    r"(?:\s*,\s*(?P<prio>\d+))?",
    re.M,
)
_HOOK_TABLE_RE = re.compile(
    r"(?:var|const)\s+(\w+)\s*(?::\s*Array\s*)?:?=\s*\[([\s\S]*?)\n\s*\]",
    re.M,
)
#: 表里的一行：`["scripts/x.gd", "method", "phase", callback],`
_TABLE_ROW_RE = re.compile(
    r"\[\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*,\s*([A-Za-z_][\w.]*)"
)


def _scan_hook_tables(cleaned: str, consts: dict[str, str]) -> list[HookSite]:
    """解析 `for x in table: add_method_hook(x[0], x[1], x[2], x[3])` 这种写法。"""
    out: list[HookSite] = []
    # 表名 -> 循环调用里写的优先级（第五个参数，通常是一个字面量给整张表用）
    table_prio: dict[str, int | None] = {}
    for m in _FOR_HOOK_TABLE_RE.finditer(cleaned):
        name = m.group(2)
        prio = int(m.group("prio")) if m.group("prio") else None
        if name not in table_prio or table_prio[name] is None:
            table_prio[name] = prio
    table_names = set(table_prio)
    if not table_names:
        return out
    for name in sorted(table_names):
        found = None
        for cand in _HOOK_TABLE_RE.finditer(cleaned):
            if cand.group(1) == name:
                found = cand
                break
        if found is None:
            continue
        for row in _TABLE_ROW_RE.finditer(found.group(2)):
            script = _norm(_literal(f'"{row.group(1)}"', consts))
            method = row.group(2)
            phase = row.group(3).strip().lower()
            if phase not in ("before", "after"):
                phase = "after"
            out.append(
                HookSite(
                    script=script,
                    method=method,
                    phase=phase,
                    callback=row.group(4).strip(),
                    priority=table_prio.get(name),
                )
            )
    return out


def scan_source(text: str) -> ScanResult:
    """从一个 .gd 源码里扫出所有 add_method_hook 调用。"""
    result = ScanResult()
    cleaned = _strip_comments(text)
    consts = {m.group(1): m.group(2) for m in _CONST_STR_RE.finditer(cleaned)}

    # 先展开「数组表 + 循环注册」的写法，再扫字面量调用
    for site in _scan_hook_tables(cleaned, consts):
        result.sites.append(site)

    for m in _ADD_HOOK_RE.finditer(cleaned):
        script = _norm(_literal(m.group("script"), consts))
        method = _literal(m.group("method"), consts)
        phase = _literal(m.group("phase"), consts).strip().lower()
        if not script or not method or not phase:
            result.unresolved += 1
            continue
        if phase not in ("before", "after"):
            # replace 相位本项目不支持运行期注册，插桩层会按 after 处理
            phase = "after"
        result.sites.append(
            HookSite(
                script=script,
                method=method,
                phase=phase,
                callback=m.group("cb").strip(),
                priority=int(m.group("prio")) if m.group("prio") else None,
            )
        )
    return result


def scan_package(src_root: Path) -> ScanResult:
    """扫一个扩展的解包源码目录（``builds/_install_<id>_<line>/src``）。"""
    total = ScanResult()
    if not src_root.is_dir():
        return total
    for path in sorted(src_root.rglob("*.gd")):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        got = scan_source(text)
        total.unresolved += got.unresolved
        for site in got.sites:
            site.source = str(path.relative_to(src_root))
            total.sites.append(site)
    return total


def real_phases(
    src_root: Path | None,
    runtime_hooks: list[dict] | None,
    script: str,
    method: str,
) -> set[str]:
    """取某个钩子点的**真实**相位集合，供撞车检查使用。

    优先级：
    1. 安装时扫描出来的 ``runtime_hooks``（最权威，就是运行时注册的那份）
    2. 源码反查（老记录没有 runtime_hooks 时）
    3. 空集合 —— 调用方要据此判「未知」，而不是假设 before+after 都存在
    """
    if runtime_hooks:
        out = {
            str(h.get("phase") or "").strip().lower()
            for h in runtime_hooks
            if _norm(str(h.get("script") or "")) == _norm(script)
            and str(h.get("method") or "") == method
        }
        out.discard("")
        if out:
            return out
    if src_root is not None:
        scanned = scan_package(src_root)
        got = scanned.phases_of(script, method)
        if got:
            return got
    return set()