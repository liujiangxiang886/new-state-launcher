#!/usr/bin/env python3
"""jjm-pack —— 1.6 启动器扩展打包工具（单文件，仅需 Python 3.9+，无第三方依赖）。

给扩展作者用。六个子命令：

    check    按《1.6 启动器扩展打包规范》检查一个包能不能发
    verify   **开发者校验准入预检**（schema 3 / 位置已处置 / 组合不冲突）
    fix      补自带基底（把「依赖玩家装源码工程」的包改成自包含）
    pack     把目录打成规范 zip
    manifest 生成 extension.json 骨架
    check --pair A.zip B.zip   两个包同时装会不会打架（1.6 新增）

设计前提：作者机器上只有发布版启动器（frozen exe，没有 Python 源码），
所以本文件不 import 启动器任何模块，规则以常量形式内置。
``verify`` 的规则与启动器 strict 口径对齐，由 tests/test_extkit_verify.py
保证两套结论一致；需要「启动器本体说了算」时用 tools/verify_admission.py。

用法示例：
    python jjm_pack.py check 我的扩展.zip
    python jjm_pack.py verify 我的扩展.zip
    python jjm_pack.py verify 我的扩展.zip --with 已有包A.zip --with 已有包B.zip
    python jjm_pack.py check 我的扩展.zip --pair 别人的扩展.zip
    python jjm_pack.py fix 我的扩展.zip --source D:\\反解工程 -o 我的扩展-自包含.zip
    python jjm_pack.py pack 我的扩展/ -o 我的扩展.zip
    python jjm_pack.py manifest 我的扩展/ --id me.my_ext --name "我的扩展"
    python jjm_pack.py verify 我的扩展.zip --json > 准入报告.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

TOOL_VERSION = "1.6"

# ---------------------------------------------------------------- 规范常量

#: 本规范对应的启动器版本与引擎线。
SPEC_VERSION = "1.7"

#: 引擎线相关的默认值。**默认仍是 4.6**，保持既有命令行行为不变；
#: 做 4.7 的包时用 ``--line 4.7`` 覆盖。
ENGINE_LINE = "4.6"
BASELINE_ID = "jjxf.v1.45"
BASELINE_ALIASES = {"jjxf.v1.45", "jjxf.v1.45.original", "v1.45", "1.45"}

#: 每条引擎线的基线定义。
#:
#: 为什么需要这张表：4.6 与 4.7 是**两个不同的基线**（本体文件、引擎版本、
#: 代码层都不同）。工具原先只认 1.45，导致 4.7 的包永远过不了
#: ``baseline`` 校验 —— 那等于「4.7 的 mod 不能用官方工具打包」。
#:
#: ``aliases`` 收作者可能写的各种形式（``jjxf.v4.7`` / ``v4.7`` / ``4.7``），
#: 免得因为一个字符串写法不同就被判不合格。
LINE_PROFILES: dict[str, dict] = {
    "4.6": {
        "baseline_id": "jjxf.v1.45",
        "aliases": {"jjxf.v1.45", "jjxf.v1.45.original", "v1.45", "1.45"},
        "label": "1.45 原版（Godot 4.6）",
        "engine_version": "4.6.0",
    },
    "4.7": {
        "baseline_id": "jjxf.v4.7",
        "aliases": {"jjxf.v4.7", "jjxf.v4.7.story", "v4.7", "4.7"},
        "label": "4.7 剧情版（Godot 4.7.2）",
        "engine_version": "4.7.2",
    },
}
SCHEMA_SUPPORTED = (1, 2, 3, 4)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

REQUIRED_FIELDS = ("schema", "id", "name", "version")
KIND_ENUM = ("extension", "minigame", "theme", "framework")

#: 数据补丁允许的操作名。`set` 是 `replace` 的别名，启动器归一后等价。
OP_WHITELIST = (
    "add", "remove", "replace", "move", "copy", "test", "merge", "merge_patch", "set",
)

#: 断言允许的键。写别的键会被当成「整对象期望值」而不是断言。
EXPECT_KEYS = ("eq", "ne", "exists", "type", "sha256", "size")

#: 断言不成立时的处置：abort 整包不生效 / warn 记一条继续 / skip 只跳过这条。
ON_MISS_ENUM = ("abort", "warn", "skip")

#: 锚点「包含」判定的最小长度。与 app/core/ext/units.py::MIN_CONTAIN_LEN 一致。
MIN_CONTAIN_LEN = 8

#: 顶层已知字段。写白名单外的键启动器会给一条 warn，不会报错。
KNOWN_FIELDS = frozenset({
    "schema", "id", "name", "version", "kind", "author", "license",
    "framework", "priority", "requires", "conflicts", "load_after",
    "load_before", "claims", "capabilities", "files", "packs",
    "data_patches", "text_patches", "method_hooks", "scenes", "scripts",
    "settings", "inputs", "story", "minigames", "baseline", "entry",
    "script_members", "project_settings", "input_actions", "scope",
    "engine", "description", "homepage", "tags", "sources",
})

#: 包里允许出现的最大条目数与单文件大小（防止误把游戏资源打进来）。
MAX_ENTRIES = 20000
MAX_SINGLE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

#: 会被导出包编译成二进制、因此补丁必须自带原文的后缀。
NEEDS_BASE_SUFFIXES = (".gd", ".tscn", ".tres", ".res", ".gdshader", ".shader")

#: 基线包里本身就是明文、补丁可以直接读到的后缀。
PLAIN_SUFFIXES = (".json", ".txt", ".cfg", ".ini", ".csv", ".yaml", ".yml")

#: 联机模块：阻断级规则（P2P / 直连协议）。与 app/core/ext/netguard.py 保持一致。
P2P_BLOCK_RULES = (
    (r"\bENetMultiplayerPeer\b", "ENet 点对点传输"),
    (r"\bWebSocketMultiplayerPeer\b", "WebSocket 联机传输"),
    (r"\bMultiplayerAPI\b", "多人对等 API"),
    (r"multiplayer\s*\.\s*multiplayer_peer\s*=", "设置多人对等端"),
    (r"\bPacketPeerUDP\b", "UDP 点对点直连"),
    (r"\bPacketPeerDTLS\b", "DTLS 点对点直连"),
    (r"\bStreamPeerTCP\b", "TCP 直连"),
    (r"\b(?:UDPServer|TCPServer)\b", "自建网络服务端"),
    (r"\bWebSocketPeer\b", "WebSocket 直连"),
    (r"\bIP\s*\.\s*resolve_hostname\b", "主机名解析（联机寻址）"),
    (r"\b(?:create_server|create_client|create_mesh)\s*\(", "ENet 建链"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b", "IP:端口直连地址"),
    (r"\b(?:wss?|tcp|udp|enet)://", "网络协议地址"),
    (r"\b(?:nat_)?punch_?through\b", "NAT 穿透"),
    (r"\blan_?discover(?:y)?\b", "局域网发现"),
)

P2P_WARN_RULES = (
    (r"@rpc\b", "RPC 注解（无对等端时不生效）"),
    (r"\brpc_id\s*\(", "RPC 调用（无对等端时不生效）"),
    (r"联机|开房|组队|公会战|快速匹配|匹配房间|房间列表", "联机相关文案"),
)

DECLARED_CAPS = ("online", "multiplayer", "netplay", "coop", "p2p", "联机")

TEXT_SUFFIXES = NEEDS_BASE_SUFFIXES + PLAIN_SUFFIXES + (".md", ".gdscript")

_BLOCK_RE = [(re.compile(p, re.I), why) for p, why in P2P_BLOCK_RULES]
_WARN_RE = [(re.compile(p, re.I), why) for p, why in P2P_WARN_RULES]


# ---------------------------------------------------------------- 结果模型


class Finding:
    def __init__(self, level: str, code: str, message: str, where: str = "") -> None:
        self.level = level
        self.code = code
        self.message = message
        self.where = where

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "where": self.where, "message": self.message}

    def __str__(self) -> str:
        tag = {"error": "错误", "warn": "提示", "ok": "通过"}.get(self.level, self.level)
        loc = f" @ {self.where}" if self.where else ""
        return f"[{tag}] {self.code} {self.message}{loc}"


class Report:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(self, level: str, code: str, message: str, where: str = "") -> None:
        self.findings.append(Finding(level, code, message, where))

    def error(self, code: str, message: str, where: str = "") -> None:
        self.add("error", code, message, where)

    def warn(self, code: str, message: str, where: str = "") -> None:
        self.add("warn", code, message, where)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "tool": "jjm-pack",
            "tool_version": TOOL_VERSION,
            "spec_version": SPEC_VERSION,
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [f.as_dict() for f in self.findings],
        }


class PackageView:
    """把 zip 与目录统一成「名字 → 字节」的只读视图。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.is_zip = path.is_file()
        self._zip: zipfile.ZipFile | None = None
        if self.is_zip:
            self._zip = zipfile.ZipFile(path)
            self._names = [i.filename for i in self._zip.infolist() if not i.is_dir()]
        else:
            self._names = [
                p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()
            ]

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def read(self, name: str) -> bytes:
        if self._zip is not None:
            return self._zip.read(name)
        return (self.path / name).read_bytes()

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()

    def __enter__(self) -> "PackageView":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------- 检查


def _norm_target(value: object) -> str:
    return str(value or "").replace("\\", "/").replace("res://", "").lstrip("/")


def _declared_requires(data: dict) -> set[str]:
    req = data.get("requires")
    out: set[str] = set()
    if isinstance(req, dict):
        out |= {str(k) for k in req}
        out |= {str(v) for v in req.values() if isinstance(v, str)}
    elif isinstance(req, list):
        for item in req:
            if isinstance(item, str):
                out.add(item)
            elif isinstance(item, dict):
                out |= {str(v) for v in item.values() if isinstance(v, str)}
    return out


def _scan_text(report: Report, where: str, text: str) -> None:
    for pattern, why in _BLOCK_RE:
        m = pattern.search(text)
        if m:
            start = max(0, m.start() - 40)
            snippet = text[start : m.end() + 40].replace("\n", " ⏎ ").strip()
            report.error("NET-01", f"包含联机模块：{why}", f"{where}｜{snippet[:120]}")
    for pattern, why in _WARN_RE:
        if pattern.search(text):
            report.warn("NET-02", f"疑似联机内容：{why}", where)


# ---------------------------------------------------------------- 位置级判定


def _iter_ops(patch: dict) -> list[dict]:
    """取出补丁里的操作列表，``operations`` 与 ``ops`` 都认。"""
    raw = patch.get("operations")
    if raw is None:
        raw = patch.get("ops")
    if not isinstance(raw, list):
        return []
    return [op for op in raw if isinstance(op, dict)]


