"""自定义基线导入验收：改版创作者的反解源码要满足什么才能用于插桩层。

为什么需要这一层
----------------
``enable._build_instrumented`` 要把 ``dispatch_before/after`` 写进**原脚本的
明文 .gd**，再打成插桩覆盖层。改版本体里通常只有 ``.gdc`` 字节码，没有明文，
改写不了；只给一份路径索引也改写不了。所以创作者必须交**可编译的反解源码**。

但「交了源码」不等于「能用来插桩」：拿别的项目、别的版本的源码凑数，
插桩产物会盖到错误的脚本上，表现为黑屏或补丁静默失效。项目内置的
``baseline/canonical_sources.json`` 早就立了规矩——本体指纹不符时不使用那份
源码。这里把同一条规矩泛化到自定义线，做成可执行的验收器。

验收项（B0–B5，全部通过才登记）
--------------------------------
- B0 元数据：baseline.json 的 identity_hash / engine_line 与闸门实测一致；
- B1 源码：sources/ 下有明文 .gd、UTF-8、非空、含 GDScript 特征；
- B2 索引一致：index.json 的 files / symbols / anchors 在源码里逐项查得到；
- B3 本体对应：源码脚本（.gd ↔ .gdc）能在本体 pck 索引里对应上，比例 >= 80%；
- B4 脚本覆盖：line.json 声明的 method/hook 能力目标脚本必须交了源码；
- B5 联机禁入：源码正文命中 netguard 协议规则一律拒绝。

验收通过后写 ``config/custom_baselines.json`` 登记；``status()`` 每次重新核对
本体指纹与源码摘要，源码被改过就自动失效，必须重新导入。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .fsutil import atomic_write_json, read_json

BASELINE_JSON = "baseline.json"
INDEX_JSON = "index.json"
SOURCES_DIR = "sources"
REGISTRY_FILE = "custom_baselines.json"

#: 源码脚本能在本体索引里对应上的最低比例（防「拿别的项目源码凑数」）
MIN_BODY_MATCH_RATIO = 0.8

#: GDScript 明文的最小特征，挡掉空文件 / 二进制 / 随便一段文本
_GDSCRIPT_HINT = re.compile(r"\b(?:extends|class_name|func|signal|var|const)\b")
_FUNC_OR_SIGNAL = re.compile(r"^\s*(?:static\s+)?(?:func|signal)\s+(\w+)", re.MULTILINE)


@dataclass
class BaselineReport:
    """一次导入验收的完整结果。"""

    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    body_match: float = 0.0
    digest: str = ""
    identity_hash: str = ""
    engine_line: str = ""
    source_root: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        head = "通过" if self.ok else "未通过"
        return (f"{head}：源码 {len(self.scripts)} 个、本体对应 {self.body_match:.0%}、"
                f"错误 {len(self.errors)} 条、告警 {len(self.warnings)} 条")


def _read_scripts(root: Path) -> tuple[dict[str, str], list[str]]:
    """读入 sources/ 下所有 .gd，返回 (相对路径→文本, 错误列表)。"""
    out: dict[str, str] = {}
    errors: list[str] = []
    if not root.is_dir():
        return out, [f"缺少源码目录：{root}"]
    for p in sorted(root.rglob("*.gd")):
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{rel} 读不出来（必须是 UTF-8 明文）：{exc}")
            continue
        if not text.strip():
            errors.append(f"{rel} 是空文件")
            continue
        if not _GDSCRIPT_HINT.search(text):
            errors.append(f"{rel} 不像 GDScript 源码（缺 extends/func/signal 等特征）")
            continue
        out[rel] = text
    if not out:
        errors.append("sources/ 下一个可用的 .gd 明文脚本都没有")
    return out, errors


def digest_of(scripts: dict[str, str]) -> str:
    """源码内容摘要；任一文件改动都会让它变化。"""
    h = hashlib.sha256()
    for rel in sorted(scripts):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(scripts[rel].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _body_paths(game_file: Path) -> tuple[set[str], str]:
    """读改版本体的路径索引；读不出来当成空集并给出原因。"""
    from .pck.reader import PckReader

    try:
        reader = PckReader.open(game_file)
        return {str(p).replace("res://", "") for p in reader.index}, ""
    except Exception as exc:  # noqa: BLE001
        return set(), f"改版本体读不出路径索引：{exc}"


def verify(
    baseline_dir: str | Path,
    verdict,
    game_file: str | Path,
    *,
    required_scripts: set[str] | None = None,
) -> BaselineReport:
    """验收一份自定义基线；任何一项不过都只报错、不抛异常、不改文件。"""
    base = Path(baseline_dir)
    report = BaselineReport(
        identity_hash=str(getattr(verdict, "identity_hash", "") or ""),
        engine_line=str(getattr(verdict, "engine_line", "") or ""),
        source_root=str((base / SOURCES_DIR).resolve()),
    )

    # --- B0 元数据
    meta_path = base / BASELINE_JSON
    try:
        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.errors.append(f"baseline.json 读不出来：{exc}")
        return report
    if not isinstance(meta, dict) or meta.get("schema") != 1:
        report.errors.append("baseline.json 需要 schema: 1")
        return report
    if str(meta.get("identity_hash") or "") != report.identity_hash:
        report.errors.append(
            "baseline.json 的 identity_hash 与改版本体不符（拿错源码 / 本体被换过）"
        )
    if str(meta.get("engine_line") or "") != report.engine_line:
        report.errors.append(
            f"baseline.json 的 engine_line {meta.get('engine_line')!r} "
            f"与闸门判定 {report.engine_line!r} 不符"
        )

    src_dir = base / str(meta.get("source_dir") or SOURCES_DIR)
    idx_path = base / str(meta.get("index") or INDEX_JSON)
    report.source_root = str(src_dir.resolve())

    # --- B1 源码
    scripts, errs = _read_scripts(src_dir)
    report.errors.extend(errs)
    report.scripts = sorted(scripts)
    report.digest = digest_of(scripts)

    # --- B3 本体对应
    body, body_err = _body_paths(Path(game_file))
    if body_err:
        report.errors.append(body_err)
    if body and scripts:
        hit = 0
        for rel in scripts:
            alt = rel[:-3] + ".gdc" if rel.endswith(".gd") else rel
            if rel in body or alt in body or "res://" + rel in body:
                hit += 1
        report.body_match = round(hit / len(scripts), 3)
        if report.body_match < MIN_BODY_MATCH_RATIO:
            report.errors.append(
                f"只有 {report.body_match:.0%} 的源码脚本能在本体里找到对应条目"
                f"（需 >= {MIN_BODY_MATCH_RATIO:.0%}），这份源码不是该改版的"
            )

    # --- B2 索引一致
    try:
        import json

        index = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.errors.append(f"index.json 读不出来：{exc}")
        index = None
    if isinstance(index, dict):
        for rel in index.get("files") or []:
            rel = str(rel).replace("res://", "")
            if rel.endswith(".gd") and rel not in scripts:
                report.errors.append(f"index.json 列了 {rel}，但 sources/ 里没有")
        for rel, names in (index.get("symbols") or {}).items():
            key = str(rel).replace("res://", "")
            available = set(_FUNC_OR_SIGNAL.findall(scripts.get(key, "")))
            for name in names or []:
                if str(name) not in available:
                    report.errors.append(f"符号 {key}:{name} 在源码里找不到")
        for rel, anchors in (index.get("anchors") or {}).items():
            key = str(rel).replace("res://", "")
            text = scripts.get(key, "")
            for anchor in anchors or []:
                if str(anchor) not in text:
                    report.errors.append(f"锚点 {key}:{str(anchor)[:40]} 在源码里找不到")
    elif index is not None:
        report.errors.append("index.json 顶层必须是对象")

    # --- B4 脚本覆盖：line.json 声明的方法/钩子能力必须有源码
    for rel in sorted(required_scripts or set()):
        key = str(rel).replace("res://", "")
        if key.endswith(".gd") and key not in scripts:
            report.errors.append(f"line.json 声明的能力目标 {key} 没有交源码，无法插桩")

    # --- B5 联机禁入（与 netguard 同一套协议规则）
    from .ext import netguard

    for rel, text in scripts.items():
        hits = netguard.scan_bytes(text.encode("utf-8"), rel)
        if hits.blocked:
            report.errors.append(
                f"{rel} 含联机协议（{hits.blocks[0].reason}），不支持联机接入"
            )
            break

    report.ok = not report.errors
    return report


def registry_path() -> Path:
    return paths.CONFIG_DIR / REGISTRY_FILE


def register(line_id: str, baseline_dir: str | Path, report: BaselineReport) -> dict:
    """验收通过后登记；未经 ``verify`` 通过不要调用。"""
    reg = read_json(registry_path(), default={}) or {}
    rec = {
        "root": str(Path(baseline_dir).resolve()),
        "source_dir": report.source_root,
        "identity_hash": report.identity_hash,
        "engine_line": report.engine_line,
        "scripts": len(report.scripts),
        "digest": report.digest,
        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    reg[line_id] = rec
    atomic_write_json(registry_path(), reg)
    return rec


def status(line_id: str, identity_hash: str = "") -> dict[str, Any]:
    """查这条线的基线是否仍可用；源码被改 / 本体被换会自动失效。"""
    reg = read_json(registry_path(), default={}) or {}
    rec = reg.get(line_id)
    if not isinstance(rec, dict):
        return {"ready": False, "reason": "未导入自定义基线"}
    if identity_hash and str(rec.get("identity_hash") or "") != identity_hash:
        return {"ready": False, "reason": "改版本体指纹已变，需重新导入基线"}
    root = Path(str(rec.get("source_dir") or rec.get("source_root") or ""))
    if not root.is_dir():
        return {"ready": False, "reason": "登记的源码目录已不存在"}
    scripts, errs = _read_scripts(root)
    if errs:
        return {"ready": False, "reason": errs[0]}
    if digest_of(scripts) != rec.get("digest"):
        return {"ready": False, "reason": "源码内容已变，需重新校验导入"}
    return {
        "ready": True,
        "reason": "基线已导入且校验有效",
        "root": str(root),
        "scripts": len(scripts),
        "verified_at": rec.get("verified_at", ""),
    }


def source_root_for(line_id: str, identity_hash: str = "") -> Path | None:
    """给后续插桩层生成器用；未就绪返回 None。"""
    st = status(line_id, identity_hash)
    return Path(st["root"]) if st.get("ready") else None
