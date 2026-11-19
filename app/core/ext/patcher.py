"""数据/文本补丁：让扩展改「已有内容」而不必整份替换。

为什么需要：覆盖一个 400 KB 的 JSON 只为了让 `daily_limit` 从 3 变成 10，
既容易随基线版本失配，也让 diff 不可读。``text_patches`` / ``data_patches``
把改动表达成「找到什么、改成什么」，并强制声明**期望命中数**——
命中数不对就说明基线变了，此时按 `on_test_fail` 决定中止还是放行。

支持的操作
----------

``text_patches``（对文本文件，含脚本源码）
    ``mode=literal`` 字面量替换；``mode=regex`` 正则替换（``re.M``）。
    ``expect_count`` 缺省 1，0 表示不校验。

``data_patches``（对 JSON）
    遵循 RFC 6902 的子集：``add`` / ``remove`` / ``replace`` / ``move`` /
    ``copy`` / ``test``，另加 ``merge``（深合并，等价于 merge-patch）。``set``
    是 ``replace`` 的别名（schema 1 的写法）。
    路径用 JSON Pointer（``/a/b/0``），空串表示整份文档。

两者都是纯函数，不碰文件系统，便于单测与「先看 diff 再决定装不装」。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import manifest as manifest_mod


class PatchError(Exception):
    """补丁无法安全应用（命中数不符、路径不存在、类型不对…）。"""

    def __init__(self, message: str, *, target: str = "", code: str = "patch") -> None:
        super().__init__(message)
        self.target = target
        self.code = code


@dataclass
class PatchReport:
    """一次补丁应用的结果，供 UI 与注册表记录。"""

    target: str
    kind: str                     # data / text
    applied: int = 0
    issues: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "kind": self.kind,
            "applied": self.applied,
            "issues": self.issues,
            "details": self.details,
        }


# ---------------------------------------------------------------- 文本补丁


#: ``scope`` 字段的合法写法：``function:open_package`` / ``class:Inner``。
_SCOPE_RE = re.compile(r"^(?:(function|func|class)[ \t]*:[ \t]*)?([A-Za-z_]\w*)$")

#: 函数/类声明。GDScript 顶层声明顶格写，内部缩进，所以只认行首。
_DECL_RE = {
    "function": re.compile(r"^[ \t]*(?:static[ \t]+)?func[ \t]+{name}[ \t]*\(", re.M),
    "class": re.compile(r"^[ \t]*class[ \t]+{name}\b", re.M),
}

#: 下一个顶层声明 —— 用来切出「这个函数到哪里结束」。
_NEXT_DECL_RE = re.compile(
    r"^(?:(?:@\w+)|(?:static[ \t]+)?(?:func|var|const|signal|enum|class)\s)", re.M
)


def scope_region(text: str, scope: str) -> tuple[int, int, str]:
    """把 ``scope`` 解析成 ``text`` 里的一段区间，返回 ``(起, 止, 错误)``。

    为什么需要它
    ------------

    ``text_patches`` 的老模型是「整个文件里只能命中一次」。这在冻结的基线
    （4.6 / V1.45）上成立，在**活的** 4.7 上不成立：剧情一更新，通用收尾片段
    就会在多个函数里重复出现，全局唯一性当场失效。

    实测例子：4.7 的 ``pet_state.gd`` 里

    ```gdscript
        if not persist(before):
            return {}
    ```

    在 ``open_package()`` 和 ``bank_experience_card()`` 各出现一次。
    奥丽的补丁要改的是前者，但老模型只能判「命中 2 处，拒绝」。

    ``scope`` 把唯一性从**全局**降到**函数级**：先框定函数体，再在里面找。
    这个唯一性是语义上的，抗剧情更新 —— 只要「开礼包」这个函数还在，
    锚点就一直成立。

    找不到、或同名函数有多个，都返回错误让调用方安全失败 —— 不猜。
    """
    matched = _SCOPE_RE.match(str(scope or "").strip())
    if not matched:
        return 0, len(text), (
            f"scope 写法不对：{scope!r}（应形如 function:open_package 或 class:Inner）"
        )
    kind = matched.group(1) or "function"
    kind = "class" if kind == "class" else "function"
    name = matched.group(2)
    pattern = re.compile(_DECL_RE[kind].pattern.format(name=re.escape(name)), re.M)
    hits = list(pattern.finditer(text))
    if not hits:
        label = "函数" if kind == "function" else "类"
        return 0, len(text), f"作用域不存在：{scope}（源码里找不到这个{label}）"
    if len(hits) > 1:
        return 0, len(text), f"作用域不唯一：{scope} 匹配到 {len(hits)} 个同名声明"
    start = hits[0].start()
    nxt = _NEXT_DECL_RE.search(text, hits[0].end())
    return start, (nxt.start() if nxt else len(text)), ""


def _replace_in(
    region: str, mode: str, find: str, repl: str, expect: int
) -> tuple[str, int, list[str]]:
    """在一段文本里应用一次替换，命中数不符时**原样返回**并给原因。"""
    if mode == "regex":
        try:
            pattern = re.compile(str(find), re.M)
        except re.error as exc:
            return region, 0, [f"正则非法：{exc}"]
        count = len(pattern.findall(region))
        if expect and count != expect:
            return region, 0, [f"正则命中 {count} 处，期望 {expect} 处（基线可能已变）"]
        return pattern.sub(repl, region), count, []

    if mode != "literal":
        return region, 0, [f"未知 mode：{mode}（支持 literal / regex）"]

    needle = str(find)
    # 换行风格容错：Windows 下写的 .gd 常是 CRLF，而清单里的锚点基本是 LF。
    # 直接 str.count 会 0 命中并误报「基线可能已变」，把一条本来正确的补丁判死。
    # 单行锚点不受影响，走下面的快路径。
    if "\n" in needle and "\r" not in needle:
        pattern = re.compile(r"\r?\n".join(re.escape(part) for part in needle.split("\n")))
        count = len(pattern.findall(region))
        if expect and count != expect:
            return region, 0, [f"字面量命中 {count} 处，期望 {expect} 处（基线可能已变）"]
        if not count:
            return region, 0, []
        body = repl.replace("\r\n", "\n")
        if "\r\n" in region:
            body = body.replace("\n", "\r\n")
        return pattern.sub(lambda _m: body, region), count, []

    count = region.count(needle)
    if expect and count != expect:
        return region, 0, [f"字面量命中 {count} 处，期望 {expect} 处（基线可能已变）"]
    return region.replace(needle, repl), count, []


def apply_text(text: str, patch: dict, *, target: str = "") -> tuple[str, int, list[str]]:
    """应用一条文本补丁，返回 ``(新文本, 命中数, 问题列表)``。

    ``scope``（可选）把搜索范围收窄到一个函数或内部类，用于「锚点在整份
    文件里不唯一、但在某个函数里唯一」的情况 —— 4.7 这种会持续更新的基线
    上，这类情况只会越来越多。
    """
    mode = str(patch.get("mode") or "literal")
    find = patch.get("find", "")
    repl = str(patch.get("replace", ""))
    expect = int(patch.get("expect_count", 1) or 0)
    scope = str(patch.get("scope") or "").strip()

    if not find:
        return text, 0, ["find 为空，补丁被忽略"]

    start, end = 0, len(text)
    if scope:
        start, end, error = scope_region(text, scope)
        if error:
            return text, 0, [error]
        if end <= start:
            return text, 0, [f"作用域为空：{scope}"]

    region, count, warns = _replace_in(text[start:end], mode, str(find), repl, expect)
    if warns:
        prefix = f"{scope} 内" if scope else ""
        return text, 0, [prefix + w for w in warns]
    if not count:
        return text, 0, []
    return text[:start] + region + text[end:], count, []


# ---------------------------------------------------------------- 脚本成员注入

#: 在脚本里找 ``extends`` 行，成员声明插在它之后（extends 必须排最前）。
#:
#: 2026-11-05 修：原正则 ``^[ \t]*extends[ \t]+\w+`` 只认裸类名，不认
#: ``extends "res://scripts/xxx.gd"`` 这种带引号的路径写法。基线里 109 个
#: 脚本中有 9 个用的是带引号形式（training_run / opening_run / skill_tips /
#: pet_panel / pet_super_panel / dungeon_map / avatar_preview /
#: flash/equipment_light / training_monster）。
#:
#: 匹配失败会走兜底分支——把注入块塞到文件**最前面**，``extends`` 被挤到
#: 第 12 行。GDScript 要求 ``extends`` 是第一条语句，于是整份脚本 parse error：
#:
#:     SCRIPT ERROR: Parse Error: Could not resolve super class inheritance
#:         from "res://scripts/training_run.gd".
#:     ERROR: Failed to load script "res://scripts/opening_run.gd"
#:
#: 后果是主城场景（game_world.gd:67 → opening_run）整条继承链断裂 → 灰屏。
#:
#: 三种合法写法都要认：``extends Node`` / ``extends "res://x.gd"`` /
#: ``extends 'res://x.gd'``。
_EXTENDS_RE = re.compile(
    r"""^[ \t]*extends[ \t]+(?:[A-Za-z_]\w*|\"[^\"]+\"|'[^']+')""",
    re.M,
)


