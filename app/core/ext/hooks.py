"""方法钩子插桩器。

原版包里脚本只有 ``.gdc``，运行时无法猴补丁，因此钩子必须在构建期写进源码：
把目标方法体改写成「先派发钩子、再跑原逻辑」，然后整份脚本作为覆盖层挂上去
（覆盖时必须同时写自指 remap，见 :mod:`app.core.pck.writer`）。

派发约定（与 ModHost 的 ``api.add_method_hook`` 对齐）：

``before``
    方法开头调用 ``HookRuntime.dispatch_before(script, method, ctx)``；
    处理函数把 ``ctx["skip_original"] = true`` 时直接 return，跳过原逻辑。

    跳过时的返回值：

    - 无返回值 → ``return``
    - 对象类型（``Node2D`` 等）→ ``return null``（调用方普遍不看返回值）
    - 内建值类型（``int`` / ``Dictionary`` …）→ **不加跳过守卫**，只派发

``after``
    在顶层 ``return`` 之前捕获返回值，派发后回写::

        var __hr_ret_1 := <原表达式>
        var __hr_ctx_1 := {"result": __hr_ret_1}
        HookRuntime.dispatch_after("scripts/x.gd", "m", __hr_ctx_1)
        return __hr_ctx_1.get("result", __hr_ret_1)

无 ``return`` 的 void 方法，在函数体末尾补一次派发。

另外提供 :func:`instrument_social`：给 ``hud.gd`` 的 ``"callPlugin"`` 分支插一段
「先查注册表，未命中再走原 match」，这是所有 HUD 类扩展的公共需求。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FUNC_HEAD = re.compile(r"^(?P<indent>[ \t]*)func\s+(?P<name>[A-Za-z_]\w*)\s*\(")

#: 不能返回 null 的内建值类型——这些方法即使被 before 拦截也不能跳过
_BUILTIN_VALUE = {
    "int", "float", "bool", "String", "StringName", "NodePath",
    "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4",
    "Color", "Rect2", "Rect2i", "Transform2D", "Transform3D",
    "Quaternion", "Basis", "Plane", "AABB", "PackedByteArray",
    "PackedInt32Array", "PackedInt64Array", "PackedFloat32Array",
    "PackedFloat64Array", "PackedStringArray", "PackedVector2Array",
    "PackedVector3Array", "Array", "Dictionary",
}

#: 可能跨行的表达式结尾（这类 return 不做捕获，避免改坏）
_RISKY_TAIL = ("(", "[", "{", ",", "\\", "+", "-", "and", "or", "if")


def _brackets_balanced(expr: str) -> bool:
    """表达式自身的括号是否配平。

    ``return`` 的表达式可能**借用了调用方的收尾括号**，典型是 lambda：

    .. code-block:: gdscript

        order.sort_custom( func(a, b):
            if a == b:
                return a < b
            return int(x) > int(y))      # 末尾那个 ')' 是 sort_custom 的

    按 ``,`` ``, ``(`` 之类的结尾判断抓不到这种形态：表达式最后一个字符是
    ``)``，看着"完整"，实际把外面的右括号吞了进来。照原样改写成
    ``var __hr_ret := ...)`` 就是语法错误，整份脚本编译失败——实测
    ``scripts/slice_run.gd`` 因此炸掉，连带上层 ``training_run`` /
    ``opening_run`` 全部解析失败，游戏里表现是角色不能动、不能交互。

    所以捕获返回值前先数括号；不配平就走"不捕获"的保守分支。
    """
    depth = 0
    quote = ""
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


@dataclass(frozen=True)
class Hook:
    script: str      # "scripts/game.gd"
    method: str
    phase: str       # before / after


@dataclass
class _Func:
    name: str
    indent: str
    params: str
    ret: str
    sig_end: int     # 签名最后一行（含）


def _scan_functions(lines: list[str]) -> list[_Func]:
    """扫描函数定义，支持跨行签名（参数表括号跨行）。"""
    out: list[_Func] = []
    i = 0
    total = len(lines)
    while i < total:
        m = _FUNC_HEAD.match(lines[i])
        if not m:
            i += 1
            continue
        indent = m.group("indent")
        name = m.group("name")
        # 从 '(' 开始累积，直到括号配平且行尾是 ':'
        depth = 0
        buf: list[str] = []
        j = i
        started = False
        while j < total:
            text = lines[j]
            if j == i:
                text = text[m.end() - 1 :]  # 从 '(' 起
            buf.append(text)
            for ch in text:
                if ch == "(":
                    depth += 1
                    started = True
                elif ch == ")":
                    depth -= 1
            if started and depth <= 0 and lines[j].rstrip().endswith(":"):
                break
            j += 1
            if j - i > 12:
                break
        joined = "\n".join(buf)
        lp = joined.find("(")
        rp = joined.rfind(")")
        params = joined[lp + 1 : rp] if lp >= 0 and rp > lp else ""
        ret = ""
        if rp >= 0:
            tail = joined[rp + 1 :]
            arrow = tail.find("->")
            if arrow >= 0:
                ret = tail[arrow + 2 :].split(":")[0].strip()
        out.append(_Func(name=name, indent=indent, params=params, ret=ret, sig_end=j))
        i = j + 1
    return out


def _indent_unit(lines: list[str], body_start: int, func_indent: str) -> str:
    for line in lines[body_start:]:
        if not line.strip():
            continue
        prefix = line[: len(line) - len(line.lstrip())]
        if len(prefix) > len(func_indent):
            return prefix[len(func_indent):]
        break
    return "\t"


def _find_block(lines: list[str], sig_end: int, func_indent: str) -> tuple[int, int]:
    """返回 (body_start, body_end)，``body_end`` 为独占上界。"""
    body_start = sig_end + 1
    end = len(lines)
    for i in range(sig_end + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        indent = line[: len(line) - len(line.lstrip())]
        if len(indent) <= len(func_indent):
            end = i
            break
    return body_start, end


def _skip_docstring(lines: list[str], body_start: int, body_end: int) -> int:
    i = body_start
    while i < body_end and not lines[i].strip():
        i += 1
    if i >= body_end:
        return body_start
    stripped = lines[i].lstrip()
    if not (stripped.startswith('"""') or stripped.startswith("'''")):
        return body_start
    quote = stripped[:3]
    if stripped.count(quote) >= 2:
        return i + 1
    i += 1
    while i < body_end:
        if quote in lines[i]:
            return i + 1
        i += 1
    return body_end


