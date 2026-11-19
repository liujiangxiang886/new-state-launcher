"""存档档案：指纹、变更摘要、历史备份。

为什么要做：启动器把 ``APPDATA`` 重定向到 profile 后，游戏写存档全落在
``profiles/<名>/appdata`` 里。一旦某次覆盖层把游戏写坏（比如把角色职业字段
改了），没有历史就只能认栽——这个项目里已经真实发生过一次，靠备份才救回来。

做法很朴素，但顺序很重要：

1. **启动前**先 ``backup()``：把当前存档文件复制到 ``.history/<时间戳>/``，
   超过 10 份就淘汰最旧的；
2. 再取一次``fingerprint()``（相对路径 + 大小 + mtime + 小文件 sha256），
   写进 ``manifest.json``；
3. **退出后**重新取指纹，与 manifest 比出 ``added/changed/removed``；
4. 摘要写进 run 目录，界面日志里显示一行。

所有写盘都走 ``atomic_write_json``（先写临时文件再 ``os.replace``），
中途断电不会留下半份 JSON。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .fsutil import atomic_write_json, read_json

#: 保留多少份历史备份
HISTORY_KEEP = 10

#: 超过这个大小的文件不逐字节哈希，用 大小+mtime 代替（存档一般都很小）
HASH_LIMIT = 8 * 1024 * 1024

#: 视为「存档文件」的后缀（backup 只备这些，避免把缓存也复制一遍）
SAVE_SUFFIXES = (".json", ".save", ".sav", ".dat", ".cfg", ".ini")

#: 文件名前缀命中这些也备一份。
#: 存在的理由：游戏自己会写 ``offline_roles.json.bak``，后缀是 ``.bak``，
#: 只按后缀判断会把这个救命备份漏掉；而它恰恰是存档写坏后最该还原的文件。
SAVE_NAME_HINTS = (
    "offline_roles",
    "mod_states",
    "training_settings",
    "dimension_ruins_tuning",
    "save_info",
    "settings",
)

#: 这些后缀不是存档：``.lock`` 是运行期互斥标记，复制/还原它会让游戏误判
SKIP_SUFFIXES = (".lock", ".pid", ".log", ".tmp", ".part")

#: 不参与指纹的目录（引擎缓存，每次启动都变）
SKIP_DIRS = {"logs", "shader_cache", "vulkan", "caches"}


def is_save_file(path: Path) -> bool:
    """是不是值得备份/还原的存档文件。"""
    name = path.name.lower()
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if path.suffix.lower() in SAVE_SUFFIXES:
        return True
    return any(name.startswith(hint) for hint in SAVE_NAME_HINTS)


def _appdata(profile: Path) -> Path:
    return profile / "appdata"


def _history(profile: Path) -> Path:
    return profile / ".history"


def _hash_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def fingerprint(profile: str | Path) -> dict[str, dict]:
    """``{相对路径: {size, mtime, sha256|""}}``。"""
    root = _appdata(Path(profile))
    out: dict[str, dict] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")[:-1]):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        digest = ""
        if stat.st_size <= HASH_LIMIT:
            try:
                digest = _hash_file(path)
            except OSError:
                digest = ""
        out[rel] = {"size": stat.st_size, "mtime": int(stat.st_mtime), "sha256": digest}
    return out


def diff(before: dict[str, dict], after: dict[str, dict]) -> dict:
    """比较两份指纹，给出新增 / 变化 / 删除。"""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        rel
        for rel in set(before) & set(after)
        if before[rel].get("size") != after[rel].get("size")
        or before[rel].get("mtime") != after[rel].get("mtime")
        or (before[rel].get("sha256") and after[rel].get("sha256"))
        and before[rel]["sha256"] != after[rel]["sha256"]
    )
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "counts": {"added": len(added), "changed": len(changed), "removed": len(removed)},
        "total": len(added) + len(changed) + len(removed),
    }


def manifest_path(profile: str | Path) -> Path:
    return Path(profile) / "manifest.json"


def load_manifest(profile: str | Path) -> dict | None:
    data = read_json(manifest_path(profile), default=None)
    return data if isinstance(data, dict) else None


def save_manifest(profile: str | Path, prints: dict[str, dict], note: str = "") -> Path:
    payload = {
        "schema": 1,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "file_count": len(prints),
        "total_bytes": sum(int(v.get("size") or 0) for v in prints.values()),
        "files": prints,
    }
    return atomic_write_json(manifest_path(profile), payload)


def backup(profile: str | Path, keep: int = HISTORY_KEEP) -> Path | None:
    """把当前存档文件复制到 ``.history/<ts>/``；没有存档就返回 None。"""
    root = _appdata(Path(profile))
    if not root.exists():
        return None
    files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and is_save_file(p)
        and not any(x in SKIP_DIRS for x in p.relative_to(root).parts[:-1])
    ]
    if not files:
        return None

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = _history(Path(profile)) / stamp
    suffix = 1
    while target.exists():
        target = _history(Path(profile)) / f"{stamp}-{suffix}"
        suffix += 1
    for src in files:
        dst = target / src.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    meta = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": [f.relative_to(root).as_posix() for f in files],
        "bytes": sum(f.stat().st_size for f in files),
    }
    atomic_write_json(target / "_backup.json", meta)
    prune(Path(profile), keep)
    return target


def prune(profile: str | Path, keep: int = HISTORY_KEEP) -> int:
    """只保留最近 ``keep`` 份备份，返回删除数量。"""
    root = _history(Path(profile))
    if not root.exists():
        return 0
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    dead = dirs[:-keep] if len(dirs) > keep else []
    for path in dead:
        shutil.rmtree(path, ignore_errors=True)
    return len(dead)


def history(profile: str | Path) -> list[dict]:
    """历史备份清单（新的在前），供界面展示。"""
    root = _history(Path(profile))
    if not root.exists():
        return []
    out: list[dict] = []
    for path in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        meta = read_json(path / "_backup.json", default=None) or {}
        out.append(
            {
                "path": str(path),
                "created_at": meta.get("created_at", path.name),
                "files": len(meta.get("files") or []),
                "bytes": int(meta.get("bytes") or 0),
            }
        )
    return out


def summary_line(delta: dict) -> str:
    counts = delta.get("counts") or {}
    if not delta.get("total"):
        return "存档无变化"
    parts = []
    if counts.get("added"):
        parts.append(f"新增 {counts['added']}")
    if counts.get("changed"):
        parts.append(f"变更 {counts['changed']}")
    if counts.get("removed"):
        parts.append(f"删除 {counts['removed']}")
    return "存档变化：" + "，".join(parts)