def inject_members(
    text: str, members: dict[str, str], *, target: str = ""
) -> tuple[str, list[str]]:
    """把 ``scripts.members`` 里的常量/函数声明注入到脚本顶层。

    为什么需要单独一步：``text_patches`` 只能改「已存在的内容」，没法凭空
    加声明。而扩展清单里的 ``scripts.members`` 提供的正是这些声明（形如
    ``const X = preload(...)`` 或 ``func helper()``），供 ``text_patches``
    引用。过去 ``members`` 只被 :mod:`manifest` 解析、从未写进产物，于是补丁
    引用了不存在的标识符 → 整份脚本 parse error → autoload 加载失败。

    典型症状（实测）：``Identifier "WhfInstructorsBaolong" not declared``。

    注入位置取 ``extends`` 之后：GDScript 的 ``extends`` 必须是最前面的
    语句，而 ``const`` / ``func`` 都要求位于顶层，插在这里两者都满足。

    已存在的同名成员会跳过，避免与包内自带声明重复。
    """
    if not members:
        return text, []
    issues: list[str] = []
    lines: list[str] = []
    for name, decl in members.items():
        key = str(name).strip()
        body = str(decl or "").rstrip()
        if not key or not body:
            continue
        # 同名已声明就跳过：重复 const 会让整个脚本报 "already declared"
        if re.search(rf"^[ \t]*(?:const|var|class_name|func)[ \t]+{re.escape(key)}\b", text, re.M):
            continue
        lines.append(body)

    if not lines:
        return text, issues

    block = "\n\n# ---- 扩展成员注入（scripts.members）----\n" + "\n".join(lines) + "\n"
    match = _EXTENDS_RE.search(text)
    if match:
        head_end = match.end()
        return text[:head_end] + "\n" + block + text[head_end:], issues

    # 没有 extends 行：只能整体前置，并提示一句（大多数脚本都有 extends）
    issues.append(f"{target or '脚本'} 没有 extends 行，成员已插到文件最前")
    return block.lstrip("\n") + text, issues


