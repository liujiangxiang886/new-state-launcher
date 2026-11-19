"""包内相对路径的读写契约。

为什么单独测这一组：启动器要能整个目录搬走（换盘符/换机器），配置和清单里
就不能存「源码目录的绝对路径」。约定是**读时按项目根展开、写时收回相对**：

- ``config.resolve_path``  → 读，相对路径按 ``paths.ROOT`` 展开
- ``config.stored_path``   → 写，落在包内的转相对，包外保持绝对

只要这两头有一头不配对，就会出现「装好的扩展在搬家后静默不挂载」——
而这恰好是本项目最容易踩、又最难看出来的坑。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import paths  # noqa: E402


def test_stored_path_returns_relative_for_in_package() -> None:
    """包内文件写成相对路径，并且用 POSIX 分隔符。"""
    target = paths.ROOT / "runtime" / "host46" / "jjxf46.exe"
    stored = cfg_mod.stored_path(target)
    assert stored == "runtime/host46/jjxf46.exe"
    assert "\\" not in stored


def test_stored_path_keeps_absolute_for_outside() -> None:
    """包外的路径（用户机器上的游戏本体）保持绝对。"""
    outside = Path("C:/Windows/System32/notepad.exe")
    assert cfg_mod.stored_path(outside) == outside.as_posix()


def test_resolve_path_expands_against_project_root() -> None:
    """相对路径按项目根展开，不受当前工作目录影响。"""
    resolved = cfg_mod.resolve_path("runtime/host46/jjxf46.exe")
    assert resolved == (paths.ROOT / "runtime" / "host46" / "jjxf46.exe").resolve()


def test_roundtrip_resolve_of_stored_is_original() -> None:
    """读回来的必须还是原来那个绝对路径。"""
    original = paths.ROOT / "packs" / "mch.sell_all" / "overlay-46.pck"
    stored = cfg_mod.stored_path(original)
    assert cfg_mod.resolve_path(stored) == original.resolve()


def test_empty_values_are_safe() -> None:
    assert cfg_mod.stored_path(None) == ""
    assert cfg_mod.stored_path("") == ""
    assert cfg_mod.resolve_path("") is None
    assert cfg_mod.resolve_path(None) is None


def test_absolute_input_survives_resolve() -> None:
    """绝对路径进 resolve_path 原样返回（不改既有行为）。"""
    outside = Path("C:/Windows/System32/notepad.exe")
    assert cfg_mod.resolve_path(outside) == outside


def test_extension_manifest_relative_overlay_is_readable() -> None:
    """清单里存相对 overlay 时，读取方必须能按项目根找到它。

    ``enable`` 会把清单里的所有已启用项交给 ``resolve_path`` 再用；
    这里直接验证契约本身，避免依赖真实安装结果。
    """
    overlay = paths.ROOT / "packs" / "mch.sell_all" / "overlay-47.pck"
    item = {"id": "mch.sell_all", "overlay": cfg_mod.stored_path(overlay)}
    assert not Path(item["overlay"]).is_absolute()
    assert cfg_mod.resolve_path(item["overlay"]) == overlay.resolve()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