def _param_names(params: str) -> list[str]:
    names: list[str] = []
    depth = 0
    current = ""
    for ch in params:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            names.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        names.append(current)
    out: list[str] = []
    for raw in names:
        head = raw.split(":")[0].split("=")[0].strip()
        if head and head.isidentifier() and head != "self":
            out.append(head)
    return out


def _args_expr(names: list[str]) -> str:
    if not names:
        return "{}"
    return "{" + ", ".join(f'"{n}": {n}' for n in names) + "}"


def _args_array_expr(names: list[str]) -> str:
    """位置参数数组：``[a, b, c]``。

    为什么另起一个：``_args_expr`` 产出的是**命名参数字典**
    （``{"param": param}``），但扩展是按**下标**取参的——
    ``var args: Array = context.get("arguments", [])`` 之后
    ``args[0]`` / ``args[2]`` / ``args[3]`` 这样用。
    键名也不一样：字典那个叫 ``args``，扩展读的是 ``arguments``。
    """
    if not names:
        return "[]"
    return "[" + ", ".join(names) + "]"


def _is_return_line(line: str) -> bool:
    """这一行是否是 ``return`` / ``return expr``。

    排除 ``return_value = 1`` 这类「标识符恰好以 return 开头」的赋值。
    """
    stripped = line.strip()
    if not stripped.startswith("return"):
        return False
    rest = stripped[len("return"):]
    return not rest or rest[0] in " \t"