# ---------------------------------------------------------------- JSON Pointer


def parse_pointer(pointer: str) -> list[str]:
    """``/a/b/0`` → ``['a','b','0']``；空串 → ``[]``。"""
    if pointer in ("", "/"):
        return []
    if not pointer.startswith("/"):
        raise PatchError(f"JSON Pointer 必须以 / 开头：{pointer!r}", code="pointer")
    return [tok.replace("~1", "/").replace("~0", "~") for tok in pointer[1:].split("/")]


def _resolve_parent(doc: Any, tokens: list[str]) -> tuple[Any, str]:
    node = doc
    for token in tokens[:-1]:
        if isinstance(node, list):
            index = _index(token, len(node))
            node = node[index]
        elif isinstance(node, dict):
            if token not in node:
                raise PatchError(f"路径不存在：{'/'.join(tokens)}", code="pointer")
            node = node[token]
        else:
            raise PatchError(f"路径中途不是容器：{'/'.join(tokens)}", code="pointer")
    return node, tokens[-1] if tokens else ""


def _index(token: str, length: int, *, allow_end: bool = False) -> int:
    if token == "-" and allow_end:
        return length
    try:
        index = int(token)
    except ValueError as exc:
        raise PatchError(f"数组下标不是数字：{token!r}", code="pointer") from exc
    limit = length if allow_end else length - 1
    if index < 0 or index > limit:
        raise PatchError(f"数组下标越界：{index}（长度 {length}）", code="pointer")
    return index


