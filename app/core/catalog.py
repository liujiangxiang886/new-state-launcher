"""条目注册表（catalog）：把「能启动的东西」统一成一种记录。

计划 §5 要求游戏库、扩展库都从一份注册表读；
「注册 catalog」。所以这里不是某个功能私有，而是**跨功能的索引**：

| kind | 来源 | 用来启动的信息 |
|---|---|---|
| ``baseline`` | 配置里的两条基线 | 引擎线 + 基线 id |
| ``extension`` | ``ext/service.py`` 的注册表 | 覆盖层 + 钩子 |

为什么不合并成一张表：三类东西的生命周期完全不同（基线不可删、剧情可整包重建、
扩展可启停），强行合表会让删除/启停语义混乱。这里做的是**只读投影**——
写仍由各自模块负责，读的时候统一成同一形状，界面一次就能列全。

附带一个「最近运行」记录：计划 §5 诊断页要「最近 20 次启动」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import paths
from .fsutil import atomic_write_json, read_json

#: 最近运行记录文件
RUNS = paths.CONFIG_DIR / "recent-runs.json"

#: 保留多少条运行记录
KEEP_RUNS = 40


@dataclass
class Entry:
    """界面消费的统一条目。"""

    id: str
    kind: str                 # baseline / extension / custom
    name: str
    line: str                 # 引擎线，目前只有 "4.6"
    version: str = ""
    enabled: bool = True
    status: str = ""
    entries_count: int = 0     # 覆盖层条目数（baseline 用基线条目数）
    size_bytes: int = 0
    path: str = ""             # 覆盖层或基线 pck
    overlay: str = ""          # 仅 extension
    detail: str = ""
    #: 归属分类 id（见 categories.py）。extension 不是「一个游戏」，恒为空。
    category: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "line": self.line,
            "version": self.version,
            "enabled": self.enabled,
            "status": self.status,
            "entries": self.entries_count,
            "bytes": self.size_bytes,
            "path": self.path,
            "overlay": self.overlay,
            "detail": self.detail,
            "category": self.category,
        }


# ---------------------------------------------------------------- 基线


def _baseline_entries(cfg: dict) -> list[Entry]:
    from . import categories as cat_mod

    out: list[Entry] = []
    for bid, meta in (cfg.get("baselines") or {}).items():
        line = str(meta.get("engine") or "")
        path = Path(str(meta.get("path") or ""))
        exists = path.exists()
        out.append(
            Entry(
                id=bid,
                kind="baseline",
                name=f"原版 {bid.split('.v')[-1]}" if "v1" in bid else bid,
                line=line,
                version=str(meta.get("version") or ""),
                enabled=True,
                status="就绪" if exists else "文件缺失",
                entries_count=int(meta.get("entry_count") or 0),
                size_bytes=int(meta.get("size") or 0),
                path=str(path),
                detail=("分片启动" if meta.get("shard_packset") else "原包直挂"),
                category=cat_mod.category_of_baseline(cfg, bid),
                extra={"shard_packset": meta.get("shard_packset", "")},
            )
        )
    out.sort(key=lambda e: e.line)
    return out


def _custom_entries(cfg: dict) -> list[Entry]:
    """自定义游戏线（exe / pck 两种形态）也进目录，界面才能一次列全。"""
    from . import categories as cat_mod
    from . import custom_lines as cl

    out: list[Entry] = []
    for line in cl.load_all(cfg):
        out.append(
            Entry(
                id=line.id,
                kind="custom",
                name=line.name or line.file_name,
                line=line.engine_line if line.is_pck else "",
                version="",
                enabled=True,
                status="就绪" if line.exists else "文件缺失",
                size_bytes=line.file_path.stat().st_size if line.exists else 0,
                path=line.path,
                detail="pck 挂载" if line.is_pck else "直接执行",
                category=cat_mod.resolve(cfg, line.category or cat_mod.MOD_ID),
                extra={"kind": line.kind, "note": line.note, "args": line.args},
            )
        )
    return out


# ---------------------------------------------------------------- 扩展


def _extension_entries(cfg: dict) -> list[Entry]:
    from .ext.service import ExtensionService

    try:
        items = ExtensionService(cfg).installed()
    except Exception:  # noqa: BLE001 - 扩展模块异常不该拖垮目录
        return []
    out: list[Entry] = []
    for item in items:
        out.append(
            Entry(
                id=str(item.get("id")),
                kind="extension",
                name=str(item.get("name") or item.get("id")),
                line=str(item.get("engine_line") or ""),
                version=str(item.get("version") or ""),
                enabled=bool(item.get("enabled", True)),
                status=str(item.get("status") or ""),
                entries_count=int(item.get("entry_count") or 0),
                size_bytes=0,
                path=str(cfg_mod.resolve_path(item.get("overlay")) or ""),
                overlay=str(cfg_mod.resolve_path(item.get("overlay")) or ""),
                detail=f"钩子 {len(item.get('runtime_hooks') or [])}",
                extra={
                    "verified": item.get("verified"),
                    "installed_at": item.get("installed_at"),
                    "patches": item.get("patches") or [],
                },
            )
        )
    return out


# ---------------------------------------------------------------- 对外


def entries(cfg: dict | None = None, *, kind: str | None = None, line: str | None = None) -> list[Entry]:
    """全量（或按 kind/line 过滤）条目。"""
    cfg = cfg or cfg_mod.load()
    out: list[Entry] = []
    if kind in (None, "baseline"):
        out.extend(_baseline_entries(cfg))
    if kind in (None, "custom"):
        out.extend(_custom_entries(cfg))
    if kind in (None, "extension"):
        out.extend(_extension_entries(cfg))
    if line:
        out = [e for e in out if e.line == line]
    return out


def games(cfg: dict | None = None, *, category: str | None = None) -> list[Entry]:
    """只有「能启动的完整游戏」：内置基线 + 自定义线。

    分类功能只对这两类有意义 —— 扩展挂在某条引擎线上，
    把它们塞进分类只会让「同一份资源出现在两个分类里」。
    """
    cfg = cfg or cfg_mod.load()
    rows = entries(cfg)
    out = [e for e in rows if e.kind in ("baseline", "custom")]
    if category:
        out = [e for e in out if e.category == category]
    return out


def summary(cfg: dict | None = None) -> dict[str, object]:
    """给界面顶栏/诊断页的一份计数汇总。"""
    cfg = cfg or cfg_mod.load()
    rows = entries(cfg)
    by_kind: dict[str, int] = {}
    by_line: dict[str, int] = {}
    by_category: dict[str, int] = {}
    problems: list[str] = []
    for row in rows:
        by_kind[row.kind] = by_kind.get(row.kind, 0) + 1
        by_line[row.line] = by_line.get(row.line, 0) + 1
        if row.kind in ("baseline", "custom"):
            by_category[row.category] = by_category.get(row.category, 0) + 1
        if row.kind in ("baseline", "custom") and row.status != "就绪":
            problems.append(f"{row.id}：{row.status}")
        if row.kind == "extension" and not Path(row.overlay).exists():
            problems.append(f"扩展 {row.id} 的覆盖层缺失")
    return {
        "total": len(rows),
        "by_kind": by_kind,
        "by_line": by_line,
        "by_category": by_category,
        "problems": problems,
        "active_extension": [e.id for e in rows if e.kind == "extension" and e.enabled],
    }


# ---------------------------------------------------------------- 最近运行


def record_run(entry_id: str, line: str, ok: bool, seconds: float, *, note: str = "") -> dict:
    """追加一条运行记录（给诊断页的「最近启动」用）。"""
    data = read_json(RUNS, default={"schema": 1, "items": []}) or {}
    items = list(data.get("items") or [])
    record = {
        "id": entry_id,
        "line": line,
        "ok": bool(ok),
        "seconds": round(float(seconds), 1),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
    }
    items.append(record)
    data["items"] = items[-KEEP_RUNS:]
    data["schema"] = 1
    atomic_write_json(RUNS, data)
    return record


def recent_runs(limit: int = 20) -> list[dict]:
    data = read_json(RUNS, default={"items": []}) or {}
    items = list(data.get("items") or [])
    return list(reversed(items[-limit:]))


def clear_runs() -> None:
    atomic_write_json(RUNS, {"schema": 1, "items": []})