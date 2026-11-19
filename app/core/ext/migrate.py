"""把「同一扩展装了多条线」的存量数据收敛成一条线。

背景
----

1.3 及以前允许同一个扩展装到多条线：``service.install(target_line=...)``
会为每条线各生成一份覆盖层。实测本机 9 个双线包的清单全部声明 4.6 ——
它们的非原生线覆盖层是「跨线重算补丁」的产物。1.4 起跨线安装改为硬拒绝，
这些历史数据必须清理，否则别的线会继续挂载一批并不适配它的包。

规则
----

- 以**包内清单**声明的 ``baseline.engine.line`` 为唯一依据（不能读
  ``lines[L].baseline``：安装时它会被改写成目标线）；
- 保留匹配线，删除其它线的 ``overlay-<tag>.pck`` / ``manifest-<tag>.v2.json``
  与 ``lines[tag]``；
- 没有任何匹配线时**不删文件**，只标记 ``needs_reinstall``，让用户重装；
- 写注册表前先备份到 ``config/backups/<时间戳>/extensions.json``；
- 幂等：第二次扫描不再有 ``drop`` 项。
"""

from __future__ import annotations

import copy
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..fsutil import atomic_write_json, read_json
from . import enable as enable_mod
from . import service as service_mod


@dataclass
class MigrationItem:
    id: str
    name: str
    declared: str
    keep_line: str
    drop_lines: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    bytes: int = 0
    action: str = "keep"        # keep / drop / needs_reinstall

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "declared": self.declared,
            "keep_line": self.keep_line,
            "drop_lines": list(self.drop_lines),
            "files": list(self.files),
            "bytes": self.bytes,
            "action": self.action,
        }


@dataclass
class MigrationPlan:
    items: list[MigrationItem] = field(default_factory=list)
    backup: Path | None = None
    errors: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def drops(self) -> list[MigrationItem]:
        return [i for i in self.items if i.action == "drop"]

    @property
    def needs_reinstall(self) -> list[MigrationItem]:
        return [i for i in self.items if i.action == "needs_reinstall"]

    @property
    def bytes(self) -> int:
        return sum(i.bytes for i in self.drops)

    def message(self) -> str:
        if not self.drops and not self.needs_reinstall:
            return "没有需要清理的双线扩展"
        parts: list[str] = []
        if self.drops:
            parts.append(
                f"清理 {len(self.drops)} 个扩展的另一条线副本，"
                f"释放 {self.bytes / 1024 / 1024:.1f} MB"
            )
        if self.needs_reinstall:
            parts.append(f"{len(self.needs_reinstall)} 个扩展没有匹配线，需重新安装")
        return "；".join(parts)


def _tag(line: str) -> str:
    return paths.line_tag(line)


def _registry_path() -> Path:
    return Path(service_mod.REGISTRY)


def backup_registry(tag: str = "migrate") -> Path | None:
    """把当前注册表原样拷一份；没有注册表时返回 None。"""
    src = _registry_path()
    if not src.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_dir = paths.CONFIG_DIR / "backups" / f"{stamp}-{tag}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "extensions.json"
    shutil.copyfile(src, dest)
    return dest


def _entry_bytes(ext_id: str, line: str) -> tuple[list[str], int]:
    root = paths.PACKS / ext_id
    files: list[str] = []
    total = 0
    for name in (f"overlay-{_tag(line)}.pck", f"manifest-{_tag(line)}.v2.json"):
        path = root / name
        if path.is_file():
            files.append(path.as_posix())
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return files, total


def scan_single_line(cfg: dict) -> MigrationPlan:
    """只读扫描：哪些扩展需要收敛、要删哪些文件。"""
    plan = MigrationPlan()
    for item in service_mod.ExtensionService(cfg).installed():
        ext_id = str(item.get("id") or "")
        if not ext_id:
            continue
        lines = item.get("lines")
        declared = enable_mod.declared_line_of(item)
        row = MigrationItem(
            id=ext_id,
            name=str(item.get("name") or ext_id),
            declared=declared,
            keep_line=declared,
        )
        if not isinstance(lines, dict) or not lines:
            top = str(item.get("engine_line") or "")
            if top and top != declared:
                row.action = "needs_reinstall"
            plan.items.append(row)
            continue
        if declared in lines:
            row.drop_lines = [str(x) for x in sorted(lines) if str(x) != declared]
            row.action = "drop" if row.drop_lines else "keep"
            for drop in row.drop_lines:
                files, size = _entry_bytes(ext_id, drop)
                row.files.extend(files)
                row.bytes += size
        else:
            row.action = "needs_reinstall"
            row.drop_lines = []
        plan.items.append(row)
    return plan


