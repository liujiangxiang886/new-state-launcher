"""静态分析：共享钩子点上的回调各自写哪些 result 键，判断是否真冲突。

背景
----
isolation.L0-8「钩子点撞车」只判定 `脚本::方法::相位` 三元组是否相同，
不判定回调实际改哪些键。结果是：

* better_experience 改 2007/2009，rose 改 2019/2020 —— 键不重叠，实际安全；
* 但两者都报「后跑的会覆盖先跑的 result」，和真冲突长得一模一样。

开发者拿到这种告警只能自己翻两份源码逐字比对。这里补上「到底改哪些键」的
静态提取，让告警能给出结论而不是风险猜测。

提取口径（保守：宁可认不出，不可认错）
------------------------------------
识别这几种写法（都是现网扩展的实际写法）：

1. `attrs["2019"] = X`                       → 键 "2019"
2. `result["2005"] = ...`                    → 键
3. `_scale_key(result, "2005", ...)`         → 键（常见封装）
4. `for key in ATTACK_KEYS: ... result[key]` → 展开常量列表
5. `result.duplicate(true)` 后整体替换        → 标记 WHOLE（视为全键）

认不出具体键的回调记 `UNKNOWN`，此时不下「安全」结论，仍按「需人工确认」报。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: 认不出具体键
UNKNOWN = "?"
#: 整份替换 result（.duplicate / = {} / = attrs 之类）
WHOLE = "*"

#: result 变量的常见别名
RESULT_NAMES = ("result", "attrs", "attributes", "ret", "value", "out")

_KEY_INDEX_RE = re.compile(
    r"\b(?:" + "|".join(RESULT_NAMES) + r')\s*\[\s*"([^"]+)"\s*\]\s*=(?!=)'
)
_KEY_INDEX_INT_RE = re.compile(
    r"\b(?:" + "|".join(RESULT_NAMES) + r")\s*\[\s*(\d+)\s*\]\s*=(?!=)"
)
#: _scale_key(result, "2005", mult, digits)
_SCALE_CALL_RE = re.compile(r'_scale_key\s*\(\s*\w+\s*,\s*"([^"]+)"')
#: _scale_keys(result, ["2007","2009"], ...)
_SCALE_LIST_RE = re.compile(r"_\w*keys?\w*\s*\(\s*\w+\s*,\s*\[([^\]]*)\]")
#: 整份替换 result：**新建**一份 dict 赋回去（没有先读入参）。
#:
#: 注意不能把 `context["result"] = attrs` 一律当成整份替换 —— 现网的写法
#: 几乎都是「先 `var attrs = context.get("result")`，就地改几个键，再写回」。
#: 那是就地修改，同一个对象，不是替换。只有赋空字典字面量、或赋一个与入参
#: 无关的新对象才算替换。
_WHOLE_RE = re.compile(
    r'context\s*\[\s*"result"\s*\]\s*=\s*(?:\{\s*\}|[A-Za-z_][\w.]*\s*\(\s*\))'
)
#: 读入参的迹象：说明回调是「读—改—写回」，不销毁别人的改动
_READS_RESULT_RE = re.compile(
    r'context\s*\.\s*get\s*\(\s*"result"|context\s*\[\s*"result"\s*\]\s*(?!\s*=)'
)

#: before 相位改的是**入参**而不是 result：
#: `arguments[0] = X` / `args[0] = X` / `context["arguments"][0] = X`
_ARG_INDEX_RE = re.compile(
    r'(?:\barguments\b|\bargs\b|context\s*\[\s*"arguments"\s*\])\s*\[\s*(\d+)\s*\]\s*=(?!=)'
)

#: `context["skip_original"] = true` —— 直接跳过原函数。
#:
#: 这是共享钩子点上影响最大的一种写：一旦某个 before 回调把它设为 true，
#: `dispatch_before` 立刻 return，**排在后面的回调一个都不会执行**
#: （见 runtime/_compat/hook_runtime.gd:dispatch_before）。
#: 这不属于「result 键重叠」，但后果比键重叠严重 —— 后装的包会被完全吞掉。
_SKIP_RE = re.compile(
    r'context\s*\[\s*"skip_original"\s*\]\s*=\s*true'
    r'|context\s*\.\s*skip_original\s*=\s*true'
)
#: for key in XXX:
_FOR_CONST_RE = re.compile(r"for\s+(\w+)\s+in\s+([A-Z_][A-Z0-9_]*)\s*:")

#: 读入参：`context.get("arguments")` / `context["arguments"]` / `arguments[0]` / `args[0]`
_READS_ARGS_RE = re.compile(
    r'context\s*\.\s*get\s*\(\s*"arguments"'
    r'|context\s*\[\s*"arguments"\s*\]'
    r"|\barguments\s*\["
    r"|\bargs\s*\["
)

#: 拒绝型跳过：把 result 直接置 false / null（点号与下标两种写法都算）
_REJECT_RE = re.compile(
    r'context\s*(?:\.\s*result|\[\s*"result"\s*\])\s*=\s*(?:false|null)\b'
)

#: 取宿主实例的三种写法。这是**唯一**允许出现在「纯副作用回调」里的
#: context 用法：拿到节点之后做的事都在节点上，改不到钩子的返回值。
_INSTANCE_ACCESS_RE = re.compile(
    r'context\s*\.\s*instance'
    r'|context\s*\.\s*get\s*\(\s*"instance"\s*\)'
    r'|context\s*\[\s*"instance"\s*\]'
)
#: const XXX := ["2007", "2009"]
_CONST_LIST_RE = re.compile(
    r'^(?:const|var)\s+([A-Z_][A-Z0-9_]*)\s*(?::\s*\w+\s*)?:?=\s*\[([^\]]*)\]',
    re.M,
)


@dataclass
class WriteInfo:
    """一个回调在某个钩子点上写了什么。"""

    ext_id: str
    script: str
    method: str
    phase: str
    keys: set[str] = field(default_factory=set)
    #: 是否整份替换 result（此时任何键都可能被改）
    whole: bool = False
    #: 是否完全没识别出写入（可能只读、也可能是复杂逻辑）
    empty: bool = True
    #: before 相位修改的形参下标（`arguments[0] = X` 这类）
    arg_indexes: set[str] = field(default_factory=set)
    #: 是否设置 skip_original —— 会跳过原函数，并吞掉同点后续回调
    skips_original: bool = False
    #: 写入方式是「读—改—写」（右值用到了当前值）。这类写入是叠加关系，
    #: 后跑的一方不会抹掉先跑的贡献；不在这个集合里的键就是直接赋值。
    rmw_keys: set[str] = field(default_factory=set)
    #: 覆盖型写入所在的条件分支（键 -> `if ...` 原文），只用于把告警说清楚
    conditions: dict[str, str] = field(default_factory=dict)
    #: **无法证明它只操作实例**。只有「确认没碰 result/入参、也没把 context
    #: 转交给别的函数」时才置 False。默认 True 是刻意的：任何没算到的路径
    #: 都要保持「需人工确认」，不能因为新加了字段就悄悄变安全。
    #:
    #: 存在的意义：现网有大量回调只做副作用（建节点、清冷却），体里压根没有
    #: 写返回值。旧实现把这些和「解析失败」一起归进 empty，于是共享钩子点
    #: 永远报「写入没解析出来」——17 个包时虚报了 2 条。
    opaque: bool = True
    #: 是否读了入参（`arguments` / `args`）。before 相位里 skip 一方如果读入参，
    #: 说明它自己会消费前面回调改过的值，改动不会白丢。
    reads_args: bool = False
    #: 是否「拒绝型」跳过：既设了 skip_original，又把 result 置成 false/null。
    #: 这类回调即便互相吞掉，技能/行为的结果也一致，只差提示文案。
    rejects: bool = False
    #: 该包在这个点上的注册优先级（小数字先跑）。认不出就是 None。
    #: 挂在 WriteInfo 上而不是单独传字典，是因为**判定有两处调用方**
    #: （体检面板走 analyze_point、安装期撞车检测走 compare(infos)），
    #: 分开传会出现「面板说安全、CLI 说冲突」的自相矛盾。
    priority: int | None = None

    def as_text(self) -> str:
        bits: list[str] = []
        if self.whole:
            bits.append("整份替换 result")
        if self.arg_indexes:
            shown = "、".join("#" + i for i in sorted(self.arg_indexes, key=int))
            bits.append(f"改入参 {shown}")
        if self.keys:
            bits.append("写 " + "、".join(sorted(self.keys)))
        if self.skips_original:
            bits.append("skip_original=true")
        return "；".join(bits) if bits else "只读，不改结果"


def _instance_only(merged: str) -> bool:
    """回调是否「只通过 context.instance 取宿主、别的一概不碰」。

    判据要**正面成立**才返回 True：必须真的出现了 `context.instance` 之类，
    并且把它抠掉后正文里不再有 `context`。这一步是刻意的保守设计——

    * 回调把整个 context 转交给别的函数（``helper.do_something(context)``）时，
      抠完还剩 ``context``，判「证明不了」，仍报需人工确认；
    * 回调连 instance 都没写（可能压根没解析到函数体），也判「证明不了」。

    只做副作用的回调（建 HUD 节点、清冷却）正好落在这里判 True：
    它们不可能改返回值，和同点别的扩展天然不冲突。
    """
    if not _INSTANCE_ACCESS_RE.search(merged):
        return False
    # 函数签名里的形参声明（`func x(context: Dictionary)`）不是「使用」，
    # 先抠掉，否则每段正文都会残留一个 context。
    rest = re.sub(r"\bcontext\s*:\s*[A-Za-z_][\w\[\]]*", "", merged)
    rest = _INSTANCE_ACCESS_RE.sub("", rest)
    # 必须按整词判：`run.spawn_context_hit_effect()` 里也含 "context"，
    # 用子串判会把只读实例的回调误判成「转交出去了」。
    return re.search(r"\bcontext\b", rest) is None


def _strip_comments(text: str) -> str:
    """去掉行尾注释与文档字符串，避免把注释里的例子当成真代码。"""
    out: list[str] = []
    for line in text.splitlines():
        # 只在「# 前面没有引号成对出现」时截断，简单处理够用
        idx = line.find("#")
        while idx != -1:
            if line[:idx].count('"') % 2 == 0 and line[:idx].count("'") % 2 == 0:
                line = line[:idx]
                break
            idx = line.find("#", idx + 1)
        out.append(line)
    return "\n".join(out)


def _collect_const_lists(text: str) -> dict[str, list[str]]:
    """收集脚本里的常量字符串列表，供 `for key in XXX` 展开。"""
    out: dict[str, list[str]] = {}
    for match in _CONST_LIST_RE.finditer(text):
        name, body = match.group(1), match.group(2)
        items = re.findall(r'"([^"]*)"', body)
        if items:
            out[name] = items
    return out


#: `func foo(` / `static func foo(`
#:
#: **必须认 `static func`**：宠物守护的 ``guard_core.gd`` 整个文件都是
#: ``static func``，旧正则只匹配行首 ``func``，于是这个文件在分析器眼里
#: 一个函数都没有 —— 钩子转发过去的写入自然「没解析出来」。
_FUNC_HEAD_RE = re.compile(r"^([ \t]*)(?:static\s+)?func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_FUNC_ANY_RE = re.compile(r"^[ \t]*(?:static\s+)?func\s+")


def _function_bodies(text: str) -> dict[str, str]:
    """把脚本切成 func 名 -> 函数体文本（含 ``static func``）。"""
    lines = text.splitlines()
    heads: list[tuple[int, str, int]] = []
    for i, line in enumerate(lines):
        m = _FUNC_HEAD_RE.match(line)
        if m:
            heads.append((i, m.group(2), len(m.group(1))))
    out: dict[str, str] = {}
    for start, name, indent in heads:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j]
            if not stripped.strip():
                continue
            cur = len(stripped) - len(stripped.lstrip())
            if cur <= indent and _FUNC_ANY_RE.match(stripped):
                end = j
                break
        out.setdefault(name, "\n".join(lines[start:end]))
    return out


#: `api.add_method_hook("scripts/x.gd", "method", "phase", _callback, prio)`
_ADD_HOOK_RE = re.compile(
    r"add_method_hook\s*\(\s*"
    r"(?P<script>[^,]+?)\s*,\s*"
    r'(?:"(?P<method>[^"]+)"|(?P<method_var>\w+))\s*,\s*'
    r'(?:"(?P<phase>[^"]+)"|(?P<phase_var>\w+))\s*,\s*'
    r"(?P<cb>[A-Za-z_][\w.]*)"
)


def resolve_callbacks(
    source: str, script: str, method: str, phase: str
) -> list[str]:
    """找出某个钩子点上真正注册的回调函数名。

    为什么需要这一步：清单里的 ``method_hooks`` 只写「挂哪个原版方法」，
    ``handler`` 普遍是空的（回调在 ``on_game_ready`` 里运行期注册）。
    所以要从源码里的 ``api.add_method_hook(...)`` 调用点反查第四个参数，
    才知道该分析哪个函数。

    只做字符串字面量匹配：``add_method_hook(RUN, ...)`` 这种变量参数认不出，
    返回空列表，交由调用方标记「需人工确认」。
    """
    text = _strip_comments(source)
    consts = _collect_const_strings(text)
    out: list[str] = []

    def _norm(value: str) -> str:
        return value.replace("res://", "").strip()

    def _literal(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            return raw[1:-1]
        # 变量形式：去常量表里查
        return consts.get(raw, raw)

    for m in _ADD_HOOK_RE.finditer(text):
        hook_script = _norm(_literal(m.group("script")))
        hook_method = _literal(m.group("method") or m.group("method_var") or "")
        hook_phase = _literal(m.group("phase") or m.group("phase_var") or "")
        if hook_script != _norm(script):
            continue
        if hook_method != method:
            continue
        if hook_phase and hook_phase != phase:
            continue
        cb = m.group("cb").strip()
        if cb not in out:
            out.append(cb)
    return out


def _collect_const_strings(text: str) -> dict[str, str]:
    """收集 `const X := "路径"` 这类字符串常量，供 add_method_hook 变量参数解析。"""
    out: dict[str, str] = {}
    for m in re.finditer(
        r'^(?:const|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*\w+\s*)?:?=\s*"([^"]*)"',
        text,
        re.M,
    ):
        out[m.group(1)] = m.group(2)
    return out


def extract_writes(
    source: str,
    ext_id: str,
    script: str,
    method: str,
    phase: str,
    *,
    callback: str | None = None,
) -> WriteInfo:
    """从源码里提取某个钩子回调写的 result 键。

    ``callback`` 由 :func:`resolve_callbacks` 反查得到；不给就退化为
    「找同名函数」，那通常找不到（回调普遍是 ``_hook_xxx``）。
    """
    info = WriteInfo(ext_id=ext_id, script=script, method=method, phase=phase)
    text = _strip_comments(source)
    bodies = _function_bodies(text)

    names: list[str] = []
    if callback:
        names.append(callback)
    else:
        names.extend(resolve_callbacks(text, script, method, phase))
        if not names:
            names.append(method)  # 最后兜底：同名函数

    body = "\n".join(bodies[n] for n in names if n in bodies)
    if not body.strip():
        return info

    consts = _collect_const_lists(text)

    if _WHOLE_RE.search(body):
        info.whole = True
        info.empty = False

    for m in _KEY_INDEX_RE.finditer(body):
        info.keys.add(m.group(1))
    for m in _KEY_INDEX_INT_RE.finditer(body):
        info.keys.add(m.group(1))
    for m in _SCALE_CALL_RE.finditer(body):
        info.keys.add(m.group(1))
    for m in _SCALE_LIST_RE.finditer(body):
        for key in re.findall(r'"([^"]*)"', m.group(1)):
            info.keys.add(key)

    # for key in ATTACK_KEYS: result[key] = ...
    for m in _FOR_CONST_RE.finditer(body):
        loop_var, const_name = m.group(1), m.group(2)
        if const_name not in consts:
            continue
        uses = re.search(r"\[\s*" + re.escape(loop_var) + r"\s*\]\s*=", body)
        if uses:
            info.keys.update(consts[const_name])

    if info.keys or info.whole:
        info.empty = False
    return info


def compare(
    infos: list[WriteInfo], phase: str = "", priorities: dict[str, int] | None = None
) -> tuple[str, str]:
    """给出结论。

    ``priorities`` 是「扩展 id -> 该点上的注册优先级」。数字小的先跑
    （见 ``hook_runtime.register`` 的升序排序）。**只有传了它**，才能判
    「谁排在 skip 后面、会不会被吞」；没传就退回「有 skip 就报冲突」。

    返回 ``(level, message)``：
    - ``safe``    键完全不重叠，可安全共存
    - ``unknown`` 有一方认不出写入，需人工确认
    - ``clash``   有键真正重叠（或整份替换），会互相影响
    """
    prio = priorities or {
        i.ext_id: i.priority for i in infos if i.priority is not None
    }
    if len(infos) < 2:
        return "safe", "只有一个回调"
    if any(i.whole for i in infos):
        owners = "、".join(i.ext_id for i in infos if i.whole)
        return "clash", f"{owners} 整份替换了 result，任何键都可能被覆盖"

    # skip_original 优先于其它判定：before 相位里一旦有人设它，
    # dispatch_before 会立刻 return，**排在后面的回调整个不执行**。
    # 这比键重叠更严重 —— 不是「值被覆盖」，是「功能直接没了」。
    if phase == "before":
        skippers = [i for i in infos if i.skips_original]
        if skippers:
            names = "、".join(i.ext_id for i in skippers)
            # 知道全部优先级，才能判「到底有没有人被吞」。不知道就按最坏情况报。
            if all(i.ext_id in prio for i in infos):
                first_skip = min(prio[i.ext_id] for i in skippers)
                swallowed = [
                    i
                    for i in infos
                    if not i.skips_original and prio[i.ext_id] > first_skip
                ]
                if not swallowed:
                    # (a) 全员都是「拒绝型」跳过：result 置 false/null + skip。
                    #     谁先触发都是同一个结果（技能被拒），只差提示文案。
                    if all(i.skips_original and i.rejects for i in infos):
                        return "safe", (
                            "各回调都是拒绝型跳过（result 置 false/null + "
                            "skip_original），谁先触发结果一致，只差提示文案"
                        )
                    # (b) 有人在 skip 之前写入了入参，且 skip 一方会读该入参：
                    #     改动会被它自己消费掉（比如跳过原函数后自己扣血）。
                    writers = [
                        i for i in infos if not i.skips_original and i.arg_indexes
                    ]
                    if writers and all(i.reads_args for i in skippers):
                        who = "、".join(
                            f"{i.ext_id}（优先级 {prio[i.ext_id]}）" for i in writers
                        )
                        whom = "、".join(
                            f"{i.ext_id}（优先级 {prio[i.ext_id]}）" for i in skippers
                        )
                        return "safe", (
                            f"{who} 改写入参排在 {whom} 的 skip 之前，"
                            "且 skip 分支会读取该入参，改动仍会被采纳"
                        )
            # **注意：这通常是有条件的。**
            #
            # 实测 ``mch.treasure_dup``：它只在「异能空间内使用被禁技能 /
            # 治疗药水」这类分支里才设 skip_original。绝大多数情况下不触发，
            # 同点其它回调正常执行。
            #
            # 但静态分析看不出触发条件（那是运行期的业务判断），所以这里
            # 仍判 clash —— 宁可提醒一次让人去核对，也不要静默放过一个
            # 「特定场景下别人功能消失」的隐患。
            #
            # 措辞必须写清是「可能 / 在某些情况下」，不能让人以为无条件失效。
            return "clash", (
                f"{names} 会设 skip_original=true 跳过原函数。"
                "若该分支触发，排在它后面的回调不会执行，同点其它扩展届时失效；"
                "静态分析看不出触发条件，请核对是否只在特定场景（如禁用某技能）"
                "才走到这一步"
            )

    # 只有「既没解析出写入、又证明不了它只碰实例」才算需人工确认。
    # 纯副作用回调（建节点、清冷却）不写返回值，和同点其它扩展天然不冲突。
    unknown = [i for i in infos if i.empty and i.opaque]
    if unknown:
        owners = "、".join(i.ext_id for i in unknown)
        return "unknown", f"{owners} 的写入没解析出来，需人工确认是否会互相影响"

    if all(i.empty for i in infos):
        return "safe", "各回调都只操作实例、不改返回值（建节点/清冷却之类），互不干扰"

    # before 相位：双方改的是同一个形参才可能互相影响
    arg_owners: dict[str, set[str]] = {}
    for info in infos:
        for idx in info.arg_indexes:
            arg_owners.setdefault(idx, set()).add(info.ext_id)
    shared_args = {k: v for k, v in arg_owners.items() if len(v) > 1}
    if shared_args:
        detail = "；".join(
            f"第 {k} 个入参被 {'、'.join(sorted(v))} 同时改"
            for k, v in sorted(shared_args.items(), key=lambda x: int(x[0]))
        )
        return "clash", f"存在真正重叠：{detail}"

    seen: dict[str, set[str]] = {}
    for info in infos:
        for key in info.keys:
            seen.setdefault(key, set()).add(info.ext_id)
    shared = {k: v for k, v in seen.items() if len(v) > 1}
    if shared:
        # 同一个键被多个包写，不等于互相覆盖：
        # 只要每处写入都是「读—改—写」（右值用到了当前值），结果就是叠加，
        # 先后顺序不影响（最多差一次取整）。真正会丢功能的是**直接赋值** ——
        # 后跑的那一下把先跑的贡献整个替换掉。
        overwritten = sorted(
            key
            for key in shared
            if any(key not in info.rmw_keys for info in infos if key in info.keys)
        )
        if overwritten:
            bits: list[str] = []
            for key in overwritten:
                who: list[str] = []
                for info in infos:
                    if key not in info.keys or key in info.rmw_keys:
                        continue
                    cond = info.conditions.get(key) or ""
                    who.append(info.ext_id + (f"（仅在 {cond} 时）" if cond else ""))
                bits.append(
                    f"键 {key} 被 {'、'.join(who)} 直接赋值（不看当前值），"
                    "会覆盖同点其它扩展对它的加成"
                )
            return "clash", "存在真正重叠：" + "；".join(bits)
        return "safe", (
            "写的是同一批键（" + "、".join(sorted(shared)) + "），"
            "但双方都是读—改—写，叠加生效，先后顺序不影响结果"
        )

    # 一方改 result、一方改入参 —— 互不相干
    if any(i.keys for i in infos) and any(i.arg_indexes for i in infos):
        return "safe", "一方改入参、一方改返回值，互不干扰"
    if all(i.arg_indexes for i in infos) and not shared_args:
        return "safe", "改的不是同一个入参，可安全共存"
    return "safe", "键不重叠，可安全共存"


# ---------------------------------------------------------------- 静态调用跟随
#
# 现网扩展普遍把钩子写成一行转发：
#
#     func _on_attributes(context: Dictionary) -> void:
#         Core.apply_to_live(context.get("result"))
#
# 真正的写入在另一个文件里，而且常常是 ``static func``（宠物守护的
# ``guard_core.gd`` 全文件都是）。旧实现只在回调体里找**同文件**同名函数，
# 跨文件只按「函数名出现过就追」兜底，于是这类包一律判「写入没解析出来」。
#
# 这里补上真正的静态调用跟随：解析 ``preload`` / ``class_name`` 找到目标
# 文件，再顺着 ``X.y(...)`` 逐层展开。键不是字面量时（``attributes[SLOTS[i].attr]``）
# 再从常量表把取值展开成具体键。

_PRELOAD_RE = re.compile(
    r'^(?:const|var)\s+([A-Za-z_]\w*)\s*(?::\s*[\w\[\]]+)?\s*:?=\s*'
    r'preload\s*\(\s*"([^"]+)"',
    re.M,
)
_CLASS_NAME_RE = re.compile(r"^[ \t]*class_name\s+([A-Za-z_]\w*)", re.M)
_CALL_WITH_RECV_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(")
_PLAIN_CALL_RE = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\s*\(")
#: 一次分析最多展开多少个函数体（防病态包把整包拖进来）
_MAX_BODIES = 40

#: `const SLOTS := [{"attr": "2005"}, ...]`
_CONST_ARRAY_RE = re.compile(
    r"^(?:const|var)\s+([A-Za-z_]\w*)\s*(?::\s*[\w\[\]]+)?\s*:?=\s*\[", re.M
)
#: `var attr: String = SLOTS[index].attr`
_ASSIGN_VALUE_RE = re.compile(
    r"^[ \t]*(?:var|const)\s+([A-Za-z_]\w*)\s*(?::\s*[\w\[\]]+)?\s*:?=\s*(.+?)[ \t]*$",
    re.M,
)
_INDEX_FIELD_RE = re.compile(r"^([A-Za-z_]\w*)\s*\[[^\]]*\]\s*(?:\.\s*([A-Za-z_]\w*))?")
#: `_term_attrs.append(attr)` —— 数组元素来源
_APPEND_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*append\s*\(\s*([A-Za-z_]\w*)\s*\)")

#: 带 dict 名的写入（键是字面量 / 整数 / 变量）
_KEY_WRITE_RE = re.compile(
    r"\b(" + "|".join(RESULT_NAMES) + r')\s*\[\s*"([^"]+)"\s*\]\s*=(?!=)'
)
_KEY_WRITE_INT_RE = re.compile(
    r"\b(" + "|".join(RESULT_NAMES) + r")\s*\[\s*(\d+)\s*\]\s*=(?!=)"
)
_KEY_WRITE_VAR_RE = re.compile(
    r"\b(" + "|".join(RESULT_NAMES) + r")\s*\[\s*([A-Za-z_]\w*)\s*\]\s*=(?!=)"
)
#: 从 result 读出来的局部变量：`var base: float = float(attributes.get(attr, 0))`
_DERIVED_RE = re.compile(
    r"(?:var\s+)?([A-Za-z_]\w*)\s*(?::\s*[\w\[\]]+)?\s*:?=\s*[^\n]*\b(?:"
    + "|".join(RESULT_NAMES)
    + r")\s*(?:\.\s*get\s*\(|\[)"
)


def _bracket_body(text: str, open_idx: int) -> str:
    """从 `[` 起截到配对的 `]`（跳过字符串里的括号）。"""
    depth = 0
    quote = ""
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
        i += 1
    return text[open_idx + 1 :]


def _const_tables(
    text: str,
) -> tuple[dict[str, list[str]], dict[str, dict[str, list[str]]]]:
    """收集常量表：``(字符串列表, 字典列表)``。

    字典列表示例::

        const SLOTS := [{"attr": "2005"}, {"attr": "2007"}]
        -> {"SLOTS": {"attr": ["2005", "2007"]}}

    这是「键来自常量表」（``attributes[SLOTS[i].attr] = ...``）的唯一线索 ——
    键不是字面量，只看赋值语句永远猜不到。
    """
    lists: dict[str, list[str]] = {}
    dicts: dict[str, dict[str, list[str]]] = {}
    for m in _CONST_ARRAY_RE.finditer(text):
        name = m.group(1)
        body = _bracket_body(text, m.end() - 1)
        fields: dict[str, list[str]] = {}
        for chunk in re.findall(r"\{([^{}]*)\}", body):
            for fm in re.finditer(r'"?([A-Za-z_]\w*)"?\s*:\s*"([^"]*)"', chunk):
                fields.setdefault(fm.group(1), []).append(fm.group(2))
        if fields:
            dicts[name] = fields
            continue
        items = re.findall(r'"([^"]*)"', body)
        if items:
            lists[name] = items
    return lists, dicts


def _alias_values(
    texts: list[str],
    lists: dict[str, list[str]],
    dicts: dict[str, dict[str, list[str]]],
) -> dict[str, set[str]]:
    """局部变量 -> 它可能取到的键值。

    典型链路（宠物守护）::

        const SLOTS := [{"attr": "2005"}, ...]
        var attr: String = SLOTS[index].attr
        _term_attrs.append(attr)            # 存进成员数组
        ...
        var attr: String = _term_attrs[index]   # 再从数组读出来
        attributes[attr] = ...                  # 键就是这个 attr

    跑几轮让「赋值 -> 追加 -> 再读出」的链收敛。
    """
    values: dict[str, set[str]] = {}
    for _ in range(3):
        for text in texts:
            for m in _ASSIGN_VALUE_RE.finditer(text):
                var, expr = m.group(1), m.group(2).strip()
                hit = _INDEX_FIELD_RE.match(expr)
                if not hit:
                    continue
                base, field = hit.group(1), hit.group(2)
                pool = dicts.get(base, {}).get(field, []) if field else lists.get(base, [])
                if not pool and base in values:
                    pool = sorted(values[base])
                if pool:
                    values.setdefault(var, set()).update(pool)
        for text in texts:
            for m in _APPEND_RE.finditer(text):
                target, arg = m.group(1), m.group(2)
                if arg in values:
                    values.setdefault(target, set()).update(values[arg])
    return values


def _derived_locals(text: str) -> set[str]:
    """从 result 读出来的局部变量名（写回时算「读—改—写」的证据）。"""
    return {m.group(1) for m in _DERIVED_RE.finditer(text)}


def _rhs_at(text: str, pos: int) -> str:
    """取 pos 之后到行尾的文本（赋值的右值）。"""
    end = text.find("\n", pos)
    return text[pos : end if end != -1 else len(text)]


def _is_rmw(rhs: str, dname: str, derived: set[str]) -> bool:
    """右值是否用到了「当前值」。

    用到 = 读—改—写，多个包叠加时不会互相抹掉；用不到 = 直接赋值，
    后跑的会覆盖先跑的。
    """
    if dname and re.search(r"\b" + re.escape(dname) + r"\s*(?:\.\s*get\s*\(|\[)", rhs):
        return True
    return any(re.search(r"\b" + re.escape(name) + r"\b", rhs) for name in derived)


def _enclosing_condition(body: str, pos: int) -> str:
    """写入语句所在的最内层 if/elif 条件；无条件返回空串。

    只用来把告警说清楚（「仅在开启某设置时才会覆盖」），不参与判定。
    """
    offset = 0
    idx = 0
    for i, line in enumerate(body.splitlines(keepends=True)):
        if offset + len(line) > pos:
            idx = i
            break
        offset += len(line)
    lines = body.splitlines()
    if idx >= len(lines):
        return ""
    cur_indent = len(lines[idx]) - len(lines[idx].lstrip())
    for j in range(idx - 1, -1, -1):
        raw = lines[j]
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent < cur_indent and stripped.startswith(("if ", "elif ")):
            return stripped.rstrip(":")
    return ""


def _block_after(text: str, pos: int) -> str:
    """取 `for ...:` 之后缩进更深的循环体。"""
    lines = text[pos:].splitlines()
    out: list[str] = []
    base: int | None = None
    for raw in lines[1:]:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        if base is None:
            if indent == 0:
                continue
            base = indent
        if indent < base:
            break
        out.append(raw)
    return "\n".join(out)


class _PackageIndex:
    """一个扩展包内的符号表：文件、函数体、preload/class_name 映射。"""

    def __init__(self, files: dict[Path, str]) -> None:
        self.texts = {p: _strip_comments(t) for p, t in files.items()}
        self.bodies = {p: _function_bodies(t) for p, t in self.texts.items()}
        self.class_names: dict[str, Path] = {}
        for path, text in self.texts.items():
            m = _CLASS_NAME_RE.search(text)
            if m:
                self.class_names.setdefault(m.group(1), path)
        self.preloads: dict[Path, dict[str, Path]] = {}
        for path, text in self.texts.items():
            table: dict[str, Path] = {}
            for m in _PRELOAD_RE.finditer(text):
                target = self._locate(m.group(2), path)
                if target is not None:
                    table[m.group(1)] = target
            self.preloads[path] = table

    def _locate(self, raw: str, ref: Path) -> Path | None:
        """把 ``res://...#x.gd`` / ``x.gd`` 解析成包内文件。"""
        rel = raw.split("#", 1)[0].strip().replace("\\", "/")
        if not rel:
            return None
        if rel.startswith("res://"):
            rel = rel[len("res://") :]
        cand = ref.parent / rel
        for path in self.texts:
            if path == cand:
                return path
        hits = [
            p for p in self.texts if p.as_posix().endswith("/" + rel) or p.name == rel
        ]
        return hits[0] if len(hits) == 1 else None

    def find_home(self, callback: str) -> Path | None:
        for path, bodies in self.bodies.items():
            if callback in bodies:
                return path
        return None

    def resolve(self, receiver: str | None, name: str, home: Path) -> tuple[Path, str] | None:
        """把一次调用解析成 ``(文件, 函数名)``；解析不出返回 None。"""
        if receiver in ("self", "super"):
            return (home, name) if name in self.bodies.get(home, {}) else None
        if receiver:
            target = self.preloads.get(home, {}).get(receiver) or self.class_names.get(receiver)
            if target is not None and name in self.bodies.get(target, {}):
                return (target, name)
            # 成员变量持有别的脚本实例（`stats.apply_player_attributes(...)`）：
            # 静态类型推不出来，只有全包同名方法唯一时才敢跟。
        if not receiver and name in self.bodies.get(home, {}):
            return (home, name)
        hits = [
            (path, name)
            for path, bodies in self.bodies.items()
            if name in bodies and not (path == home and not receiver)
        ]
        return hits[0] if len(hits) == 1 else None

    def closure(self, home: Path, callback: str, depth: int = 4) -> list[tuple[Path, str, str]]:
        """从回调出发按调用关系展开，返回 ``[(文件, 函数名, 函数体)]``。"""
        out: list[tuple[Path, str, str]] = []
        seen: set[tuple[Path, str]] = set()
        frontier: list[tuple[Path, str, int]] = [(home, callback, 0)]
        while frontier and len(out) < _MAX_BODIES:
            path, name, level = frontier.pop(0)
            if (path, name) in seen:
                continue
            seen.add((path, name))
            body = self.bodies.get(path, {}).get(name)
            if body is None:
                continue
            out.append((path, name, body))
            if level >= depth:
                continue
            for recv, fn in _CALL_WITH_RECV_RE.findall(body):
                tgt = self.resolve(recv, fn, path)
                if tgt is not None and tgt not in seen:
                    frontier.append((tgt[0], tgt[1], level + 1))
            for fn in _PLAIN_CALL_RE.findall(body):
                tgt = self.resolve(None, fn, path)
                if tgt is not None and tgt not in seen:
                    frontier.append((tgt[0], tgt[1], level + 1))
        return out


