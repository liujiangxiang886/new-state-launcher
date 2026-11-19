"""游戏 ``user://`` 目录解析 + 角色存档导入。

为什么必须单独一层
------------------

Godot 把 ``user://`` 写到 ``%APPDATA%\\Godot\\app_userdata\\<工程名>``，而
**工程名来自 project.godot 的 ``config/name``**。三处名字并不相同：

===================================  ==========================================
场景                                 实际目录
===================================  ==========================================
玩家直接双击原版游戏                    ``机甲旋风 · 单机``
启动器底座（4.6 线）                    ``机甲旋风 · 兼容底座 4.6``
===================================  ==========================================

底座名由 ``tools/build_base.py`` 的 ``BASE_NAME`` 决定（改它是为了不和原版
抢同一份 ``user://``）。副作用是：**玩家换用启动器后，原版时期的角色存档
在启动器里"看不见"**——存档没丢，只是两个进程读的不是同一个目录。
这正是「角色存档丢失」反馈的根因。

本模块把三处路径算清楚，并提供「把原版存档复制进启动器档位」的安全导入：
先备份现有存档，跳过日志/缓存/锁文件，游戏运行期间一律拒绝。

所有函数只读路径、不做任何隐式删除；唯一写盘操作是 :func:`import_saves`
（复制文件）与 :mod:`saves` 的备份。
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import paths
from . import profiles as profiles_mod
from . import saves as saves_mod
from .errors import LauncherError

#: Godot 在 Windows 上的厂商标识目录
GODOT_VENDOR = "Godot"

#: 原版游戏的工程名（实测自 project.godot 的 config/name）
ORIGINAL_PROJECT = "机甲旋风 · 单机"

#: 启动器底座工程名前缀（与 tools/build_base.py 的 BASE_NAME 保持一致）
BASE_PREFIX = "机甲旋风 · 兼容底座"

#: 存档扫描要认的底座目录后缀。
#:
#: 两条线各有独立的存档目录（工程名带线号：``机甲旋风 · 兼容底座 4.6`` /
#: ``… 4.7``），导入存档时必须**两条都扫**：玩家从 4.6 转到 4.7 时，
#: 源档在 4.6 的目录里；只认当前线就找不到，只能手工拷目录。
#:
#: 存档本身天然隔离（工程名不同 → 目录不同），所以「都扫」不会串档：
#: 只是把候选列全，具体导入哪份仍由玩家选，写进去也只会写当前档位。
SCAN_LINES = paths.LINES

#: 不参与导入的目录（引擎缓存，复制过去只会污染目标）
SKIP_DIRS = {"logs", "shader_cache", "vulkan", "caches", "cache"}

#: 不参与导入的后缀：锁文件复制过去会让游戏误判「已有实例在跑」
SKIP_SUFFIXES = {".lock", ".log", ".tmp", ".part", ".pid"}

#: 单个文件上限：存档一般几十 KB，超过这个量级的多半是误放进去的资源
MAX_FILE_BYTES = 64 * 1024 * 1024

#: 见到这些文件名就当「角色存档」重点提示（不是白名单，只用于文案）
SAVE_FILE_HINTS = (
    "offline_roles",
    "mod_states",
    "training_settings",
    "dimension_ruins_tuning",
    "save_info",
    "settings",
)


# ---------------------------------------------------------------- 路径解析


def base_project_name(line: str) -> str:
    """底座工程名，决定底座进程的 ``user://`` 目录名。"""
    return f"{BASE_PREFIX} {line}"


def _windows_roaming() -> Path | None:
    """用 Shell API 取**真实**的 Roaming 目录。

    为什么不直接读 ``%APPDATA%``：启动器给游戏子进程注入的是重定向后的
    ``APPDATA``；万一启动器自己也是被别的壳带起来的，环境变量可能已经被改，
    那样「原版存档位置」就会算到 profile 里去。Shell API 不受环境变量影响。
    """
    try:
        import ctypes
        from ctypes import wintypes

        CSIDL_APPDATA = 0x001A
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_APPDATA, None, 0, buf) == 0:
            value = buf.value.strip()
            if value:
                return Path(value)
    except Exception:  # noqa: BLE001 - 非 Windows / 无权限时静默回落
        return None
    return None


def system_appdata() -> Path:
    """真实 ``%APPDATA%``（不受启动器重定向影响）。"""
    found = _windows_roaming()
    if found is not None:
        return found
    raw = os.environ.get("APPDATA") or ""
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Roaming"