def _mirror_from(entry: dict) -> dict:
    """把某条线的字段投影到顶层（顶层只是「最近一次安装」的镜像）。"""
    return {
        "engine_line": str(entry.get("line") or ""),
        "baseline": str(entry.get("baseline") or ""),
        "overlay": str(entry.get("overlay") or ""),
        "entry_count": int(entry.get("entry_count") or 0),
        "sha256": str(entry.get("sha256") or ""),
        "status": str(entry.get("status") or ""),
        "verified": bool(entry.get("verified")),
        "runtime_hooks": copy.deepcopy(entry.get("runtime_hooks") or []),
        "patches": copy.deepcopy(entry.get("patches") or []),
        "hooks_pending": int(entry.get("hooks_pending") or 0),
    }


def migrate_single_line(cfg: dict, *, dry_run: bool = False, backup: bool = True) -> MigrationPlan:
    """执行收敛。``dry_run=True`` 只返回计划，不动磁盘。"""
    plan = scan_single_line(cfg)
    if dry_run or not plan.drops:
        return plan

    registry = _registry_path()
    if backup:
        try:
            plan.backup = backup_registry("migrate")
        except OSError as exc:
            plan.errors.append(f"备份注册表失败，已中止：{exc}")
            return plan

    raw = read_json(registry, default={"schema": 1, "items": []}) or {}
    items = [i for i in (raw.get("items") or []) if isinstance(i, dict)]
    drop_map = {i.id: i for i in plan.drops}
    touched_lines: set[str] = set()

    for item in items:
        row = drop_map.get(str(item.get("id") or ""))
        if row is None:
            continue
        lines = item.get("lines")
        if not isinstance(lines, dict):
            continue
        for line in row.drop_lines:
            lines.pop(line, None)
            touched_lines.add(line)
        # 顶层镜像改成保留线里「最近安装」的那一条
        remain = [v for v in lines.values() if isinstance(v, dict)]
        if remain and row.keep_line in lines:
            keep_entry = lines[row.keep_line]
            if isinstance(keep_entry, dict):
                item.update(_mirror_from(keep_entry))
        elif remain:
            newest = max(remain, key=lambda v: str(v.get("installed_at") or ""))
            item.update(_mirror_from(newest))

    # 先写注册表（唯一事实来源），再删文件。写失败时文件一个都没动。
    try:
        atomic_write_json(registry, {"schema": 1, "items": items})
    except OSError as exc:
        plan.errors.append(f"写注册表失败，文件未删除：{exc}")
        return plan

    # 删之前先把清单落到备份目录：注册表能还原，文件还原不了，
    # 至少留下「删了什么」的证据，出问题时能对照排查。
    if plan.backup is not None:
        try:
            rows = [
                f"# {time.strftime('%Y-%m-%d %H:%M:%S')} 跨线收敛删除清单",
                f"# 注册表备份：{plan.backup.as_posix()}",
            ]
            for row in plan.drops:
                rows.append(f"[{row.id}] {row.name} 声明={row.declared} 删除线={','.join(row.drop_lines)}")
                rows.extend(f"  {p}" for p in row.files)
            (plan.backup.parent / "deleted-files.txt").write_text(
                "\n".join(rows) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    for row in plan.drops:
        for raw_path in row.files:
            path = Path(raw_path)
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:
                plan.errors.append(f"{path.name}：{exc}")
        # 目录空了就顺手删掉，避免 packs/ 下留空壳
        root = paths.PACKS / row.id
        try:
            if root.is_dir() and not any(root.iterdir()):
                root.rmdir()
        except OSError:
            pass

    for line in sorted(touched_lines):
        try:
            enable_mod._purge_instrumented_cache(line)
        except Exception as exc:  # noqa: BLE001 - 缓存清不掉不影响迁移
            plan.errors.append(f"清 {line} 线缓存失败：{exc}")

    plan.applied = True
    # 迁移后 cfg 里的基线/路径没变，但注册表变了；这里不写 cfg，
    # 调用方负责刷新界面（main_window.invalidate_readiness）。
    return plan


def registry_mtime() -> float:
    """注册表最近修改时间，供界面判断要不要重扫。"""
    try:
        return _registry_path().stat().st_mtime
    except OSError:
        return 0.0
