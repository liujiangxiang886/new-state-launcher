"""小游戏注册表与启动参数。

小游戏是**独立进程**，与游戏本体无关：各自有 exe、参数、工作目录，存档目录
默认隔离（重定向 ``APPDATA/LOCALAPPDATA``），避免把本体存档搅乱。

注册表落在 ``config/minigames.json``，纯 JSON，便于手改与版本管理。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import paths
from .fsutil import atomic_write_json, read_json

REGISTRY = paths.CONFIG_DIR / "minigames.json"


@dataclass
class Minigame:
    id: str
    name: str
    exe: str
    args: list[str] = field(default_factory=list)
    workdir: str = ""
    isolate_save: bool = True
    note: str = ""
    added_at: str = ""
    env: dict[str, str] = field(default_factory=dict)
    timeout_sec: int = 0
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "exe": self.exe,
            "args": list(self.args),
            "workdir": self.workdir,
            "isolate_save": self.isolate_save,
            "note": self.note,
            "added_at": self.added_at,
            "env": dict(self.env),
            "timeout_sec": self.timeout_sec,
            "tags": list(self.tags),
        }

    @property
    def exists(self) -> bool:
        return Path(self.exe).exists()


def load() -> list[Minigame]:
    data = read_json(REGISTRY, default={"schema": 1, "items": []}) or {}
    out: list[Minigame] = []
    for item in data.get("items") or []:
        if not isinstance(item, dict) or not item.get("exe"):
            continue
        out.append(
            Minigame(
                id=str(item.get("id") or Path(item["exe"]).stem),
                name=str(item.get("name") or Path(item["exe"]).stem),
                exe=str(item["exe"]),
                args=[str(a) for a in (item.get("args") or [])],
                workdir=str(item.get("workdir") or ""),
                isolate_save=bool(item.get("isolate_save", True)),
                note=str(item.get("note") or ""),
                added_at=str(item.get("added_at") or ""),
                env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
                timeout_sec=int(item.get("timeout_sec") or 0),
                tags=[str(x) for x in (item.get("tags") or [])],
            )
        )
    return out


def save(items: list[Minigame]) -> Path:
    return atomic_write_json(REGISTRY, {"schema": 1, "items": [i.as_dict() for i in items]})


def add(exe: str | Path, name: str = "", args: list[str] | None = None) -> Minigame:
    p = Path(exe).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"程序不存在：{p}")
    items = load()
    mg = Minigame(
        id=_unique_id(p.stem, items),
        name=name or p.stem,
        exe=str(p.resolve()),
        args=list(args or []),
        workdir=str(p.parent.resolve()),
        isolate_save=True,
        added_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    items.append(mg)
    save(items)
    return mg


def remove(mg_id: str) -> bool:
    items = load()
    kept = [i for i in items if i.id != mg_id]
    if len(kept) == len(items):
        return False
    save(kept)
    return True


def get(mg_id: str) -> Minigame | None:
    return next((i for i in load() if i.id == mg_id), None)


def update(mg: Minigame) -> bool:
    """按 id 覆盖一条记录（界面编辑用）。"""
    items = load()
    hit = False
    for index, item in enumerate(items):
        if item.id == mg.id:
            items[index] = mg
            hit = True
            break
    if hit:
        save(items)
    return hit


def _unique_id(base: str, items: list[Minigame]) -> str:
    used = {i.id for i in items}
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def save_dir(mg: Minigame, profile: str = "default") -> Path:
    return paths.profile_dir(profile) / "minigames" / mg.id


def build_invocation(
    mg: Minigame, cfg: dict | None = None, profile: str | None = None
) -> tuple[str, list[str], str, dict[str, str]]:
    """返回 (program, args, cwd, env)。存档隔离通过重定向 APPDATA 实现。"""
    cfg = cfg or cfg_mod.load()
    profile = profile or (cfg.get("profiles") or {}).get("default", "default")
    env = os.environ.copy()
    if mg.isolate_save:
        target = save_dir(mg, profile)
        target.mkdir(parents=True, exist_ok=True)
        env["APPDATA"] = str(target)
        env["LOCALAPPDATA"] = str(target)
    for key, value in (mg.env or {}).items():
        if key:
            env[str(key)] = str(value)
    cwd = mg.workdir or str(Path(mg.exe).parent)
    return mg.exe, list(mg.args), cwd, env
