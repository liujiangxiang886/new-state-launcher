"""临时文件位置：**一律落在项目盘，不碰 C 盘**。

用户硬约定（2026-11-13）：依赖、缓存、临时目录、构建产物都不放 C 盘。
只设 ``--basetemp`` 不够——``tempfile`` 走 ``TMP``/``TEMP``，默认就是
``C:\\Users\\<用户>\\AppData\\Local\\Temp``。这里守住三件事：

1. ``paths.temp_dir()`` 必须落在 ``cache/tmp`` 下，且真的建得出来；
2. pytest 会话里 ``tempfile.gettempdir()`` 已被 conftest 钉到项目盘；
3. 主程序里所有 ``TemporaryDirectory()`` 都带 ``dir=``（源码级扫描，
   防止以后新增调用又忘了参数）。
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import paths  # noqa: E402


def test_temp_dir_lives_under_cache() -> None:
    d = paths.temp_dir()
    assert d.is_dir(), d
    assert d == paths.CACHE / "tmp", d
    assert d.drive.upper() == ROOT.drive.upper(), f"临时目录跑到别的盘了：{d}"


def test_temp_dir_is_writable_and_unique_subdirs_work() -> None:
    with tempfile.TemporaryDirectory(dir=str(paths.temp_dir())) as tmp:
        p = Path(tmp) / "x.txt"
        p.write_text("ok", encoding="utf-8")
        assert p.read_text(encoding="utf-8") == "ok"
        assert Path(tmp).drive.upper() == ROOT.drive.upper()


def test_pytest_tempdir_is_pinned_to_project_drive() -> None:
    """conftest 把 ``TMP``/``TEMP`` 与 ``tempfile.tempdir`` 都改到项目内。

    没有这一条，某个测试随手 ``mkdtemp()`` 就会写回 C 盘。
    """
    resolved = Path(tempfile.gettempdir())
    assert resolved.drive.upper() == ROOT.drive.upper(), resolved


def test_no_bare_temporary_directory_in_app() -> None:
    """源码级兜底：``app/`` 里不许出现不带 ``dir=`` 的临时目录调用。

    用 ``ast`` 而不是正则：文档字符串里提到 ``TemporaryDirectory()`` 很常见
    （本文件的被测函数就在注释里写了它），正则会把说明文字当成代码误报。
    """
    import ast

    offenders: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in ("TemporaryDirectory", "mkdtemp"):
                continue
            if not any(kw.arg == "dir" for kw in node.keywords):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, "这些临时目录没指定 dir=，会落到 C 盘：" + "、".join(offenders)


def test_runtime_dirs_are_isolated_from_real_ones() -> None:
    """测试默认跑在临时 ``packs/`` / ``builds/`` / ``cache/`` 上，不碰真实数据。

    守护 ``conftest._isolate_runtime_dirs``（autouse）。删掉它不会立刻报错，
    而是**静默写坏真实数据**：2026-11-13 实测跑一轮全量后，
    真实 ``config/extensions.json``（已装扩展清单）从 28 条变成 2 条测试数据，
    真实 ``packs/`` 里多出 30+ 个 ``mod.a`` / ``test.gate.*`` 假包。

    隔离生效时这些路径都指向 pytest 的 basetemp（``_build_tools/tmp``）下。
    """
    from app.core import paths as paths_mod
    from app.core.ext import service as service_mod
    from app.core.ext import verify_mark

    real = {
        "PACKS": paths_mod.ROOT / "packs",
        "BUILDS": paths_mod.ROOT / "builds",
        "CACHE": paths_mod.ROOT / "cache",
    }
    for name, real_path in real.items():
        current = Path(getattr(paths_mod, name))
        assert current != real_path, f"paths.{name} 还是真实目录：{current}"
        assert str(current).startswith(str(paths_mod.ROOT / "_build_tools" / "tmp")), (
            f"paths.{name} 没落在测试临时目录：{current}"
        )

    assert Path(service_mod.REGISTRY) != paths_mod.CONFIG_DIR / "extensions.json", (
        "注册表还是真实的 config/extensions.json，跑测试会覆盖已装扩展清单"
    )
    assert Path(verify_mark._local_path()) != paths_mod.CONFIG_DIR / "verified.json", (
        "本地验证记录还是真实的 config/verified.json"
    )


def test_no_test_hardcodes_real_builds_path() -> None:
    """测试里不许写 ``WORK = ROOT / "builds" / ...`` 这种常量。

    为什么值一条回归（2026-11-13 实测）：隔离 fixture 改的是 ``paths.BUILDS``，
    模块级常量在 import 时就已经算好，补丁对它无效。三个真引擎用例
    （``test_ctx_extended`` / ``test_hook_overwrite`` / ``test_mod_host_isolation``）
    都这么写过，每个 case 拷一份 164 MB 的 ``jjxf46.exe`` —— **一轮全量往真实
    ``builds/`` 里写 1.5 GB**，跑三遍就 4.5 GB。

    正确写法是 ``def _work_dir(): return paths.BUILDS / "xxx"``，调用时才取。
    """
    import ast

    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        for node in tree.body:  # 只看模块顶层赋值
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.BinOp):
                continue
            # 形如 ROOT / "builds" / ... —— 左操作数递归取最左的名字
            left = node.value
            while isinstance(left, ast.BinOp):
                left = left.left
            if not (isinstance(left, ast.Name) and left.id in ("ROOT", "BUILDS")):
                continue
            consts = [n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant)]
            if any(isinstance(c, str) and c.strip("/\\") == "builds" for c in consts):
                targets = ", ".join(getattr(t, "id", "?") for t in node.targets)
                offenders.append(f"{path.name}:{node.lineno} {targets}")
    assert not offenders, (
        "这些模块级常量绕过了 conftest 的目录隔离，会让测试往真实 builds/ 写几百 MB："
        + "、".join(offenders)
    )


def test_host_delete_shim_is_neutralized() -> None:
    """守护 ``conftest._restore_stock_delete()``：删除函数必须是标准库原版。

    为什么值得单独钉一条（2026-11-13 的真实事故）：
    宿主 shim 给 ``shutil.rmtree`` 套了一层「本轮累计删除满 50 次就
    ``raise SystemExit(1)``」的壳。而 pytest 的 ``tmp_path`` 在每个用例结束时
    都要删一次临时目录 —— 一轮 740+ 次，删到第 50 个就撞钩子，
    报成 ``ERROR at teardown ... SystemExit: 1``，随后整个会话被打崩，
    连带上百个 ERROR + ``INTERNALERROR``。**代码一直是全绿的**，
    红的是被壳打断的测试进程。

    有人把这个还原逻辑删掉，那条雪崩就会原样回来，而且很难再联想到原因。
    """
    import os
    import shutil
    from pathlib import Path as _Path

    suspects = {
        "shutil.rmtree": shutil.rmtree,
        "os.remove": os.remove,
        "Path.unlink": _Path.unlink,
        "Path.rmdir": _Path.rmdir,
    }
    still_wrapped = [name for name, func in suspects.items()
                     if getattr(func, "__module__", "") == "sitecustomize"]
    assert not still_wrapped, (
        f"这些删除函数还是宿主 shim 的包装版本：{still_wrapped}；"
        "conftest._restore_stock_delete() 没生效，跑全量会在第 50 次删除处雪崩"
    )

    # 从 ``shutil`` 直接 import 过的引用（pytest 的 tmpdir 就是这么干的）
    # 不会被 ``shutil.rmtree = ...`` 覆盖，必须单独核对。
    from _pytest import tmpdir as pytest_tmpdir

    assert getattr(pytest_tmpdir.rmtree, "__module__", "") != "sitecustomize", (
        "pytest.tmpdir 仍绑着 shim 的 rmtree，tmp_path 收尾会抛 SystemExit(1)"
    )