def _get(doc: Any, tokens: list[str]) -> Any:
    node = doc
    for token in tokens:
        if isinstance(node, list):
            node = node[_index(token, len(node))]
        elif isinstance(node, dict):
            if token not in node:
                raise PatchError(f"路径不存在：{'/'.join(tokens)}", code="pointer")
            node = node[token]
        else:
            raise PatchError(f"路径中途不是容器：{'/'.join(tokens)}", code="pointer")
    return node


def _set(doc: Any, tokens: list[str], value: Any, *, mode: str) -> None:
    if not tokens:
        raise PatchError("不能替换根文档（请用 merge 或具体路径）", code="pointer")
    parent, last = _resolve_parent(doc, tokens)
    if isinstance(parent, list):
        index = _index(last, len(parent), allow_end=(mode == "add"))
        if mode == "add":
            parent.insert(index, value)
        else:
            parent[index] = value
    elif isinstance(parent, dict):
        if mode != "add" and last not in parent:
            raise PatchError(f"路径不存在：{'/'.join(tokens)}", code="pointer")
        parent[last] = value
    else:
        raise PatchError(f"父节点不是容器：{'/'.join(tokens)}", code="pointer")


def _remove(doc: Any, tokens: list[str]) -> None:
    if not tokens:
        raise PatchError("不能删除根文档", code="pointer")
    parent, last = _resolve_parent(doc, tokens)
    if isinstance(parent, list):
        parent.pop(_index(last, len(parent)))
    elif isinstance(parent, dict):
        if last not in parent:
            raise PatchError(f"路径不存在：{'/'.join(tokens)}", code="pointer")
        parent.pop(last)
    else:
        raise PatchError(f"父节点不是容器：{'/'.join(tokens)}", code="pointer")


def deep_merge(base: Any, patch: Any) -> Any:
    """merge-patch 语义：对象递归合并，``null`` 删键，其余整体替换。"""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def assert_expect(
    doc: Any, tokens: list[str], expect: dict, *, path: str = "", target: str = ""
) -> None:
    """写前断言：``expect`` 里的条件全部成立才允许改。

    这是「接管」能成立的前提。没有它，后包只要排在后面就能改掉前包的值，
    和「后装即赢」没有区别。
    """
    if not isinstance(expect, dict) or not expect:
        return
    exists = True
    try:
        actual = _get(doc, tokens) if tokens else doc
    except PatchError:
        actual = None
        exists = False

    if "exists" in expect and bool(expect.get("exists")) != exists:
        raise PatchError(
            f"expect 失败：{path or '/'} {'存在' if exists else '不存在'}，"
            f"期望 {'存在' if expect.get('exists') else '不存在'}",
            target=target,
            code="expect",
        )
    if not exists:
        # `expect: {"exists": false}` 是 add 型操作的合法前置断言：「这里现在必须
        # 还没有人占」。上面那条 exists 比对已经通过，这里直接放行即可。
        # 原实现无条件抛「引用的位置不存在」，等于 exists:false 永远不可能通过。
        if expect.get("exists") is False:
            return
        raise PatchError(f"expect 引用的位置不存在：{path or '/'}", target=target, code="expect")

    if "eq" in expect and actual != expect.get("eq"):
        raise PatchError(
            f"expect 失败：{path or '/'} 实际 {actual!r}，期望 {expect.get('eq')!r}",
            target=target,
            code="expect",
        )
    if "ne" in expect and actual == expect.get("ne"):
        raise PatchError(
            f"expect 失败：{path or '/'} 不能等于 {expect.get('ne')!r}",
            target=target,
            code="expect",
        )
    if "type" in expect and _type_name(actual) != str(expect.get("type")):
        raise PatchError(
            f"expect 失败：{path or '/'} 类型是 {_type_name(actual)}，"
            f"期望 {expect.get('type')}",
            target=target,
            code="expect",
        )
    if "sha256" in expect:
        import hashlib
        import json as _json

        blob = _json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        if digest != str(expect.get("sha256")):
            raise PatchError(
                f"expect 失败：{path or '/'} 内容指纹不符（基线可能已变）",
                target=target,
                code="expect",
            )


