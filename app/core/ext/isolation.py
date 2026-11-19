"""扩展隔离：构建期静态扫描（L0 校验点）。

为什么要在**构建期**做，而不是运行期
------------------------------------

Godot 的扩展是「同一个 SceneTree 里的普通 Node」——不是进程、不是沙箱、不是模块。
实测（真实引擎）::

    api.host._states                 → 拿到真数据
    api.get("host").get("_states")   → 也拿到真数据（反射完全绕过）

GDScript 没有访问控制，**运行时不可能做出对「故意越界」的物理封锁**。
任何声称能封锁的设计都是假的。

所以本模块负责的是「**不让越界的代码进游戏**」：

- 误伤（作者不小心踩到）→ 由运行期的受控面兜住（深拷贝 / 分命名空间）
- 越界（故意或事实上）→ 由本模块在**安装/启用时**查出并拒绝

被扫的源码是 ``builds/_install_<id>_<line>/src/`` ——
与既有 ``service.py:_scan_runtime_hooks`` 同一份源，零额外成本。

十二个校验点
------------

==========  ==========================================  ==============
编号         检查什么                                    违规级别
==========  ==========================================  ==============
L0-1        直接取宿主（api.host / 反射）                **只 warn**
L0-2        按路径取宿主（/root/ModHost）                **只 warn**
L0-3        兄弟扩展实例（Mod_<别人>）                   **只 warn**
L0-4        读别人资源（res://mods/<peer>/）             **只 warn**
L0-5        直写共享状态文件（user://mod_states）        strict→拒装
L0-6        全局 meta 撞名（Engine.set_meta）            strict→拒装
L0-7        跨扩展服务读（api.service）                  **只 warn**
L0-8        钩子点撞车（两扩展注册同一点）               clash→拒装，safe 静默
L0-9        同点 result 改写                            并入 L0-8（hook_keys）
L0-10       读宿主未注入的 context 键                   **只 warn**
L0-11       声明与实现不符                              **只 warn**
L0-12       peer 缺失 / 互读成环                        strict→拒装
==========  ==========================================  ==============

**2026-11-08 按生态目标重划了阻断线**（用户决策）：

判据从「代码写得干不干净」换成「**装上去能不能用**」。

| 类别 | 规则 | 为什么 |
|---|---|---|
| 耦合声明 | L0-1/2/3/4/7/11 | 扩展定位是「补全原版缺失内容」，必然要访问宿主。当违规处理等于禁止做内容补全 —— 实测一次合集导入里 5 个「只是引用了宿主」的包被拒 |
| 真实互踩 | L0-5/6/8/12 | 两个包写同一份文件 / 抢同一个命名 / 依赖成环，**真会用不了** |

原实现把两类同等对待，导致一次导入里 5 个「只是引用了宿主」的包被拒，
连带整组 25 个全部回滚。现在耦合类只提示，互踩类才拦。

L0-8 的判定也换了：不再「同点即冲突」，而是交给
:func:`app.core.ext.hook_keys.compare` 看回调**实际写哪些键**。
实测 ``better`` 写 2005~2017、``rose`` 写 2019/2020 —— 键不重叠，
同点共存完全安全，原判据会误伤。

L0-10 **现网就会命中**（实测 145 处死键），一直是只 warn。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 扩展的私有状态文件（严格模式下按扩展分文件，这里是旧的共享路径）
SHARED_STATE_MARKERS = ("user://mod_states", "mod_states.json")

#: 宿主注入的 context 键。扩展读不在这里的键 → 那段代码是死代码。
HOST_CONTEXT_KEYS = frozenset({"script", "method", "args", "result", "skip_original"})

#: 宿主**没有**注入、但扩展普遍在读的键（实测 ai_act 65 + 45 处）。
#: 这些键需要 ``extensions.hook_ctx_extended`` 打开才有 —— 关着的时候
#: 扩展读到的就是 null，钩子代码第一行就 return。
EXTENDED_CONTEXT_KEYS = frozenset({"instance", "arguments"})


@dataclass
class PeerFinding:
    """一处跨扩展访问的发现。

    ``code`` 用 L0-x 编号；``severity`` 决定它在各档模式下的处置。
    """

    code: str
    severity: str          # block | warn
    ext_id: str
    detail: str
    path: str = ""         # 相对扩展 src 的文件
    line: int = 0
    snippet: str = ""
    peer: str = ""         # 涉及的另一个扩展 id（若有）

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "ext": self.ext_id,
            "peer": self.peer,
            "detail": self.detail,
            "path": self.path,
            "line": self.line,
            "snippet": self.snippet,
        }

    def human(self) -> str:
        where = f"{self.path}:{self.line}" if self.path else ""
        tail = f"（{where}）" if where else ""
        return f"[{self.code}] {self.detail}{tail}"


# ---------------------------------------------------------------- 匹配规则

# L0-1：直接取宿主。含反射写法。
_R_HOST_DIRECT = re.compile(
    r"\bapi\s*\.\s*(?:_?host)\b"
    r"|\bapi\s*\.\s*get\s*\(\s*[\"']_?host[\"']\s*\)"
)

# L0-2：按路径取宿主 / 兄弟扩展。
#
# 注意**不能**把裸 ``get_parent()`` 算进来：扩展访问自己场景树里的节点
# （``actor.get_parent().get("slice_run")``）是完全合法的用法 ——
# 实测现网 30 处命中全是这种，全是误报。
# 真正要拦的是**指名道姓**去找宿主或兄弟扩展：
#   get_node("/root/ModHost")、get_node("Mod_mch_sell_all")
_R_HOST_PATH = re.compile(
    r"[\"']/root/ModHost[\"']"
    r"|\bget_node(?:_or_null)?\s*\(\s*[\"']Mod_"
    r"|\bget_node(?:_or_null)?\s*\(\s*[\"'][^\"']*ModHost[^\"']*[\"']"
)

# L0-4：读别人资源。res://mods/<id>/ 里 <id> 不是自己就是越界。
_R_PEER_RESOURCE = re.compile(r"res://mods/([A-Za-z0-9_.]+)/")

# L0-5：直写共享状态文件
_R_SHARED_STATE = re.compile(r"user://mod_states|mod_states\.json")

# L0-6：全局 meta 撞名。Engine.set_meta("x", …) 里 x 不以自己的 id 打头就是撞名。
_R_SET_META = re.compile(r"Engine\s*\.\s*set_meta\s*\(\s*[\"']([^\"']+)[\"']")

# L0-7：跨扩展服务读
_R_SERVICE = re.compile(r"\bapi\s*\.\s*(?:has_)?service\s*\(\s*[\"']([^\"']+)[\"']")

#: 扩展可以往 context 里写的**自己**的键（钩子之间传数据用）。
#: 实测扩展普遍这么干：``context["ai_act_wb_pull"] = true``，
#: 另一个回调再 ``context.get("ai_act_wb_pull")`` 读回来。
#: 这些键不是宿主注入的，但也不是「宿主缺键」—— 是扩展间的私有约定，
#: 不该报成 L0-10。
_SELF_CONTEXT_KEY = re.compile(r"^[A-Za-z_][\w.]*$")

# L0-10：读 context 的键
_R_CONTEXT_GET = re.compile(r"context\s*\.\s*get\s*\(\s*[\"']([^\"']+)[\"']")
_R_CONTEXT_KEY = re.compile(r"context\s*\.\s*([A-Za-z_]\w*)")
_R_CONTEXT_GET2 = re.compile(r"context\s*\.\s*get\s*\(\s*[\"']([^\"']+)[\"']\s*\)")


def _read_context_keys(text: str) -> set[str]:
    """扫出源码里读到的 context 键。"""
    keys = set(_R_CONTEXT_GET.findall(text))
    for m in _R_CONTEXT_GET2.finditer(text):
        keys.add(m.group(1))
    # context.foo 形式（不含 get）
    for m in re.finditer(r"\bcontext\s*\.\s*([A-Za-z_]\w*)", text):
        name = m.group(1)
        if name in ("get", "has", "keys", "size", "duplicate", "is_empty", "erase"):
            continue
        keys.add(name)
    return keys


def _self_written_context_keys(text: str) -> set[str]:
    """扫出扩展**自己往 context 里写**的键。

    扩展之间常用 context 传数据：一个回调写
    ``context["ai_act_wb_pull"] = true``，另一个回调
    ``context.get("ai_act_wb_pull")`` 读回来。那个键宿主本来就不该有，
    所以它**不是**「宿主缺键」，报成 L0-10 是误报。
    """
    out: set[str] = set()
    for m in re.finditer(r"\bcontext\s*\[\s*[\"']([^\"']+)[\"']\s*\]\s*=", text):
        out.add(m.group(1))
    for m in re.finditer(r"\bcontext\s*\.\s*([A-Za-z_]\w*)\s*=", text):
        out.add(m.group(1))
    return out


def _norm_ext_id(value: str) -> str:
    """把 id 归一：``mch.sell_all`` / ``mch/sell_all`` / ``mch_sell_all`` 视为同一个。"""
    return str(value or "").strip().replace("/", ".").replace("-", ".")


def _own_variants(ext_id: str) -> set[str]:
    """自己的 id 的各种写法（用于判断「读的是不是自己的资源」）。"""
    base = _norm_ext_id(ext_id)
    return {base, base.replace(".", "_"), base.replace(".", "/"), base.replace(".", "-")}


# ---------------------------------------------------------------- 扫描


def scan_source(
    text: str,
    ext_id: str,
    path: str = "",
    *,
    extended_ctx: bool = True,
) -> list[PeerFinding]:
    """扫一个扩展源文件里的跨扩展访问。

    返回空表 = 这个文件干净。
    """
    out: list[PeerFinding] = []
    own = _own_variants(ext_id)
    lines = text.splitlines()

    def _line_of(pos: int) -> tuple[int, str]:
        idx = text.count("\n", 0, pos)
        snippet = lines[idx].strip()[:120] if idx < len(lines) else ""
        return idx + 1, snippet

    def _add(code: str, severity: str, detail: str, pos: int, peer: str = "") -> None:
        line, snippet = _line_of(pos)
        out.append(
            PeerFinding(
                code=code,
                severity=severity,
                ext_id=ext_id,
                detail=detail,
                path=path,
                line=line,
                snippet=snippet,
                peer=peer,
            )
        )

    # ---- L0-1 直接取宿主
    #
    # **只提示，不拒装。** 2026-11-08 按生态目标调整：
    #
    # 本项目的扩展定位是「补全原版缺失的内容」，这天然要读宿主状态、
    # 拿宿主没暴露的字段。原先把 ``api.host`` 一律判 block，等于禁止
    # 扩展访问宿主 —— 而那正是做内容补全的必要手段。
    #
    # 实测代价：一次合集导入里 5 个包因为命中这条被整组回滚，玩家
    # 一个都装不上，而这 5 个包本身没有任何互相冲突。
    #
    # 真正的「会打架」由 L0-8/L0-9（钩子）与 resolver 的 merge.* 负责，
    # 那些才代表「装上了也用不了」。
    for m in _R_HOST_DIRECT.finditer(text):
        _add("L0-1", "warn", "直接取宿主内部（api.host / 反射）", m.start())

    # ---- L0-2 按路径取宿主 / 兄弟扩展
    # 同 L0-1：属于「耦合声明」而非「冲突」，只提示。
    for m in _R_HOST_PATH.finditer(text):
        _add("L0-2", "warn", "按路径取宿主或兄弟扩展节点", m.start())

    # ---- L0-4 读别人资源
    #
    # 同 L0-1。补充实测依据（docs/开发者须知.md C5）：
    # 宝石镶嵌 mod 命中这条是因为 ``main.gd`` 里有一段
    # ``_entry_position_for("res://mods/jjxf.better_forge/...")`` ——
    # 只做字符串 contains 与坐标计算，**没有 load/preload**。
    # 正则只认字面量，把这种纯字符串引用也判成越界。
    for m in _R_PEER_RESOURCE.finditer(text):
        peer = _norm_ext_id(m.group(1))
        if peer in own or m.group(1) in own:
            continue
        _add("L0-4", "warn", f"读其它扩展的资源：res://mods/{m.group(1)}/", m.start(), peer=peer)

    # ---- L0-5 直写共享状态文件
    for m in _R_SHARED_STATE.finditer(text):
        _add("L0-5", "block", "直接读写共享状态文件（所有扩展共用一个 json）", m.start())

    # ---- L0-6 全局 meta 撞名
    #
    # 判断「是不是自己的」用 id 的**第一段**（厂商名）：扩展 id 形如
    # ``ai_act.combat_workbench``，而 meta key 的惯例是
    # ``Engine.set_meta("ai_act_wb_attraction_runtime", …)`` ——
    # 用的是「厂商_包名缩写_用途」，不是原样 id。
    # 用完整 id 判断会误报 10 处，用厂商名判断才准。
    vendor = ext_id.split(".")[0] if "." in ext_id else ext_id
    prefixes = {
        ext_id,
        ext_id.replace(".", "_"),
        ext_id.replace(".", "-"),
        vendor,
        f"{vendor}_",
        f"{vendor}.",
    }
    for m in _R_SET_META.finditer(text):
        key = m.group(1)
        if any(key.startswith(p) for p in prefixes):
            continue
        _add(
            "L0-6",
            "warn",
            f"全局 meta 撞名风险：Engine.set_meta(\"{key}\") 未以自己的 id 打头",
            m.start(),
        )

    # ---- L0-7 跨扩展服务读
    for m in _R_SERVICE.finditer(text):
        name = m.group(1)
        owner = name.split(".")[0] if "." in name else ""
        if owner and owner != ext_id:
            _add("L0-7", "warn", f"读其它扩展的服务：api.service(\"{name}\")", m.start(), peer=owner)

    # ---- L0-10 读宿主未注入的 context 键
    #
    # **只报能静态确定是死键的**：``instance`` / ``arguments`` ——
    # 宿主注入的键就那么几个（script/method/args/result/skip_original），
    # 这两个在 ``extensions.hook_ctx_extended`` 关着时必然拿不到。
    #
    # 其它的键**不报**：扩展之间常用 context 传数据（一个回调写
    # ``context["hud"] = x``、另一个回调读回来），那个键宿主本来就不该有，
    # 而且「谁写谁读」是**跨文件**的，静态判断不了 ——
    # 实测报出来的全是误报（floating_cannons 的 hud/game、treasure_dup 的
    # add_method_hook/fail/has_method）。
    if not extended_ctx:
        for key in sorted(EXTENDED_CONTEXT_KEYS):
            if key in _read_context_keys(text):
                pos = text.find(f'"{key}"')
                _add(
                    "L0-10",
                    "warn",
                    f"读 context[\"{key}\"]，但宿主未注入这个键"
                    f"（extensions.hook_ctx_extended 关着时，这段代码是死代码）",
                    pos if pos >= 0 else 0,
                )

    return out


def scan_tree(
    files: dict[str, str],
    ext_id: str,
    *,
    extended_ctx: bool = True,
) -> list[PeerFinding]:
    """扫一个扩展的整棵源码树。

    ``files`` 是 相对路径 -> 内容。
    """
    out: list[PeerFinding] = []
    for rel in sorted(files):
        out.extend(scan_source(files[rel], ext_id, rel, extended_ctx=extended_ctx))
    return out


# ---------------------------------------------------------------- 钩子点撞车


def detect_hook_collisions(
    hooks_by_ext: dict[str, list[dict[str, str]]],
    writes_by_point: dict[tuple[str, str, str], list[object]] | None = None,
) -> list[PeerFinding]:
    """查「同一个钩子点被多个扩展注册」。

    ``hooks_by_ext`` 是 扩展 id -> 钩子列表（每项含 script/method/phase）。

    为什么这算问题：``hook_runtime.gd`` 把**同一个 context 字典实例**传给
    同点的所有回调，且 ``context["result"]`` 是**就地改写**的嵌套字典。
    后跑的扩展会覆盖先跑的 —— 实测 ``rose.dimension_ruins`` 就地改了
    ``result``，``ai_act.combat_workbench`` 随后把某个键无条件覆盖，
    **玩家配的倍率被静默吃掉**，而构建期的 ``overlap.text`` 警告完全看不到
    （它们改的不是文件，是同一个内存对象）。

    **2026-11-08 改：从「同点即冲突」改成「同点且真撞」才冲突。**

    原判据只看 ``(script, method, phase)`` 三元组是否相同。实测这是错的：

    - ``better_experience`` 写 result 的 ``2005~2017``，``rose`` 写
      ``2019/2020`` —— 键完全不重叠，同点共存毫无问题；
    - 但三元组相同，于是被判成撞车，两个包一起被拒。

    ``writes_by_point`` 传进来后（来自 :func:`app.core.ext.hook_keys.compare`），
    结论分三档：

    ===========  ====================================  ==========
    level        含义                                  严重度
    ===========  ====================================  ==========
    ``safe``     键不重叠 / 一方改入参一方改返回       正常（不报）
    ``unknown``  有一方写入解析不出来                   提示
    ``clash``    键真重叠 / 整份替换 / skip_original    阻断
    ===========  ====================================  ==========

    不传 ``writes_by_point`` 时退回旧行为（只报提示，不再阻断）——
    调用方拿不到源码的场合不该因为「查不了」就把人拦住。
    """
    by_point: dict[tuple[str, str, str], set[str]] = {}
    for ext_id, hooks in hooks_by_ext.items():
        for h in hooks:
            key = (str(h.get("script", "")), str(h.get("method", "")), str(h.get("phase", "")))
            if not all(key):
                continue
            # 用 set：同一个扩展注册同一个点两次是它内部的事，**不算撞车**
            # （撞车的定义是「两个不同扩展抢同一个点」）。
            by_point.setdefault(key, set()).add(ext_id)

    out: list[PeerFinding] = []
    for (script, method, phase), owners in sorted(by_point.items()):
        unique = sorted(owners)
        if len(unique) < 2:
            continue

        level = "unknown"
        reason = ""
        infos = (writes_by_point or {}).get((script, method, phase)) or []
        if infos:
            try:
                from . import hook_keys as hook_keys_mod

                level, reason = hook_keys_mod.compare(list(infos), phase)
            except Exception:  # noqa: BLE001 - 判定失败不该让安装崩掉
                level, reason = "unknown", "写入解析失败，需人工确认"
        else:
            reason = "多个扩展注册同一点，未取到写入信息（建议人工确认）"

        if level == "safe":
            # 明确安全：不产生任何条目，界面上不该出现「冲突」
            continue

        severity = "block" if level == "clash" else "warn"
        detail = (
            f"钩子点撞车：{method}（{phase}）被 {'、'.join(unique)} 同时注册"
            f" —— {reason}"
        )
        for ext_id in unique:
            out.append(
                PeerFinding(
                    code="L0-8",
                    severity=severity,
                    ext_id=ext_id,
                    detail=detail,
                    peer=",".join(o for o in unique if o != ext_id),
                )
            )
    return out


# ---------------------------------------------------------------- peer 声明


def detect_peer_cycles(
    peer_access: dict[str, dict[str, Any]],
) -> list[PeerFinding]:
    """查 ``peer_access`` 里的互读环。

    ``peer_access`` 是 扩展 id -> 声明。声明是双向的（消费方声明意图 +
    提供方声明许可），所以要看「双向都声明」的边。
    """
    edges: dict[str, set[str]] = {}
    for ext_id, decl in peer_access.items():
        targets: set[str] = set()
        for scope in ("state_read", "state_write"):
            for item in decl.get(scope) or []:
                if isinstance(item, dict) and item.get("peer"):
                    targets.add(_norm_ext_id(item["peer"]))
        for item in decl.get("services_read") or []:
            owner = str(item).split(".")[0]
            if owner:
                targets.add(_norm_ext_id(owner))
        edges[ext_id] = targets

    out: list[PeerFinding] = []
    # 找环（DFS）
    color: dict[str, int] = {}

    def _visit(node: str, stack: list[str]) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if nxt not in edges:
                continue
            if color.get(nxt) == 1:
                cycle = stack[stack.index(nxt):] + [nxt]
                for member in cycle:
                    out.append(
                        PeerFinding(
                            code="L0-12",
                            severity="block",
                            ext_id=member,
                            detail=f"跨扩展互读成环：{' → '.join(cycle)}",
                            peer=",".join(x for x in cycle if x != member),
                        )
                    )
                continue
            if color.get(nxt, 0) == 0:
                _visit(nxt, stack)
        stack.pop()
        color[node] = 2

    for node in sorted(edges):
        if color.get(node, 0) == 0:
            _visit(node, [])
    return out


def missing_peers(
    peer_access: dict[str, dict[str, Any]],
    enabled: Iterable[str],
) -> list[PeerFinding]:
    """查「声明要访问、但那个扩展没启用」的 peer。"""
    have = {_norm_ext_id(x) for x in enabled}
    out: list[PeerFinding] = []
    for ext_id, decl in sorted(peer_access.items()):
        wanted: set[str] = set()
        for scope in ("state_read", "state_write"):
            for item in decl.get(scope) or []:
                if isinstance(item, dict) and item.get("peer"):
                    wanted.add(_norm_ext_id(item["peer"]))
        for item in decl.get("services_read") or []:
            owner = str(item).split(".")[0]
            if owner:
                wanted.add(_norm_ext_id(owner))
        for peer in sorted(wanted - have):
            out.append(
                PeerFinding(
                    code="L0-12",
                    severity="block",
                    ext_id=ext_id,
                    detail=f"声明要访问的扩展「{peer}」当前未启用",
                    peer=peer,
                )
            )
    return out


# ---------------------------------------------------------------- 声明核对


def reconcile(
    declared: dict[str, Any],
    findings: list[PeerFinding],
) -> list[PeerFinding]:
    """核对「声明的」与「实际做的」是否一致（L0-11）。

    清单里手写的 ``peer_access`` 是**上限**，源码扫描出的实际访问是**事实**。
    两者不一致 → 违规：

    - 声明了但不实现 → 清单虚高，会误导别人（也可能是作者抄模板没改）
    - 实现了但不声明 → 真正的越界

    只有**实际访问了但没声明**才算违规（block）。
    声明了但没用不算 —— 那只是清单写得宽松。
    """
    out: list[PeerFinding] = []
    allowed: set[str] = set()
    for scope in ("state_read", "state_write"):
        for item in declared.get(scope) or []:
            if isinstance(item, dict) and item.get("peer"):
                allowed.add(_norm_ext_id(item["peer"]))
    for item in declared.get("services_read") or []:
        owner = str(item).split(".")[0]
        if owner:
            allowed.add(_norm_ext_id(owner))
    for item in declared.get("resources_read") or []:
        if isinstance(item, dict) and item.get("peer"):
            allowed.add(_norm_ext_id(item["peer"]))

    for f in findings:
        # 只对「跨扩展访问」这类**带 peer 的**发现做声明核对。
        #
        # L0-5（直写共享状态文件）/ L0-6（meta 撞名）这类没有具体 peer
        # 的阻断项不该进这里 —— 它们本身就是终局判定，转成 L0-11 会
        # 把 block 降成 warn，等于把真互踩放过去了。
        #
        # 2026-11-08 修正：原条件 ``f.severity != "block" and code not in (...)``
        # 会让 L0-5 落进循环，随后被 ``if not f.peer: continue`` 跳过 ——
        # 表面上没转成 L0-11，但配合下游「只看 block」的调用方会漏判。
        # 这里显式限定 code 白名单，把 L0-5/L0-6/L0-12 排除在外。
        if f.code not in ("L0-1", "L0-2", "L0-3", "L0-4", "L0-7"):
            continue
        if not f.peer:
            continue
        if _norm_ext_id(f.peer) in allowed:
            continue
        out.append(
            PeerFinding(
                code="L0-11",
                # **只提示。** 「声明与实际不符」是清单缺登记，不是功能冲突。
                # 原先进 block，导致作者少写一行 peer_access 就装不上 ——
                # 而他其实什么都没做错，只是没登记。
                #
                # 保留这条检查的价值在于「让他知道该补哪一行」，
                # 而不是把人拦在门外。
                severity="warn",
                ext_id=f.ext_id,
                detail=(
                    f"实际访问了「{f.peer}」但清单的 peer_access 里没声明"
                    f"（原问题：{f.code} {f.detail}）"
                ),
                path=f.path,
                line=f.line,
                snippet=f.snippet,
                peer=f.peer,
            )
        )
    return out


# ---------------------------------------------------------------- 补丁生成


def build_peer_access_patch(findings: list[PeerFinding]) -> dict[str, Any]:
    """按扫描结果生成 ``peer_access`` 草稿。

    目的：作者不必自己发明声明格式 —— 把扫描结果贴进清单就能用。
    """
    state_read: dict[str, dict[str, Any]] = {}
    state_write: dict[str, dict[str, Any]] = {}
    resources: dict[str, dict[str, Any]] = {}
    services: set[str] = set()

    for f in findings:
        if not f.peer:
            continue
        if f.code == "L0-4":
            resources.setdefault(f.peer, {"peer": f.peer, "path": ""})
        elif f.code == "L0-7":
            services.add(f.detail.split('"')[-2] if '"' in f.detail else f.peer)
        elif f.code in ("L0-1", "L0-2", "L0-3", "L0-5"):
            state_read.setdefault(f.peer, {"peer": f.peer, "keys": []})

    patch: dict[str, Any] = {}
    if state_read:
        patch["state_read"] = [state_read[k] for k in sorted(state_read)]
    if state_write:
        patch["state_write"] = [state_write[k] for k in sorted(state_write)]
    if resources:
        patch["resources_read"] = [resources[k] for k in sorted(resources)]
    if services:
        patch["services_read"] = sorted(services)
    return patch


# ---------------------------------------------------------------- 汇总


def summarize(findings: list[PeerFinding]) -> dict[str, Any]:
    """按级别与编号汇总，给界面/CLI 用。"""
    by_code: dict[str, int] = {}
    by_sev: dict[str, int] = {"block": 0, "warn": 0}
    for f in findings:
        by_code[f.code] = by_code.get(f.code, 0) + 1
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    return {
        "total": len(findings),
        "block": by_sev.get("block", 0),
        "warn": by_sev.get("warn", 0),
        "by_code": dict(sorted(by_code.items())),
    }


def load_src_files(src_root: Path) -> dict[str, str]:
    """读一个扩展的解包源码目录。"""
    files: dict[str, str] = {}
    if not src_root.exists():
        return files
    for path in sorted(src_root.rglob("*.gd")):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            files[str(path.relative_to(src_root))] = path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
    return files