def extract_writes_deep(
    files: dict[Path, str],
    ext_id: str,
    script: str,
    method: str,
    phase: str,
    callback: str,
    *,
    depth: int = 4,
) -> WriteInfo:
    """提取回调写入，并顺着静态调用逐层展开到 ``depth`` 层。

    两种转发都要跟：

    * 同文件的成员方法 —— ``_hook_attributes`` -> ``_apply``；
    * 另一个脚本的方法 —— ``Core.apply_to_live(result)``，其中
      ``const Core := preload("guard_core.gd")``。

    键可能不是字面量（``attributes[SLOTS[i].attr] = ...``），所以还要把常量
    表里的取值展开成具体键。反过来，**解不出键就保持 empty**：宁可报
    「需人工确认」，也不假装安全。
    """
    info = WriteInfo(ext_id=ext_id, script=script, method=method, phase=phase)
    index = _PackageIndex(files)
    home = index.find_home(callback)
    if home is None:
        return info

    chain = index.closure(home, callback, depth)
    merged = "\n".join(body for _p, _n, body in chain)
    if not merged.strip():
        return info

    lists: dict[str, list[str]] = {}
    dicts: dict[str, dict[str, list[str]]] = {}
    for text in index.texts.values():
        one_lists, one_dicts = _const_tables(text)
        lists.update(one_lists)
        dicts.update(one_dicts)
    aliases = _alias_values([body for _p, _n, body in chain], lists, dicts)
    derived = _derived_locals(merged)

    writes: dict[str, list[bool]] = {}

    def record(key: str, rmw: bool, cond: str = "") -> None:
        info.keys.add(key)
        writes.setdefault(key, []).append(rmw)
        if not rmw and cond:
            info.conditions.setdefault(key, cond)

    for m in _KEY_WRITE_RE.finditer(merged):
        record(
            m.group(2),
            _is_rmw(_rhs_at(merged, m.end()), m.group(1), derived),
            _enclosing_condition(merged, m.start()),
        )
    for m in _KEY_WRITE_INT_RE.finditer(merged):
        record(
            m.group(2),
            _is_rmw(_rhs_at(merged, m.end()), m.group(1), derived),
            _enclosing_condition(merged, m.start()),
        )
    for m in _KEY_WRITE_VAR_RE.finditer(merged):
        var = m.group(2)
        if var not in aliases:
            continue  # 键名变量解不出来：不猜（保持 empty 或只算已认出的键）
        rhs = _rhs_at(merged, m.end())
        cond = _enclosing_condition(merged, m.start())
        for key in sorted(aliases[var]):
            record(key, _is_rmw(rhs, m.group(1), derived), cond)

    # scale 类封装：`_scale_key(result, "2005", 2.0, 1)` 是标准的读—改—写
    for m in _SCALE_CALL_RE.finditer(merged):
        record(m.group(1), True)
    for m in _SCALE_LIST_RE.finditer(merged):
        for key in re.findall(r'"([^"]*)"', m.group(1)):
            record(key, True)

    # for key in ATTACK_KEYS: _scale_key(result, key, ...) / result[key] = ...
    for m in _FOR_CONST_RE.finditer(merged):
        loop_var, const_name = m.group(1), m.group(2)
        pool = lists.get(const_name)
        if not pool:
            continue
        block = _block_after(merged, m.end())
        if not block:
            continue
        if re.search(r"_scale\w*\s*\(", block):
            for key in pool:
                record(key, True)
            continue
        wm = re.search(
            r"([A-Za-z_]\w*)\s*\[\s*" + re.escape(loop_var) + r"\s*\]\s*=(?!=)",
            block,
        )
        if wm:
            rhs = _rhs_at(block, wm.end())
            cond = _enclosing_condition(block, wm.start())
            for key in pool:
                record(key, _is_rmw(rhs, wm.group(1), derived), cond)

    if _WHOLE_RE.search(merged):
        info.whole = True
    if _SKIP_RE.search(merged):
        info.skips_original = True
    for m in _ARG_INDEX_RE.finditer(merged):
        info.arg_indexes.add(m.group(1))

    # 顺序判定用的三个判据（见 compare）。都是「正面证据」，
    # 认不出就保持默认值，结论仍是保守的那一侧。
    info.reads_args = bool(_READS_ARGS_RE.search(merged))
    info.rejects = info.skips_original and bool(_REJECT_RE.search(merged))
    info.opaque = not _instance_only(merged)

    # 每个键都必须是「读—改—写」才算叠加；有任何一处直接赋值就记成覆盖
    info.rmw_keys = {k for k, flags in writes.items() if all(flags)}
    info.empty = not (info.keys or info.whole or info.arg_indexes or info.skips_original)
    return info


