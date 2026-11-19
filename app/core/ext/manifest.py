"""扩展清单：schema 1 → schema 2 无损导入与校验。

schema 1 是既有扩展包（如 ``rose.dimension_ruins``）在用的格式：
根目录一个 ``extension.json`` + ``payload/`` 载荷树。

本模块只做「读懂 + 检查 + 升格」，不做任何写入；副作用为零，可安全用于
导入预览（UI 里「看 diff 再决定装不装」）。

升格规则见 :data:`SCHEMA1_MAP`，逐条对应实施计划 §4.6。
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..errors import ManifestError

#: 基线别名 → 内部基线 id / 引擎线
BASELINE_ALIAS: dict[str, tuple[str, str]] = {
    "jjxf.v1.45.original": ("jjxf.v1.45", "4.6"),
    "jjxf.v1.45": ("jjxf.v1.45", "4.6"),
}

#: schema 1 → 2 的字段映射说明（同时用于文档与 ``issues`` 提示）
SCHEMA1_MAP: dict[str, str] = {
    "schema": "schema: 1 → 2",
    "baseline": "字符串 → {id, engine.line}",
    "entry": "→ scripts.entry",
    "files[].expect_sha256": "→ files[].expect.sha256",
    "data_patches[].operations": "→ ops；set → replace",
    "text_patches[]": "补 mode=literal、expect_count=1",
    "script_members": "→ scripts.members",
    "project_settings": "→ settings.project",
    "input_actions": "→ inputs.actions",
    "requires": "字典 → 列表",
    "method_hooks[]": "补 phase=after、priority=50、handler 由运行期注册",
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$")

#: 支持的清单版本。
#:
#: 3 = 指针级写入（``operations`` / ``expect`` / ``overrides``）；
#: 4 = 3 + **强制源码声明**（``sources``）。打 ``text_patches`` / ``method_hooks``
#:     的包必须写明按哪份反解源码算的，否则安装期直接拒绝——用错源码的
#:     补丁可能锚点照样命中、语义却不对，比装不上难查得多。
#: 1/2 继续按原路径升格（没声明 sources 只提示，不拦），保证存量包能装。
SCHEMA_SUPPORTED = (1, 2, 3, 4)

#: 顶层已知字段。白名单外的键进 ``extra`` 并给 warn，不再静默丢弃。
_KNOWN_FIELDS = frozenset(
    {
        "schema", "id", "name", "version", "kind", "author", "license",
        "framework", "priority", "requires", "conflicts", "load_after",
        "load_before", "claims", "capabilities", "files", "packs",
        "data_patches", "text_patches", "method_hooks", "scenes", "scripts",
        "settings", "inputs", "story", "minigames", "baseline", "entry",
        "script_members", "project_settings", "input_actions", "scope",
        "engine", "description", "homepage", "tags", "sources",
    }
)

_OP_WHITELIST = frozenset(
    {"add", "remove", "replace", "move", "copy", "test", "merge", "merge_patch", "set"}
)
_EXPECT_KEYS = frozenset({"eq", "ne", "exists", "type", "sha256", "size"})


def ops_of(patch: dict) -> list[dict]:
    """取一个 ``data_patches[]`` 条目里的操作列表。

    ``operations`` 是 schema 3 的正式名，``ops`` 是 1.45 的别名。
    归一化后的清单只有 ``ops``，但直接读原始清单的代码（合成、补丁、界面）
    两种都要认，否则作者按新规范写 ``operations`` 会静默不生效。
    """
    ops = patch.get("operations")
    if ops is None:
        ops = patch.get("ops")
    if not isinstance(ops, list):
        return []
    return [op for op in ops if isinstance(op, dict)]


@dataclass
class Issue:
    level: str          # error / warn / info
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass
class Manifest:
    """升格后的 schema 2 清单 + 原始数据 + 校验结果。"""

    data: dict[str, Any]
    raw: dict[str, Any]
    source: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.data.get("id", ""))

    @property
    def entry(self) -> str:
        scripts = self.data.get("scripts") or {}
        return str(scripts.get("entry") or "")

    @property
    def engine_line(self) -> str:
        return str(((self.data.get("baseline") or {}).get("engine") or {}).get("line") or "")

    @property
    def files(self) -> list[dict]:
        return list(self.data.get("files") or [])

    @property
    def method_hooks(self) -> list[dict]:
        return list(self.data.get("method_hooks") or [])

    @property
    def sources(self) -> dict:
        """打包时声明使用的反解源码（没有声明返回空表）。"""
        value = self.data.get("sources")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def needs_source(self) -> bool:
        """这个包的实现是否**依赖**某份具体源码。

        ``text_patches`` 的锚点、``method_hooks`` 的方法体都按源码文本算；
        没有源码就只能盲打。纯 ``files`` / ``data_patches`` 的包不依赖源码。
        """
        return bool(self.data.get("text_patches") or self.data.get("method_hooks"))

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.data.get("name", ""),
            "version": self.data.get("version", ""),
            "kind": self.data.get("kind", "extension"),
            "engine_line": self.engine_line,
            "baseline": (self.data.get("baseline") or {}).get("id", ""),
            "entry": self.entry,
            "files": len(self.files),
            "method_hooks": len(self.method_hooks),
            "data_patches": len(self.data.get("data_patches") or []),
            "text_patches": len(self.data.get("text_patches") or []),
            "ok": self.ok,
            "issues": [i.as_dict() for i in self.issues],
        }


# ---------------------------------------------------------------- 读取


def read_manifest(package: str | Path) -> tuple[dict[str, Any], str, list[str]]:
    """从目录或 zip 读出 ``extension.json``。

    返回 ``(manifest_dict, 来源描述, 包内条目名列表)``。
    """
    p = Path(package)
    if p.is_dir():
        f = p / "extension.json"
        if not f.exists():
            raise ManifestError(f"目录里没有 extension.json：{p}", path=str(p))
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(f"extension.json 不是合法 JSON：{exc}", path=str(f)) from exc
        names = [str(x.relative_to(p).as_posix()) for x in p.rglob("*") if x.is_file()]
        return data, str(p), names

    if not p.exists():
        raise ManifestError(f"文件不存在：{p}", path=str(p))
    if p.suffix.lower() not in (".zip", ".jjm", ".pack"):
        raise ManifestError(f"不支持的包格式：{p.suffix}（支持 .zip / .jjm / .pack）", path=str(p))

    with zipfile.ZipFile(p) as zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        # 允许包内多一层目录（payload/ 之外的包裹层）
        cand = [n for n in names if n == "extension.json"]
        if not cand:
            cand = [n for n in names if n.endswith("/extension.json") and n.count("/") == 1]
        if not cand:
            # 合集包（一个 zip 里装着多个扩展 zip）是最常见的误拖：报
            # 「找不到 extension.json」玩家根本不知道下一步该干什么。
            # 识别出来直接告诉他先解压。
            inner = [
                n for n in names if n.lower().endswith((".zip", ".jjm", ".pack"))
            ]
            if inner and len(inner) >= max(1, len(names) // 2):
                raise ManifestError(
                    f"这是一个压缩包合集（里面 {len(inner)} 个扩展包），不是单个扩展；"
                    "请先解压，再把里面的扩展包拖进来逐个安装",
                    path=str(p),
                    entries=len(names),
                )
            raise ManifestError("包里找不到 extension.json", path=str(p), entries=len(names))
        root = cand[0][: -len("extension.json")]
        try:
            data = json.loads(zf.read(cand[0]).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ManifestError(f"extension.json 解析失败：{exc}", path=str(p)) from exc
        if root:
            names = [n[len(root):] for n in names if n.startswith(root)]
        return data, f"{p}::{cand[0]}", names


# ---------------------------------------------------------------- 升格


def normalize(
    raw: dict[str, Any],
    entries: Iterable[str] = (),
    baseline_paths: frozenset[str] | None = None,
) -> Manifest:
    """schema 1/2 统一升格成 schema 2 结构，并做静态校验。"""
    if not isinstance(raw, dict):
        raise ManifestError("清单顶层必须是对象")

    version = int(raw.get("schema", 1) or 1)
    m = Manifest(data={}, raw=raw, source="")
    entry_names = set(entries)

    if version not in SCHEMA_SUPPORTED:
        m.issues.append(
            Issue(
                "error",
                "schema.unsupported",
                f"不支持的 schema：{version}（支持 1 / 2 / 3 / 4）",
            )
        )
        version = 1

    out: dict[str, Any] = {
        "schema": version if version in (2, 3, 4) else 2,
        "id": str(raw.get("id", "")).strip(),
        "name": str(raw.get("name", "")).strip(),
        "version": str(raw.get("version", "")).strip(),
        "kind": str(raw.get("kind", "extension")).strip() or "extension",
        "author": raw.get("author", ""),
        "license": raw.get("license", ""),
        "framework": raw.get("framework", ""),
        "priority": int(raw.get("priority", 50) or 50),
        "requires": [],
        "conflicts": [],
        "load_after": list(raw.get("load_after") or []),
        "load_before": list(raw.get("load_before") or []),
        "claims": list(raw.get("claims") or []),
        "capabilities": list(raw.get("capabilities") or []),
        "files": [],
        "packs": list(raw.get("packs") or []),
        "data_patches": [],
        "text_patches": [],
        "method_hooks": [],
        "scenes": list(raw.get("scenes") or []),
        "scripts": {},
        "settings": {},
        "inputs": {},
        "story": raw.get("story") or {},
        "minigames": list(raw.get("minigames") or []),
    }
    if raw.get("scope"):
        out["scope"] = raw.get("scope")
    extra = {k: v for k, v in raw.items() if k not in _KNOWN_FIELDS and not k.startswith("x_")}
    if extra:
        out["extra"] = extra
        m.issues.append(
            Issue("warn", "field.unknown", f"未识别的清单字段：{'、'.join(sorted(extra))}")
        )
    if version == 1:
        out["x_origin"] = {"schema": 1, "mapped_by": "schema1_map"}

    # --- 基线
    baseline_raw = raw.get("baseline")
    if isinstance(baseline_raw, str) and baseline_raw:
        mapped = BASELINE_ALIAS.get(baseline_raw)
        if mapped is None:
            m.issues.append(Issue("warn", "baseline.unknown", f"未知基线别名：{baseline_raw}"))
            out["baseline"] = {"id": baseline_raw, "engine": {"line": ""}}
        else:
            bid, line = mapped
            out["baseline"] = {"id": bid, "engine": {"line": line}}
            if baseline_raw != bid:
                m.issues.append(Issue("info", "baseline.alias", f"{baseline_raw} → {bid}（{line}）"))
    elif isinstance(baseline_raw, dict):
        out["baseline"] = {
            "id": str(baseline_raw.get("id", "")),
            "engine": {"line": str((baseline_raw.get("engine") or {}).get("line", ""))},
        }
    else:
        out["baseline"] = {"id": "", "engine": {"line": ""}}
        m.issues.append(Issue("warn", "baseline.missing", "清单未声明 baseline"))

    # --- 源码声明（打包时用了哪份反解源码）
    #
    # ``text_patches`` 的锚点、``method_hooks`` 的方法签名都是按**某一份具体
    # 源码**算出来的。不写明用了哪份，安装时只能盲信；写明了就能和本线的
    # 随包源码（``baseline/sources-<line>.zip``）比对，避免「锚点命中但语义
    # 不对」这类最难查的错。
    src_raw = raw.get("sources")
    if isinstance(src_raw, str):
        out["sources"] = {"name": src_raw}
    elif isinstance(src_raw, dict):
        used = [str(x) for x in (src_raw.get("used_for") or []) if str(x)]
        out["sources"] = {
            "name": str(src_raw.get("name") or ""),
            "sha256": str(src_raw.get("sha256") or "").lower(),
            "line": str(src_raw.get("line") or ""),
            "used_for": used,
        }
    elif src_raw is not None:
        m.issues.append(Issue("warn", "sources.bad", "sources 必须是字符串或对象"))

    # --- 入口
    if raw.get("entry"):
        out["scripts"]["entry"] = str(raw["entry"])

    # --- files
    for item in raw.get("files") or []:
        if not isinstance(item, dict):
            m.issues.append(Issue("error", "files.bad_item", f"files 项不是对象：{item!r}"))
            continue
        src = str(item.get("source", ""))
        dst = str(item.get("target", "")).replace("\\", "/").lstrip("/")
        rec: dict[str, Any] = {"source": src, "target": dst}
        if item.get("replace") is not None:
            rec["replace"] = bool(item["replace"])
        expect = item.get("expect") if isinstance(item.get("expect"), dict) else {}
        sha = item.get("expect_sha256") or expect.get("sha256")
        norm_expect: dict[str, Any] = {}
        if sha:
            norm_expect["sha256"] = str(sha)
        if expect.get("size") is not None:
            norm_expect["size"] = int(expect["size"])
        if norm_expect:
            rec["expect"] = norm_expect
        if item.get("mode"):
            rec["mode"] = str(item["mode"])
        overrides = [str(x) for x in (item.get("overrides") or []) if str(x)]
        if overrides:
            rec["overrides"] = overrides
        out["files"].append(rec)

    # --- data_patches
    for item in raw.get("data_patches") or []:
        if not isinstance(item, dict):
            continue
        ops = item.get("operations") or item.get("ops") or []
        norm_ops = []
        for op in ops:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op", "replace"))
            if kind == "set":
                kind = "replace"
            if kind not in _OP_WHITELIST:
                m.issues.append(
                    Issue("error", "op.unknown", f"不支持的操作：{kind}（{item.get('target')}）")
                )
            path = op.get("path", "")
            if path not in (None, "") and not str(path).startswith("/"):
                m.issues.append(
                    Issue("error", "pointer.invalid", f"JSON Pointer 必须以 / 开头：{path!r}")
                )
            rec = {**op, "op": kind}
            expect = op.get("expect")
            if expect is not None:
                if not isinstance(expect, dict):
                    # 1.45.1 的写法：expect 直接写「改之前应该是什么值」
                    rec["expect"] = {"eq": expect}
                else:
                    unknown = set(expect) - _EXPECT_KEYS
                    if unknown and not (set(expect) & _EXPECT_KEYS):
                        # 整对象作为期望值（不是结构化断言）
                        rec["expect"] = {"eq": dict(expect)}
                    else:
                        if unknown:
                            m.issues.append(
                                Issue(
                                    "warn",
                                    "expect.bad_shape",
                                    f"expect 里有未支持的键：{'、'.join(sorted(unknown))}",
                                )
                            )
                        rec["expect"] = dict(expect)
            overrides = op.get("overrides")
            if overrides is not None:
                if not isinstance(overrides, list) or not all(
                    isinstance(x, str) and x for x in overrides
                ):
                    m.issues.append(
                        Issue("error", "override.bad_shape", f"overrides 必须是扩展 id 列表：{path!r}")
                    )
                else:
                    rec["overrides"] = list(overrides)
            on_miss = str(op.get("on_miss") or "abort")
            if on_miss not in ("abort", "warn", "skip"):
                m.issues.append(
                    Issue("warn", "expect.bad_shape", f"on_miss 取值不认识：{on_miss}")
                )
            elif "on_miss" in op:
                rec["on_miss"] = on_miss
            norm_ops.append(rec)
        record: dict[str, Any] = {
            "target": str(item.get("target", "")),
            "ops": norm_ops,
            "on_test_fail": item.get("on_test_fail", "abort"),
        }
        if isinstance(item.get("expect"), dict):
            record["expect"] = dict(item["expect"])
        if isinstance(item.get("overrides"), list):
            record["overrides"] = [str(x) for x in item["overrides"] if str(x)]
        out["data_patches"].append(record)

    # --- text_patches
    for item in raw.get("text_patches") or []:
        if not isinstance(item, dict):
            continue
        rec = {
            "target": str(item.get("target", "")),
            "find": item.get("find", ""),
            "replace": item.get("replace", ""),
            "mode": item.get("mode", "literal"),
            "expect_count": int(item.get("expect_count", 1) or 1),
        }
        if item.get("expect_sha256"):
            rec["expect_sha256"] = str(item["expect_sha256"])
        if item.get("scope"):
            # 作用域锚点：把搜索范围收窄到一个函数/内部类。
            # 必须原样带下去 —— 这里曾把未列出的字段直接丢掉，导致运行期
            # 根本看不到 scope，4.7 上「全文件不唯一」的锚点依旧被拒。
            rec["scope"] = str(item["scope"])
        if item.get("on_test_fail"):
            rec["on_test_fail"] = str(item["on_test_fail"])
        if isinstance(item.get("overrides"), list):
            rec["overrides"] = [str(x) for x in item["overrides"] if str(x)]
        out["text_patches"].append(rec)

    # --- method_hooks
    for item in raw.get("method_hooks") or []:
        if not isinstance(item, dict):
            continue
        rec = {
            "target": str(item.get("target", "")).replace("\\", "/"),
            "method": str(item.get("method", "")),
            "phase": str(item.get("phase", "after")),
            "priority": int(item.get("priority", 50) or 50),
            # 文档里写的是 code，旧实现只认 handler——两个都收，避免照抄示例被丢字段
            "handler": item.get("handler", "") or item.get("code", ""),
            "async": bool(item.get("async", False)),
        }
        out["method_hooks"].append(rec)

    # --- scripts（schema 2 的规范写法）
    #
    # 踩过的坑：`out["scripts"]` 初始化成 `{}` 后，这里过去只从**旧键**
    # `script_members` 补数据，导致 schema 2 的 `scripts.entry` 和
    # `scripts.members` 被整块丢掉——包明明写了入口脚本，启动器却认为它
    # 「没有 scripts.entry」，按纯覆盖层挂载；写了 members 也从不注入，
    # 于是补丁引用新标识符时整份脚本 parse error。
    raw_scripts = raw.get("scripts")
    if isinstance(raw_scripts, dict):
        entry = str(raw_scripts.get("entry") or "").strip()
        if entry:
            out["scripts"]["entry"] = entry
        members = raw_scripts.get("members")
        if isinstance(members, list) and members:
            out["scripts"]["members"] = members

    # --- script_members（schema 1 的旧名，保留兼容）
    legacy_members = raw.get("script_members") or []
    if legacy_members and "members" not in out["scripts"]:
        out["scripts"]["members"] = legacy_members

    # --- settings / inputs
    if raw.get("project_settings"):
        out["settings"]["project"] = raw["project_settings"]
    if raw.get("input_actions"):
        out["inputs"]["actions"] = raw["input_actions"]

    # --- requires / conflicts
    req = raw.get("requires") or {}
    if isinstance(req, dict):
        for rid, rng in req.items():
            out["requires"].append({"id": rid, "version": rng})
    elif isinstance(req, list):
        out["requires"] = req
    if isinstance(raw.get("conflicts"), list):
        out["conflicts"] = raw["conflicts"]

    m.data = out
    _validate(m, entry_names, baseline_paths)
    _validate_sources(m)
    return m


def _validate_sources(m: Manifest) -> None:
    """校验 ``sources`` 声明：是不是「按本线源码算的」。

    **只在包确实依赖源码时检查**（有 text_patches / method_hooks）。纯
    ``files`` / ``data_patches`` 的包与源码无关，要求它声明只是噪音。

    分级：
    - ``schema >= 4`` 且没声明 → error（新规范强制，安装期直接拒）
    - ``schema <= 3`` 且没声明 → warn（存量包放行，但提示重新打包）
    - 声明的 line / sha256 与本线对不上 → error（比不声明更危险：看起来有据）
    """
    if not m.needs_source:
        return
    src = m.sources
    schema = int(m.data.get("schema") or 1)
    if not src:
        text = (
            "包里有 text_patches / method_hooks，但没有 sources 声明："
            "无法确认补丁与插桩是按哪份源码算的。"
        )
        if schema >= 4:
            m.issues.append(
                Issue(
                    "error",
                    "sources.missing",
                    text + "schema 4 要求必填 sources（name + sha256 + line）。",
                )
            )
        else:
            m.issues.append(
                Issue(
                    "warn",
                    "sources.missing",
                    text + "旧格式（schema≤3）放行，建议用 1.7 工具重新打包。",
                )
            )
        return
    line = m.engine_line
    declared_line = str(src.get("line") or "")
    if declared_line and line and declared_line != line:
        m.issues.append(
            Issue(
                "error",
                "sources.line_mismatch",
                f"清单声明引擎线 {line}，但 sources.line 写的是 {declared_line}。",
            )
        )
        return
    try:
        from . import basecontent

        info = basecontent.source_info(line or "4.6")
    except Exception:  # noqa: BLE001 - 取不到本机源码就不做比对
        return
    want = str(src.get("sha256") or "").lower()
    got = str(info.get("sha256") or "").lower()
    if want and got and want != got:
        m.issues.append(
            Issue(
                "error",
                "sources.sha_mismatch",
                "sources.sha256 与本机随包源码不一致：补丁是按另一份源码算的，"
                "锚点可能命中但改错位置。",
            )
        )
    elif want and not got:
        m.issues.append(
            Issue("warn", "sources.sha_unknown", "本机取不到随包源码指纹，无法核对 sources.sha256。")
        )



# ---------------------------------------------------------------- 校验


def _validate(
    m: Manifest,
    entries: set[str],
    baseline_paths: frozenset[str] | None = None,
) -> None:
    d = m.data
    if not _ID_RE.match(m.id):
        m.issues.append(Issue("error", "id.invalid", f"id 不合法：{m.id!r}（小写字母数字与 . _ -）"))
    if not d["name"]:
        m.issues.append(Issue("warn", "name.empty", "清单缺少 name"))
    if not _SEMVER_RE.match(d["version"]):
        m.issues.append(Issue("warn", "version.format", f"version 不是 x.y.z：{d['version']!r}"))

    # files 的 source 必须在包内；target 不能越界
    for rec in d["files"]:
        src, dst = rec["source"], rec["target"]
        if entries and src not in entries:
            m.issues.append(Issue("error", "files.missing_source", f"包内缺少文件：{src}"))
        if not dst:
            m.issues.append(Issue("error", "files.empty_target", f"{src} 的 target 为空"))
            continue
        if dst.startswith("/") or ".." in Path(dst).parts:
            m.issues.append(Issue("error", "files.unsafe_target", f"target 越界：{dst}"))
        if rec.get("mode") and rec["mode"] not in ("copy", "patch"):
            m.issues.append(Issue("warn", "files.mode", f"未识别的 files.mode：{rec['mode']}"))

    # 数据补丁：目标、覆盖声明、整档断言
    for patch in d["data_patches"]:
        target = str(patch.get("target") or "")
        if not target:
            m.issues.append(Issue("error", "data.empty_target", "data_patches 缺少 target"))
        exp = patch.get("expect")
        if exp is not None and not isinstance(exp, dict):
            m.issues.append(Issue("error", "expect.bad_shape", f"{target} 的整档 expect 必须是对象"))
        elif isinstance(exp, dict):
            unknown = set(exp) - _EXPECT_KEYS
            if unknown:
                m.issues.append(
                    Issue("warn", "expect.bad_shape", f"{target} 的整档 expect 有未支持的键：{'、'.join(sorted(unknown))}")
                )

    # 哪些是「覆盖基线既有资源」（必须写自指 remap，否则改了不生效），
    # 哪些只是「新增文件」（无需 remap）。有基线索引时按真实路径判断。
    if baseline_paths is not None:
        def _covers(target: str) -> bool:
            # 导出包里脚本只有 x.gdc + x.gd.remap，没有 x.gd；两者都算「覆盖既有」
            return target in baseline_paths or (
                target.endswith((".gd", ".tscn", ".tres", ".res"))
                and f"{target}.remap" in baseline_paths
            )

        overrides = [r["target"] for r in d["files"] if _covers(r["target"])]
        fresh = [r["target"] for r in d["files"] if not _covers(r["target"])]
        if overrides:
            m.issues.append(
                Issue(
                    "warn",
                    "files.override_baseline",
                    "覆盖基线资源 %d 项，将自动写入自指 remap：%s"
                    % (len(overrides), ", ".join(overrides[:5]) + (" …" if len(overrides) > 5 else "")),
                )
            )
        if fresh:
            m.issues.append(Issue("info", "files.new", f"新增文件 {len(fresh)} 项（无需 remap）"))
    else:
        suspects = [r["target"] for r in d["files"] if r["target"].endswith((".gd", ".tscn", ".tres", ".res"))]
        if suspects:
            m.issues.append(
                Issue(
                    "info",
                    "files.need_remap",
                    f"{len(suspects)} 个脚本/场景类条目：若在基线中存在则需自指 remap（未提供基线索引，无法判定）",
                )
            )

    # entry 必须在 files 里出现
    if m.entry:
        targets = {r["target"] for r in d["files"]}
        rel = m.entry[len("res://"):] if m.entry.startswith("res://") else m.entry
        if targets and rel not in targets:
            m.issues.append(Issue("warn", "entry.not_in_files", f"entry 未在 files 中列出：{m.entry}"))

    # 钩子目标
    for h in m.method_hooks:
        if not h["target"] or not h["method"]:
            m.issues.append(Issue("error", "hook.incomplete", f"钩子缺少 target/method：{h}"))
        if h["phase"] not in ("before", "after", "replace"):
            m.issues.append(Issue("error", "hook.phase", f"非法 phase：{h['phase']}"))
        t = h["target"]
        if t.startswith("res://"):
            m.issues.append(Issue("warn", "hook.res_prefix", f"target 建议省略 res:// 前缀：{t}"))


def check_hooks_against_sources(manifest: Manifest, source_root: str | Path) -> list[Issue]:
    """把钩子逐条对到真实源码上，确认方法确实存在。

    这一步很关键：钩子写错方法名时，框架只会在运行期静默失效。
    在这里提前用反解工程核对，可以把问题挡在导入阶段。
    """
    root = Path(source_root)
    issues: list[Issue] = []
    if not root.exists():
        return [Issue("warn", "hook.no_source", f"源码根不存在，跳过钩子核对：{root}")]

    for h in manifest.method_hooks:
        rel = h["target"][len("res://"):] if h["target"].startswith("res://") else h["target"]
        src = root / rel
        if not src.exists():
            issues.append(Issue("error", "hook.target_missing", f"源码里没有该脚本：{rel}"))
            continue
        text = src.read_text(encoding="utf-8", errors="replace")
        pat = re.compile(rf"^\s*func\s+{re.escape(h['method'])}\s*\(", re.M)
        if not pat.search(text):
            issues.append(
                Issue("error", "hook.method_missing", f"{rel} 里找不到方法 {h['method']}()")
            )
        else:
            issues.append(Issue("info", "hook.ok", f"{rel}::{h['method']} ✓"))
    return issues


def load(
    package: str | Path,
    source_root: str | Path | None = None,
    baseline_paths: frozenset[str] | None = None,
) -> Manifest:
    """一步到位：读包 → 升格 → （可选）对源码核钩子。

    ``baseline_paths`` 传入基线条目集合时，能把「覆盖既有资源」与「新增文件」
    区分开；不传则退化为按后缀猜测。
    """
    raw, source, entries = read_manifest(package)
    m = normalize(raw, entries, baseline_paths)
    m.source = source
    if source_root is not None:
        m.issues.extend(check_hooks_against_sources(m, source_root))
    return m
