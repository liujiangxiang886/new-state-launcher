"""启动器卸载：范围统计、执行、自删除。

设计取舍
--------

**为什么不用 ``MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT)``**
    那要重启才清干净，用户会以为「没卸掉」，体验很差。

**为什么用临时 bat 而不是直接删**
    Windows 不允许删除正在运行的 exe / 已加载的 dll。唯一可靠做法是：
    写一个批处理，用 ``DETACHED_PROCESS`` 启动，主进程立刻退出，
    bat 等句柄释放后再删目录，最后删自己。

**范围分档，用户资产默认保留**
    ``packs/`` 是用户装的扩展，``profiles/`` 是存档，都属于用户资产而非安装
    残留。默认不勾选，避免「卸载顺手删掉几百 MB 的 mod 和存档」。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import paths

#: 卸载范围标识 → 显示名
SCOPES = {
    "program": "程序文件",
    "config": "配置",
    "residue": "缓存与运行残留",
    "packs": "已安装的扩展包",
    "profiles": "游戏存档",
}

#: 游戏本体卸载范围：键是 ``config.baselines`` 里的 id。
#:
#: 本体**不随启动器分发**，路径是玩家自己机器上的绝对路径（通常在别的盘），
#: 所以它不能混进 :data:`SCOPES`——那套逻辑只处理安装目录内的相对路径，
#: 而且会交给自删除 bat。这里单独一套 API，只删登记过的**单个文件**，
#: 绝不删目录、绝不递归。
GAME_BODY_SCOPES: dict[str, str] = {
    "jjxf.v1.45": "游戏本体 · 1.45 原版",
    "jjxf.v4.7": "游戏本体 · 4.7 剧情版",
}

#: 允许被「卸载游戏本体」删除的后缀（防止配置被改坏后误删无关文件）
_BODY_SUFFIXES = {".pck", ".exe"}

#: 各范围覆盖的目录/文件（相对项目根）
_SCOPE_PATHS: dict[str, tuple[str, ...]] = {
    # 程序本体：exe + 运行库 + 引擎 + 基线索引 + 运行时底座 + schema
    "program": ("_internal", "runtime", "engines", "baseline", "schemas"),
    "config": ("config",),
    "residue": ("cache", "logs", "builds"),
    "packs": ("packs",),
    "profiles": ("profiles",),
}

#: 程序本体的额外单文件（目录之外）。
#:
#: 包含**历史别名**：从 1.x 升上来的目录里可能还留着旧 exe，
#: 不认它就清不掉，卸载后残留一个能双击的旧程序最误导人。
_PROGRAM_FILES = ("New state launcher.exe", "新起点启动器.exe", "机甲旋风兼容启动器.exe")

#: 默认勾选：程序文件与配置必删；残留数据可选（默认勾）；用户资产默认不勾
DEFAULT_SCOPES = ("program", "config", "residue")


@dataclass
class ScopeStat:
    key: str
    label: str
    paths: list[Path] = field(default_factory=list)
    bytes: int = 0

    @property
    def exists(self) -> bool:
        return any(p.exists() for p in self.paths)


def _scope_paths(root: Path, key: str) -> list[Path]:
    out = [root / name for name in _SCOPE_PATHS.get(key, ())]
    if key == "program":
        out.extend(root / name for name in _PROGRAM_FILES)
    return out


def _dir_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    if not path.is_dir():
        return 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total


def scan(root: Path | None = None) -> list[ScopeStat]:
    """统计每个卸载范围的体积。只读，不删任何东西。"""
    base = root or paths.ROOT
    out: list[ScopeStat] = []
    for key, label in SCOPES.items():
        stat = ScopeStat(key=key, label=label, paths=_scope_paths(base, key))
        stat.bytes = sum(_dir_size(p) for p in stat.paths)
        out.append(stat)
    return out


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def plan(root: Path | None, scopes: list[str] | tuple[str, ...]) -> list[Path]:
    """把范围展开成待删除路径（去重、过滤不存在的）。"""
    base = root or paths.ROOT
    seen: set[Path] = set()
    out: list[Path] = []
    for key in scopes:
        for path in _scope_paths(base, key):
            resolved = path.resolve() if path.exists() else path
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(path)
    return out


def write_uninstall_script(root: Path | None, targets: list[Path]) -> Path:
    """写一个自删除批处理，返回脚本路径。

    脚本逻辑：等 2 秒（让启动器退出、句柄释放）→ 逐个 ``rmdir /s /q`` /
    ``del /f /q`` → 最后删自己。

    注意（踩过）：批处理文件必须用 **ANSI/GBK** 或纯 ASCII 写入。用 UTF-8
    带 BOM 时 ``cmd.exe`` 会把首行当命令解析失败。这里第一行 ``@echo off``
    之后全部走 ASCII 路径判断，中文路径交给 ``chcp 65001`` 处理。
    """
    base = (root or paths.ROOT).resolve()
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "rem 等待启动器进程退出，释放 exe / dll 句柄",
        "timeout /t 2 /nobreak >nul",
    ]
    for target in targets:
        try:
            rel = target.resolve().relative_to(base)
        except (ValueError, OSError):
            continue  # 超出安装目录的一律不碰，防误删
        quoted = str(rel)
        if target.is_dir() or not target.exists():
            lines.append(f'if exist "{quoted}\\" rmdir /s /q "{quoted}"')
        else:
            lines.append(f'if exist "{quoted}" del /f /q "{quoted}"')
    # 清掉空目录树（自下而上），最后删安装根目录与脚本自身
    lines.extend(
        [
            "rem 递归清空剩余空目录",
            f'for /f "delims=" %%d in (\'dir /s /b /ad "{base}" 2^>nul ^| sort /r\') do rd "%%d" 2>nul',
            f'if exist "{base}\\*" rmdir /s /q "{base}" 2>nul',
            'del /f /q "%~f0"',
        ]
    )
    fd, name = tempfile.mkstemp(prefix="jjxf_uninstall_", suffix=".bat")
    os.close(fd)
    script = Path(name)
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return script


def launch_uninstall_script(script: Path) -> None:
    """以脱离进程方式启动批处理，调用方随后应立即退出。"""
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
    subprocess.Popen(
        ["cmd", "/c", str(script)],
        creationflags=flags,
        close_fds=True,
        cwd=str(script.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def remove_now(targets: list[Path], root: Path | None = None) -> tuple[int, list[str]]:
    """尽力立即删除（不含程序本体时可用）。返回 (成功数, 失败说明)。"""
    base = (root or paths.ROOT).resolve()
    ok = 0
    failures: list[str] = []
    for target in targets:
        try:
            target.resolve().relative_to(base)
        except (ValueError, OSError):
            failures.append(f"跳过安装目录外的路径：{target}")
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=False)
            elif target.exists():
                target.unlink()
            ok += 1
        except OSError as exc:
            failures.append(f"{target.name}：{exc}")
    return ok, failures


# ---------------------------------------------------------------- 游戏本体


@dataclass
class BodyStat:
    """一条版本线的游戏本体占用（可能包含 pck + exe 两个文件）。"""

    key: str
    label: str
    paths: list[Path] = field(default_factory=list)
    bytes: int = 0
    missing: list[Path] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return any(p.is_file() for p in self.paths)

    def files(self) -> list[Path]:
        return [p for p in self.paths if p.is_file()]


def scan_game_body(cfg: dict | None = None) -> list[BodyStat]:
    """统计各版本线的游戏本体体积（只 stat，不读内容，3 GB 也是瞬间）。"""
    from . import config as cfg_mod

    cfg = cfg or cfg_mod.load()
    out: list[BodyStat] = []
    for bid, label in GAME_BODY_SCOPES.items():
        meta = (cfg.get("baselines") or {}).get(bid) or {}
        seen: list[Path] = []
        for field_name in ("path", "exe_path"):
            resolved = cfg_mod.resolve_path(str(meta.get(field_name) or ""))
            if resolved is not None and resolved not in seen:
                seen.append(resolved)
        stat = BodyStat(key=bid, label=label, paths=seen)
        for path in seen:
            if path.is_file():
                stat.bytes += _dir_size(path)
            else:
                stat.missing.append(path)
        out.append(stat)
    return out


def remove_game_body(
    cfg: dict | None, keys: list[str] | tuple[str, ...]
) -> tuple[int, int, list[str]]:
    """删除指定版本线的游戏本体文件。返回 ``(删除数, 释放字节, 失败说明)``。

    安全边界（每一条都不可放宽）：

    - **只删配置里登记过的绝对路径**，不扫目录、不递归、不认通配符；
    - 必须是**文件**且后缀是 ``.pck`` / ``.exe``，防止配置被改坏后误删别的东西；
    - 目标是目录、或指向目录的符号链接，一律跳过；
    - 单个文件失败只记一条说明，继续处理下一个，绝不整体回滚式乱删。
    """
    wanted = {str(k) for k in keys}
    removed = 0
    freed = 0
    failures: list[str] = []
    for stat in scan_game_body(cfg):
        if stat.key not in wanted:
            continue
        for path in stat.files():
            if path.is_dir():
                failures.append(f"跳过目录：{path}")
                continue
            if path.suffix.lower() not in _BODY_SUFFIXES:
                failures.append(f"跳过非游戏文件：{path.name}")
                continue
            try:
                size = path.stat().st_size
                path.unlink()
                removed += 1
                freed += size
            except OSError as exc:
                failures.append(f"{path.name}：{exc}")
    return removed, freed, failures


# ---------------------------------------------------------------- 按线级联


#: 本体 id → 引擎线。
#:
#: 这张表**不能写死**。原先是 ``{BUILTIN_BASELINE: BUILTIN_LINE}``，
#: 只有 1.45 → 4.6 一条。加了 4.7 之后，设置页卸载 4.7 本体时
#: ``LINE_OF_BODY.get("jjxf.v4.7")`` 拿到 ``None`` → 空线名 →
#: ``line_assets("")`` 查不到任何资产，**卸载会留下一堆该删没删的东西**。
#:
#: 下面用函数动态推导（读配置里每个基线的 ``engine`` 字段），
#: 只在配置读不出来时回落到内置那条。保留 ``LINE_OF_BODY`` 这个名字
#: 是为了不破坏调用方与测试（它们把它当字典用）。
def _build_line_of_body() -> dict[str, str]:
    out: dict[str, str] = {cfg_mod.BUILTIN_BASELINE: cfg_mod.BUILTIN_LINE}
    try:
        cfg = cfg_mod.load()
    except Exception:  # noqa: BLE001 - 配置读不出来就用内置那条
        return out
    for bid, meta in (cfg.get("baselines") or {}).items():
        line = str((meta or {}).get("engine") or "").strip()
        if bid and line:
            out[str(bid)] = line
    # 4.7 的 engine 字段历史上有过缺失，兜一层
    if "jjxf.v4.7" in (cfg.get("baselines") or {}):
        out.setdefault("jjxf.v4.7", "4.7")
    return out


LINE_OF_BODY: dict[str, str] = _build_line_of_body()


def line_of_body(cfg: dict | None = None) -> dict[str, str]:
    """``本体 id → 引擎线``。

    传 ``cfg`` 时**以它为准重新算**，不读模块级快照 ——
    配置在运行期会变（导入本体、改 engine 字段），快照会过时。
    界面应当走这个函数，而不是直接读 ``LINE_OF_BODY``。
    """
    if cfg is None:
        return dict(LINE_OF_BODY)
    out: dict[str, str] = {cfg_mod.BUILTIN_BASELINE: cfg_mod.BUILTIN_LINE}
    for bid, meta in (cfg.get("baselines") or {}).items():
        line = str((meta or {}).get("engine") or "").strip()
        if bid and line:
            out[str(bid)] = line
    if "jjxf.v4.7" in (cfg.get("baselines") or {}):
        out.setdefault("jjxf.v4.7", "4.7")
    return out


@dataclass
class LineAsset:
    """一条线上的一份扩展/剧情包（只记展示需要的信息）。"""

    id: str
    name: str
    bytes: int = 0

    def label(self) -> str:
        return f"{self.name}（{human_size(self.bytes)}）" if self.bytes else self.name


@dataclass
class LineAssets:
    """卸载某条线的游戏本体时，会被一并清掉的东西。"""

    line: str
    extensions: list[LineAsset] = field(default_factory=list)
    stories: list[LineAsset] = field(default_factory=list)
    stale_workdirs: list[Path] = field(default_factory=list)

    @property
    def extra_bytes(self) -> int:
        return sum(a.bytes for a in self.extensions) + sum(a.bytes for a in self.stories)

    @property
    def ext_names(self) -> list[str]:
        return [a.name for a in self.extensions]

    @property
    def story_names(self) -> list[str]:
        return [a.name for a in self.stories]

    def summary(self) -> str:
        parts: list[str] = []
        if self.extensions:
            parts.append(f"{len(self.extensions)} 个扩展（{human_size(sum(a.bytes for a in self.extensions))}）")
        if self.stories:
            parts.append(f"{len(self.stories)} 个剧情包（{human_size(sum(a.bytes for a in self.stories))}）")
        if self.stale_workdirs:
            parts.append(f"{len(self.stale_workdirs)} 个工作目录")
        return "、".join(parts)


def _file_bytes(*paths: Path) -> int:
    total = 0
    for path in paths:
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _stat_ext_overlay(item: dict, line: str) -> int:
    """某扩展在 ``line`` 线上的落盘体积（覆盖层 + 清单；没有就 0）。

    注册表里 ``lines.<line>.overlay`` 与 ``packs/<id>/overlay-<tag>.pck`` 指的是
    **同一个文件**，所以按 resolve 后的路径去重，否则体积会算成两倍。
    """
    tag = paths.line_tag(line)
    ext_id = str(item.get("id") or "")
    seen: set[Path] = set()
    total = 0

    def add(path: Path | None) -> None:
        nonlocal total
        if path is None:
            return
        try:
            key = path.resolve()
        except OSError:
            return
        if key in seen or not key.is_file():
            return
        seen.add(key)
        try:
            total += key.stat().st_size
        except OSError:
            pass

    if ext_id:
        folder = paths.PACKS / ext_id
        add(folder / f"overlay-{tag}.pck")
        add(folder / f"manifest-{tag}.v2.json")
    lines = item.get("lines")
    if isinstance(lines, dict):
        entry = lines.get(line) or {}
        for key in ("overlay", "manifest"):
            add(_stored(str(entry.get(key) or "")))
    elif str(item.get("engine_line") or "") == line:
        add(_stored(str(item.get("overlay") or "")))
    return total


def _stored(value: str) -> Path | None:
    if not value:
        return None
    from . import config as cfg_mod

    return cfg_mod.resolve_path(value)


def line_assets(cfg: dict | None = None, line: str = cfg_mod.BUILTIN_LINE) -> LineAssets:
    """统计某条线上「会随本体一起消失」的扩展与剧情包。**只 stat，不删**。

    为什么要在确认框里先列出来：本体是几个 GB 的删除，用户按下确认前必须
    知道「我还会失去哪些 mod」，否则删完才发现扩展全没了。

    1.4 起剧情功能已移除，``LineAssets.stories`` 恒为空表——字段保留是为了
    不破坏确认框的既有排版与调用方签名。
    """
    from . import config as cfg_mod
    from .ext import service as ext_service_mod
    from .fsutil import read_json

    cfg = cfg or cfg_mod.load()
    assets = LineAssets(line=line)

    ext_reg = read_json(ext_service_mod.REGISTRY, default={"items": []}) or {}
    for item in ext_reg.get("items") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        lines = item.get("lines")
        on_line = line in lines if isinstance(lines, dict) else str(item.get("engine_line") or "") == line
        if not on_line:
            continue
        assets.extensions.append(
            LineAsset(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                bytes=_stat_ext_overlay(item, line),
            )
        )

    assets.stale_workdirs = line_workdirs(line, include_release=False)
    return assets


def line_workdirs(line: str, *, include_release: bool = True) -> list[Path]:
    """``builds/`` 下属于这条线的工作目录。

    判定依据按可靠性排序：目录名 ``_install_<id>_<tag>`` / ``release-<tag>-*``
    / 后缀 ``_<tag>`` → ``launcher.json`` / ``packs/packset.json`` 里的 ``line``。
    只认 ``builds`` 的直接子目录，绝不递归猜；``_pyi`` 这类工具目录不碰。

    ``include_release=False`` 时只返回**中间产物**（安装工作区），不返回
    ``release-`` 开头的发布包——那是用户自己打出来的成品，卸载游戏本体时
    不该顺手删掉。
    """
    tag = paths.line_tag(line)
    out: list[Path] = []
    if not paths.BUILDS.is_dir():
        return out
    for child in sorted(paths.BUILDS.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith(f"release-{tag}-"):
            if include_release:
                out.append(child)
            continue
        if name.startswith(f"_install_") and name.endswith(f"_{tag}"):
            out.append(child)
            continue
        if name.endswith(f"_{tag}"):
            out.append(child)
            continue
        if name.startswith("_"):
            # ``_pyi`` 之类是工具目录，不是工作期缓存
            continue
        for probe in (child / "launcher.json", child / "packs" / "packset.json"):
            if not probe.is_file():
                continue
            try:
                meta = json.loads(probe.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            if str(meta.get("line") or "") == line:
                out.append(child)
                break
    return out


def purge_line_workdirs(
    line: str, *, include_release: bool = False
) -> tuple[int, int, list[str]]:
    """清理这条线的工作期缓存。返回 ``(删除目录数, 释放字节, 失败说明)``。

    清三样东西：

    1. ``cache/instrumented/`` 里属于这条线的插桩缓存（派生物，随时可重建）；
    2. ``builds/_install_<id>_<tag>`` 这类安装中间目录；
    3. ``builds/`` 下声明了这条线的工作区（``include_release=True`` 才含发布包）。
    """
    freed = 0
    count = 0
    failures: list[str] = []

    try:
        from .ext.enable import _purge_instrumented_cache

        cache_dir = paths.CACHE / "instrumented"
        before = _dir_size(cache_dir)
        count += _purge_instrumented_cache(line)
        freed += max(0, before - _dir_size(cache_dir))
    except Exception as exc:  # noqa: BLE001 - 缓存清不掉不该让卸载失败
        failures.append(f"插桩缓存：{exc}")

    targets = line_workdirs(line, include_release=include_release)
    seen: set[Path] = set()
    for path in targets:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            size = _dir_size(path)
            shutil.rmtree(path, ignore_errors=False)
            count += 1
            freed += size
        except OSError as exc:
            failures.append(f"{path.name}：{exc}")
    return count, freed, failures

