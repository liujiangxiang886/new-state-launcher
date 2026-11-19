"""扩展残留清理：让「卸载」在重启后依然算数。

问题现象
--------
扩展卸载后，**重启工具/游戏仍然能看到它**。根因不是游戏主动扫描目录，
而是 ``packs/``、插桩缓存、安装临时目录三处派生数据互相独立，卸载只清了
注册表和 ``packs/<id>``，其余会留下来：

1. ``config/extensions.json`` 写失败或旧版本留下的条目——注册表是唯一
   的「已安装」事实来源，只要它还有这条，界面就照旧列出来。
2. ``packs/<id>/`` 目录删不掉（文件被占用、权限不足）——注册表已无此条，
   但目录还在，重装/排查时会看到「幽灵覆盖层」。
3. ``cache/instrumented/*.pck``——插桩层按「线 + 钩子 + 源文件 + overlay
   签名」缓存。卸载后 overlay 不在了，旧缓存的键就再也命中不了，属于纯垃圾；
   但如果重启时某条链路上仍带着旧键（例如只删了注册表没删缓存、或升级回滚
   中途失败），它就会被挂载，表现为「扩展还在生效」。
4. ``builds/_install_<id>_<line>/``、``builds/_bundle_<name>/``——安装中间
   产物，含 mods.json 与 mod_report.json。它们不参与启动组装，但会一直堆着，
   且排障时容易读到上一轮的 ``mod_report.json`` 得到过期结论。

设计原则
--------
**只删派生物，绝不碰用户资产。** ``packs/`` 里仍被注册表引用的目录、
``profiles/`` 存档、``config/launcher.json`` 一律不在此模块的删除范围。
所有删除都先算计划（:func:`plan`）再执行（:func:`apply`），便于界面预览
与「清理了哪些」的可审计输出。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..fsutil import read_json


# ---------------------------------------------------------------- 数据结构


@dataclass
class Finding:
    """一条待清理的残留。"""

    #: 机器可读的分类，便于界面分组与测试断言
    kind: str
    #: 人类可读说明
    detail: str
    #: 目标路径
    path: Path
    #: 占用字节数（目录按递归求和；算不出来时给 0）
    bytes: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "path": str(self.path),
            "bytes": self.bytes,
        }


@dataclass
class CleanReport:
    """清理结果。"""

    removed: list[Finding] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    freed: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def message(self) -> str:
        if not self.removed and not self.failed:
            return "没有发现需要清理的残留"
        parts = []
        if self.removed:
            parts.append(f"已清理 {len(self.removed)} 项残留（{_human(self.freed)}）")
        if self.failed:
            parts.append(f"{len(self.failed)} 项未能删除")
        return "；".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "message": self.message(),
            "freed": self.freed,
            "removed": [f.as_dict() for f in self.removed],
            "failed": list(self.failed),
        }


def human_size(num: int) -> str:
    """字节数转可读单位，供界面与日志共用。"""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


#: 向后兼容的别名（早期版本内部叫 ``_human``）
_human = human_size


def _dir_bytes(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    if not path.is_dir():
        return 0
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


# ---------------------------------------------------------------- 注册表


def _registry_items() -> list[dict]:
    data = read_json(paths.CONFIG_DIR / "extensions.json", default={"schema": 1, "items": []}) or {}
    items = data.get("items") or []
    return [i for i in items if isinstance(i, dict)]


def _save_registry(items: list[dict]) -> None:
    from ..fsutil import atomic_write_json

    atomic_write_json(paths.CONFIG_DIR / "extensions.json", {"schema": 1, "items": items})


def _overlay_paths_of(item: dict) -> list[Path]:
    """一个注册表条目声明过的所有覆盖层路径（多线各自一份）。"""
    out: list[Path] = []
    raw = str(item.get("overlay") or "")
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = paths.ROOT / raw
        out.append(p)
    lines = item.get("lines")
    if isinstance(lines, dict):
        for entry in lines.values():
            if not isinstance(entry, dict):
                continue
            raw = str(entry.get("overlay") or "")
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = paths.ROOT / raw
            if p not in out:
                out.append(p)
    return out


def _is_story_or_packset_dir(name: str) -> bool:
    """``packs/`` 下不归扩展注册表管的目录。

    剧情包（``story-*``）与分片包（``*-shards``）由 ``config/stories.json``
    与基线配置管理，不能因为「不在扩展注册表里」就判成孤儿。
    """
    return name.startswith("story-") or name.endswith("-shards")


# ---------------------------------------------------------------- 计划


def plan(*, include_registry: bool = True) -> list[Finding]:
    """算出所有可安全清理的残留（只读，不动文件）。"""
    findings: list[Finding] = []
    items = _registry_items()
    registered = {str(i.get("id") or "") for i in items if i.get("id")}
    known_overlays: set[str] = set()
    for item in items:
        for p in _overlay_paths_of(item):
            try:
                known_overlays.add(str(p.resolve()).lower())
            except OSError:
                known_overlays.add(str(p).lower())

    # --- 1 注册表里指向已不存在覆盖层的条目
    #     这类条目是「卸载只删了文件、注册表没写成」或手工删目录的后果：
    #     界面照旧列出来，点启动却挂不上，必须清掉。
    if include_registry:
        for item in items:
            ext_id = str(item.get("id") or "")
            if not ext_id:
                continue
            overlays = _overlay_paths_of(item)
            if not overlays:
                continue
            if any(p.exists() for p in overlays):
                continue
            findings.append(
                Finding(
                    kind="registry.stale",
                    detail=f"{ext_id}：注册表登记了，但覆盖层文件已不存在",
                    path=paths.CONFIG_DIR / "extensions.json",
                )
            )

    # --- 2 packs/ 下无人认领的目录
    if paths.PACKS.is_dir():
        for child in sorted(paths.PACKS.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if name in registered or _is_story_or_packset_dir(name):
                continue
            # 目录里没有任何 .pck，多半是安装中断留下的空壳
            has_pck = any(child.glob("*.pck"))
            findings.append(
                Finding(
                    kind="packs.orphan",
                    detail=f"{name}：不在注册表里的残留目录"
                    + ("" if has_pck else "（无 .pck，安装中断产物）"),
                    path=child,
                    bytes=_dir_bytes(child),
                )
            )

    # --- 3 插桩缓存：没有任何已注册扩展在用的都算垃圾
    cache_dir = paths.CACHE / "instrumented"
    if cache_dir.is_dir():
        for pck in sorted(cache_dir.glob("*.pck")):
            meta_path = cache_dir / f"{pck.stem}.json"
            meta = read_json(meta_path, default={}) or {}
            sig = meta.get("overlay_sig") or []
            # 缓存元数据里记着它依赖哪些 overlay；只要全都已不存在，
            # 这份缓存就不可能再被正确命中（键里的 overlay 签名对不上）。
            alive = False
            for rec in sig:
                if not isinstance(rec, dict):
                    continue
                target = str(rec.get("target") or "")
                if not target:
                    continue
                try:
                    if Path(target).exists():
                        alive = True
                        break
                    if str(Path(target).resolve()).lower() in known_overlays:
                        alive = True
                        break
                except OSError:
                    continue
            if alive:
                continue
            findings.append(
                Finding(
                    kind="cache.instrumented",
                    detail=f"插桩缓存 {pck.stem[:8]}（来源覆盖层已不存在）",
                    path=pck,
                    bytes=pck.stat().st_size,
                )
            )

    # --- 4 安装中间目录
    if paths.BUILDS.is_dir():
        for child in sorted(paths.BUILDS.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if not (name.startswith("_install_") or name.startswith("_bundle_") or name == "_ext_src"):
                continue
            findings.append(
                Finding(
                    kind="builds.temp",
                    detail=f"{name}：安装中间产物",
                    path=child,
                    bytes=_dir_bytes(child),
                )
            )

    return findings


# ---------------------------------------------------------------- 执行


def apply(findings: list[Finding]) -> CleanReport:
    """执行清理。

    注册表条目单独处理：``packs.orphan`` 只删目录，``registry.stale`` 只删
    注册表条目，两者互不牵连——否则「只删目录」会把还在用的条目一起干掉。
    """
    report = CleanReport()
    stale_ids: set[str] = set()

    for finding in findings:
        if finding.kind == "registry.stale":
            # detail 形如 "<id>：注册表登记了..."
            stale_ids.add(finding.detail.split("：", 1)[0])
            continue
        try:
            if finding.path.is_dir():
                shutil.rmtree(finding.path, ignore_errors=False)
            elif finding.path.exists():
                finding.path.unlink()
            # 插桩缓存的同名元数据一起删，避免只剩 .json 的残骸
            if finding.kind == "cache.instrumented":
                meta = finding.path.with_suffix(".json")
                if meta.exists():
                    try:
                        meta.unlink()
                    except OSError:
                        pass
            report.removed.append(finding)
            report.freed += finding.bytes
        except OSError as exc:
            report.failed.append({"path": str(finding.path), "error": str(exc)})

    if stale_ids:
        keep = [i for i in _registry_items() if str(i.get("id") or "") not in stale_ids]
        try:
            _save_registry(keep)
            for ext_id in sorted(stale_ids):
                report.removed.append(
                    Finding(
                        kind="registry.stale",
                        detail=f"{ext_id}：已从注册表移除过期条目",
                        path=paths.CONFIG_DIR / "extensions.json",
                    )
                )
        except OSError as exc:
            report.failed.append(
                {"path": str(paths.CONFIG_DIR / "extensions.json"), "error": str(exc)}
            )

    return report


def clean_all() -> CleanReport:
    """算计划并立即执行（供「一键清理」与 CLI 使用）。"""
    return apply(plan())