"""pytest 全局夹具与会话级补丁。

这里做四件事，每一件都踩过坑，改之前先看注释：

1. **临时文件不碰 C 盘**（用户硬约定 2026-11-13：依赖/缓存/临时物一律 D 盘）。
   只设 ``--basetemp`` 不够 —— ``tempfile`` 走 ``TMP``/``TEMP``，默认是
   ``C:\\Users\\<用户>\\AppData\\Local\\Temp``。所以 import 阶段就把三个环境变量
   和 ``tempfile.tempdir`` 一起钉到项目内的 ``_build_tools/tmp``。

2. **每轮用独立 basetemp**。pytest 的 ``getbasetemp()`` 会无差别
   ``rm_rf`` 整个 basetemp 目录。2026-11-13 D 盘写满时，上一轮留下一个删不掉的
   目录（WinError 5），结果**当轮 747 个用例全部在 setup 阶段报 error**，
   一条真失败都看不到，误判成产品挂了。改成 ``run-<时间>-<pid>`` 子目录后，
   历史残留只会脏它自己那一轮，不会连坐。

3. **把删除函数还原成标准库原版**。宿主（WorkBuddy CLI）的
   ``sitecustomize`` 给 ``shutil.rmtree`` / ``os.remove`` / ``Path.unlink`` 套了壳：
   「本轮累计删除满 50 次就 ``raise SystemExit(1)`` 请求确认」。而
   ``tmp_path`` 在**每个通过的用例结尾**都要删一次临时目录 —— 一轮 740+ 次，
   删到第 50 个就开始 ``ERROR at teardown ... SystemExit: 1``，随后会话被打崩，
   连带几百个 ERROR 和 ``INTERNALERROR ValueError: ... is not in the subpath of ...``。
   **产品代码一直是全绿的**，红的是被壳打断的测试进程。

4. **Qt 收尾**。见文件末尾 ``pytest_unconfigure``。

报告头会打印 ``temp:`` / ``safe-delete:`` 两行，跑完先看这两行确认环境是对的。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Qt 无界面模式：必须在**任何 PySide6 导入之前**设置，否则 Qt 已经挑了平台插件，
# 再改就无效。测试只需要构造控件、读文本，不需要真开窗口；
# 没有这个设置，在无桌面会话的环境（CI、远程 shell）里构造 QApplication 会直接失败。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 仓库根目录（``tests/`` 的上一层）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 项目内的临时根目录。所有测试临时物都必须在它下面。
_P_TMP = PROJECT_ROOT / "_build_tools" / "tmp"


# ---------------------------------------------------------------------------
# 1. 临时目录钉在项目盘
# ---------------------------------------------------------------------------

def _pin_temp_to_project() -> Path:
    """把 ``TMP`` / ``TEMP`` / ``TMPDIR`` 与 ``tempfile.tempdir`` 钉到项目内。

    只改 ``--basetemp`` 是不够的：任何一处直接调 ``tempfile.mkdtemp()``
    或 ``TemporaryDirectory()`` 都会回到系统 Temp（C 盘）。
    """
    try:
        _P_TMP.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(tempfile.gettempdir())      # 建不出来就只能认了，别让测试起不来

    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(_P_TMP)
    tempfile.tempdir = str(_P_TMP)
    return _P_TMP


_PINNED_TMP = _pin_temp_to_project()

#: 每轮独立的 basetemp 根，形如 ``_build_tools/tmp/pytest/run-20261113-150000-1234``。
_BASETEMP_ROOT = _PINNED_TMP / "pytest"

#: 上一轮目录多久之后可以被回收（秒）。并发跑两个 pytest 时互相别踩。
_SWEEP_AGE_SECONDS = 2 * 60 * 60


# ---------------------------------------------------------------------------
# 2. 还原标准库删除语义
# ---------------------------------------------------------------------------

def _stock_path_unlink(self, missing_ok: bool = False) -> None:
    """``pathlib.Path.unlink`` 的标准行为（宿主版本被套了壳）。"""
    try:
        os.unlink(self)
    except FileNotFoundError:
        if not missing_ok:
            raise


def _stock_path_rmdir(self) -> None:
    """``pathlib.Path.rmdir`` 的标准行为。"""
    os.rmdir(self)


def _patch_stale_rmtree_refs() -> list[str]:
    """修掉「已经 ``from shutil import rmtree`` 过」的模块。

    这类模块持有的是壳函数的**直接引用**，光换 ``shutil.rmtree`` 管不到它。
    pytest 自己的 ``_pytest.tmpdir`` 就是这种写法，而 ``tmp_path`` 的收尾
    正好走它 —— 不修这里，前面做的还原等于白做。
    """
    patched: list[str] = []
    for name, module in list(sys.modules.items()):
        if module is None or name.startswith("shutil"):
            continue
        func = getattr(module, "rmtree", None)
        if func is None or getattr(func, "__module__", "") != "sitecustomize":
            continue
        try:
            setattr(module, "rmtree", shutil.rmtree)
            patched.append(name)
        except Exception:
            pass                            # 只读模块，跳过
    return patched


def _restore_stock_delete() -> str:
    """把删除函数换回标准库原版，返回一句可打印的说明。

    为什么不能「记下原函数再还原」：壳把原始实现存成了**函数内的局部变量**
    （``_orig_shutil_rmtree`` 之类），外部拿不到。所以换个路子：
    原生实现从 ``nt`` / ``posix`` 模块取（``os.remove`` 是它的别名，壳只改了
    ``os`` 上的名字），``shutil.rmtree`` 则在修好 ``os`` 之后整模块
    ``reload`` 一次，让它按标准源码重新定义。
    """
    native = sys.modules.get("nt") if os.name == "nt" else sys.modules.get("posix")
    if native is None:                      # 理论上不可能，保险
        return "跳过（拿不到原生 os 模块）"

    os.remove = native.remove
    os.unlink = native.unlink
    os.rmdir = native.rmdir

    import pathlib

    pathlib.Path.unlink = _stock_path_unlink
    pathlib.Path.rmdir = _stock_path_rmdir

    # 先修好 os，再 reload —— 顺序反了会得到「干净的壳 + 被套壳的 os」。
    importlib.reload(shutil)

    patched = _patch_stale_rmtree_refs()
    detail = f"shutil.rmtree / os.remove / Path.unlink 已还原"
    if patched:
        detail += f"；另外修正了 {len(patched)} 处早期 import（{', '.join(patched[:3])}"
        detail += " 等）" if len(patched) > 3 else "）"
    return detail


_DELETE_RESTORE_NOTE = _restore_stock_delete()


# ---------------------------------------------------------------------------
# 3. 每轮独立 basetemp
# ---------------------------------------------------------------------------

def _new_run_basetemp() -> Path | None:
    """本轮专用的 basetemp 目录（``run-<时间>-<pid>``）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = _BASETEMP_ROOT / f"run-{stamp}-{os.getpid()}"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def _sweep_old_runs(keep: Path) -> int:
    """回收上一轮遗留的 basetemp。

    只删**足够旧**的：并发起两个 pytest 时，另一个会话的目录不能动。
    单轮跑完到下一轮通常间隔几分钟，所以两小时的门槛实际不会挡住正常回收
    —— 真正起作用的是「下一次跑之前清干净」，不是即时性。
    """
    cutoff = time.time() - _SWEEP_AGE_SECONDS
    removed = 0
    try:
        children = list(keep.parent.iterdir())
    except OSError:
        return 0
    for child in children:
        if child == keep or not child.is_dir():
            continue
        try:
            if child.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)   # 删不掉也不该影响本轮
        removed += 1
    return removed