# ---------------------------------------------------------------- 包级分析


@dataclass
class PointAnalysis:
    """一个共享钩子点的分析结果。"""

    script: str
    method: str
    phase: str
    infos: list[WriteInfo] = field(default_factory=list)
    level: str = "unknown"
    message: str = ""

    def owners(self) -> list[str]:
        return [i.ext_id for i in self.infos]


def analyze_point(
    src_roots: dict[str, Path],
    script: str,
    method: str,
    phase: str,
    owners: list[str],
) -> PointAnalysis:
    """在「包源码根 -> 目录」给了的前提下，分析一个共享钩子点。

    回调可能在包内**另一个文件**里（更好的体验就是：钩子在 main.gd 注册，
    实际写入在 modules/stats/stats_module.gd）。所以这里先全包扫一遍
    `add_method_hook` 定位注册文件，再按回调名在整包里找函数体。

    注册写法要认两种：字面量调用，以及「数组表 + for 循环」那种
    （``for hook in hooks: add_method_hook(hook[0], ...)``，六联浮游炮就是
    这么写的）。后者只有 :mod:`hook_scan` 认得出来——旧实现只用
    ``resolve_callbacks``，于是那类包永远解析不到回调、一律报「需人工确认」。
    """
    # 局部导入：hook_scan 不依赖本模块，但顶层互导会让 CLI 冷启动多绕一圈
    from . import hook_scan

    result = PointAnalysis(script=script, method=method, phase=phase)
    priorities: dict[str, int] = {}

    for ext_id in sorted(set(owners)):
        root = src_roots.get(ext_id)
        if root is None or not root.is_dir():
            result.infos.append(
                WriteInfo(ext_id=ext_id, script=script, method=method, phase=phase)
            )
            continue

        files = sorted(root.rglob("*.gd"))
        callbacks: list[str] = []
        for f in files:
            text = f.read_text("utf-8", "replace")
            # 先走 hook_scan：它能展开表驱动注册，还带优先级
            hits = [
                s
                for s in hook_scan.scan_source(text).sites
                if s.script == script.replace("res://", "").strip()
                and s.method == method
                and s.phase == phase
            ]
            for site in hits:
                if site.callback and site.callback not in callbacks:
                    callbacks.append(site.callback)
                if site.priority is not None:
                    old = priorities.get(ext_id)
                    priorities[ext_id] = (
                        site.priority if old is None else min(old, site.priority)
                    )
            if callbacks:
                break
            # 兜底：常量表里的脚本路径写法等 hook_scan 没覆盖到的情况
            for name in resolve_callbacks(text, script, method, phase):
                if name not in callbacks:
                    callbacks.append(name)
            if callbacks:
                break

        if not callbacks:
            result.infos.append(
                WriteInfo(ext_id=ext_id, script=script, method=method, phase=phase)
            )
            continue

        texts = {f: f.read_text("utf-8", "replace") for f in files}
        info = WriteInfo(ext_id=ext_id, script=script, method=method, phase=phase)
        overwritten: set[str] = set()
        for name in callbacks:
            single = extract_writes_deep(
                texts, ext_id, script, method, phase, name
            )
            # 逐个回调合并：所有字段都要带走，漏一个结论就会错。
            # （2026-11-05：一开始漏了 arg_indexes，把「两个包都改 arguments[0]」
            #  误判成「只读安全」；后来漏了 skips_original，把「会吞掉后者」的
            #  点误判成「可安全共存」。）
            info.keys |= single.keys
            info.arg_indexes |= single.arg_indexes
            info.whole = info.whole or single.whole
            info.skips_original = info.skips_original or single.skips_original
            info.rmw_keys |= single.rmw_keys
            info.conditions.update(single.conditions)
            info.reads_args = info.reads_args or single.reads_args
            info.rejects = info.rejects or single.rejects
            info.opaque = info.opaque and single.opaque
            overwritten |= single.keys - single.rmw_keys
            info.empty = info.empty and single.empty
        # 「读—改—写」按最严格的口径：任何一处是直接赋值，就不算叠加
        info.rmw_keys -= overwritten
        info.priority = priorities.get(ext_id)
        result.infos.append(info)

    result.level, result.message = compare(result.infos, phase, priorities)
    return result