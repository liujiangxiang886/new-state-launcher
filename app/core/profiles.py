"""存档档案（profile）：隔离、指纹、历史备份。

计划 §4.10 要求：

- ``profiles/<name>/appdata`` + ``saves/`` + ``manifest.json``
- 启动时重定向 ``APPDATA``/``LOCALAPPDATA``，``user://`` 自动落到 profile
- 启动前记录指纹，退出后 diff，``.history/<ts>`` 保留 10 份

指纹/diff/备份的实现在 :mod:`app.core.saves`；本模块负责**档案本身**的生命周期：
创建、列举、重命名、删除、当前指针、以及「把环境变量指过去」这一件事。

为什么要单独一层：runner 只管启动，UI 要列档案、切档案、删档案，两边都需要
同一套「档案是否合法、名字能不能用、目录在哪」的规则。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import paths
from . import saves as saves_mod
from .errors import LauncherError
from .fsutil import atomic_write_json, read_json

#: 档案名允许的字符（要能直接做目录名）
_NAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff .-]{0,31}$")

#: 不允许的档案名（Windows 保留字 + 与目录语义冲突）
RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
    ".", "..",
}

DEFAULT_NAME = "default"

#: 开发期档位名的前缀。
#:
#: 这些名字不该出现在玩家机上 —— 它们是打包时漏改 ``profiles.default``
#: 留下的。1.5 之前真的发生过：玩家日志里写着「存档 profile = smoke46」，
#: 那是挂载冒烟测试用的档位名，跟着出厂配置一起发出去了。
#: 迁移见 :func:`migrate_legacy_default`。
LEGACY_TEST_PREFIXES = ("smoke", "test", "qa_")


def is_legacy_test_name(name: str) -> bool:
    low = (name or "").strip().lower()
    return bool(low) and low.startswith(LEGACY_TEST_PREFIXES)


def migrate_legacy_default(cfg: dict | None = None) -> str:
    """把开发期残留的档位名迁成 ``default``（连同目录改名）。

    返回迁移后（或保持原样）的档位名。幂等：改完之后再调就是空转。

    **只在目标目录不存在时改名。** 玩家可能已经有一个 ``default`` 档位，
    硬把两份存档并到一起等于丢档 —— 那种情况下保持原名不动，宁可名字
    难看，也不能让玩家进游戏发现角色没了。

    改名失败（目录被占用 / 权限不足）时同样保持原名并返回，
    下次启动还会再试一次 —— 迁移不是一次性的不可逆操作。
    """
    cfg = cfg if cfg is not None else cfg_mod.load()
    current = str((cfg.get("profiles") or {}).get("default") or "")
    if not is_legacy_test_name(current):
        return current or DEFAULT_NAME

    src = paths.profile_dir(current)
    dst = paths.profile_dir(DEFAULT_NAME)

    if src.is_dir() and dst.exists():
        # 两份存档都在：不能并，保持原名。
        return current
    if src.is_dir():
        try:
            src.rename(dst)
        except OSError:
            return current

    cfg.setdefault("profiles", {})["default"] = DEFAULT_NAME
    try:
        cfg_mod.save(cfg)
    except OSError:
        pass
    return DEFAULT_NAME


@dataclass
class Profile:
    name: str
    path: Path
    #: manifest.json 里的摘要（文件数、字节、最后更新时间）
    file_count: int = 0
    total_bytes: int = 0
    updated_at: str = ""
    backups: int = 0
    #: 是否当前默认档案
    active: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def appdata(self) -> Path:
        return self.path / "appdata"

    @property
    def saves_dir(self) -> Path:
        """历史遗留属性：早先版本建过一个空的 ``saves/``，现已不用。

        真实存档在 ``appdata/Godot/app_userdata/<底座工程名>/``；
        保留这个属性只为不破坏外部脚本。
        """
        return self.path / "saves"

    def exists(self) -> bool:
        return self.path.is_dir()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path.as_posix(),
            "appdata": self.appdata.as_posix(),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "backups": self.backups,
            "updated_at": self.updated_at,
            "active": self.active,
        }


def validate_name(name: str) -> str:
    """校验并规范化档案名。非法时抛 :class:`LauncherError`。"""
    cleaned = (name or "").strip()
    if not cleaned:
        raise LauncherError("档案名不能为空", name=name)
    if cleaned.lower() in RESERVED:
        raise LauncherError(f"档案名是系统保留字：{cleaned}", name=cleaned)
    if not _NAME_RE.match(cleaned):
        raise LauncherError(
            "档案名只能用中英文、数字、空格与 . _ -，且不超过 32 字符", name=cleaned
        )
    return cleaned


def path_of(name: str) -> Path:
    return paths.profile_dir(validate_name(name))


def ensure(name: str = DEFAULT_NAME) -> Path:
    """创建档案目录（幂等），返回档案根。

    同时写一份最小的 ``manifest.json``，让界面在游戏还没跑过时也能列出档案。

    **不再创建 ``saves/`` 子目录**（历史遗留）：没有任何代码往里写东西，
    但玩家看到 ``profiles/<档位>/saves`` 会以为存档在那里，实际存档在
    ``appdata/Godot/app_userdata/<底座工程名>/``。空目录纯属误导，去掉；
    老档位里已经存在的 ``saves/`` 保持原样，不做删除。
    """
    clean = validate_name(name)
    root = paths.profile_dir(clean)
    (root / "appdata").mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.json"
    if not manifest.exists():
        atomic_write_json(
            manifest,
            {
                "schema": 1,
                "profile": clean,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "file_count": 0,
                "total_bytes": 0,
            },
        )
    return root


def active_name(cfg: dict | None = None) -> str:
    """当前默认档案（配置里的 ``profiles.default``）。"""
    cfg = cfg or cfg_mod.load()
    name = str((cfg.get("profiles") or {}).get("default") or DEFAULT_NAME)
    try:
        return validate_name(name)
    except LauncherError:
        return DEFAULT_NAME


def list_all(cfg: dict | None = None) -> list[Profile]:
    """列出全部档案（含大小与备份数），按名字排序，当前档案打 ``active``。"""
    cfg = cfg or cfg_mod.load()
    current = active_name(cfg)
    out: list[Profile] = []
    if not paths.PROFILES.exists():
        return out
    for root in sorted(p for p in paths.PROFILES.iterdir() if p.is_dir()):
        manifest = read_json(root / "manifest.json", default=None) or {}
        history = saves_mod.history(root)
        out.append(
            Profile(
                name=root.name,
                path=root,
                file_count=int(manifest.get("file_count") or 0),
                total_bytes=int(manifest.get("total_bytes") or 0),
                updated_at=str(manifest.get("updated_at") or manifest.get("created_at") or ""),
                backups=len(history),
                active=(root.name == current),
            )
        )
    out.sort(key=lambda p: (not p.active, p.name))
    return out


def get(name: str) -> Profile | None:
    root = paths.profile_dir(name)
    if not root.is_dir():
        return None
    manifest = read_json(root / "manifest.json", default=None) or {}
    return Profile(
        name=root.name,
        path=root,
        file_count=int(manifest.get("file_count") or 0),
        total_bytes=int(manifest.get("total_bytes") or 0),
        updated_at=str(manifest.get("updated_at") or ""),
        backups=len(saves_mod.history(root)),
        active=(root.name == active_name()),
    )


def set_active(name: str, cfg: dict | None = None) -> dict:
    """把某个档案设为默认，写回配置。"""
    clean = validate_name(name)
    if not paths.profile_dir(clean).is_dir():
        raise LauncherError(f"档案不存在：{clean}", name=clean)
    cfg = cfg or cfg_mod.load()
    cfg.setdefault("profiles", {})["default"] = clean
    cfg_mod.save(cfg)
    return cfg


def rename(old: str, new: str, cfg: dict | None = None) -> Path:
    """改名（移动目录 + 必要时更新默认档案指针）。"""
    src = paths.profile_dir(validate_name(old))
    dst = paths.profile_dir(validate_name(new))
    if not src.is_dir():
        raise LauncherError(f"档案不存在：{old}", name=old)
    if dst.exists():
        raise LauncherError(f"目标档案已存在：{new}", name=new)
    src.rename(dst)
    cfg = cfg or cfg_mod.load()
    if active_name(cfg) == old:
        set_active(new, cfg)
    return dst


def delete(name: str, cfg: dict | None = None, *, keep_history: bool = False) -> None:
    """删除档案。默认连历史备份一起删——否则"删了还在"会让人困惑。"""
    clean = validate_name(name)
    if clean == active_name(cfg):
        raise LauncherError("不能删除当前使用中的档案，请先切换到别的档案", name=clean)
    root = paths.profile_dir(clean)
    if not root.is_dir():
        raise LauncherError(f"档案不存在：{clean}", name=clean)
    if keep_history:
        for child in root.iterdir():
            if child.is_dir() and child.name != ".history":
                shutil.rmtree(child, ignore_errors=True)
    else:
        shutil.rmtree(root, ignore_errors=True)


def env_for(name: str | None = None, cfg: dict | None = None) -> dict[str, str]:
    """返回要注入进程的环境变量（``APPDATA`` / ``LOCALAPPDATA``）。

    配置里 ``profiles.redirect_appdata=false`` 时不重定向——有些用户希望
    存档就留在系统默认位置，这个开关必须能被尊重。
    """
    cfg = cfg or cfg_mod.load()
    if not (cfg.get("profiles") or {}).get("redirect_appdata", True):
        return {}
    root = ensure(name or active_name(cfg))
    appdata = root / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    return {
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(appdata),
        # Godot 在 Windows 上也认 XDG，一并指过去，避免个别版本走另一条路径
        "XDG_DATA_HOME": str(appdata),
    }


def snapshot(name: str | None = None, *, backup: bool = True) -> dict:
    """启动前快照：备份 + 取指纹 + 写 manifest，返回供退出后比对的数据。"""
    root = ensure(name or active_name())
    backup_dir = saves_mod.backup(root) if backup else None
    prints = saves_mod.fingerprint(root)
    saves_mod.save_manifest(root, prints, note="pre-launch")
    return {
        "profile": root.name,
        "path": root.as_posix(),
        "backup": backup_dir.as_posix() if backup_dir else "",
        "files": prints,
    }


def finish(snapshot_data: dict) -> dict:
    """退出后比对，返回 ``{delta, summary, manifest}``。"""
    root = Path(str(snapshot_data.get("path") or ""))
    if not root.is_dir():
        return {"delta": {}, "summary": "档案已不存在", "manifest": ""}
    before = snapshot_data.get("files") or {}
    after = saves_mod.fingerprint(root)
    delta = saves_mod.diff(before, after)
    manifest = saves_mod.save_manifest(root, after, note="post-launch")
    return {
        "delta": delta,
        "summary": saves_mod.summary_line(delta),
        "manifest": manifest.as_posix(),
        "backup": snapshot_data.get("backup", ""),
    }


def info(name: str | None = None, cfg: dict | None = None) -> dict:
    """给界面用的一份汇总（大小、文件数、备份、最近变更时间）。"""
    root = ensure(name or active_name(cfg))
    manifest = read_json(root / "manifest.json", default=None) or {}
    history = saves_mod.history(root)
    return {
        "name": root.name,
        "path": root.as_posix(),
        "appdata": (root / "appdata").as_posix(),
        "file_count": int(manifest.get("file_count") or 0),
        "total_bytes": int(manifest.get("total_bytes") or 0),
        "updated_at": str(manifest.get("updated_at") or ""),
        "backups": history,
        "history_count": len(history),
    }


def history_of(name: str | None = None) -> list[dict]:
    return saves_mod.history(ensure(name or active_name()))


def restore_backup(name: str, stamp: str, cfg: dict | None = None) -> Path:
    """把某份历史备份还原回 ``appdata``（还原前先给当前状态做一份备份）。"""
    root = ensure(name)
    source = root / ".history" / stamp
    if not source.is_dir():
        raise LauncherError(f"备份不存在：{stamp}", name=name)
    backup = saves_mod.backup(root)     # 先保住现状
    appdata = root / "appdata"
    restored = 0
    for path in source.rglob("*"):
        if not path.is_file() or path.name == "_backup.json":
            continue
        rel = path.relative_to(source)
        target = appdata / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        restored += 1
    saves_mod.save_manifest(root, saves_mod.fingerprint(root), note=f"restore {stamp}")
    if restored == 0:
        raise LauncherError(f"备份里没有可还原的文件：{stamp}", name=name)
    _ = backup  # 现状备份路径，界面需要时可以进一步暴露
    return root