def pytest_configure(config: pytest.Config) -> None:
    """会话初始化：补一次引用修正 + 给本轮指定独立 basetemp。

    调用方显式给了 ``--basetemp`` 就尊重它（调试时常用）。
    """
    _patch_stale_rmtree_refs()              # 收集期可能又 import 了新模块

    if config.option.basetemp:
        return
    run_dir = _new_run_basetemp()
    if run_dir is None:
        return                              # 建不出来就退回默认，别让测试起不来
    config.option.basetemp = str(run_dir)
    _sweep_old_runs(run_dir)


def pytest_report_header(config: pytest.Config) -> list[str]:
    """报告头两行环境自证：临时目录在哪、删除语义是不是原版。

    这两条都是「静默失效」型故障：不对的话测试会以难以理解的方式崩，
    所以让它们每轮都自报一次，别等出事再查。
    """
    lines = [f"temp: {tempfile.gettempdir()}"]
    lines.append(f"safe-delete: {_DELETE_RESTORE_NOTE}")
    base = getattr(config.option, "basetemp", None)
    if base:
        lines.append(f"basetemp: {base}")
    return lines


# ---------------------------------------------------------------------------
# 4. 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp(tmp_path: Path) -> Path:
    """历史夹具名：就是 ``tmp_path``。

    早期测试全用 ``tmp / "x"`` 这种写法，改名成本高于收益，保留别名。
    """
    return tmp_path


