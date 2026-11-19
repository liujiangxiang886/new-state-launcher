"""项目内路径解析。

约定：所有目录都相对项目根（``D:\\jjxf-launcher``），可在 ``config/launcher.json``
里覆盖 ``root``。任何时候都不要把缓存/日志写到系统目录。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# app/core/paths.py -> app/core -> app -> <root>
_DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """返回项目根目录，可用环境变量 ``JJXF_ROOT`` 覆盖（便于测试）。

    冻结为 exe 后，``__file__`` 指向临时解压目录（_MEIPASS），不再对应项目根。
    此时改用可执行文件所在目录作为根，因此 ``启动器.exe`` 必须与
    ``config/``、``engines/``、``packs/`` 等目录放在同一级（即项目根目录）。
    """
    env = os.environ.get("JJXF_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _DEFAULT_ROOT


ROOT = project_root()

# ---------------------------------------------------------------- 一级目录

APP = ROOT / "app"
RUNTIME = ROOT / "runtime"
ENGINES = ROOT / "engines"
BASELINE = ROOT / "baseline"
PACKS = ROOT / "packs"
BUILDS = ROOT / "builds"
PROFILES = ROOT / "profiles"
CACHE = ROOT / "cache"
CONFIG_DIR = ROOT / "config"
SCHEMAS = ROOT / "schemas"
SOURCES = ROOT / "sources"
TOOLS = ROOT / "tools"
TESTS = ROOT / "tests"
LOGS = ROOT / "logs"

# ---------------------------------------------------------------- 运行时底座

RUNTIME_BASE46 = RUNTIME / "base46"

#: 内置引擎线。**唯一真源**在 :data:`app.core.config.BUILTIN_LINE`，
#: 这里只做同值镜像——paths 不依赖 config（config 反过来依赖 paths），
#: 循环导入会炸，所以只能各存一份。改动时必须两边一起改。
BUILTIN_LINE = "4.6"

#: 支持的引擎线。4.6 = 原版 1.45（默认），4.7 = 剧情版（发布者版恢复的可选线）。
LINES = ("4.6", "4.7")

#: 引擎线 → 目录/文件名标签（``4.7`` → ``47``、``custom:abc`` → ``custom_abc``）。
#:
#: **所有按线分文件的命名都必须走这里**（``overlay-<tag>.pck``、
#: ``manifest-<tag>.v2.json``、插桩缓存、安装中间目录）。原因：自定义线 id
#: 形如 ``custom:<uuid>``，冒号在 Windows 文件名里非法；直接用
#: ``line.replace(".", "")`` 会得到一个建不出来的路径，表现为「装上了但
#: 找不到覆盖层」。内置线（4.6 / 4.7）的标签保持原样，不影响存量数据。
def line_tag(line: str) -> str:
    text = str(line).strip()
    if text.startswith("custom:"):
        text = "custom_" + text.split(":", 1)[1]
    tag = re.sub(r"[^0-9A-Za-z_-]+", "_", text.replace(".", "")).strip("_")
    return tag or "line"


def temp_dir() -> Path:
    """临时工作目录：**落在本项目内**（``cache/tmp``），不用系统 Temp。

    硬约定（用户 2026-11-13 明确）：不往 C 盘放东西——依赖、缓存、临时物
    都算。Windows 的 ``TEMP`` 默认是 ``C:\\Users\\<用户>\\AppData\\Local\\Temp``，
    所以任何 ``TemporaryDirectory()`` 不带 ``dir=`` 都会违反这条。

    为什么放 ``cache/`` 而不是 ``builds/``：``cache/`` 本来就是「可随时删」的
    运行期目录，``housekeeping`` 会连带清理；``builds/`` 是安装/启动中间物，
    语义不同。

    建不出来（只读安装目录）时退回系统 Temp —— 宁可临时物落 C 盘，也不该让
    功能直接失败。
    """
    import tempfile

    target = CACHE / "tmp"
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        return Path(tempfile.gettempdir())



def engine_binary(line: str) -> Path:
    """引擎二进制路径（``engines/`` 下的原始文件；host 目录里另有一份副本）。

    4.7 的文件名带补丁号（``godot-4.7.2.exe``），所以先精确匹配，
    匹配不到再按 ``godot-<line>*.exe`` 找。
    """
    if line not in LINES:
        raise ValueError(f"未知引擎线：{line!r}（支持 {', '.join(LINES)}）")
    exact = ENGINES / f"godot-{line}.exe"
    if exact.is_file():
        return exact
    candidates = sorted(ENGINES.glob(f"godot-{line}*.exe"))
    return candidates[0] if candidates else exact


def runtime_for(line: str) -> Path:
    if line not in LINES:
        raise ValueError(f"未知引擎线：{line!r}（支持 {', '.join(LINES)}）")
    return RUNTIME / f"base{line_tag(line)}"


# ---------------------------------------------------------------- 派生路径


def pack_dir(packset_id: str) -> Path:
    return PACKS / packset_id


def profile_dir(name: str) -> Path:
    return PROFILES / name


def build_dir(run_id: str) -> Path:
    return BUILDS / run_id


def log_file(name: str) -> Path:
    return LOGS / name


def ensure_dirs() -> None:
    """创建运行所需目录（幂等）。"""
    for d in (
        ENGINES,
        BASELINE,
        PACKS,
        BUILDS,
        PROFILES,
        CACHE,
        CONFIG_DIR,
        SCHEMAS,
        SOURCES,
        LOGS,
        RUNTIME,
    ):
        d.mkdir(parents=True, exist_ok=True)


def rel_to_root(path: Path) -> str:
    """转成相对项目根的 POSIX 风格路径，日志/清单里更短。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)