def userdata_root(appdata: Path | None = None) -> Path:
    """``.../Godot/app_userdata``。"""
    return (appdata or system_appdata()) / GODOT_VENDOR / "app_userdata"


def original_dir(project: str = ORIGINAL_PROJECT) -> Path:
    """原版游戏直接启动时的存档目录（玩家原有角色存档就在这里）。"""
    return userdata_root() / project


def redirect_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg or cfg_mod.load()
    return bool((cfg.get("profiles") or {}).get("redirect_appdata", True))


def launcher_dir(profile: str, line: str, cfg: dict | None = None) -> Path:
    """启动器里该档位 / 该版本线的**实际**存档目录。

    关闭「存档隔离」时不重定向 ``APPDATA``，游戏会写进真实的
    ``%APPDATA%\\Godot\\app_userdata\\<底座工程名>``——这里如实反映，
    否则界面显示的路径和文件真正所在的地方对不上。
    """
    cfg = cfg or cfg_mod.load()
    name = base_project_name(line)
    if redirect_enabled(cfg):
        return paths.profile_dir(profile) / "appdata" / GODOT_VENDOR / "app_userdata" / name
    return userdata_root() / name


def profile_appdata(profile: str) -> Path:
    return paths.profile_dir(profile) / "appdata"


# ---------------------------------------------------------------- 文件枚举


@dataclass
class SaveFile:
    path: Path
    rel: str
    size: int
    mtime: float


@dataclass
class Source:
    """一个可导入的来源目录。"""

    key: str
    label: str
    path: Path
    note: str = ""
    files: list[SaveFile] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.path.is_dir()

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def usable(self) -> bool:
        return self.exists and bool(self.files)

    def latest(self) -> float:
        return max((f.mtime for f in self.files), default=0.0)

    def describe(self) -> str:
        if not self.exists:
            return "目录不存在"
        if not self.files:
            return "目录存在，但没有可导入的存档文件"
        return f"{len(self.files)} 个文件 · {human_size(self.total_bytes)}"


def human_size(num: int | float) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def list_save_files(root: str | Path) -> list[SaveFile]:
    """列出目录下应导入的文件：跳过日志/缓存目录与锁文件。"""
    base = Path(root)
    out: list[SaveFile] = []
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        parts = rel.split("/")
        if any(part.lower() in SKIP_DIRS for part in parts[:-1]):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_FILE_BYTES:
            continue
        out.append(SaveFile(path=path, rel=rel, size=stat.st_size, mtime=stat.st_mtime))
    return out


def _source(key: str, label: str, path: Path, note: str = "") -> Source:
    return Source(key=key, label=label, path=path, note=note, files=list_save_files(path))


def candidates(cfg: dict, profile: str, line: str) -> list[Source]:
    """自动探测可能存有玩家角色存档的目录，按「最可能是玩家原有存档」排序。"""
    out: list[Source] = []
    seen: set[str] = set()

    def add(key: str, label: str, path: Path, note: str = "", *, keep_empty: bool = True) -> None:
        try:
            key_path = str(path.resolve())
        except OSError:
            key_path = str(path)
        if key_path in seen:
            return
        seen.add(key_path)
        src = _source(key, label, path, note)
        if not keep_empty and not src.usable:
            return
        out.append(src)

    # 1) 原版游戏（玩家直接双击 mchGame.exe / 机甲旋风Demo.exe 时的目录）
    add("original", "原版游戏（默认存档位置）", original_dir(), ORIGINAL_PROJECT)

    # 2) 旧启动器 / 关闭隔离时留下的底座目录
    add("legacy_base", "旧启动器留下的底座存档", userdata_root() / BASE_PREFIX, BASE_PREFIX)
    for other in SCAN_LINES:
        add(
            f"legacy_{other}",
            f"本机底座 {other}（未隔离运行）",
            userdata_root() / base_project_name(other),
            base_project_name(other),
        )

    # 3) 同一档位的另一条版本线
    for other in SCAN_LINES:
        if other == line:
            continue
        add(
            f"profile_other_{other}",
            f"本档位 {profile} · {other} 线",
            launcher_dir(profile, other, cfg),
            "同档位另一条版本线",
        )

    # 4) 其它档位（换过档位的玩家常把存档留在旧档位里）
    #    只列真的有文件的档位：测试/临时档位很多，全列出来会把下拉框淹掉。
    if paths.PROFILES.is_dir():
        for prof in sorted(p for p in paths.PROFILES.iterdir() if p.is_dir()):
            if prof.name == profile:
                continue
            for other in SCAN_LINES:
                add(
                    f"profile_{prof.name}_{other}",
                    f"档位 {prof.name} · {other} 线",
                    launcher_dir(prof.name, other, cfg),
                    "其它存档档位",
                    keep_empty=False,
                )
    return out