def _norm_pointer(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.startswith("/") else "/" + text.lstrip("/")


def _check_op(
    report: Report,
    op: dict,
    where: str,
    target: str,
    ext_id: str,
    requires: set[str] | None = None,
) -> None:
    """单条写操作的静态检查（MAN-18..25）。"""
    requires = requires or set()
    kind = str(op.get("op", "replace")).strip() or "replace"
    if kind not in OP_WHITELIST:
        report.error(
            "MAN-18",
            f"{where} 的操作名不支持：{kind!r}（可用：{'、'.join(OP_WHITELIST)}）",
            target,
        )
    path = op.get("path")
    if path in (None, ""):
        if kind not in ("merge_patch",):
            report.error("MAN-19", f"{where} 缺 path（JSON Pointer，形如 /catalog/360261）", target)
    elif not str(path).startswith("/"):
        report.error("MAN-19", f"{where} 的 path 必须以 / 开头：{path!r}", target)

    expect = op.get("expect")
    if expect is not None:
        if isinstance(expect, dict):
            unknown = [k for k in expect if k not in EXPECT_KEYS]
            # 一个断言键都没有 => 作者写的是「整对象期望值」，这是合法写法
            if unknown and any(k in EXPECT_KEYS for k in expect):
                report.warn(
                    "MAN-20",
                    f"{where} 的 expect 里有未支持的键：{'、'.join(sorted(unknown))}"
                    f"（可用：{'、'.join(EXPECT_KEYS)}）",
                    target,
                )
            if "sha256" in expect and not re.fullmatch(r"[0-9a-fA-F]{64}", str(expect["sha256"])):
                report.error("MAN-20", f"{where} 的 expect.sha256 不是 64 位十六进制", target)
        # 非 dict（字符串/数字/布尔）视为 {"eq": ...}，1.45.1 的写法，合法

    on_miss = op.get("on_miss")
    if on_miss is not None and str(on_miss) not in ON_MISS_ENUM:
        report.error(
            "MAN-21",
            f"{where} 的 on_miss 取值不认识：{on_miss!r}（可用：{'、'.join(ON_MISS_ENUM)}）",
            target,
        )

    overrides = op.get("overrides")
    if overrides is not None:
        if not isinstance(overrides, list) or not all(
            isinstance(x, str) and x.strip() for x in overrides
        ):
            report.error("MAN-22", f"{where} 的 overrides 必须是扩展 id 列表", target)
        else:
            named = {x.strip() for x in overrides}
            if ext_id and ext_id in named:
                report.error("MAN-22", f"{where} 的 overrides 点名了自己（{ext_id}）", target)
            if expect is None:
                report.error(
                    "MAN-23",
                    f"{where} 声明了 overrides 却没写 expect。接管别人的位置必须先断言"
                    f"「现在是什么」，否则对方换版本后你会改错地方",
                    target,
                )
            missing = sorted(named - requires)
            if missing:
                report.warn(
                    "MAN-25",
                    f"{where} 接管了 {'、'.join(missing)}，但 requires 里没写。"
                    f"接管关系必须同时出现在 requires 里，启动器才会据此排序：先装被接管方",
                    target,
                )


def collect_points(data: dict, names: set[str] | None = None) -> list[dict]:
    """把一份清单里所有「落到某个具体位置」的改动摊平成点。

    返回 ``[{"target","locator","kind","who","expect","overrides"}]``：

    - ``kind="ptr"``    数据补丁按 JSON Pointer 写某处；
    - ``kind="whole"``  数据补丁整档替换（写整个文件）；
    - ``kind="anchor"`` 文本补丁按 find 锚点改某段；
    - ``kind="file"``   整份文件替换。

    两个包在同一 ``target`` 上出现位置重叠的点，就是「抢同一个地方」。
    """
    names = names or set()
    points: list[dict] = []

    for index, rec in enumerate(data.get("files") or []):
        if not isinstance(rec, dict):
            continue
        points.append(
            {
                "target": _norm_target(rec.get("target")),
                "locator": "",
                "kind": "file",
                "who": f"files[{index}]",
                "expect": (rec.get("expect") if isinstance(rec.get("expect"), dict) else None),
                "overrides": [str(x) for x in (rec.get("overrides") or []) if isinstance(x, str)],
            }
        )

    for index, patch in enumerate(data.get("data_patches") or []):
        if not isinstance(patch, dict):
            continue
        target = _norm_target(patch.get("target"))
        ops = _iter_ops(patch)
        shared = {
            "expect": (patch.get("expect") if isinstance(patch.get("expect"), dict) else None),
            "overrides": [str(x) for x in (patch.get("overrides") or []) if isinstance(x, str)],
        }
        if not ops:
            points.append({"target": target, "locator": "", "kind": "whole",
                           "who": f"data_patches[{index}]", **shared})
            continue
        for op_index, op in enumerate(ops):
            points.append(
                {
                    "target": target,
                    "locator": _norm_pointer(op.get("path")),
                    "kind": "ptr",
                    "who": f"data_patches[{index}].operations[{op_index}]",
                    "expect": op.get("expect") if "expect" in op else shared["expect"],
                    "overrides": (
                        [str(x) for x in (op.get("overrides") or []) if isinstance(x, str)]
                        or shared["overrides"]
                    ),
                }
            )

    for index, patch in enumerate(data.get("text_patches") or []):
        if not isinstance(patch, dict):
            continue
        find = str(patch.get("find") or "")
        points.append(
            {
                "target": _norm_target(patch.get("target")),
                "locator": find.strip(),
                "kind": "anchor",
                "who": f"text_patches[{index}]",
                "expect": (patch.get("expect") if isinstance(patch.get("expect"), dict) else None),
                "overrides": [str(x) for x in (patch.get("overrides") or []) if isinstance(x, str)],
            }
        )
    return [p for p in points if p["target"]]


def _pointer_relation(a: str, b: str) -> str:
    """两个指针的关系：``same`` / ``overlap``（父子）/ 空串。"""
    if not a or not b:
        return ""
    if a == b:
        return "same"
    if a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/"):
        return "overlap"
    return ""


def _anchor_relation(a: str, b: str) -> str:
    """两段 find 文本的关系：完全相同，或一方包含另一方（短串不算，容易误判）。

    阈值必须与运行时 ``app/core/ext/units.py::MIN_CONTAIN_LEN`` 一致，
    否则工具说没问题、启动器却标降级。
    """
    if not a or not b:
        return ""
    if a == b:
        return "same"
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= MIN_CONTAIN_LEN and short in long:
        return "overlap"
    return ""


def _overlap(a: dict, b: dict) -> str:
    """两个点是否抢同一处。返回 'same' / 'overlap' / ''。"""
    if a["target"] != b["target"]:
        return ""
    if a["kind"] == "file" or b["kind"] == "file" or "whole" in (a["kind"], b["kind"]):
        return "same"
    if a["kind"] == "ptr" and b["kind"] == "ptr":
        return _pointer_relation(a["locator"], b["locator"])
    if a["kind"] == "anchor" and b["kind"] == "anchor":
        return _anchor_relation(a["locator"], b["locator"])
    return ""


def _check_self_overlap(report: Report, points: list[dict]) -> None:
    """同一个包里两条改动落在同一处：后一条会盖掉前一条。"""
    seen: list[dict] = []
    for point in points:
        for other in seen:
            if _overlap(point, other) == "same":
                report.warn(
                    "MAN-24",
                    f"包内两条改动打在同一处：{other['who']} 与 {point['who']}"
                    f"（{point['target']} {point['locator'] or '整份文件'}）。后一条会改掉前一条的结果",
                    point["target"],
                )
                break
        seen.append(point)


def check_pair(first: Path, second: Path) -> Report:
    """两个包同时装会不会打架（``check --pair``）。

    只做静态可判的部分：同一位置 / 父子位置 / 同一文本锚点 / 双份整文件。
    真实的最终值要看启动器「处理冲突…」里的预览，这里只回答「会不会撞」。
    """
    report = Report()
    loaded: list[tuple[Path, dict, set[str]]] = []
    for path in (first, second):
        try:
            with PackageView(path) as pkg:
                raw = json.loads(pkg.read("extension.json").decode("utf-8"))
                loaded.append((path, raw, set(pkg.names)))
        except (OSError, KeyError, ValueError, UnicodeDecodeError, RuntimeError) as exc:
            report.error("PAIR-00", f"读不了这个包：{path}（{exc}）")
            return report

    (_, left, left_names), (_, right, right_names) = loaded
    left_id = str(left.get("id") or "第一个包")
    right_id = str(right.get("id") or "第二个包")

    for owner, data in ((left_id, left), (right_id, right)):
        if data.get("schema") not in SCHEMA_SUPPORTED:
            report.error("PAIR-00", f"{owner} 的 schema 不受支持：{data.get('schema')!r}")
    if report.errors:
        return report

    left_points = collect_points(left, left_names)
    right_points = collect_points(right, right_names)

    found = 0
    for a in left_points:
        for b in right_points:
            relation = _overlap(a, b)
            if not relation:
                continue
            found += 1
            same_place = "同一处" if relation == "same" else "父子位置"
            code = "PAIR-04" if "file" in (a["kind"], b["kind"]) or "whole" in (
                a["kind"], b["kind"]
            ) else "PAIR-01"
            authorized = right_id in a["overrides"] and a.get("expect") is not None
            if authorized:
                report.warn(
                    "PAIR-02",
                    f"{left_id} 的 {a['who']} 显式接管了 {right_id}（带 expect 断言），"
                    f"{same_place}：{a['target']} {a['locator'] or '整份文件'}。"
                    f"装的时候要先装 {right_id}",
                    a["target"],
                )
            else:
                report.warn(
                    code,
                    f"{left_id} 的 {a['who']} 与 {right_id} 的 {b['who']} 撞在{same_place}："
                    f"{a['target']} {a['locator'] or '整份文件'}。"
                    f"玩家侧默认后者生效；要共存由一方写 overrides + expect 接管",
                    a["target"],
                )

    if not found:
        report.add(
            "ok",
            "PAIR-OK",
            f"{left_id} 与 {right_id} 没有位置重叠，可以同时启用",
        )
    return report


# ---------------------------------------------------------------- 钩子目标


#: 随包分发的源码基线：``baseline/sources-<line>.zip``。
#:
#: 为什么需要它：``method_hooks`` 写错方法名时，框架**不会报错** —— 运行期
#: ``add_method_hook`` 返回 false，而扩展普遍写成
#: ``if not add_method_hook(...): return false``，于是一个错名字让后面所有
#: 钩子全部丢失，玩家看到的只是「装上了没反应」。
#:
#: 把 4.7 的 138 个脚本（约 1.2 MB）随开发包发出去，作者不用自备反解工程
#: 也能在打包阶段把这类错误挡下来。4.6 线直接复用已有的
#: ``canonical_sources.zip``（那就是 1.45 的源码层）。
SOURCES_NAME = "sources-{line}.zip"

#: 4.6 线回落的源码包（发布版启动器随包带的那份）。
SOURCES_FALLBACK = {"4.6": "canonical_sources.zip"}

#: 只认**方法定义**，不认调用点。Godot 4 的定义行形如 ``func name(...``。
_FUNC_RE = re.compile(r"^[ \t]*func[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(", re.M)


def _source_index_from_dir(root: Path) -> dict[str, set[str]]:
    """扫一个反解工程目录，得到 ``脚本相对路径 -> 方法名集合``。"""
    index: dict[str, set[str]] = {}
    for path in root.rglob("*.gd"):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        index[path.relative_to(root).as_posix()] = set(_FUNC_RE.findall(text))
    return index


def _source_index_from_zip(path: Path) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith(".gd"):
                    continue
                try:
                    text = zf.read(name).decode("utf-8", errors="replace")
                except (OSError, KeyError, zipfile.BadZipFile):
                    continue
                index.setdefault(_norm_target(name), set()).update(_FUNC_RE.findall(text))
    except (OSError, zipfile.BadZipFile):
        return {}
    return index


def _find_baseline_dir() -> Path | None:
    """往上找最近的 ``baseline/``，允许 ``JJXF_BASELINE_DIR`` 显式指定。

    与 :func:`_code_digest_for` 同一套规则：这个工具会被单独发给作者，
    不能假设当前工作目录就是启动器源码树。
    """
    env = os.environ.get("JJXF_BASELINE_DIR", "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, here.parent.parent, Path.cwd()):
        candidates.append(base / "baseline")
    for base in candidates:
        if base.is_dir():
            return base
    return None


def _load_source_index(
    source: Path | None, line: str
) -> tuple[dict[str, set[str]] | None, str]:
    """取「这条线的源码」，返回 ``(索引, 来源说明)``；索引 None 表示拿不到。"""
    if source is not None:
        source = Path(source)
        if source.is_dir():
            return _source_index_from_dir(source), str(source)
        if source.is_file() and source.suffix.lower() == ".zip":
            return _source_index_from_zip(source), str(source)
        return None, ""
    base = _find_baseline_dir()
    if base is None:
        return None, ""
    names = [SOURCES_NAME.format(line=line)]
    if line in SOURCES_FALLBACK:
        names.append(SOURCES_FALLBACK[line])
    for name in names:
        path = base / name
        if not path.is_file():
            continue
        index = _source_index_from_zip(path)
        if index:
            return index, f"随包源码 {name}"
    return None, ""


def _sources_zip_for(line: str) -> Path | None:
    """这条线的随包源码 zip（没有返回 None）。"""
    base = _find_baseline_dir()
    if base is None:
        return None
    names = [SOURCES_NAME.format(line=line)]
    if line in SOURCES_FALLBACK:
        names.append(SOURCES_FALLBACK[line])
    for name in names:
        path = base / name
        if path.is_file():
            return path
    return None


def _check_sources(report: Report, data: dict, line: str) -> None:
    """校验 ``sources`` 声明：包里的补丁/钩子是按哪份源码算的。

    为什么必须写：``text_patches`` 的锚点和 ``method_hooks`` 的方法签名都
    依赖具体源码文本。不声明时启动器只能盲打——补丁锚点可能照样命中，
    改的却是另一个函数，表现为「装上了、功能不对」，比装不上难查得多。

    规范分级：
    - schema 4：必填，缺失即 error；
    - schema ≤3：warn（存量包放行，但提示重打包）；
    - 写了就别写错：``line`` / ``sha256`` 与本机随包源码不一致 → error。
    """
    needs = bool(data.get("text_patches") or data.get("method_hooks"))
    if not needs:
        return
    schema = int(data.get("schema") or 1)
    raw = data.get("sources")
    if not isinstance(raw, dict):
        message = (
            "包里用了 text_patches / method_hooks，但没有 sources 声明——"
            "无法确认补丁是按哪份反解源码算的。"
        )
        if schema >= 4:
            report.error(
                "SRC-01",
                message + " schema 4 要求必填："
                '{"name":"sources-4.6.zip","sha256":"…","line":"4.6"}',
            )
        else:
            report.warn("SRC-00", message + " 旧格式放行；请用 1.7 工具重新打包。")
        return

    src_line = str(raw.get("line") or "")
    if src_line and src_line != line:
        report.error(
            "SRC-02",
            f"sources.line 写的是 {src_line}，但包声明的引擎线是 {line}",
        )
    name = str(raw.get("name") or "")
    sha = str(raw.get("sha256") or "").lower()
    if not sha:
        if schema >= 4:
            report.error("SRC-03", "sources.sha256 必填（用工具 manifest 自动生成）")
        else:
            report.warn("SRC-03", "sources 没有 sha256，无法核对源码版本")
    local = _sources_zip_for(line)
    if local is None:
        report.warn(
            "SRC-04",
            f"本机没有 baseline/{SOURCES_NAME.format(line=line)}，无法核对 sources.sha256",
        )
    elif sha:
        actual = hashlib.sha256(local.read_bytes()).hexdigest()
        if actual != sha:
            report.error(
                "SRC-05",
                f"sources.sha256 与本机随包源码不一致（声明 {sha[:12]}…，"
                f"实际 {actual[:12]}…）——锚点是按另一份源码算的",
            )
        elif name and name != local.name:
            report.warn(
                "SRC-06",
                f"sources.name 写的是 {name}，本机是 {local.name}（内容一致，可忽略）",
            )


def _check_hook_targets(
    report: Report,
    data: dict,
    file_targets: set[str],
    line: str,
    source: Path | None,
) -> None:
    """逐条核对 ``method_hooks`` 的目标脚本与方法是否真实存在。

    **这一步必须做**：钩子写错名字时打包、安装、装载三处都不会报错，
    只有运行期 ``add_method_hook`` 返回 false，而扩展普遍
    ``if not add_method_hook(...): return false`` —— 一个错名字让后面所有
    钩子一起失效。参考启动器在打包期就把这种错拦下来了，我们过去没有。

    三种情况分得很清：

    ================  ============================================
    情况              处置
    ================  ============================================
    目标由本包提供      跳过（脚本在包里，源码层当然没有）
    目标由 requires 提供 提醒（依赖包此刻不在手上，静态核不了）
    谁都提供不了        错误（这就是写错了）
    ================  ============================================
    """
    hooks = data.get("method_hooks")
    if not isinstance(hooks, list) or not hooks:
        return

    index, origin = _load_source_index(source, line)
    if index is None:
        report.warn(
            "HOOK-00",
            f"没有 {line} 线的源码，{len(hooks)} 条 method_hooks 无法核对目标是否存在。"
            f"用 `--source <反解工程>` 指定，或把 {SOURCES_NAME.format(line=line)} "
            "放进 baseline/。跳过这一步的代价：方法名写错时运行期静默失效，"
            "玩家只看到「装上了没反应」",
            "method_hooks",
        )
        return

    # 级别同 PATCH：4.7 判错误，4.6 只提醒。
    # 4.6 是冻结的存量线，历史包里大量「运行期才注册」的钩子写法，
    # 拿 4.7 的严格口径去卡它等于让工具否定既有生态。
    fail = report.error if line == "4.7" else report.warn
    requires = _declared_requires(data)
    checked = 0
    for pos, hook in enumerate(hooks):
        if not isinstance(hook, dict):
            continue
        target = _norm_target(hook.get("target"))
        method = str(hook.get("method") or "").strip()
        if not target or not method:
            fail(
                "HOOK-04", f"method_hooks[{pos}] 缺 target 或 method，这条钩子落不了地",
                "method_hooks",
            )
            continue
        if target in file_targets:
            # 本包自带的脚本，源码层里当然没有；运行期由覆盖层提供。
            checked += 1
            continue
        if target not in index:
            if requires:
                report.warn(
                    "HOOK-03",
                    f"method_hooks[{pos}] 的目标 {target} 不在 {line} 源码里，"
                    f"按 requires={sorted(requires)} 的依赖处理；安装时必须先装依赖，"
                    f"否则这条钩子会静默失效",
                    target,
                )
            else:
                fail(
                    "HOOK-01",
                    f"method_hooks[{pos}] 的目标脚本在 {line} 源码里不存在：{target}。"
                    f"它既不在包内、也没有声明 requires —— 这条钩子永远不会命中",
                    target,
                )
            continue
        if method not in index[target]:
            fail(
                "HOOK-02",
                f"method_hooks[{pos}] 在 {target} 里找不到方法 {method}()。"
                f"写错方法名时打包和安装都不会报错，只在运行期让 add_method_hook "
                f"返回 false —— 扩展普遍据此 return false，后面所有钩子会一起失效",
                target,
            )
            continue
        checked += 1

    if checked:
        report.add(
            "ok", "HOOK-OK",
            f"{checked}/{len(hooks)} 条钩子目标已对到 {line} 源码（来源：{origin}）",
        )


def _source_text_from_dir(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in (".gd", ".tscn", ".tres", ".gdshader", ".cfg"):
            continue
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
            out[path.relative_to(root).as_posix()] = path.read_text(
                encoding="utf-8", errors="replace"
            ).replace("\r\n", "\n")
        except OSError:
            continue
    return out


def _source_text_from_zip(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if Path(name).suffix.lower() not in (".gd", ".tscn", ".tres", ".gdshader", ".cfg"):
                    continue
                try:
                    text = zf.read(name).decode("utf-8", errors="replace")
                except (OSError, KeyError, zipfile.BadZipFile):
                    continue
                out[_norm_target(name)] = text.replace("\r\n", "\n")
    except (OSError, zipfile.BadZipFile):
        return {}
    return out


def _load_source_text(
    source: Path | None, line: str
) -> tuple[dict[str, str] | None, str]:
    """取「这条线的源码原文」，返回 ``(索引, 来源说明)``。"""
    if source is not None:
        source = Path(source)
        if source.is_dir():
            return _source_text_from_dir(source), str(source)
        if source.is_file() and source.suffix.lower() == ".zip":
            return _source_text_from_zip(source), str(source)
        return None, ""
    base = _find_baseline_dir()
    if base is None:
        return None, ""
    names = [SOURCES_NAME.format(line=line)]
    if line in SOURCES_FALLBACK:
        names.append(SOURCES_FALLBACK[line])
    for name in names:
        path = base / name
        if not path.is_file():
            continue
        index = _source_text_from_zip(path)
        if index:
            return index, f"随包源码 {name}"
    return None, ""


#: ``scope`` 的合法写法，与运行期 ``patcher.scope_region`` 保持同一套正则。
_SCOPE_RE = re.compile(r"^(?:(function|func|class)[ \t]*:[ \t]*)?([A-Za-z_]\w*)$")

_SCOPE_DECL = {
    "function": re.compile(r"^[ \t]*(?:static[ \t]+)?func[ \t]+{name}[ \t]*\(", re.M),
    "class": re.compile(r"^[ \t]*class[ \t]+{name}\b", re.M),
}

_SCOPE_NEXT_DECL = re.compile(
    r"^(?:(?:@\w+)|(?:static[ \t]+)?(?:func|var|const|signal|enum|class)\s)", re.M
)


def _count_anchor(text: str, find: str) -> int:
    """数锚点在这段文本里命中几次，容忍换行风格不一致。

    判据要和运行期 ``patcher._replace_in`` 保持一致：清单里的锚点基本是
    LF，而 Windows 上导出的 ``.gd`` 常是 CRLF。直接 ``str.count`` 会 0 命中，
    把一条运行期明明打得上的补丁误报成「基线可能已变」。

    只做 LF 锚点 → CRLF 源码这一个方向。反方向（CRLF 锚点 + LF 源码）
    不兜：那种锚点自己就该先归一，兜了反而把作者的问题藏起来。
    """
    if "\n" in find and "\r" not in find:
        pattern = re.compile(r"\r?\n".join(re.escape(part) for part in find.split("\n")))
        return len(pattern.findall(text))
    return text.count(find)


def _count_in_scope(text: str, find: str, scope: str) -> tuple[int, str]:
    """在 ``scope`` 框定的范围内数锚点命中几次；返回 ``(次数, 错误)``。

    为什么不 import 运行期的 ``patcher.scope_region``：``jjm_pack.py`` 要
    **单独发给作者**，工具箱里没有 ``app/`` 全量源码，import 会直接崩。
    所以这里照着那份实现写一份等价逻辑 —— 判据必须一致，代码必须独立。
    """
    matched = _SCOPE_RE.match(str(scope or "").strip())
    if not matched:
        return 0, f"scope 写法不对：{scope!r}（应形如 function:open_package）"
    kind = "class" if (matched.group(1) or "") == "class" else "function"
    name = matched.group(2)
    pattern = re.compile(_SCOPE_DECL[kind].pattern.format(name=re.escape(name)), re.M)
    hits = list(pattern.finditer(text))
    if not hits:
        label = "函数" if kind == "function" else "类"
        return 0, f"作用域不存在：{scope}（源码里找不到这个{label}）"
    if len(hits) > 1:
        return 0, f"作用域不唯一：{scope} 匹配到 {len(hits)} 个同名声明"
    nxt = _SCOPE_NEXT_DECL.search(text, hits[0].end())
    end = nxt.start() if nxt else len(text)
    return _count_anchor(text[hits[0].start():end], find), ""


def _check_patch_anchors(
    report: Report,
    data: dict,
    line: str,
    source: Path | None,
) -> None:
    """逐条核对 ``text_patches`` 的锚点在源码里命中几次。

    **这一步是 PATCH-01 的核心**：``expect_count`` 过去只是清单里写着玩的
    一个字段，没有任何人核对。于是「锚点在 4.7 里命中 2 处」这种问题能一路
    通过 check / verify / seal，直到玩家装上才发现某条改动没生效。

    实测教训：``mch.treasure_dup`` 的
    ``if not persist(before):\\n\\t\\treturn {}`` 在 4.7 的 ``pet_state.gd``
    里同时出现在 ``open_package()`` 和 ``bank_experience_card()``，
    清单写 ``expect_count: 1``，实际 2 处 —— 补丁被拒，礼盒保底失效，
    而界面上只有一行小字。

    ``scope`` 是同一件事的另一半：把搜索范围收窄到某个函数，
    让「函数内唯一」代替「全文件唯一」（4.7 会持续更新，全局唯一撑不住）。
    """
    patches = data.get("text_patches")
    if not isinstance(patches, list) or not patches:
        return

    index, origin = _load_source_text(source, line)
    if index is None:
        report.warn(
            "PATCH-00",
            f"没有 {line} 线的源码原文，{len(patches)} 条 text_patches 的锚点无法核对。"
            f"用 `--source <反解工程>` 指定，或把 {SOURCES_NAME.format(line=line)} "
            "放进 baseline/。跳过这一步的代价：锚点失配时只在安装期报一行小字，"
            "玩家那边表现为「装了但某个功能没有」",
            "text_patches",
        )
        return

    # 级别：4.7 判错误，4.6 只提醒。
    #
    # 依据是项目自己那条判 error 的标准 —— 「会不会导致拒装」。严格说
    # 补丁失配不会拒装（`install.partial` 照装），但 4.7 是固定完全校验的线，
    # 半条补丁失效＝功能静默缺失，不能当没事发生；4.6 是冻结的存量线，
    # 那边有大量历史包就是这么写的，突然判死等于让工具去否定既有生态。
    strict = line == "4.7"
    fail = report.error if strict else report.warn
    ok = 0
    for pos, patch in enumerate(patches):
        if not isinstance(patch, dict):
            continue
        target = _norm_target(patch.get("target"))
        find = str(patch.get("find") or "")
        if not target or not find:
            continue
        text = index.get(target)
        if text is None:
            # 目标不在源码里：要么是包内自带脚本，要么是本该由 requires 提供。
            # 这两种都不该在这里判死（SELF-01 / SELF-03 已经管了）。
            continue
        expect = int(patch.get("expect_count", 1) or 0)
        scope = str(patch.get("scope") or "").strip()
        if scope:
            count, error = _count_in_scope(text, find, scope)
            if error:
                fail(
                    "PATCH-02",
                    f"text_patches[{pos}] 的 scope 用不了：{error}",
                    target,
                )
                continue
        else:
            count = _count_anchor(text, find)

        if not expect:
            # 作者显式写 0 = 不做命中数校验，只做能命中就行的宽松模式。
            if count == 0:
                report.warn(
                    "PATCH-03",
                    f"text_patches[{pos}] 的锚点在 {target} 里一处都没命中（expect_count=0，按放行处理）",
                    target,
                )
            else:
                ok += 1
            continue

        if count == expect:
            ok += 1
            continue

        where = f"{target}" + (f" · {scope}" if scope else "")
        if count == 0:
            fail(
                "PATCH-01",
                f"text_patches[{pos}] 的锚点在 {line} 源码里一处都没命中"
                f"（期望 {expect} 处）。基线改过了，这条补丁装上也不生效",
                where,
            )
        elif count > expect:
            hint = "" if scope else "；如果它只在某个函数里唯一，加 scope:\"function:函数名\""
            fail(
                "PATCH-01",
                f"text_patches[{pos}] 的锚点在 {where} 里命中 {count} 处，"
                f"期望 {expect} 处 —— 多处命中会被补丁器整条拒绝，"
                f"这条改动不会生效{hint}",
                where,
            )
        else:
            fail(
                "PATCH-01",
                f"text_patches[{pos}] 的锚点在 {where} 里只命中 {count} 处，"
                f"期望 {expect} 处 —— 清单与实际基线对不上",
                where,
            )

    if ok:
        report.add(
            "ok", "PATCH-OK",
            f"{ok}/{len(patches)} 条文本补丁锚点命中数与 expect_count 一致（来源：{origin}）",
        )


def check_package(
    package: Path, line: str = None, source: Path | None = None
) -> Report:
    """完整静态自检。返回报告，由调用方决定如何展示。

    ``line`` 指定引擎线（``"4.6"`` / ``"4.7"``）。``None`` 表示「按包自己
    声明的线来判断」—— 这样同一个包无论作者写的哪条线都能被正确校验，
    不会因为工具默认 4.6 就把 4.7 的包判死。

    ``source`` 是核对钩子目标用的源码（反解工程目录，或打包好的源码 zip）。
    不给则用 ``baseline/sources-<line>.zip``，两者都没有时出 ``HOOK-00``
    提醒 —— **不静默放过**，否则「验证过了」和「真的验过」就分不清了。
    """
    report = Report()
    # 下面 files 循环里的局部变量 `source` 会覆盖同名参数，先存一份。
    hook_source = Path(source) if source is not None else None
    if not package.exists():
        report.error("PKG-00", f"路径不存在：{package}")
        return report

    with PackageView(package) as pkg:
        names = set(pkg.names)
        if "extension.json" not in names:
            report.error("PKG-01", "包根目录缺少 extension.json（规范要求放在根目录）")
            return report

        try:
            data = json.loads(pkg.read("extension.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            report.error("PKG-02", f"extension.json 不是合法 UTF-8 JSON：{exc}")
            return report

        if not isinstance(data, dict):
            report.error("PKG-03", "extension.json 顶层必须是对象")
            return report

        # --- 清单字段
        for key in REQUIRED_FIELDS:
            if key not in data:
                report.error("MAN-01", f"缺少必填字段：{key}")

        schema = data.get("schema")
        if schema not in SCHEMA_SUPPORTED:
            report.error("MAN-02", f"schema 必须是 {SCHEMA_SUPPORTED}，当前 {schema!r}")
        elif schema == 1:
            report.warn("MAN-03", "schema:1 会被启动器自动升格；新包请直接写 schema:3")
        elif schema == 2:
            report.warn(
                "MAN-03",
                "schema:2 仍然可用，但没有位置级接管能力；新包建议写 schema:3",
            )

        unknown_fields = sorted(
            k for k in data if k not in KNOWN_FIELDS and not k.startswith("x_")
        )
        if unknown_fields:
            report.warn(
                "MAN-17",
                f"未识别的顶层字段：{'、'.join(unknown_fields)}（启动器会忽略并提示；"
                f"自定义字段请用 x_ 前缀）",
            )

        ext_id = str(data.get("id") or "")
        if not ID_RE.match(ext_id):
            report.error(
                "MAN-04",
                f"id 不合规：{ext_id!r}（小写字母/数字开头，仅含 a-z0-9._-，长度 2-64）",
            )

        kind = data.get("kind")
        if kind is None:
            # 启动器对老包按 extension 处理，规范跟随运行时行为，不把缺省判成错误。
            report.warn("MAN-05", "未写 kind，启动器按 extension 处理；新包建议显式填写")
        elif kind not in KIND_ENUM:
            report.error("MAN-05", f"kind 必须是 {KIND_ENUM} 之一，当前 {kind!r}")

        # --- 引擎线与基线
        #
        # 判定规则：
        # - 调用方给了 ``line``（命令行 --line）→ 必须与它一致；
        # - 没给 → 按包自己声明的线判断（任一已知线的别名都接受），
        #   再看基线 id 与 engine.line 是否与那条线自洽。
        #
        # 为什么允许「不指定就按包声明」：一个工具要同时服务两条线的作者，
        # 默认锁死 4.6 会让所有 4.7 的包都判不合格 —— 那是工具的问题，
        # 不是包的问题。
        bl = data.get("baseline")
        bl_id = bl.get("id") if isinstance(bl, dict) else bl
        eng = data.get("engine")
        declared_line = eng.get("line") if isinstance(eng, dict) else None

        profile_line = _resolve_line(bl_id, declared_line, line)
        if profile_line is None:
            known = "、".join(f"{k}（{v['baseline_id']}）" for k, v in LINE_PROFILES.items())
            report.error(
                "MAN-06",
                f"baseline 指向 {bl_id!r}，不认识。本规范支持的基线：{known}",
            )
            profile_line = ENGINE_LINE if line is None else line
        profile = LINE_PROFILES.get(profile_line) or LINE_PROFILES[ENGINE_LINE]

        if bl_id is not None and str(bl_id) not in profile["aliases"]:
            report.error(
                "MAN-06",
                f"baseline 指向 {bl_id!r}，与引擎线 {profile_line} 不匹配"
                f"（该线应为 {profile['baseline_id']}，即 {profile['label']}）",
            )
        if isinstance(eng, dict) and eng.get("line") not in (None, profile_line):
            report.error(
                "MAN-07",
                f"engine.line 必须是 {profile_line}，当前 {eng.get('line')!r}",
            )

        # --- 源码声明（1.7 起强制）
        _check_sources(report, data, profile_line)

        # --- files
        files = data.get("files") or []
        file_targets: set[str] = set()
        if not isinstance(files, list):
            report.error("MAN-08", "files 必须是数组")
            files = []
        for index, rec in enumerate(files):
            if not isinstance(rec, dict):
                report.error("MAN-09", f"files[{index}] 不是对象")
                continue
            source = _norm_target(rec.get("source"))
            target = _norm_target(rec.get("target"))
            if not source or not target:
                report.error("MAN-10", f"files[{index}] 缺 source 或 target")
                continue
            if ".." in target.split("/"):
                report.error("MAN-11", f"files[{index}] target 越界：{target}")
            if source not in names:
                report.error("MAN-12", f"files[{index}] 的源文件不在包内：{source}")
            file_targets.add(target)

        # --- 补丁与自包含
        missing_base: dict[str, list[int]] = {}
        requires = _declared_requires(data)
        for key in ("text_patches", "data_patches"):
            patches = data.get(key) or []
            if not isinstance(patches, list):
                report.error("MAN-13", f"{key} 必须是数组")
                continue
            for index, patch in enumerate(patches):
                if not isinstance(patch, dict):
                    report.error("MAN-14", f"{key}[{index}] 不是对象")
                    continue
                target = _norm_target(patch.get("target"))
                if not target:
                    report.error("MAN-15", f"{key}[{index}] 缺 target")
                    continue
                if ".." in target.split("/"):
                    report.error("MAN-16", f"{key}[{index}] target 越界：{target}")
                    continue
                if key == "text_patches":
                    suffix = "." + target.rsplit(".", 1)[-1].lower() if "." in target else ""
                    if suffix in NEEDS_BASE_SUFFIXES and target not in names \
                            and target not in file_targets:
                        missing_base.setdefault(target, []).append(index)
                    elif suffix in PLAIN_SUFFIXES:
                        pass  # 基线里是明文，运行时可读
                    elif not suffix:
                        report.warn("SELF-02", f"补丁目标没有扩展名，无法判断能否取到原文：{target}")
                    if not str(patch.get("find") or "").strip() and int(
                        patch.get("expect_count", 1) or 1
                    ) > 0:
                        report.error(
                            "MAN-26",
                            f"{key}[{index}] 没有 find 锚点，这一条不知道改哪里",
                            target,
                        )
                for op_index, op in enumerate(_iter_ops(patch)):
                    where = f"{key}[{index}].operations[{op_index}]"
                    _check_op(report, op, where, target, ext_id, requires)

        self_points = collect_points(data, names)
        _check_self_overlap(report, self_points)

        # --- 补丁锚点：命中数必须与 expect_count 一致
        _check_patch_anchors(report, data, profile_line, hook_source)

        # --- 钩子目标：逐条对到源码
        #
        # 放在补丁检查之后：它需要 file_targets（判断钩子是不是指向包内脚本）
        # 和 requires（判断目标是不是依赖包提供的）。
        _check_hook_targets(report, data, file_targets, profile_line, hook_source)

        # 1.7 起启动器随包带 `sources-<line>.zip`，补丁目标只要在那份源码里
        # 就能在运行时取到原文——不再要求作者把原版脚本打进包里（那会覆盖
        # 基线、把别的扩展的补丁冲掉）。这里先按源码兜底放行，只有源码里
        # 也没有的目标才算「不自包含」。
        src_index, src_note = _load_source_index(hook_source, profile_line)
        for target, indexes in missing_base.items():
            if src_index is not None and target in src_index:
                report.add(
                    "ok",
                    "SELF-04",
                    f"{target} 由随包源码兜底（{src_note}），无需自带基底",
                    target,
                )
                continue
            if requires:
                # 作者声明了依赖：该文件由被依赖的扩展提供，安装时必须先装它。
                # 静态检查看不到依赖包的产出，按"待确认"处理而不是判死。
                report.warn(
                    "SELF-03",
                    f"补丁目标未随包提供：{target}（text_patches 第 {indexes} 条），"
                    f"按 requires={sorted(requires)} 的依赖处理；安装时须先装依赖，否则取不到原文",
                    target,
                )
                continue
            report.error(
                "SELF-01",
                f"补丁目标不自包含：{target}（text_patches 第 {indexes} 条）。"
                f"包内没有这份原文，也没有声明 requires，玩家端没有源码工程时会因取不到原文而失败。"
                f"用 `jjm_pack.py fix <包> --source <反解工程>` 补入自带基底。",
                target,
            )

        # --- 联机闸门
        caps_text = json.dumps(
            {k: data.get(k) for k in ("capabilities", "features") if k in data},
            ensure_ascii=False,
        ).lower()
        for word in DECLARED_CAPS:
            if word in caps_text:
                report.error("NET-00", f"清单自述携带联机能力：{word}", "extension.json")

        scan_targets = ["extension.json"]
        scan_targets += [n for n in names if n.lower().endswith(TEXT_SUFFIXES) and n != "extension.json"]
        for name in scan_targets:
            if name == "extension.json":
                text = json.dumps(data, ensure_ascii=False)
            else:
                try:
                    if len(pkg.names) and name not in names:
                        continue
                    text = pkg.read(name).decode("utf-8", errors="ignore")
                except (OSError, KeyError, RuntimeError):
                    continue
            _scan_text(report, name, text)

        # --- 体积与条目
        if len(names) > MAX_ENTRIES:
            report.error("PKG-04", f"条目数 {len(names)} 超过上限 {MAX_ENTRIES}")
        total = 0
        for name in names:
            try:
                size = len(pkg.read(name))
            except (OSError, KeyError, RuntimeError):
                continue
            total += size
            if size > MAX_SINGLE_BYTES:
                report.error("PKG-05", f"单文件超限（{size} 字节）：{name}")
        if total > MAX_TOTAL_BYTES:
            report.error("PKG-06", f"包体总量 {total} 字节超过上限 {MAX_TOTAL_BYTES}")

    if report.ok:
        report.add(
            "ok",
            "SPEC-OK",
            f"符合 {SPEC_VERSION} 打包规范"
            f"（引擎线 {profile_line}，基线 {profile['baseline_id']}）",
        )
    return report


def _resolve_line(
    baseline_id: object, declared_line: object, forced: str | None
) -> str | None:
    """定出这个包属于哪条引擎线。

    优先级：命令行强制 > 包里的 ``engine.line`` > 基线 id 反查。

    反查这一步是必要的：老包里常见只写 ``baseline`` 不写 ``engine``，
    这时只能靠基线 id 认线。
    """
    if forced:
        return forced if forced in LINE_PROFILES else None
    text = str(declared_line or "").strip()
    if text in LINE_PROFILES:
        return text
    target = str(baseline_id or "").strip()
    if not target:
        # 两者都没写：按默认线处理（工具既有行为），不额外报错 ——
        # 「没声明」本身已由其它规则提示。
        return ENGINE_LINE
    for name, profile in LINE_PROFILES.items():
        if target in profile["aliases"]:
            return name
    return None


# ---------------------------------------------------------------- fix


def fix_package(package: Path, source: Path, out: Path) -> Report:
    """把缺的自带基底从反解工程补进包内，产出可发布的自包含包。"""
    report = Report()
    if not source.is_dir():
        report.error("FIX-00", f"--source 不是目录：{source}")
        return report

    with PackageView(package) as pkg:
        names = pkg.names
        if "extension.json" not in names:
            report.error("FIX-01", "包根目录缺少 extension.json")
            return report
        data = json.loads(pkg.read("extension.json").decode("utf-8"))

        targets: list[str] = []
        for patch in data.get("text_patches") or []:
            target = _norm_target(patch.get("target"))
            if not target:
                continue
            suffix = "." + target.rsplit(".", 1)[-1].lower() if "." in target else ""
            if suffix not in NEEDS_BASE_SUFFIXES:
                continue
            if target in names:
                continue
            if target not in targets:
                targets.append(target)

        if not targets:
            report.add("ok", "FIX-02", "所有补丁目标都已自带基底，无需处理")
            return report

        added: list[str] = []
        skipped: list[str] = []
        for target in targets:
            src = source / target
            if src.is_file():
                added.append(target)
            else:
                skipped.append(target)

        if skipped:
            for target in skipped:
                report.warn(
                    "FIX-03",
                    f"反解工程里找不到 {target}；若它由 requires 声明的扩展提供，属于正常依赖，保持原样",
                )

        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zo:
            for name in names:
                zo.writestr(name, pkg.read(name))
            for target in added:
                zo.writestr(target, (source / target).read_bytes())

    if added:
        report.add("ok", "FIX-01", f"已补入 {len(added)} 份基底：" + "、".join(added))
        report.add("ok", "FIX-04", f"产物：{out}")
    return report


# ---------------------------------------------------------------- pack / manifest


# .godot 是 Godot 本地导入缓存：里面 .godot/imported/*.ctex 带 32 位哈希，
# 单文件名可达 99 字符，打进分发包后深目录安装会顶到 Windows MAX_PATH(260)
# 导致解压失败（误报「文件可能损坏」）。.import 是同族导入元数据。
SKIP_DIRS = {"__pycache__", ".git", ".svn", ".idea", ".vscode", ".pytest_cache", ".godot", ".import"}
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def pack_dir(folder: Path, out: Path) -> Report:
    report = Report()
    if not folder.is_dir():
        report.error("PACK-00", f"不是目录：{folder}")
        return report
    if not (folder / "extension.json").is_file():
        report.error("PACK-01", "目录根下缺少 extension.json")
        return report

    probe = check_package(folder)
    # `SELF-01` 是 1.5 时代的旧口径（见规范 4.1.1）：发布版启动器随包带
    # `baseline/canonical_sources.zip`，改基线脚本不必再自带原文，`verify` 早已把它
    # 降为提醒。pack 若仍按 check 的严格口径中止，就成了「规范允许、工具不让打包」。
    # 其余 error 照旧中止。
    for finding in probe.findings:
        if finding.code == "SELF-01":
            report.warn(finding.code, finding.message, finding.where)
        else:
            report.findings.append(finding)
    if [f for f in report.findings if f.level == "error"]:
        report.error("PACK-02", "自检未通过，已中止打包（先修掉上面的错误）")
        return report

    entries: list[tuple[str, Path]] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(folder)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.name in SKIP_NAMES or rel.name.endswith((".pyc", ".pyo", ".tmp", ".bak", ".import")):
            continue
        if path.is_symlink():
            report.error("PACK-03", f"包内不允许符号链接：{rel.as_posix()}")
            continue
        entries.append((rel.as_posix(), path))

    if report.errors:
        return report

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, path in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    report.add("ok", "PACK-OK", f"已打包 {len(entries)} 个文件 → {out}")
    report.add("ok", "PACK-SHA", f"sha256={digest}")
    return report


MANIFEST_TEMPLATE = {
    "schema": 4,
    "id": "",
    "name": "",
    "version": "1.0.0",
    "kind": "extension",
    "author": "",
    "license": "",
    "baseline": {"id": BASELINE_ID, "engine": {"line": ENGINE_LINE}},
    "priority": 50,
    "requires": [],
    "conflicts": [],
    "load_after": [],
    "load_before": [],
    "claims": [],
    "files": [],
    # 数据补丁按 JSON Pointer 写到具体位置；expect 断言「改之前应该是什么」，
    # 断言不成立时启动器按 on_miss 处理（abort/warn/skip），不会静默改错地方。
    "data_patches": [
        {
            "target": "data/example.json",
            "operations": [
                {
                    "op": "replace",
                    "path": "/catalog/100001/icon_path",
                    "expect": "assets/equipment/icons/old.png",
                    "value": "assets/equipment/icons/new.png",
                    "on_miss": "abort",
                }
            ],
        }
    ],
    "text_patches": [],
}


def write_manifest(
    folder: Path, ext_id: str, name: str, force: bool = False, line: str = ENGINE_LINE
) -> Report:
    report = Report()
    target = folder / "extension.json"
    if target.exists() and not force:
        report.error("MANI-01", f"已存在 extension.json，未覆盖（要覆盖加 --force）：{target}")
        return report
    if not ID_RE.match(ext_id):
        report.error("MANI-02", f"id 不合规：{ext_id!r}")
        return report
    profile = LINE_PROFILES.get(line) or LINE_PROFILES[ENGINE_LINE]
    data = json.loads(json.dumps(MANIFEST_TEMPLATE))   # 深拷贝，避免改到模板
    data["id"] = ext_id
    data["name"] = name or ext_id
    # 按选中的引擎线写基线，而不是永远 1.45
    data["baseline"] = {
        "id": profile["baseline_id"],
        "engine": {"line": line},
    }
    # 源码声明：直接写本机随包源码的文件名与 sha256。
    # 作者不需要手抄哈希；用错源码的包会在 check / 安装期被挡下来。
    local = _sources_zip_for(line)
    if local is not None:
        data["sources"] = {
            "name": local.name,
            "sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
            "line": line,
        }
        report.add(
            "ok",
            "MANI-SRC",
            f"已写入源码声明：{local.name}（sha256 {data['sources']['sha256'][:12]}…）",
        )
    else:
        report.warn(
            "MANI-SRC",
            f"本机没有 baseline/sources-{line}.zip，未能写入 sources 声明；"
            "带 text_patches / method_hooks 的包会被 schema 4 判不合格",
        )
    folder.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.add(
        "ok", "MANI-OK", f"已生成骨架（{line} 线，基线 {profile['baseline_id']}）：{target}"
    )
    return report


# ------------------------------------------------- 完全校验（strict）准入预检
#
# 与 ``check`` 的分工：check 判「这个包合不合规」，verify 判「这个包在开发者版
# 里能不能一导入就过」。开发者校验是导入即准入，静态过 ≠ 装得上，所以这里
# 额外把 schema、钩子固化、接管三条件、位置重叠、依赖闭环、合集成员加严。
# 错误码 ADM-01..10 见《扩展打包规范-开发者校验》第 9 节。

ADM_MODE = "strict"


def _content_digest_of(package: Path) -> str:
    """包内容指纹（用于验证记录）。

    与 ``app/core/ext/verify_mark.content_digest`` 必须**算法一致**，
    否则启动器那边永远判 ``stale``。这里不 import 那个模块：
    工具要能独立分发给作者，不能依赖启动器源码树。
    两边都遵守同一份约定——排序后的 ``相对路径 + sha256(内容)``，
    跳过 ``verification.json`` 自身。
    """
    import hashlib

    RECORD = "verification.json"
    h = hashlib.sha256()
    if package.is_dir():
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(package).as_posix()
            if rel == RECORD:
                continue
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            try:
                h.update(hashlib.sha256(path.read_bytes()).digest())
            except OSError:
                h.update(b"<unreadable>")
            h.update(b"\n")
        return h.hexdigest()
    # zip 形态：按条目名排序，同样跳过记录文件
    with zipfile.ZipFile(package) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith("/") or name == RECORD:
                continue
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            h.update(hashlib.sha256(zf.read(name)).digest())
            h.update(b"\n")
    return h.hexdigest()


def _code_digest_for(line: str) -> str:
    """读 ``baseline/identity-<line>.json`` 的代码层指纹。

    找不到就返回空串 —— 空串意味着「无法绑定」，那种情况下**不能出记录**：
    出了也只会被判 ``stale``，反而让作者以为已经验过了。

    注意路径推导：这个工具会被**单独发给作者**（不在启动器源码树里），
    所以不能假设 ``baseline/`` 一定存在于当前工作目录。往上找最近的
    ``baseline/``，同时允许用环境变量 ``JJXF_BASELINE_DIR`` 显式指定。
    """
    env = os.environ.get("JJXF_BASELINE_DIR", "").strip()
    roots: list[Path] = []
    if env:
        roots.append(Path(env))
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, here.parent.parent, Path.cwd()):
        roots.append(base / "baseline")

    for base in roots:
        for name in (f"identity-{line}.json", f"identity-v{line}.json"):
            path = base / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            layer = data.get("code_layer") or {}
            digest = str(layer.get("digest") or "")
            if digest:
                return digest
    return ""


def seal_package(
    package: Path,
    *,
    line: str | None = None,
    force: bool = False,
    source: Path | None = None,
) -> tuple[Report, Path | None]:
    """跑一遍检查并写入 ``verification.json``。

    返回 ``(报告, 写入路径或 None)``。

    三条硬约束：

    1. **绝不写进 extension.json** —— 参考启动器对清单字段是严格白名单，
       多一个字段就拒接（``compat.py:293``）。所以记录放独立文件。
    2. **必须有 code_digest** —— 拿不到基线代码层指纹时不出记录。
       否则记录一写出来就是过期的，只会误导人。
    3. **content_digest 要在写记录之前算** —— 写进去之后再算会把自己
       算进指纹，启动器那边立刻判 stale。
    """
    report = Report()
    if not package.exists():
        report.error("SEAL-00", f"路径不存在：{package}")
        return report, None

    data, names = _read_manifest(package)
    if not data:
        report.error("SEAL-01", "读不到 extension.json，无法确定这个包属于哪条线")
        return report, None

    resolved = _resolve_line(
        (data.get("baseline") or {}).get("id") if isinstance(data.get("baseline"), dict)
        else data.get("baseline"),
        (data.get("engine") or {}).get("line") if isinstance(data.get("engine"), dict) else None,
        line,
    )
    if resolved is None:
        report.error("SEAL-02", f"无法确定引擎线（baseline={data.get('baseline')!r}）")
        return report, None

    code_digest = _code_digest_for(resolved)
    if not code_digest:
        report.error(
            "SEAL-03",
            f"找不到 {resolved} 线的代码层指纹（baseline/identity-{resolved}.json）。"
            "没有它就无法把记录绑定到具体版本，出了记录也会立刻过期。",
        )
        return report, None

    # 静态检查 + 准入预检：两条都过才出记录
    #
    # `SELF-01` 在这里必须**降为提醒**，与 `verify` / `pack` 保持一致。
    #
    # 2026-11-10 修（P8 的同类遗漏）：`check` 把 SELF-01 判 error 是 1.5 时代
    # 的口径 —— 那时改基线脚本必须自带原文。现在发布版启动器随包带
    # `baseline/canonical_sources.zip`，运行时能取到原文，这条已不成立。
    # `verify_package()` 与 `pack_dir()` 都已降级，唯独 `seal_package()`
    # 原样倒入 `check` 的 error，导致：
    #
    #     verify -> ADMITTED（0 error）
    #     seal   -> SELF-01 × N + SEAL-04，拒绝出记录
    #
    # 于是合法包永远拿不到 `verification.json`，被 4.7 的 strict 档以
    # `verify.absent` 剔除。表现为「包能装、覆盖层也生成了，但游戏里没反应」。
    #
    # 判死的口径必须只有一份：会**导致拒装**的错才算 error。SELF-01 不会
    # 导致拒装（运行期有 canonical 层兜底），所以它是提醒不是错误。
    base = check_package(package, resolved, source)
    for f in base.findings:
        if f.code == "SELF-01":
            report.warn(f.code, f.message, f.where)
        else:
            report.findings.append(f)
    adm = verify_package(package, source=source)
    for f in adm.findings:
        # 准入预检的 ok 项不重复堆，只要问题
        if f.level != "ok":
            report.findings.append(f)

    if not report.ok and not force:
        report.error(
            "SEAL-04",
            "检查未通过，未写验证记录。修完再跑，或加 --force（不建议："
            "记录里会标出未通过的项，启动器侧仍按完全校验处置）。",
        )
        return report, None

    # 内容指纹必须在写记录**之前**算
    content = _content_digest_of(package)

    # ``passed`` 记的是「哪几类检查通过了」。
    #
    # jjm_pack 的 Finding 只有 level/code（不像启动器的 inspect.Finding 带
    # check 字段），所以按错误码前缀归类：MAN- / PKG- / MANI- 属于静态规范，
    # ADM- 属于准入预检，SELF- 属于自包含检查。
    _KIND = {
        "MAN": "manifest",
        "PKG": "package",
        "MANI": "manifest",
        "ADM": "admission",
        "SELF": "selfcontained",
        "SPEC": "spec",
    }
    failed_kinds = {
        _KIND.get(f.code.split("-")[0], "other")
        for f in report.findings
        if f.level == "error"
    }
    all_kinds = {
        _KIND.get(f.code.split("-")[0], "other")
        for f in report.findings
        if f.level in ("error", "ok")
    }
    passed = sorted((all_kinds - failed_kinds) - {"other"})

    record = {
        "schema": 1,
        "line": resolved,
        "code_digest": code_digest,
        "content_digest": content,
        # 内容指纹是对着什么算的：
        #   package —— 源 zip（载荷 mods/**）；装完文件集合变了，不可比
        #   dir     —— 解包目录；装完还在同一个目录，可比
        # 判定端靠这个字段决定要不要比 content_digest，否则「装完永远 stale」。
        "content_scope": "package" if package.is_file() else "dir",
        "passed": passed,
        "tool": "jjm_pack",
        "tool_version": TOOL_VERSION,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if not report.ok:
        record["forced"] = True
        record["open_issues"] = [
            {"code": f.code, "message": f.message}
            for f in report.findings
            if f.level == "error"
        ]

    if package.is_dir():
        target = package / "verification.json"
        target.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return report, target

    # zip 形态：在原包旁边另存一份带记录的包，不动原件 —— 覆盖原 zip
    # 等于销毁作者手上的那份，出问题没法对比。
    out = package.with_name(package.stem + ".sealed.zip")
    with zipfile.ZipFile(package) as src, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            if item.filename == "verification.json":
                continue
            dst.writestr(item, src.read(item.filename))
        dst.writestr(
            "verification.json",
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        )
    return report, out


def _read_manifest(package: Path) -> tuple[dict, set[str]]:
    """读包内根目录的 extension.json。读不到返回 ({}, 名字集合)。"""
    try:
        with PackageView(package) as pkg:
            names = set(pkg.names)
            if "extension.json" not in names:
                return {}, names
            data = json.loads(pkg.read("extension.json").decode("utf-8"))
            return (data if isinstance(data, dict) else {}), names
    except (OSError, ValueError, UnicodeDecodeError, KeyError, RuntimeError, zipfile.BadZipFile):
        return {}, set()


def _requires_ids(data: dict) -> tuple[set[str], dict[str, str]]:
    """返回 (依赖 id 集合, id → 版本范围)。字典与数组两种写法都认。

    启动器 ``manifest.py`` 同时接受这两种写法，而 schema 文件里只声明了数组，
    所以这里不能只按 schema 来判，否则会把合法包误杀。
    """
    raw = data.get("requires")
    ids: set[str] = set()
    ranges: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            rid = str(key)
            ids.add(rid)
            if isinstance(value, str):
                ranges[rid] = value
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                ids.add(item)
            elif isinstance(item, dict):
                rid = str(item.get("id") or "")
                if rid:
                    ids.add(rid)
                    if isinstance(item.get("version"), str):
                        ranges[rid] = item["version"]
    return ids, ranges


def _takeovers(data: dict) -> list[tuple[str, bool, list[str]]]:
    """收集位置级 overrides 声明：``(位置, 是否带 expect, 点名的 id)``。

    只扫 ``data_patches[].operations[]``，与启动器 ``manifest.py::_check_op``
    （MAN-22/23/25）完全一致。``files[]`` 与 ``text_patches[]`` 上的同名键
    启动器并不按「位置级接管」处理，拿这三条件去判会把合法包误杀。
    """
    out: list[tuple[str, bool, list[str]]] = []
    for index, patch in enumerate(data.get("data_patches") or []):
        if not isinstance(patch, dict):
            continue
        for op_index, op in enumerate(_iter_ops(patch)):
            if not isinstance(op, dict):
                continue
            named = op.get("overrides")
            if isinstance(named, list) and named:
                out.append(
                    (
                        f"data_patches[{index}].operations[{op_index}]",
                        op.get("expect") is not None,
                        [str(x) for x in named if isinstance(x, str) and x.strip()],
                    )
                )
    return out


def _takes_over(a: dict, b: dict, requires: set[str]) -> bool:
    """a 是否合法接管了 b：点名对方 + 带 expect + requires 里有对方。"""
    other = str(b.get("ext") or "")
    return bool(
        other
        and other in set(a.get("overrides") or [])
        and a.get("expect") is not None
        and other in requires
    )


def _bundle_members(package: Path) -> list[str]:
    """合集包（外层全是成员 zip、根没有 extension.json）的成员名列表。"""
    if not package.is_file():
        return []
    try:
        with zipfile.ZipFile(package) as outer:
            names = outer.namelist()
            if "extension.json" in names:
                return []
            return [n for n in names if n.lower().endswith(".zip")]
    except (OSError, zipfile.BadZipFile):
        return []


def temp_workspace(package: Path) -> str | None:
    """解包用的临时目录**优先放在包所在盘**，返回 ``None`` 表示用系统 Temp。

    为什么不直接用系统 Temp：Windows 的 ``TEMP`` 默认在 C 盘，而合集包动辄
    上百 MB。本项目有一条硬约定——**不往 C 盘放东西**（依赖、缓存、临时物
    都算），作者机器上通常也只有 D/E 盘有空间。

    顺序：``JJXF_TMP`` 环境变量 → 包所在目录 → 系统 Temp。
    ``TemporaryDirectory`` 会在选中的目录里建 ``jjm-verify-*`` 并在结束时删掉，
    所以不会在作者的包旁边留垃圾；选中的目录不可写时会抛错，由调用方兜底。
    """
    env = os.environ.get("JJXF_TMP", "").strip()
    if env:
        return env
    try:
        return str(package.resolve().parent)
    except OSError:
        return None


def verify_bundle(package: Path, members: list[str]) -> Report:
    """合集包：每个成员都要独立过关，一个不过整包不装。"""
    report = Report()
    try:
        outer = zipfile.ZipFile(package)
    except (OSError, zipfile.BadZipFile) as exc:
        report.error("ADM-09", f"合集包读不了：{exc}")
        return report
    try:
        tmpcm = tempfile.TemporaryDirectory(
            prefix="jjm-verify-", dir=temp_workspace(package)
        )
    except OSError:
        # 包所在盘不可写（只读介质 / 网络盘）→ 退回系统 Temp，不让功能失效
        tmpcm = tempfile.TemporaryDirectory(prefix="jjm-verify-")
    with outer, tmpcm as tmp:
        for member in members:
            dest = Path(tmp) / Path(member).name
            try:
                dest.write_bytes(outer.read(member))
            except (OSError, KeyError, RuntimeError) as exc:
                report.error("ADM-09", f"合集成员 {member} 读不出来：{exc}")
                continue
            sub = verify_package(dest)
            if sub.ok:
                report.add("ok", "ADM-MEMBER", f"合集成员 {member} 通过准入预检")
            else:
                brief = "；".join(f"{f.code} {f.message}" for f in sub.errors[:2])
                report.error(
                    "ADM-09",
                    f"合集成员 {member} 未通过（整包事务下会导致整个合集不装）：{brief}",
                    member,
                )
    if report.ok:
        report.add("ok", "ADM-OK", f"合集 {len(members)} 个成员全部通过，ADMITTED")
    return report


def verify_package(
    package: Path, with_packages: list[Path] | None = None, source: Path | None = None
) -> Report:
    """开发者校验准入预检。纯静态，不依赖启动器源码、不写盘。"""
    with_packages = list(with_packages or [])
    report = Report()

    # 合集包走独立分支：它没有根 extension.json，按单包 check 会误报 PKG-01。
    members = _bundle_members(package)
    if members:
        return verify_bundle(package, members)

    # --- 1. 静态基座：error 汇总成一条，warn 原样继承。
    # SELF-01 是 1.5 时代的旧口径：那时改动基线脚本必须自带原文。发布版启动器
    # 现在随包带 baseline/canonical_sources.zip，运行时能取到原文，这条已不成立，
    # 降为提醒。2026-11-04 用今晚 4 个实际跑通的修复包核对过，不能判死。
    base = check_package(package, source=source)
    for finding in base.warnings:
        report.add("warn", finding.code, finding.message, finding.where)
    self_base = [f for f in base.errors if f.code == "SELF-01"]
    hard = [f for f in base.errors if f.code != "SELF-01"]
    for finding in self_base:
        report.warn(
            "ADM-10",
            "补丁目标未随包提供；发布版启动器自带 canonical 源码层时可正常工作，"
            "请确认目标环境附带该层并实测：" + finding.message,
            finding.where,
        )
    if hard:
        brief = "；".join(f"{f.code} {f.message}" for f in hard[:3])
        report.error(
            "ADM-02",
            f"静态自检未通过（{len(hard)} 条）：{brief}"
            + ("…" if len(hard) > 3 else "")
            + "；先跑 `jjm_pack.py check` 修干净",
        )

    data, names = _read_manifest(package)
    if not data:
        if not base.errors:
            report.error("ADM-02", "读不到包根目录的 extension.json，或它不是合法 JSON 对象")
        return report

    ext_id = str(data.get("id") or "")

    # --- 2. schema：1/2 由启动器自动升格，实测能过开发者校验，只给提醒。
    # 只有出现「需要位置级接管」的重叠时才必须升到 3（见下面 ADM-07）。
    if data.get("schema") != 3:
        report.warn(
            "ADM-01",
            f"schema={data.get('schema')!r}；启动器会自动升格，能正常工作。"
            "但 1/2 没有位置级 overrides / expect，一旦和别的包抢同一处就无法在"
            "打包阶段消掉冲突；新包建议直接写 3",
            "extension.json",
        )

    # --- 3. 钩子：phase 缺省由升格补 after、handler 由运行期注册，都是合法写法。
    # 只提醒作者实测，不判死（今晚浮游炮 19 条钩子就是这种写法，实际全部生效）。
    hooks = data.get("method_hooks") or []
    if isinstance(hooks, list):
        loose = [
            index
            for index, hook in enumerate(hooks)
            if isinstance(hook, dict)
            and (not hook.get("phase") or not (hook.get("handler") or hook.get("code")))
        ]
        if loose:
            report.warn(
                "ADM-03",
                f"{len(loose)} 条 method_hooks 依赖升格补默认值 / 运行期注册；"
                "启动器支持这种写法，但发布前必须实测插桩确实生效",
                "method_hooks",
            )

    # --- 4. 接管三条件：expect + requires 点名
    req_ids, ranges = _requires_ids(data)
    for where, has_expect, named in _takeovers(data):
        if not has_expect:
            report.error("ADM-06", f"{where} 声明接管却缺 expect；开发者校验下接管不成立", where)
        missing = [n for n in named if n not in req_ids]
        if missing:
            report.error(
                "ADM-05",
                f"{where} 点名了 {'、'.join(missing)}，但 requires 里没有；"
                "启动器无法据此排序（被接管方必须先落位）",
                where,
            )

    # --- 5. 位置重叠：包内自重叠 + 与 --with 包重叠
    my_points = collect_points(data, names)
    for point in my_points:
        point["ext"] = ext_id
    for index, point in enumerate(my_points):
        for other in my_points[:index]:
            if _overlap(point, other) == "same" and not _takes_over(point, other, req_ids):
                # 同一个包内的两条改动由合并层按声明顺序叠加，启动器不判冲突。
                # 实测（战斗工作台 158 条补丁）里有 4 处同锚点，都能正常工作。
                report.warn(
                    "ADM-04",
                    f"包内两条改动落在同一处：{other['who']} 与 {point['who']}"
                    f"（{point['target']} {point['locator'] or '整份文件'}）；"
                    "合并层会按声明顺序叠加，请实测最终结果是你要的",
                    point["target"],
                )

    provided: set[str] = set()
    for other_path in with_packages:
        other_data, other_names = _read_manifest(other_path)
        if not other_data:
            report.error("ADM-07", f"--with 包读不到清单：{other_path}")
            continue
        other_id = str(other_data.get("id") or other_path.stem)
        provided.add(other_id)
        # 同 id 是「自己升版本重装」，启动器 preview_install 也会排除自己，
        # 不参与位置比对，否则会被误判成抢同一处。
        if other_id == ext_id:
            report.add("ok", "ADM-SELF", f"--with 里的 {other_id} 是同 id 旧版本，按重装处理")
            continue
        other_points = collect_points(other_data, other_names)
        for point in other_points:
            point["ext"] = other_id
        for mine in my_points:
            for theirs in other_points:
                if not _overlap(mine, theirs):
                    continue
                if _takes_over(mine, theirs, req_ids) or _takes_over(
                    theirs, mine, _requires_ids(other_data)[0]
                ):
                    report.add(
                        "ok",
                        "ADM-TAKEOVER",
                        f"与 {other_id} 在 {mine['target']} 重叠，但已声明合法接管",
                        mine["target"],
                    )
                else:
                    report.error(
                        "ADM-07",
                        f"与 {other_id} 在 {mine['target']} "
                        f"{mine['locator'] or '整份文件'} 位置重叠且未处置；"
                        "开发者校验会在导入阶段直接拒装。避让（改深指针）"
                        "或写全 overrides + requires + expect",
                        mine["target"],
                    )

    # --- 6. 依赖闭环
    if with_packages:
        for rid in sorted(req_ids):
            if rid and rid != ext_id and rid not in provided:
                report.error(
                    "ADM-08",
                    f"依赖 {rid} 未在 --with 里提供；开发者校验会按 requires.missing 拒绝或剔除",
                    "requires",
                )
    else:
        report.warn(
            "ADM-10",
            "没有 --with，组合准入这一关没验；发布前请至少带上最小组合两个包一起跑",
        )
    for rid, version_range in ranges.items():
        if version_range and "<" not in version_range:
            report.warn(
                "ADM-10",
                f"依赖 {rid} 的范围 {version_range!r} 没有上界；对方升大版本会静默错位",
                "requires",
            )

    # --- 7. 提醒汇总 + 结论
    if report.warnings:
        count = len(report.warnings)
        report.warn("ADM-10", f"共 {count} 条提醒；开发者校验会全部列出，建议清掉再发")
    if report.ok:
        report.add("ok", "ADM-OK", "开发者校验准入预检通过（ADMITTED）")
    return report


# ---------------------------------------------------------------- CLI


def _print(report: Report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return
    for finding in report.findings:
        print(finding)
    print()
    print(f"结果：错误 {len(report.errors)}，提示 {len(report.warnings)}，"
          f"{'通过' if report.ok else '未通过'}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        prog="jjm-pack",
        description=(
            f"机甲旋风启动器 {SPEC_VERSION} 扩展打包工具（规范版本 {SPEC_VERSION}）"
            "；verify 子命令按完全校验 strict 做准入预检"
        ),
    )
    ap.add_argument("--version", action="version", version=f"jjm-pack {TOOL_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="按规范检查一个扩展包")
    p_check.add_argument("package")
    p_check.add_argument("--json", action="store_true", help="输出机器可读报告")
    p_check.add_argument("--line", default=None, choices=sorted(LINE_PROFILES),
                         help="%s" % ('指定引擎线；不给则按包自己声明的线判断',))
    p_check.add_argument(
        "--pair",
        metavar="另一个包",
        default="",
        help="再给一个包，检查两者同时装会不会抢同一个位置（只做静态判定）",
    )
    p_check.add_argument(
        "--source",
        default=None,
        metavar="反解工程",
        help="核对钩子目标的源码目录或源码 zip；不给则用 baseline/sources-<线>.zip",
    )

    p_verify = sub.add_parser("verify", help="完全校验（strict）准入预检，发之前必跑")
    p_verify.add_argument("package", help="扩展包（zip 或目录）或合集包")
    p_verify.add_argument(
        "--with",
        dest="with_pkg",
        action="append",
        default=[],
        metavar="已有包",
        help="会与本包共存的已有扩展，可重复；用于验证组合准入",
    )
    p_verify.add_argument("--json", action="store_true", help="输出机器可读报告")
    p_verify.add_argument("--line", default=None, choices=sorted(LINE_PROFILES),
                         help="指定引擎线；不给则按包自己声明的线判断")
    p_verify.add_argument(
        "--source",
        default=None,
        metavar="反解工程",
        help="核对钩子目标的源码目录或源码 zip；不给则用 baseline/sources-<线>.zip",
    )

    p_fix = sub.add_parser("fix", help="从反解工程补自带基底，产出自包含包")
    p_fix.add_argument("package")
    p_fix.add_argument("--source", required=True, help="与基线匹配的反解工程目录（含 project.godot）")
    p_fix.add_argument("-o", "--out", required=True, help="输出 zip 路径")
    p_fix.add_argument("--json", action="store_true")

    p_pack = sub.add_parser("pack", help="把目录按规范打成 zip")
    p_pack.add_argument("folder")
    p_pack.add_argument("-o", "--out", required=True)
    p_pack.add_argument("--json", action="store_true")

    p_man = sub.add_parser("manifest", help="生成 extension.json 骨架")
    p_man.add_argument("folder")
    p_man.add_argument("--id", required=True)
    p_man.add_argument("--name", default="")
    p_man.add_argument("--force", action="store_true")
    p_man.add_argument("--json", action="store_true")
    p_man.add_argument("--line", default=ENGINE_LINE, choices=sorted(LINE_PROFILES),
                       help="用哪条引擎线的基线模板")

    # seal：跑一遍检查，通过就在包里写 verification.json（独立文件，
    # 不动 extension.json —— 参考启动器对清单字段是严格白名单）。
    p_seal = sub.add_parser(
        "seal", help="跑一遍完全校验并写入验证记录 verification.json"
    )
    p_seal.add_argument("package", help="扩展包（zip 或目录）")
    p_seal.add_argument("--line", default=None, choices=sorted(LINE_PROFILES),
                        help="指定引擎线；不给则按包自己声明的线判断")
    p_seal.add_argument(
        "--force", action="store_true",
        help="检查有 error 时也写记录（记录里会标出未通过的项；不建议）",
    )
    p_seal.add_argument("--json", action="store_true", help="输出机器可读报告")
    p_seal.add_argument(
        "--source",
        default=None,
        metavar="反解工程",
        help="核对钩子目标的源码目录或源码 zip；不给则用 baseline/sources-<线>.zip",
    )

    args = ap.parse_args(argv)

    if args.cmd == "check":
        report = check_package(
            Path(args.package), args.line, Path(args.source) if args.source else None
        )
        if args.pair:
            other = check_pair(Path(args.package), Path(args.pair))
            report.findings.extend(other.findings)
        _print(report, args.json)
        return 0 if report.ok else 1
    if args.cmd == "verify":
        report = verify_package(
            Path(args.package),
            [Path(x) for x in args.with_pkg],
            Path(args.source) if args.source else None,
        )
        if args.json:
            payload = report.as_dict()
            payload.update(
                {
                    "mode": ADM_MODE,
                    "verdict": "ADMITTED" if report.ok else "REJECTED",
                    "with": [str(x) for x in args.with_pkg],
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print(report, False)
            print(
                "开发者校验结论："
                + ("ADMITTED（可发）" if report.ok else "REJECTED（禁止发布）")
            )
        return 0 if report.ok else 1
    if args.cmd == "fix":
        report = fix_package(Path(args.package), Path(args.source), Path(args.out))
        _print(report, args.json)
        return 0 if report.ok else 1
    if args.cmd == "pack":
        report = pack_dir(Path(args.folder), Path(args.out))
        _print(report, args.json)
        return 0 if report.ok else 1
    if args.cmd == "seal":
        report, written = seal_package(
            Path(args.package),
            line=args.line,
            force=args.force,
            source=Path(args.source) if args.source else None,
        )
        if args.json:
            payload = report.as_dict()
            payload["record"] = str(written) if written else ""
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print(report, False)
            if written:
                print(f"验证记录已写入：{written}")
            else:
                print("未写入验证记录（检查未通过或包不可写）")
        return 0 if (report.ok or args.force) and written else 1
    if args.cmd == "manifest":
        report = write_manifest(Path(args.folder), args.id, args.name, args.force, args.line)
        _print(report, args.json)
        return 0 if report.ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