def _body_ends_with_return(lines: list[str], body_end: int) -> bool:
    """函数体最后一条有效语句是否已经是 return。

    已经以 return 结尾时不用再补派发，否则会多派发一次。
    """
    for i in range(body_end - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        return _is_return_line(lines[i])
    return False


#: 值类型的「跳过原方法」返回语句。
#:
#: 以前这里对 ``int`` / ``bool`` / ``String`` 一律返回 None（"值类型不能返回 null，
#: 所以不支持跳过"）。但扩展里靠 ``skip_original`` 生效的恰恰多是这些方法——
#: 技能解锁（``learned_skill_id`` 返回 int）、施放拦截（``request_skill`` 返回 bool）。
#: 不支持等于钩子静默失效：钩子算了半天写进 ``context.result``，原方法照样跑完把结果盖掉。
#:
#: 这里按类型给出带 cast 的零值返回：钩子没写 result 时落到该类型的空值，
#: 写错类型也会被转成合法值，不会让调用方拿到 null 崩掉。
_SKIP_BY_VALUE: dict[str, str] = {
    "int": 'return int(__hr_ctx.get("result", 0))',
    "float": 'return float(__hr_ctx.get("result", 0.0))',
    "bool": 'return bool(__hr_ctx.get("result", false))',
    "String": 'return str(__hr_ctx.get("result", ""))',
    "StringName": 'return StringName(__hr_ctx.get("result", ""))',
    "NodePath": 'return NodePath(__hr_ctx.get("result", ""))',
    "Array": 'return __hr_ctx.get("result") if __hr_ctx.get("result") is Array else []',
    "Dictionary": (
        'return __hr_ctx.get("result") if __hr_ctx.get("result") is Dictionary else {}'
    ),
}


def _skip_return(ret: str) -> str | None:
    """按返回类型决定"跳过原方法"时该 return 什么；None 表示不能跳过。"""
    ret = ret.strip()
    if ret in ("", "void"):
        return "return"
    skip = _SKIP_BY_VALUE.get(ret)
    if skip is not None:
        return skip
    if ret in _BUILTIN_VALUE:
        return None
    return "return null"


def has_method(source: str, method: str) -> bool:
    """这份 GDScript 源码里有没有定义 ``method`` 这个函数。

    给检查器用（``ext.inspect``）：扩展声明「要在 game.gd 的 receive_loot
    上挂钩子」，但那个方法可能根本不叫这个名字（作者记错、或目标版本改了
    方法名）。那种情况下插桩会静默跳过，扩展装上了但功能一句不生效 ——
    玩家看到的是「装了没反应」，比报错更难查。

    只看函数定义（``func name(``），不看调用。跨行签名由
    :func:`_scan_functions` 处理，所以这里直接复用它，保证与插桩时的
    判定完全一致 —— 两处各写一套正则迟早会漂移。
    """
    if not method:
        return False
    lines = source.splitlines()
    return any(fn.name == method for fn in _scan_functions(lines))


def instrument_script(
    source: str,
    hooks: list[Hook],
    script: str,
    *,
    extended_ctx: bool = False,
) -> tuple[str, list[str]]:
    """对一份 GDScript 源码插桩，返回 (新源码, 警告列表)。

    ``extended_ctx`` 打开后会往 context 里多注入两个键：

    - ``instance`` —— 被钩方法所在的宿主节点（方法体内的 ``self``）
    - ``arguments`` —— 被钩方法的**位置参数数组**

    为什么需要（实测 145 处扩展代码在读它们）
    -------------------------------------------

    扩展普遍这么写：

    .. code-block:: gdscript

        var interact: Node = context.get("instance")
        if not is_instance_valid(interact) or int(interact.pull_ticks) <= 0:
            return
        var args: Array = context.get("arguments", [])
        if args.size() < 4 or args[3] != run.player:
            return

    而原来注入的 context 只有 ``{"script", "method", "args"}`` 三个键
    —— ``instance`` / ``arguments`` **从来就不存在**。于是
    ``is_instance_valid(null)`` 恒为 false，回调第一行就 return，
    **整段钩子代码是死代码**。

    实测 ``ai_act.combat_workbench`` 有 65 处读 ``instance``、45 处读
    ``arguments``：它的战斗手感、怪物受伤钩子**从来没真正执行过**。
    扩展作者看到「钩子注册成功」就以为功能生效了。

    ``self`` 的安全性：``_FUNC_HEAD`` 的正则是 ``^[ \\t]*func``，
    ``static func`` **匹配不上** → static 方法不会被扫到、不会被插桩，
    所以不会出现「static 方法里没有 self」的语法错误。
    """
    lines = source.splitlines()
    warnings: list[str] = []
    by_method: dict[str, list[Hook]] = {}
    for h in hooks:
        if h.script.replace("res://", "") != script.replace("res://", ""):
            continue
        by_method.setdefault(h.method, []).append(h)
    if not by_method:
        return source, warnings

    targets = [f for f in _scan_functions(lines) if f.name in by_method]
    found = {f.name for f in targets}
    # 声明了钩子但脚本里没有这个函数：必须报出来。静默跳过会让扩展「装上但完全不生效」，
    # 而运行期只会看到 add_method_hook 返回 false，很难联想到是方法名写错了。
    for missing in sorted(set(by_method) - found):
        warnings.append(
            f"{script}::{missing} 在源码里找不到同名函数（钩子不会生效，检查方法名与脚本路径）"
        )
    # 从后往前改，避免行号漂移
    for func in reversed(targets):
        name = func.name
        func_indent = func.indent
        ret_type = func.ret
        body_start, body_end = _find_block(lines, func.sig_end, func_indent)
        if body_end <= body_start:
            warnings.append(f"{script}::{name} 没有函数体，跳过")
            continue
        unit = _indent_unit(lines, body_start, func_indent)
        inner = func_indent + unit
        phases = {h.phase for h in by_method[name]}
        param_names = _param_names(func.params)
        args = _args_expr(param_names)
        args_array = _args_array_expr(param_names)
        key = f'"{script}", "{name}"'

        # context 里除 result 之外的公共字段。
        # extended_ctx 打开时多带 instance（被钩方法所在的宿主节点，即方法体
        # 内的 self）与 arguments（位置参数数组）——扩展普遍按这两个键取数据，
        # 缺了它们的钩子代码是死代码（实测 145 处）。
        extra = ""
        if extended_ctx:
            extra = f'"instance": self, "arguments": {args_array}, '

        # ---------------- before
        if "before" in phases:
            insert_at = _skip_docstring(lines, body_start, body_end)
            block = [
                f'{inner}var __hr_ctx := {{"script": "{script}", "method": "{name}", '
                f'"args": {args}, {extra}}}',
                f"{inner}HookRuntime.dispatch_before({key}, __hr_ctx)",
            ]
            skip = _skip_return(ret_type)
            if skip is None:
                warnings.append(
                    f"{script}::{name} 返回 {ret_type}（值类型），before 钩子不提供跳过能力"
                )
            else:
                block += [
                    f'{inner}if bool(__hr_ctx.get("skip_original", false)):',
                    f"{inner}{unit}{skip}",
                ]
            # 参数回写：`arguments` 是派发时新建的 Array，钩子改它只是改了数组元素，
            # **不会**影响函数形参（int 等是值拷贝）。不回写的话，「before 钩子调整
            # 调用参数」这类用法全部静默失效——浮游炮大招改 pose 就踩在这里：
            # 钩子算好了 pose 写回 arguments[0]，原方法仍然用旧 value，而护甲层
            # 没有该 motion，update_frames 里 frame_index 越界直接 sprite.hide()，
            # 表现就是「放大招时角色（或部分部件）消失」。
            if param_names:
                n = len(param_names)
                block += [
                    f'{inner}var __hr_args: Variant = __hr_ctx.get("arguments")',
                    f"{inner}if __hr_args is Array and (__hr_args as Array).size() >= {n}:",
                ]
                for idx, pname in enumerate(param_names):
                    block.append(f"{inner}{unit}{pname} = __hr_args[{idx}]")
            lines[insert_at:insert_at] = block
            body_end += len(block)

        # ---------------- after
        if "after" in phases:
            # 顺序很重要：**先**在函数末尾补派发，**再**改写 return。
            # 反过来说：改写 return 会让行数变化（1 行变 4 行），此时 body_end
            # 已失效，补的派发会插到 return 前面，生成语法错误（踩过）。
            tail_needed = not _body_ends_with_return(lines, body_end)
            if tail_needed:
                if ret_type in ("", "void"):
                    # 末尾补派发：原来是把 context 内联在调用里，加 instance /
                    # arguments 就得先落成变量（行数变了，body_end 要跟着走）。
                    tail = [
                        f'{inner}var __hr_ctx_tail := {{"result": null, {extra}}}',
                        f'{inner}HookRuntime.dispatch_after({key}, __hr_ctx_tail)',
                    ]
                    lines[body_end:body_end] = tail
                    body_end += len(tail)
                else:
                    warnings.append(
                        f"{script}::{name} 返回 {ret_type} 但末尾没有 return，正常路径无法派发"
                    )

            # 不能只看「与函数体同缩进的 return」。函数里常见
            # ``if cond: return early`` 这种嵌套提前返回，漏掉它会让钩子在
            # 那条路径上完全不触发，而且看不出错。所以扫函数体内**所有**
            # return，按各自缩进改写。
            all_returns = [
                i for i in range(body_start, body_end) if _is_return_line(lines[i])
            ]
            for n, i in enumerate(reversed(all_returns), start=1):
                raw = lines[i].strip()
                indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                suffix = f"__hr_ret_{n}"
                ctx = f"__hr_ctx_{n}"
                if raw == "return":
                    lines[i : i + 1] = [
                        f'{indent}var {ctx} := {{"result": null, {extra}}}',
                        f"{indent}HookRuntime.dispatch_after({key}, {ctx})",
                        f"{indent}return",
                    ]
                    continue
                expr = raw[len("return") :].strip()
                if expr.endswith(_RISKY_TAIL) or not _brackets_balanced(expr):
                    warnings.append(
                        f"{script}::{name} 第 {i + 1} 行的 return 表达式可能跨行/括号不配平，已跳过捕获"
                    )
                    lines[i : i + 1] = [
                        f'{indent}var {ctx} := {{"result": null, {extra}}}',
                        f"{indent}HookRuntime.dispatch_after({key}, {ctx})",
                        lines[i],
                    ]
                    continue
                lines[i : i + 1] = [
                    f"{indent}var {suffix} = {expr}",
                    f'{indent}var {ctx} := {{"result": {suffix}, {extra}}}',
                    f"{indent}HookRuntime.dispatch_after({key}, {ctx})",
                    f'{indent}return {ctx}.get("result", {suffix})',
                ]

    return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), warnings