def apply_json(
    doc: Any,
    ops: list[dict],
    *,
    target: str = "",
    inplace: bool = False,
) -> tuple[Any, int, list[str]]:
    """按顺序应用一组 JSON 操作。任何一步失败即抛 :class:`PatchError`。

    ``inplace=True`` 时**不做开头的整份深拷贝**，直接改传进来的文档。
    只给「文档是本次调用自己解析出来的、且失败就整体作废」的调用方用
    （``compose._apply_data`` / ``patch_bytes`` 的严格路径）。

    为什么需要这个开关：素材类 JSON（``items.json`` / ``equipment.json``）
    动辄几 MB，一个扩展常有十几条 data_patch，逐条各拷一份的代价实测
    11.5 秒（2026-11-17 导入耗时排查，4.7 合集 21 个成员）。
    默认仍是拷贝 —— 外部调用方可能复用 ``doc``，悄悄改成原地修改会串味。
    """
    node = doc if inplace else copy.deepcopy(doc)
    applied = 0
    issues: list[str] = []
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise PatchError(f"第 {index} 个操作不是对象", target=target, code="structure")
        kind = str(op.get("op") or "replace")
        if kind == "set":
            kind = "replace"
        path = str(op.get("path", ""))
        tokens = parse_pointer(path)

        try:
            assert_expect(doc=node, tokens=tokens, expect=op.get("expect") or {}, path=path, target=target)
        except PatchError as exc:
            on_miss = str(op.get("on_miss") or "abort")
            if on_miss in ("warn", "skip"):
                issues.append(str(exc))
                continue
            raise

        if kind == "replace":
            if not tokens:
                node = copy.deepcopy(op.get("value"))
            else:
                _set(node, tokens, copy.deepcopy(op.get("value")), mode="replace")
        elif kind == "add":
            if not tokens:
                node = copy.deepcopy(op.get("value"))
            else:
                _set(node, tokens, copy.deepcopy(op.get("value")), mode="add")
        elif kind == "remove":
            _remove(node, tokens)
        elif kind in ("merge", "merge_patch"):
            if not tokens:
                node = deep_merge(node, op.get("value") or {})
            else:
                _set(node, tokens, deep_merge(_get(node, tokens), op.get("value") or {}), mode="replace")
        elif kind == "test":
            actual = _get(node, tokens) if tokens else node
            if actual != op.get("value"):
                raise PatchError(
                    f"test 失败：{path or '/'} 实际 {actual!r}，期望 {op.get('value')!r}",
                    target=target,
                    code="test",
                )
        elif kind in ("move", "copy"):
            src_tokens = parse_pointer(str(op.get("from", "")))
            value = _get(node, src_tokens) if src_tokens else node
            value = copy.deepcopy(value)
            if kind == "move":
                _remove(node, src_tokens)
            _set(node, tokens, value, mode="add")
        else:
            raise PatchError(f"不支持的操作：{kind}", target=target, code="op")

        applied += 1
    return node, applied, issues