# ---------------------------------------------------------------- 导入


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def import_saves(
    profile: str,
    line: str,
    source: str | Path,
    *,
    cfg: dict | None = None,
    backup: bool = True,
    extra_lines: tuple[str, ...] | list[str] = (),
) -> dict:
    """把 ``source`` 里的存档复制进档位 ``profile`` 的 ``line`` 线。

    安全规则（每一条都对应一次真实事故或可预期的误操作）：

    1. **游戏运行中拒绝导入**——运行中的进程退出时会用内存里的旧数据覆盖
       刚写进去的文件，导了等于没导，还可能把存档写坏；
    2. **默认先备份现有存档**（``.history/<时间戳>/``），导错能退回去；
    3. 只复制文件，不删来源目录里的任何东西；
    4. 跳过 ``logs/``、``shader_cache/``、``*.lock``——锁文件复制过去会让
       游戏以为已经有实例在跑；
    5. 目标是来源本身或其子目录时，该目标直接跳过，避免自我复制。

    返回 ``{profile, targets, copied, bytes, skipped, backup, source}``。
    """
    from . import runtime_lock

    if runtime_lock.is_game_running():
        raise LauncherError(
            "游戏正在运行，无法导入存档。\n请先在启动器里「结束游戏」，再导入。",
            action="import_saves",
        )

    src = Path(source).expanduser()
    if not src.is_dir():
        raise LauncherError(f"来源目录不存在：{src}", path=str(src))

    files = list_save_files(src)
    if not files:
        raise LauncherError(
            f"这个目录里没有可导入的存档文件：\n{src}\n\n"
            "原版存档目录里应该有 offline_roles.json。",
            path=str(src),
        )

    cfg = cfg or cfg_mod.load()
    targets: list[tuple[str, Path]] = [(line, launcher_dir(profile, line, cfg))]
    for extra in extra_lines or ():
        extra = str(extra or "").strip()
        if not extra or extra == line:
            continue
        targets.append((extra, launcher_dir(profile, extra, cfg)))

    root = profiles_mod.ensure(profile)
    backup_dir = saves_mod.backup(root) if backup else None

    copied: list[str] = []
    skipped: list[str] = []
    total = 0
    used: list[dict] = []
    for target_line, target in targets:
        # 来源就是目标（或目标在来源里面）时跳过：否则会把文件复制给自己
        if _inside(src, target) or _inside(target, src):
            skipped.append(f"{target_line} 线：来源与目标相同，已跳过")
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            skipped.append(f"{target_line} 线：创建目录失败 {exc}")
            continue
        ok_here = 0
        for item in files:
            dst = target / item.rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.path, dst)
            except OSError as exc:
                skipped.append(f"{item.rel} → {target_line}：{exc}")
                continue
            ok_here += 1
            total += item.size
            if item.rel not in copied:
                copied.append(item.rel)
        used.append({"line": target_line, "path": target.as_posix(), "files": ok_here})

    if total == 0 and not any(t["files"] for t in used):
        raise LauncherError(
            "导入失败：没有任何文件被写入。\n" + ("\n".join(skipped[:5]) if skipped else ""),
            path=str(src),
        )

    # 让界面/清单立刻看到新存档（指纹 + manifest）
    try:
        saves_mod.save_manifest(root, saves_mod.fingerprint(root), note=f"import {src.name}")
    except OSError:
        pass

    return {
        "profile": profile,
        "source": src.as_posix(),
        "targets": used,
        "copied": len(copied),
        "files": copied,
        "bytes": total,
        "skipped": skipped,
        "backup": backup_dir.as_posix() if backup_dir else "",
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def describe(cfg: dict, profile: str, line: str) -> dict:
    """给界面用的一行/两行路径说明。键名前缀固定，方便 QSS 之外复用。"""
    default = original_dir()
    active = launcher_dir(profile, line, cfg)
    return {
        "original": default.as_posix(),
        "original_exists": default.is_dir(),
        "active": active.as_posix(),
        "active_exists": active.is_dir(),
        "redirect": redirect_enabled(cfg),
        "profile": profile,
        "line": line,
        "save_file": (active / "offline_roles.json").as_posix(),
        "save_file_exists": (active / "offline_roles.json").is_file(),
    }