def instrument_social(source: str, script: str = "scripts/hud.gd") -> tuple[str, list[str]]:
    """在 ``"callPlugin":`` 分支开头插入注册表查询。

    插完之后分支变成「先问 HookRuntime，没被接管再走原来的 match」，
    这样任何 HUD 类扩展都不必再各自改 ``hud.gd``。
    """
    lines = source.splitlines()
    warnings: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != '"callPlugin":':
            continue
        branch_indent = line[: len(line) - len(line.lstrip())]
        unit = "\t"
        for nxt in lines[index + 1 :]:
            if nxt.strip():
                prefix = nxt[: len(nxt) - len(nxt.lstrip())]
                if len(prefix) > len(branch_indent):
                    unit = prefix[len(branch_indent):]
                break
        window = "\n".join(lines[max(0, index - 3) : index + 4])
        if "dispatch_social" in window:
            warnings.append(f"{script} 已插过 dispatch_social，跳过")
            return source, warnings
        inner = branch_indent + unit
        block = [
            f"{inner}# 框架注入：先查扩展注册表，没被接管再走原 match",
            f'{inner}if HookRuntime.dispatch_social(self, "callPlugin", args, plugin):',
            f"{inner}{unit}return",
        ]
        lines[index + 1 : index + 1] = block
        return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), warnings
    warnings.append(f'{script} 里找不到 "callPlugin": 分支，社交指令注册表未注入')
    return source, warnings


