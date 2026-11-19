"""把扩展清单落成实际的覆盖层 pck（预览/安装共用）。

三件事：

1. **文件层**：按 ``files[].target`` 铺进 pck，覆盖基线资源的自动补自指 remap；
2. **数据/文本补丁**：把 ``data_patches`` / ``text_patches`` 真正应用到目标内容上，
   原文按「包内自带 → 基线 pck → 反解源码」的顺序取；
3. **待插桩清单**：``method_hooks`` 需要源码插桩，这里原样返回，供 UI 明示还差什么。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import paths
from ..errors import ManifestError
from ..pck.writer import PckWriter
from ..fsutil import extract_zip_safe, remove_tree_long, win_long_path
from . import patcher
from . import symcheck
from .manifest import Manifest

#: 需要自指 remap 的后缀（这类资源在导出包里会被重定向到 .gdc/.scn/.res）
_REMAP_SUFFIXES = (".gd", ".tscn", ".tres", ".res")


# 判定统一收敛到 pck.remap（导出包脚本只有 x.gdc + x.gd.remap，没有 x.gd，
# 这个坑在剧情库/扩展 files/扩展 patches 各踩过一次，只保留一份实现）。
from ..pck.remap import is_override  # noqa: E402


@dataclass
class OverlayResult:
    pck: Path
    entry_count: int
    size: int
    sha256: str
    overridden: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    pending_hooks: list[dict] = field(default_factory=list)
    patches: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "pck": str(self.pck),
            "entry_count": self.entry_count,
            "size": self.size,
            "sha256": self.sha256,
            "added": len(self.added),
            "overridden": self.overridden,
            "pending_hooks": len(self.pending_hooks),
            "patches": self.patches,
        }


def extract_payload(package: str | Path, dest: str | Path) -> Path:
    """把包解开到指定目录（已存在则先清空，避免上一版残留）。

    清理与建目录都走长路径：``builds/_install_*/src`` 在深安装目录里会超过
    MAX_PATH，普通 ``rmtree(ignore_errors=True)`` 会静默不删，新装一版就混进
    旧版残留（着色器缓存那次踩过同款坑）。清不掉要明确报错，不能当成功。
    """
    root = Path(dest)
    if not remove_tree_long(root):
        raise OSError(f"旧的解包目录清理不掉：{root}")
    os.makedirs(win_long_path(root), exist_ok=True)
    package_long = win_long_path(package)
    if os.path.isdir(package_long):
        shutil.copytree(package_long, win_long_path(root), dirs_exist_ok=True)
    else:
        extract_zip_safe(package, root)
    return root


def _read_payload_file(path: Path) -> bytes | None:
    """读解出来的载荷文件（走长路径）；不存在返回 None。

    ``PckWriter.add_file`` 内部用 ``Path.exists()`` / ``Path.open()``，在
    >260 字符路径上会静默判「源文件不存在」——即使文件已经解出来了。
    所以这里自己读成 bytes 再交给 ``add_bytes``，把整条装配链也拉进长路径。
    """
    long = win_long_path(path)
    if not os.path.isfile(long):
        return None
    with open(long, "rb") as f:
        return f.read()


def build_overlay(
    package: str | Path,
    manifest: Manifest,
    out_pck: str | Path,
    baseline_paths: frozenset[str] | None = None,
    line: str | None = None,
    work_dir: str | Path | None = None,
    read_base: Callable[[str], bytes | None] | None = None,
    allow_partial: bool = False,
) -> OverlayResult:
    """按清单把载荷打成覆盖层 pck。

    ``baseline_paths`` 有值时能区分"覆盖"与"新增"；覆盖脚本/场景会自动补
    自指 remap —— 少了这一步，引擎会顺着基线的 ``x.gd.remap`` 读回旧 ``.gdc``。

    ``read_base`` 用来取补丁的**原文**（签名 ``(target) -> bytes | None``），
    通常传 :class:`~app.core.ext.basecontent.BaseContent.read`。不传时只能补丁
    包内自带的文件。
    """
    if not manifest.files and not (manifest.data.get("text_patches") or manifest.data.get("data_patches")):
        raise ManifestError("清单里没有 files / patches，无法生成覆盖层", ext=manifest.id)

    engine_line = line or manifest.engine_line or "4.6"
    work = Path(work_dir) if work_dir else (paths.BUILDS / f"_ext_{manifest.id}")
    src_root = extract_payload(package, work / "src")

    writer = PckWriter(out_pck, line=engine_line)
    overridden: list[str] = []
    added: list[str] = []
    reports: list[dict] = []

    # 只有补丁、没有文件的扩展，如果补丁全部失效就会得到一个空包。
    # 那时候报「空包：没有任何条目」很难懂，得说清是补丁没打上。
    patch_only = not manifest.files and bool(
        manifest.data.get("text_patches") or manifest.data.get("data_patches")
    )
    for rec in manifest.files:
        source = src_root / rec["source"]
        target = rec["target"]
        payload = _read_payload_file(source)
        if payload is None:
            raise ManifestError(f"载荷缺文件：{rec['source']}", ext=manifest.id, file=str(source))
        writer.add_bytes(target, payload)
        override = is_override(target, baseline_paths)
        if override:
            overridden.append(target)
            if target.endswith(_REMAP_SUFFIXES):
                writer.add_remap(target)
        else:
            added.append(target)

    # --- 数据/文本补丁
    grouped: dict[str, dict[str, list[dict]]] = {}
    symbol_checks: list = []

    # 跨脚本可见的符号：Godot 4 有一批全局枚举（HORIZONTAL_ALIGNMENT_CENTER
    # 这类不带前缀），手写内建名单总会漏，直接从基线脚本里扫出来。
    globals_available: set[str] = set()
    if allow_partial and read_base is not None and baseline_paths:
        script_paths = [p for p in baseline_paths if p.endswith(".gd")]
        globals_available = symcheck.baseline_globals(read_base, script_paths)
    for patch in manifest.data.get("data_patches") or []:
        target = str(patch.get("target") or "").replace("res://", "")
        if target:
            grouped.setdefault(target, {"text": [], "data": []})["data"].append(patch)
    for patch in manifest.data.get("text_patches") or []:
        target = str(patch.get("target") or "").replace("res://", "")
        if target:
            grouped.setdefault(target, {"text": [], "data": []})["text"].append(patch)

    for target, groups in grouped.items():
        base: bytes | None = _read_payload_file(src_root / target)
        if base is None:
            base = read_base(target) if read_base is not None else None
        if base is None:
            if allow_partial:
                # 目标文件在目标基线里不存在 → 这个补丁无从谈起，跳过。
                # 跨版本安装时很常见（那个文件是另一版才有的）。
                reports.append(
                    {
                        "target": target,
                        "kind": "skip",
                        "applied": 0,
                        "issues": [f"补丁目标在目标基线里不存在：{target}"],
                        "details": [],
                    }
                )
                continue
            raise ManifestError(
                f"补丁目标取不到原文：{target}（包里没有，基线/源码里也没有）",
                ext=manifest.id,
                file=target,
            )
        try:
            new_bytes, patch_reports = patcher.patch_bytes(
                target, base, groups["text"], groups["data"], allow_partial=allow_partial
            )
        except patcher.PatchError as exc:
            # 命中数不符绝大多数是「包是给别的游戏版本写的」。把这条判断写进
            # 错误信息里，比只报「命中 0 处」有用得多——用户会知道该去找对应版本。
            hint = ""
            if "命中" in str(exc) and "期望" in str(exc):
                hint = (
                    f"（这个包声明的目标是 {manifest.engine_line or '未知'} 线，"
                    f"但补丁内容在目标版本里找不到，很可能它是给别的游戏版本写的）"
                )
            raise ManifestError(
                f"补丁失败：{exc}{hint}", ext=manifest.id, file=target
            ) from exc

        applied = sum(r.applied for r in patch_reports)
        reports.extend(r.as_dict() for r in patch_reports)

        # 符号依赖自检：跳过补丁可能让别的补丁引用到不存在的符号
        if allow_partial and groups["text"]:
            try:
                symbol_checks.append(
                    symcheck.check_patched_script(
                        target,
                        base.decode("utf-8", "replace"),
                        new_bytes.decode("utf-8", "replace"),
                        groups["text"],
                        globals_available=globals_available,
                    )
                )
            except Exception:  # noqa: BLE001 - 检查失败不该阻断安装
                pass

        if bytearray(new_bytes) == bytearray(base):
            continue
        writer.add_bytes(target, new_bytes)
        if is_override(target, baseline_paths):
            if target not in overridden:
                overridden.append(target)
            if target.endswith(_REMAP_SUFFIXES):
                writer.add_remap(target)
        elif target not in added:
            added.append(target)
        if applied == 0:
            reports.append(
                {
                    "target": target,
                    "kind": "text",
                    "applied": 0,
                    "issues": ["补丁未命中任何内容，已跳过"],
                    "details": [],
                }
            )

    # --- 脚本成员注入（scripts.members）
    #
    # 这一步必须在补丁**之后**：补丁负责改已有语句，members 负责补上新的
    # 常量/函数声明，两者的产物都写进同一份脚本。先补丁后注入，才能保证
    # 注入的声明排在 extends 之后、且不与补丁改过的内容冲突。
    #
    # 踩过的坑：这段逻辑过去完全缺失，导致 whf.all_instructors 的 12 条
    # text_patches 引用了 WhfInstructorsBaolong 等 11 个标识符，而这些标识符
    # 的声明一条都没写进 game.gd → 整份脚本 parse error → Game autoload
    # 加载失败 → 开始页的「开始」按钮点了毫无反应。
    member_scripts = (manifest.data.get("scripts") or {}).get("members") or []
    for rec in member_scripts:
        if not isinstance(rec, dict):
            continue
        target = str(rec.get("target") or "").replace("res://", "")
        members = rec.get("members") or {}
        if not target or not isinstance(members, dict) or not members:
            continue
        # 取当前内容：优先用已写进 writer 的（补丁后的版本），否则回落到源文件/基线
        existing = writer.get_text(target)
        if existing is None:
            local_bytes = _read_payload_file(src_root / target)
            if local_bytes is not None:
                existing = local_bytes.decode("utf-8", errors="replace")
            elif read_base is not None:
                raw = read_base(target)
                existing = raw.decode("utf-8", "replace") if raw else None
        if existing is None:
            reports.append(
                {
                    "target": target,
                    "kind": "members",
                    "applied": 0,
                    "issues": [f"成员注入的目标取不到原文：{target}，已跳过"],
                    "details": [],
                }
            )
            continue
        injected, issues = patcher.inject_members(existing, members, target=target)
        applied = sum(1 for _ in members) if injected != existing else 0
        reports.append(
            {
                "target": target,
                "kind": "members",
                "applied": applied,
                "issues": issues,
                "details": [f"注入 {len(members)} 个成员声明"],
            }
        )
        if injected != existing:
            writer.add_bytes(target, injected.encode("utf-8"))
            if is_override(target, baseline_paths):
                if target not in overridden:
                    overridden.append(target)
                if target.endswith(_REMAP_SUFFIXES):
                    writer.add_remap(target)
            elif target not in added:
                added.append(target)

    # --- 符号依赖自检
    # 部分兼容模式下被跳过的补丁，可能正好提供别的补丁要用的常量，
    # 于是脚本引用了不存在的符号 → 解析失败 → 整个 Autoload 加载不了。
    #
    # 这里**只记警告不拦**：静态符号差集无法区分「本脚本没声明」和
    # 「Godot 内建全局 / 别的类的成员」，误报率高（实测 HORIZONTAL_ALIGNMENT_CENTER
    # 这类全局枚举用了 101 次却没在任何脚本里声明）。
    # 真正的把关是安装后的挂载冒烟——解析错误会立刻暴露。
    if symbol_checks:
        broken = [c for c in symbol_checks if not c.ok]
        if broken:
            detail = "；".join(
                f"{c.target} 引用了疑似未定义的 {', '.join(c.broken)}" for c in broken
            )
            reports.append(
                {
                    "target": broken[0].target,
                    "kind": "symbol",
                    "applied": 0,
                    "issues": [
                        f"补丁之间可能有依赖断裂：{detail}"
                        "（若启动时报脚本解析错误，就是这里指出的问题）"
                    ],
                    "details": [],
                }
            )

    if patch_only and len(writer) == 0:
        details = "；".join(
            f"{r.get('target')}：{(r.get('issues') or ['未命中'])[0]}"
            for r in reports
            if r.get("issues")
        )
        raise ManifestError(
            f"这个扩展只靠补丁生效，但补丁在 {engine_line} 基线里全部没命中"
            f"（{details or '无详细信息'}）。"
            "它很可能是给另一个游戏版本写的，请找对应版本的包。",
            ext=manifest.id,
        )

    result = writer.commit()
    return OverlayResult(
        pck=Path(out_pck),
        entry_count=result.entry_count,
        size=result.size,
        sha256=result.sha256,
        overridden=overridden,
        added=added,
        pending_hooks=manifest.method_hooks,
        patches=reports,
    )