# ---------------------------------------------------------------- 统一入口


def patch_bytes(
    target: str,
    data: bytes,
    text_patches: list[dict],
    data_patches: list[dict],
    *,
    encoding: str = "utf-8",
    allow_partial: bool = False,
) -> tuple[bytes, list[PatchReport]]:
    """对一个目标应用所有相关补丁，返回新字节与逐条报告。

    目标以 ``.json`` 结尾时先把 data_patches 当作 JSON 操作应用，再把
    text_patches 当文本处理（顺序固定，避免两边互相踩）。

    ``allow_partial=True`` 时，某条补丁命中数不符**不会中断整体**——
    该条被跳过，其余补丁照常应用。用于「跨版本安装」：扩展里有一部分补丁
    是针对别的游戏版本的，跳过它们比整包失败更有用。
    """
    reports: list[PatchReport] = []
    current = data

    # 严格路径（``allow_partial=False`` 且所有补丁都是「失败即中止」）：
    # 同一目标的所有 data_patch 合并成 **一次解析 + 一次序列化**。
    #
    # 旧实现每条补丁都要把整份 JSON 解析、深拷贝、再序列化一遍。实测一个
    # 几 MB 的 items.json 上 14 条补丁，光 deepcopy 就 11.5 秒 —— 这是
    # 完全校验导入慢的第二大头（第一是逐包冒烟，2026-11-17 排查）。
    #
    # 只在「没有任何补丁要求跳过失败」时才合并：那种情况下语义完全一致
    # （任何一步出错都整次安装失败，中间状态不会被谁看到）。
    merge_data = (
        bool(data_patches)
        and target.lower().endswith(".json")
        and not allow_partial
        and all(str(p.get("on_test_fail") or "abort") == "abort" for p in data_patches)
    )
    if merge_data:
        try:
            doc = json.loads(current.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PatchError(f"目标不是合法 JSON：{exc}", target=target, code="data") from exc
        for patch in data_patches:
            report = PatchReport(target=target, kind="data")
            doc, applied, _ = apply_json(
                doc, manifest_mod.ops_of(patch), target=target, inplace=True
            )
            report.applied = applied
            report.details.append(f"{applied} 个操作")
            reports.append(report)
        current = json.dumps(doc, ensure_ascii=False, indent=2).encode(encoding)
        data_patches = []

    for patch in data_patches:
        report = PatchReport(target=target, kind="data")
        if not target.lower().endswith(".json"):
            report.issues.append("data_patch 只支持 .json 目标")
            reports.append(report)
            continue
        try:
            doc = json.loads(current.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if not allow_partial:
                raise PatchError(f"目标不是合法 JSON：{exc}", target=target, code="data") from exc
            report.issues.append(f"目标不是合法 JSON：{exc}")
            reports.append(report)
            continue
        try:
            new_doc, applied, _ = apply_json(doc, manifest_mod.ops_of(patch), target=target)
        except PatchError as exc:
            if str(patch.get("on_test_fail") or "abort") == "abort" and not allow_partial:
                raise
            report.issues.append(str(exc))
            reports.append(report)
            continue
        current = json.dumps(new_doc, ensure_ascii=False, indent=2).encode(encoding)
        report.applied = applied
        report.details.append(f"{applied} 个操作")
        reports.append(report)

    for patch in text_patches:
        report = PatchReport(target=target, kind="text")
        try:
            text = current.decode(encoding)
        except UnicodeDecodeError as exc:
            report.issues.append(f"目标不是 {encoding} 文本：{exc}")
            reports.append(report)
            continue
        new_text, count, issues = apply_text(text, patch, target=target)
        report.applied = count
        report.issues.extend(issues)
        report.details.append(f"{patch.get('mode', 'literal')} × {count}")
        if issues:
            on_fail = str(patch.get("on_test_fail") or "abort")
            if on_fail == "abort" and not allow_partial:
                raise PatchError(issues[0], target=target, code="count")
        else:
            current = new_text.encode(encoding)
        reports.append(report)

    return current, reports