# ---------------------------------------------------------------- 注册点扫描

#: ``add_method_hook(脚本, 方法, 阶段)`` 的调用。
#: 第一个参数可以是字符串字面量，也可以是常量名（见 :func:`scan_registrations`）。
_REG_RE = re.compile(
    r"""add_method_hook\(\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))"""
    r"""\s*,\s*["']([^"']+)["']\s*,\s*["']([^"']+)["']""",
    re.S,
)

#: ``const NAME = "value"`` / ``const NAME := "value"``
_CONST_RE = re.compile(
    r"""^\s*const\s+([A-Za-z_][A-Za-z0-9_]*)\s*:?=\s*(?:"([^"]*)"|'([^']*)')""",
    re.M,
)


def scan_consts(source: str) -> dict[str, str]:
    """扫出源码里的字符串常量：``const NAME = "value"``。

    为什么需要：扩展常把脚本路径提成常量再传给 ``add_method_hook``——

    .. code-block:: gdscript

        const PLAYER_SCRIPT := "scripts/player.gd"
        context.add_method_hook(PLAYER_SCRIPT, "_physics_process", "before", cb, 0)

    原来���扫描器只认字符串字面量，这种写法**一个钩子都扫不出来**，
    于是那些钩子不会被插桩，运行期 ``has_point()`` 拒绝注册，
    扩展自己的 ``install()`` 返回 false → **整个扩展静默失效**
    （实测 ``ai_act.combat_workbench`` 就是这样挂掉的）。
    """
    out: dict[str, str] = {}
    for m in _CONST_RE.finditer(source):
        name = m.group(1)
        value = m.group(2) if m.group(2) is not None else m.group(3)
        if name and value is not None:
            out[name] = value
    return out