@pytest.fixture()
def work(tmp_path: Path) -> Path:
    """可写的「工作台」目录，一定已创建（``tmp_path/work``）。"""
    path = tmp_path / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def _isolate_runtime_dirs(request, tmp_path, monkeypatch):
    """每个用例默认跑在临时的 ``builds/`` / ``packs/`` / ``cache/`` 上。

    为什么做成 autouse（2026-11-13 的真实事故，不是理论风险）：
    ``service.install()``、``uninstall.purge_line_workdirs()``、
    ``housekeeping.sweep()`` 都直接读写运行期目录。只要有一个测试忘了重定向，
    跑一次全量就会：

      · 把 ``mod.a`` / ``test.member`` / ``test.gate.*`` 这类测试包写进真实
        ``packs/``（本仓库实测残留 30+ 个目录）；
      · 把真实 ``config/extensions.json`` —— 也就是**已装扩展清单** ——
        覆盖成测试数据。实测跑完一轮，28 个已装扩展只剩 2 条。

    所以默认隔离，个别确实要读真实数据的用例用
    ``@pytest.mark.real_dirs`` 显式退出。
    """
    if request.node.get_closest_marker("real_dirs"):
        return

    from app.core import paths as paths_mod
    from app.core.ext import service as service_mod
    from app.core.ext import verify_mark

    builds = tmp_path / "builds"
    packs = tmp_path / "packs"
    cache = tmp_path / "cache"
    for folder in (builds, packs, cache):
        folder.mkdir()
    monkeypatch.setattr(paths_mod, "BUILDS", builds, raising=False)
    monkeypatch.setattr(paths_mod, "PACKS", packs, raising=False)
    monkeypatch.setattr(paths_mod, "CACHE", cache, raising=False)
    monkeypatch.setattr(
        service_mod, "REGISTRY", tmp_path / "extensions.json", raising=False
    )
    # 「验证通过」的本地记录也落在 config/verified.json；不隔离的话，
    # 测试造出来的 good.ext / old.ext 会把真实信任记录顶掉。
    monkeypatch.setattr(verify_mark, "_local_path", lambda: tmp_path / "verified.json")


@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    """显式版隔离：返回 ``builds`` / ``packs`` / ``cache`` 三个临时目录。

    默认已经由 ``_isolate_runtime_dirs`` 自动隔离；这个夹具给需要**拿到路径**
    的用例用（例如断言某个文件确实写到了 ``packs/<id>/``）。
    """
    from types import SimpleNamespace

    from app.core import paths as paths_mod

    builds = tmp_path / "builds"
    packs = tmp_path / "packs"
    cache = tmp_path / "cache"
    for d in (builds, packs, cache):
        d.mkdir(exist_ok=True)
    monkeypatch.setattr(paths_mod, "BUILDS", builds, raising=False)
    monkeypatch.setattr(paths_mod, "PACKS", packs, raising=False)
    monkeypatch.setattr(paths_mod, "CACHE", cache, raising=False)
    return SimpleNamespace(builds=builds, packs=packs, cache=cache)


@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    """显式版隔离：把扩展注册表指到临时文件并返回它。

    注册表是 ``service.REGISTRY = paths.CONFIG_DIR / "extensions.json"``
    ——**模块级常量**，不跟着 ``paths`` 的补丁走，必须单独钉住。
    """
    from app.core.ext import service as service_mod

    target = tmp_path / "extensions.json"
    monkeypatch.setattr(service_mod, "REGISTRY", target, raising=False)
    return target


@pytest.fixture(scope="session")
def qapp():
    """共享的 QApplication（Qt 不允许一个进程建两个）。

    ``scope="session"``：整个会话只建一次。若按函数建，第二次构造会拿到
    已存在的实例，``QApplication([])`` 在部分 Qt 版本上会崩。

    用到它的测试**必须自己销毁创建出来的控件**（``close()`` →
    ``setParent(None)`` → ``deleteLater()`` → 派发 ``DeferredDelete``）。
    没有事件循环时 ``deleteLater()`` 永远不会执行，窗口会活到解释器退出，
    shiboken 再去析构 C++ 对象就是 access violation —— 报告写着 745 passed，
    进程退出码却是 ``0xC0000005``。
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# 5. 会话收尾
# ---------------------------------------------------------------------------

#: 真实退出码，``pytest_sessionfinish`` 里记下来给收尾用。
_SESSION_EXITSTATUS = 0


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _SESSION_EXITSTATUS
    _SESSION_EXITSTATUS = int(exitstatus)


def pytest_unconfigure(config: pytest.Config) -> None:
    """最后一个钩子：关掉 Qt，然后再跳过解释器拆解。

    分两步，缺一不可：

    (1) ``QApplication.shutdown()`` —— PySide6 官方收尾入口，在 Python 运行时
        还完整的时候把 QApplication 与 shiboken 的簿记清干净。
    (2) ``os._exit`` —— 只要这个进程碰过 Qt，就让它在**退出码已经确定**之后
        直接结束。原因是 ``ExitProcess`` 阶段 Qt 的 DLL 分离仍可能抛一次
        access violation（实测 ``0xC0000409`` / ``0xC0000005``），
        把 ``746 passed`` 变成「退出码 3 / 127 / 139」的假失败；
        而且崩栈会打一整篇到 stderr，看起来像测试挂了。

    这里不跑 atexit：测试进程本来就没有需要 atexit 收尾的资源。

    调试时想走正常退出路径（例如查还有没有控件没销毁），设
    ``JJXF_TEST_KEEP_PROCESS=1``。
    """
    if os.environ.get("JJXF_TEST_KEEP_PROCESS") == "1":
        return
    if "PySide6.QtWidgets" not in sys.modules:
        return                              # 没跑过界面测试，保持库语义

    try:
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is not None:
            QApplication.shutdown()
    except Exception:
        pass                                # 收尾失败不该把整轮测试染红

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        import faulthandler

        faulthandler.disable()
    except Exception:
        pass
    os._exit(_SESSION_EXITSTATUS)
