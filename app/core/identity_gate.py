#!/usr/bin/env python
"""机甲旋风改版文件接入闸门 · v0.2（产品内置）

v0.2：以 1.45 正典核心内容为**准入基准**，同时支持 4.6 / 4.7 两条线。

设计前提（已与需求方对齐）
--------------------------
- 反解工程 / 基线 / mod 规范由改版创作者自备；启动器只给「运行壳 + 接入壳」。
- 但「是不是 Godot 的机甲旋风改版」必须由启动器**内置特征库**扫文件内容判定，
  不采信创作者自己的声明，否则闸门形同虚设。

基准怎么来的
------------
``family_signature.py`` 由项目正典材料生成：
  canonical_sources.zip（1.45 GDRE 反解的 109 个核心脚本）
  + index-1.45.json.gz 与 index-4.7.json.gz 的路径交集
→ 104 个跨线家族核心脚本（.gdc）+ 133 张核心数据表 + 线特有标记。
1.45 本体只有 .gdc，4.7 本体 .gd/.gdc 都有，所以判定时两种后缀都算命中。

判定分层
--------
  E0 容器   GDPC + pack_version(3=4.6 / 4=4.7) + 引擎主版本 4
  E1 本体级 条目数 >= 1000（区分游戏本体与扩展覆盖层 / 补丁包）
  E2 工程   project.binary 的 application/config/name / run/main_scene
  E3 结构   顶层 assets / scripts / scenes / data
  E4 指纹   1.45 基准核心脚本 + 数据表命中率（决定家族准入）
  E5 血缘   线特有标记命中率，与头部 pack_version 交叉校验

exe 形态：有内嵌 pck 直接扫（1.45 原版）；没有则找同目录同名 .pck
（4.7 的 105MB 纯模板 exe + 旁边 2.9GB pck）。
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .family_signature import SIGNATURE as SIG
from .pck.reader import PckReader

# ---------------------------------------------------------------- 内置特征库
# 随启动器发布、只读；由 1.45 正典生成，不接受创作者的描述文件覆盖。
# 特征库缺了闸门就不该静默放行，所以内联成模块常量（冻结态一定能读到）。
SIGNATURE_VERSION = SIG["version"]
BASELINE = SIG["baseline"]

FAMILY_NAMES = tuple(SIG["project"]["family_names"])
MAIN_SCENES = tuple(s.encode("utf-8") for s in SIG["project"]["main_scenes"])
TOP_DIRS = tuple(SIG["project"]["top_dirs"])
CORE_SCRIPTS = tuple(SIG["family_core_scripts"])
CORE_DATA = tuple(SIG["family_core_data"])
LINE_MARKERS = {k: tuple(v) for k, v in SIG["line_markers"].items()}
T = SIG["thresholds"]

ACCEPT = "accept"
REJECT = "reject"


@dataclass
class Verdict:
    accepted: bool
    kind: str
    reason: str
    path: str
    identity_source: str = ""
    engine_line: str = ""
    baseline: str = BASELINE["id"]
    confidence: float = 0.0
    signature_version: str = SIGNATURE_VERSION
    identity_hash: str = ""
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _read_config_string(blob: bytes, key: bytes) -> str:
    """从 Godot 二进制 ConfigFile 取字符串值（1.45 / 4.7 正典逐字节核对过）。

    key 明文 + 4 字节对齐 + u32 type(4=STRING) + u32 字节长度 + UTF-8 值。
    """
    i = blob.find(key)
    if i < 0:
        return ""
    tail = blob[i + len(key): i + len(key) + 96]
    for off in range(0, 20):
        if off + 8 > len(tail):
            break
        typ, ln = struct.unpack_from("<II", tail, off)
        if typ == 4 and 2 <= ln <= 256 and off + 8 + ln <= len(tail):
            try:
                return tail[off + 8: off + 8 + ln].decode("utf-8").rstrip("\x00")
            except UnicodeDecodeError:
                continue
    return ""


def _has_code(paths: set[str], gdc: str) -> bool:
    """1.45 只有 .gdc，4.7 两种都有 —— 任一命中即算这个核心脚本存在。"""
    return gdc in paths or gdc[:-4] + ".gd" in paths


def _reject(path: Path, kind: str, reason: str, **extra) -> Verdict:
    return Verdict(False, kind, reason, str(path), **extra)


def _resolve_candidate(path: Path) -> tuple[Path | None, str, str]:
    if path.suffix.lower() == ".exe":
        try:
            PckReader.open(path)
            return path, "exe 内嵌 pck", ""
        except Exception:  # noqa: BLE001 - 任何读失败都走配对
            sibling = path.with_suffix(".pck")
            if sibling.is_file():
                return sibling, f"配对 pck（{sibling.name}）", ""
            return None, "", "exe 无内嵌 pck，同目录也没有同名 .pck，无法自证身份"
    return path, "pck 本体", ""


def probe(path: str | Path) -> Verdict:
    target = Path(path)
    if not target.is_file():
        return _reject(target, "missing", "文件不存在")

    candidate, source, err = _resolve_candidate(target)
    if candidate is None:
        return _reject(target, "unverifiable-exe", err)

    try:
        reader = PckReader.open(candidate)
        header = reader.header
    except Exception as exc:  # noqa: BLE001
        return _reject(target, "not-godot", f"不是可解析的 Godot 容器：{exc}")

    # E0 容器
    if header.pack_version not in (3, 4) or header.engine_version[0] != 4:
        return _reject(target, "not-godot-4x", "不是 Godot 4.x 的 pck 格式")
    engine_line = "4.7" if header.pack_version == 4 else "4.6"

    # E1 本体级
    entries = len(reader)
    if entries < T["min_entries"]:
        return _reject(
            target, "overlay-not-body",
            f"只有 {entries} 条，是扩展覆盖层或补丁包，不是游戏本体（需 >= {T['min_entries']} 条）",
            identity_source=source, engine_line=engine_line,
            evidence={"entries": entries},
        )

    paths = set(reader.index)

    # E4 以 1.45 核心为基准的家族指纹
    script_hits = [p for p in CORE_SCRIPTS if _has_code(paths, p)]
    data_hits = [p for p in CORE_DATA if p in paths]
    script_ratio = len(script_hits) / len(CORE_SCRIPTS)
    data_ratio = len(data_hits) / len(CORE_DATA)
    top_hits = [d for d in TOP_DIRS if any(p.startswith(d + "/") for p in paths)]

    # E5 血缘：线特有标记与头部 pack_version 是否自洽
    marker_rates = {}
    for line, markers in LINE_MARKERS.items():
        marker_rates[line] = round(
            sum(1 for p in markers if _has_code(paths, p)) / max(1, len(markers)), 3
        )
    expected_marker = "4.6" if engine_line == "4.6" else "4.7"
    other_marker = "4.7" if expected_marker == "4.6" else "4.6"
    line_consistent = marker_rates[expected_marker] >= 0.5 and marker_rates[other_marker] < 0.5

    # E2 工程
    pb = reader.get("project.binary")
    blob = reader.read_bytes(pb) if pb is not None else b""
    config_name = _read_config_string(blob, b"application/config/name")
    family_hit = any(n in config_name for n in FAMILY_NAMES)
    scene_hit = any(s in blob for s in MAIN_SCENES)
    pb_md5 = hashlib.md5(blob).hexdigest() if blob else ""

    evidence = {
        "baseline": BASELINE["id"],
        "entries": entries,
        "pack_version": header.pack_version,
        "engine": ".".join(str(n) for n in header.engine_version),
        "config_name": config_name,
        "family_hit": family_hit,
        "main_scene_hit": scene_hit,
        "top_dirs_hit": top_hits,
        "script_hits": f"{len(script_hits)}/{len(CORE_SCRIPTS)}",
        "data_hits": f"{len(data_hits)}/{len(CORE_DATA)}",
        "script_ratio": round(script_ratio, 3),
        "data_ratio": round(data_ratio, 3),
        "line_marker_rates": marker_rates,
        "line_consistent": line_consistent,
        "project_binary_md5": pb_md5,
    }

    strong = (
        family_hit and script_ratio >= T["script_ratio"]
        and data_ratio >= T["data_ratio"] and len(top_hits) >= T["min_top_dirs"]
    )
    fallback = (
        script_ratio >= T["fallback_script_ratio"]
        and data_ratio >= T["fallback_data_ratio"]
        and len(top_hits) >= T["fallback_top_dirs"]
    )

    if not (strong or fallback):
        return Verdict(
            False, "godot-other",
            "是合法 Godot 4.x 包，但不符合 1.45 核心内容准入基准，拒绝接入",
            str(target), identity_source=source, engine_line=engine_line,
            confidence=round(max(script_ratio, data_ratio), 3),
            evidence=evidence,
        )

    digest_src = json.dumps({
        "signature": SIGNATURE_VERSION,
        "baseline": BASELINE["id"],
        "engine": evidence["engine"],
        "pack_version": header.pack_version,
        "config_name": config_name,
        "project_binary_md5": pb_md5,
        "entries": entries,
        "script_hits": sorted(script_hits),
        "data_hits": sorted(data_hits),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    identity_hash = hashlib.sha256(digest_src).hexdigest()

    confidence = 0.6 + 0.2 * script_ratio + 0.2 * data_ratio
    if family_hit:
        confidence = min(1.0, confidence + 0.05)
    if not line_consistent:
        confidence = max(0.0, confidence - 0.1)

    mode = "工程名+1.45 核心指纹" if strong else "1.45 核心指纹兜底（工程名已改）"
    note = "" if line_consistent else "；头部与内容血缘不一致，建议复核"
    return Verdict(
        True, "godot-jjxf-body",
        f"确认是 Godot 机甲旋风改版本体（{mode}，{engine_line} 线）{note}",
        str(target), identity_source=source, engine_line=engine_line,
        confidence=round(confidence, 3), identity_hash=identity_hash,
        evidence=evidence,
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("用法: python identity_gate.py <pck 或 exe> [--json]")
        return 2
    v = probe(args[0])
    if "--json" in args:
        print(json.dumps(v.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"{'放行' if v.accepted else '拒绝'}  {v.kind}  {v.reason}")
        print(f"  身份来源: {v.identity_source} | 引擎线: {v.engine_line} | 基准: {v.baseline}")
        print(f"  置信度: {v.confidence} | 指纹: {v.identity_hash}")
        print(json.dumps(v.evidence, ensure_ascii=False, indent=2))
    return 0 if v.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