def scan_registrations(source: str, consts: dict[str, str] | None = None) -> list[Hook]:
    """从扩展源码里扫出 ``add_method_hook("脚本","方法","阶段")`` 的调用。

    清单里的 ``phase`` 只是声明性质的，真正生效的是运行期注册。
    以源码字面量为准，能避免"插桩点写成 after、注册时用 before"这种错配。

    脚本路径支持两种写法：

    * 字符串字面量 —— ``add_method_hook("scripts/player.gd", "_ready", "after", ...)``
    * 常量名 —— ``add_method_hook(PLAYER_SCRIPT, "_ready", "after", ...)``，
      其中 ``const PLAYER_SCRIPT := "scripts/player.gd"``。

    ``consts`` 可传入外部合并好的常量表（跨文件共用常量时用）；
    不传就只扫本文件的常量。

    常量名解析不出来时**不产出钩子也不报错**——静态扫描本来就是尽力而为，
    真正的把关是运行期的注册结果。
    """
    local = scan_consts(source)
    table = dict(consts) if consts else {}
    for name, value in local.items():
        table.setdefault(name, value)
    out: list[Hook] = []
    for m in _REG_RE.finditer(source):
        # 三组捕获里只有一组会命中：双引号 / 单引号 / 标识符
        script = m.group(1) or m.group(2)
        if script is None:
            # 标识符形式：查常量表
            script = table.get(m.group(3) or "")
            if not script:
                continue
        out.append(
            Hook(
                script=script.replace("res://", ""),
                method=m.group(4),
                phase=m.group(5),
            )
        )
    return out


def scan_tree(files: dict[str, str]) -> list[Hook]:
    """对一组源文件做扫描并去重。``files`` 是 相对路径 -> 内容。

    常量表是**跨文件**合并的：扩展常把脚本路径定义在一个公共文件里
    （比如 ``const PLAYER_SCRIPT := "scripts/player.gd"`` 写在
    ``combat/constants.gd``），而 ``add_method_hook`` 在另一个文件里用。
    只在单文件内查常量会漏掉这种组合。
    """
    consts: dict[str, str] = {}
    for text in files.values():
        for name, value in scan_consts(text).items():
            # 同名常量取先出现的；不同值时保留先扫到的，不做覆盖
            consts.setdefault(name, value)
    seen: set[tuple[str, str, str]] = set()
    out: list[Hook] = []
    for _name, text in files.items():
        for h in scan_registrations(text, consts):
            key = (h.script, h.method, h.phase)
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
    return out
