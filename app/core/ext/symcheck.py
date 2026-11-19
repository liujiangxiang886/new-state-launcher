"""跨版本补丁的符号依赖自检。

跨版本安装时，部分补丁可能因为原版脚本变了而没命中，于是被跳过。但被跳过的
补丁常常**提供了别的补丁要用的符号**——比如补丁 A 引入常量
``CHALLENGE_DIFFICULTY``，补丁 B 引用它。A 没命中 → B 引用了一个不存在的符号
→ 脚本解析失败 → 整个 Autoload 加载不了 → 游戏直接起不来。

这类故障在运行期才暴露（``Parse Error: Identifier not declared``），而且症状
离病因很远。这里做静态检查：补丁应用后的脚本里，如果引用了补丁没有引入、
原版脚本里也没有的标识符，就判定为「补丁之间有依赖但断了」。

做法是**符号差集**：
1. 从补丁的 ``find`` / ``replace`` 里抽出「补丁引入的标识符」；
2. 从原版脚本里抽出「本来就有的标识符」；
3. 补丁产出的脚本引用了不在 (1 ∪ 2) 里的标识符 → 判定断裂。

这是启发式检查，会有误报（漏报的更多），所以它只用来**拒绝明显断裂的安装**，
不做全量语义分析。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: GDScript 里常见的声明形式：func / var / const / class / signal / enum
_DECL = re.compile(
    r"^\s*(?:static\s+)?(?:func|var|const|class|signal|enum)\s+([A-Za-z_]\w*)",
    re.M,
)
#: 大写常量通常是补丁引入的（CHALLENGE_DIFFICULTY 这类）
_CONST_DECL = re.compile(r"^\s*const\s+([A-Z_][A-Z0-9_]*)\s*=", re.M)
#: 引用：大写常量 / 帕斯卡命名的类型
#: 前面带 ``.`` 的排除掉——``Color.WHITE``、``Vector2.ONE`` 是枚举成员，
#: 不是本脚本的标识符；``Game.ITEM_SLOT_COUNT`` 是别的类的成员，同样不算。
_USE_UPPER = re.compile(r"(?<![.\w])([A-Z][A-Z0-9_]{2,})(?!\s*\.)")
_USE_TYPE = re.compile(r"\b([A-Z][a-zA-Z0-9_]*)\b")

#: 字符串字面量里的内容不该被当成引用
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')

#: GDScript / Godot 内建与常见名字，不参与检查
_BUILTIN = {
    "Godot",
    "Node",
    "Node2D",
    "Node3D",
    "Control",
    "CanvasItem",
    "Resource",
    "RefCounted",
    "Object",
    "SceneTree",
    "Engine",
    "OS",
    "FileAccess",
    "DirAccess",
    "JSON",
    "Time",
    "RandomNumberGenerator",
    "Input",
    "InputEvent",
    "ProjectSettings",
    "Color",
    "Vector2",
    "Vector3",
    "Rect2",
    "String",
    "StringName",
    "Array",
    "Dictionary",
    "PackedStringArray",
    "Callable",
    "Signal",
    "Variant",
    "int",
    "float",
    "bool",
    "get_tree",
    "get_node",
    "print",
    "push_error",
    "push_warning",
    "load",
    "preload",
    "is_instance_valid",
    "call_deferred",
    "set_deferred",
    "emit_signal",
    "connect",
    "disconnect",
    "has_method",
    "get",
    "set",
    "call",
    "free",
    "queue_free",
    "duplicate",
    "instantiate",
    "new",
    # --- Godot 全局常量（大小写风格各异，容易被误判）
    "READ",
    "WRITE",
    "READ_WRITE",
    "CURSOR_POINTING_HAND",
    "CURSOR_ARROW",
    "CURSOR_IBEAM",
    "CURSOR_FORBIDDEN",
    "CURSOR_HELP",
    "CURSOR_MOVE",
    "CURSOR_WAIT",
    "CURSOR_CROSS",
    "FOCUS_NONE",
    "FOCUS_CLICK",
    "FOCUS_ALL",
    "KEY_V",
    "KEY_ESCAPE",
    "KEY_ENTER",
    "KEY_SPACE",
    "KEY_CTRL",
    "KEY_SHIFT",
    "KEY_ALT",
    "KEY_TAB",
    "MOUSE_FILTER_STOP",
    "MOUSE_FILTER_PASS",
    "MOUSE_FILTER_IGNORE",
    "MOUSE_BUTTON_LEFT",
    "MOUSE_BUTTON_RIGHT",
    "MOUSE_BUTTON_MIDDLE",
    "NOTIFICATION_WM_CLOSE_REQUEST",
    "NOTIFICATION_WM_SIZE_CHANGED",
    "NOTIFICATION_WM_GO_BACK_REQUEST",
    "NOTIFICATION_WM_MOUSE_ENTER",
    "NOTIFICATION_WM_MOUSE_EXIT",
    "NOTIFICATION_PREDELETE",
    "NOTIFICATION_ENTER_TREE",
    "NOTIFICATION_READY",
    "NOTIFICATION_EXIT_TREE",
    "NOTIFICATION_PROCESS",
    "NOTIFICATION_PHYSICS_PROCESS",
    "NOTIFICATION_CRASH",
    "SIZE_EXPAND_FILL",
    "SIZE_SHRINK_CENTER",
    "PRESET_MODE_MINSIZE",
    "PRESET_MODE_KEEP_HEIGHT",
    "PRESET_MODE_KEEP_WIDTH",
    "PRESET_MODE_FULL_RECT",
    "ALIGN_LEFT",
    "ALIGN_CENTER",
    "ALIGN_RIGHT",
    "ALIGN_TOP",
    "ALIGN_BOTTOM",
    "ANCHOR_LEFT",
    "ANCHOR_RIGHT",
    "ANCHOR_TOP",
    "ANCHOR_BOTTOM",
    "EXPAND_FIT_WIDTH",
    "EXPAND_FIT_HEIGHT",
    "EXPAND_IGNORE_SIZE",
    "FLAG_REFERENCE",
    "PROPERTY_HINT_NONE",
    "PROPERTY_USAGE_DEFAULT",
    "TYPE_NIL",
    "TYPE_BOOL",
    "TYPE_INT",
    "TYPE_FLOAT",
    "TYPE_STRING",
    "TYPE_ARRAY",
    "TYPE_DICTIONARY",
    "TYPE_OBJECT",
    "TYPE_VECTOR2",
    "TYPE_RECT2",
    "TYPE_COLOR",
    "FORMAT_JSON",
    "FORMAT_TEXT",
    "OK",
    "FAILED",
    "ERR_INVALID_DATA",
    "ERR_FILE_NOT_FOUND",
    "ERR_UNAVAILABLE",
    "PW_LEFT",
    "PW_TOP",
    "PW_RIGHT",
    "PW_BOTTOM",
    "PW_CENTER",
    "PRESET_TOP_LEFT",
    "PRESET_TOP_RIGHT",
    "PRESET_BOTTOM_LEFT",
    "PRESET_BOTTOM_RIGHT",
    "PRESET_FULL_RECT",
    "PRESET_CENTER",
}


@dataclass
class SymbolCheck:
    """一次符号依赖检查的结果。"""

    target: str = ""
    broken: list[str] = field(default_factory=list)
    declared: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.broken

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "ok": self.ok,
            "broken": list(self.broken),
            "declared": list(self.declared),
        }


def _declared_identifiers(text: str) -> set[str]:
    out: set[str] = set()
    out.update(_DECL.findall(text))
    out.update(_CONST_DECL.findall(text))
    return out


def _bare_constants(text: str) -> set[str]:
    """文本里「独立出现」的大写常量。

    要排除三种情况：
    - ``Color.WHITE`` —— 枚举成员，前面是 ``.``
    - ``"HUD"``      —— 字符串里的内容
    - ``Game.ITEM_SLOT_COUNT`` —— 别的类的成员

    做法：先去掉字符串内容，再按「前面不能是 ``.``、后面不能紧跟 ``.``」取词。
    后面紧跟 ``.`` 的那种（``Type.CONST`` 里的 CONST）会被排除。
    """
    # 先把字符串内容替换掉，避免字符串里的词被当引用
    stripped = _STRING.sub('""', text)
    out: set[str] = set()
    for match in re.finditer(r"(?<![.\w])([A-Z][A-Z0-9_]{2,})", stripped):
        # 紧跟在 ``.`` 前面 → 是 ``XXX.YYY`` 的后半段，不算本脚本的标识符
        head = stripped[max(0, match.start() - 1) : match.start()]
        if head == ".":
            continue
        # 后面紧跟 ``.`` → 这是类型名（``Type.CONST``），也不算
        tail = stripped[match.end() : match.end() + 1]
        if tail == ".":
            continue
        out.add(match.group(1))
    return out


def _introduced_by_patches(patches: list[dict]) -> set[str]:
    """补丁引入的标识符（看 replace 侧新出现的声明）。"""
    out: set[str] = set()
    for patch in patches:
        replace = str(patch.get("replace") or "")
        if not replace:
            continue
        out.update(_declared_identifiers(replace))
    return out


#: 基线里出现过的全局常量（跨脚本可见），做一次缓存
_GLOBAL_CACHE: dict[str, set[str]] = {}


def baseline_globals(read_script, paths: list[str]) -> set[str]:
    """收集基线里所有脚本声明过的常量。

    Godot 4 有一批**全局**枚举（``HORIZONTAL_ALIGNMENT_CENTER``、
    ``MOUSE_FILTER_STOP``…），它们不带前缀，本脚本直接用。这类符号靠手写
    内建名单总会漏，所以直接从基线脚本里扫出来。

    ``read_script(path)`` 返回 bytes 或 None。
    """
    key = "|".join(sorted(paths)[:8])
    cached = _GLOBAL_CACHE.get(key)
    if cached is not None:
        return cached

    out: set[str] = set()
    for path in paths:
        try:
            raw = read_script(path)
        except Exception:  # noqa: BLE001
            continue
        if not raw:
            continue
        text = raw.decode("utf-8", "replace")
        out.update(_CONST_DECL.findall(text))
        out.update(_DECL.findall(text))
    _GLOBAL_CACHE[key] = out
    return out


def check_patched_script(
    target: str,
    original: str,
    patched: str,
    patches: list[dict],
    globals_available: set[str] | None = None,
) -> SymbolCheck:
    """检查一个被补丁改过的脚本有没有「引用了不存在的符号」。

    ``original`` 是补丁前的原文，``patched`` 是应用后的结果。
    ``globals_available`` 是基线里其它脚本声明过的符号（跨脚本可见的常量）。
    """
    out = SymbolCheck(target=target)

    available = _declared_identifiers(original)
    if globals_available:
        available |= globals_available
    introduced = _introduced_by_patches(patches)
    out.declared = sorted(introduced)

    # 只查「补丁引入的常量」这一类——它们最容易被依赖，也最容易被漏掉。
    # 类型名（帕斯卡命名）来自 Godot 内建或别的脚本，误报率高，不查。
    used_consts = _bare_constants(patched)
    # 补丁自己的 replace 里出现的常量也算「被引用」
    for patch in patches:
        used_consts.update(_bare_constants(str(patch.get("replace") or "")))

    candidates = used_consts - available - introduced - _BUILTIN
    out.broken = sorted(candidates)
    return out


def check_overlay_patches(
    patches: list[dict],
    read_original,
    read_patched,
) -> list[SymbolCheck]:
    """对一组补丁做检查。

    ``read_original(target)`` / ``read_patched(target)`` 分别是取补丁前后的内容。
    """
    by_target: dict[str, list[dict]] = {}
    for p in patches:
        target = str(p.get("target") or "")
        if target:
            by_target.setdefault(target, []).append(p)

    out: list[SymbolCheck] = []
    for target, group in by_target.items():
        original = read_original(target)
        patched = read_patched(target)
        if original is None or patched is None:
            continue
        try:
            out.append(
                check_patched_script(
                    target,
                    original.decode("utf-8", "replace"),
                    patched.decode("utf-8", "replace"),
                    group,
                )
            )
        except Exception:  # noqa: BLE001 - 检查失败不该阻断安装
            continue
    return out
