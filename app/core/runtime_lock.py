"""游戏运行期锁：进程未退出时禁止改动扩展启停。

为什么要单独一个模块、而且**直接查进程表**
------------------------------------------

界面上的「运行中」状态是启动器自己维护的，会漏：CLI 启动的实例、启动器重启后
遗留的游戏进程、崩溃后没走完退出回调的进程，界面上都显示「空闲」，但游戏其实
还在跑。此时改扩展启用状态，正在运行的实例和下次启动的组装结果会不一致——
玩家看到的现象是「我明明关了它，怎么还在生效」。

所以判定依据必须是**系统进程表**，不是界面状态。

锁的粒度
--------

只锁「影响启动组装结果」的操作：启用 / 停用 / 卸载扩展、启停剧情包。
不锁安装（装完不自动生效，要下次启动才组装）、不锁存档、不锁导出。

为什么连卸载也锁：卸载会删覆盖层文件，而运行中的实例已经把 pck 挂载进内存，
此时删文件在 Windows 上会失败或留下半删状态。锁住比让它半途失败好。

为什么不用 tasklist
------------------

原来判定「游戏在跑」靠 ``tasklist /fo csv /nh`` 全表扫描。``tasklist`` 一次要
几百毫秒到十几秒，而退出瞬间的回调链里连着调了三遍（``is_game_running`` +
``describe_running`` + ``running_engine_pids``），全在 UI 线程上——这就是
「关游戏时启动器卡死」的第一嫌疑。

现在改成走 Windows 原生的 Toolhelp32 快照（``CreateToolhelp32Snapshot`` +
``Process32First/Next``）：一次调用拿到全部进程号和进程名，**单次耗时毫秒级**，
既不用开子进程，也不用把查询挪到别的线程。POSIX 上继续用 ``ps`` 兜底。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

#: 引擎进程前缀。底座、用户自己添加的改版都可能带这些名字
_ENGINE_HINTS = ("jjxf46", "jjxf47", "godot", "mchgame")

#: 记录本启动器亲手拉起的进程号，用于「按 pid 精确判断」
_PID_FILE = "running_pids.json"

#: 进程快照的缓存有效期（秒）。退出瞬间会连着问好几次「还在跑吗」，
#: 没有缓存就是好几次系统调用；有缓存则一次快照就够。
#: 取 0.5 秒——短到「游戏刚退出」能及时反映，长到足以覆盖同一轮回调链。
_SNAPSHOT_TTL = 0.5


class ModLockedError(RuntimeError):
    """游戏运行中，扩展启停被拒绝。"""

    def __init__(self, message: str = "", *, pids: list[int] | None = None) -> None:
        super().__init__(message or "游戏正在运行，无法修改扩展启用状态")
        self.pids = pids or []


@contextlib.contextmanager
def _no_window():
    """子进程不弹黑框（Windows 下 tasklist 默认会闪一下）。"""
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    yield flags


# ---------------------------------------------------------------- 进程快照


class _Snapshot:
    """一次进程表扫描的结果。

    缓存 0.5 秒：退出回调链里会连着问「还在跑吗」「跑的是哪个」，
    同一轮里应该只扫一次系统进程表。
    """

    __slots__ = ("names", "pids", "taken_at")

    def __init__(self, pids: list[int], names: list[str]) -> None:
        self.pids = pids
        self.names = names
        self.taken_at = time.monotonic()


_snapshot: _Snapshot | None = None

#: kernel32 句柄缓存。``ctypes.WinDLL`` 每次调用都要重新加载 DLL、解析导出表，
#: 实测这一项就占了快照耗时的绝大部分——缓存下来后单次扫描降到 1ms 以内。
_kernel32 = None


def _get_kernel32():
    """拿 kernel32 的句柄（进程内缓存，只加载一次）。"""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    import ctypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return _kernel32


def _windows_process_list() -> tuple[list[int], list[str]]:
    """用 Toolhelp32 快照列出全部进程（pid + 可执行文件名）。

    比 ``tasklist`` 快两到三个数量级：不开子进程、不做文本解析，
    一次内核调用把所有进程走完。失败时返回空表——查不到就当没有游戏在跑，
    宁可少锁一次，也不能让界面卡住。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 - 非 Windows 或 ctypes 不可用
        return [], []

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    MAX_PATH = 260

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH),
        ]

    try:
        kernel32 = _get_kernel32()
        snapshot = kernel32.CreateToolhelp32Snapshot
        snapshot.restype = wintypes.HANDLE
        snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        first = kernel32.Process32FirstW
        first.restype = wintypes.BOOL
        first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        nxt = kernel32.Process32NextW
        nxt.restype = wintypes.BOOL
        nxt.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        close = kernel32.CloseHandle
        close.restype = wintypes.BOOL
        close.argtypes = [wintypes.HANDLE]
    except Exception:  # noqa: BLE001 - API 不可用就当查不到
        return [], []

    handle = snapshot(TH32CS_SNAPPROCESS, 0)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return [], []
    pids: list[int] = []
    names: list[str] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = first(handle, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile
            stem = name.lower().removesuffix(".exe")
            if stem and any(hint in stem for hint in _ENGINE_HINTS):
                pids.append(int(entry.th32ProcessID))
                names.append(name)
            ok = nxt(handle, ctypes.byref(entry))
    except Exception:  # noqa: BLE001 - 枚举中途出错就用已取到的部分
        pass
    finally:
        with contextlib.suppress(Exception):
            close(handle)
    return pids, names


def _posix_process_list() -> tuple[list[int], list[str]]:
    """非 Windows 的兜底：按进程名匹配（开发机调试用，不影响 Windows 产物）。"""
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,comm="], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return [], []
    pids: list[int] = []
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        bits = line.split(None, 1)
        if len(bits) != 2:
            continue
        try:
            pid = int(bits[0])
        except ValueError:
            continue
        comm = Path(bits[1].strip()).name
        stem = comm.lower().removesuffix(".exe")
        if any(hint in stem for hint in _ENGINE_HINTS):
            pids.append(pid)
            names.append(comm)
    return pids, names


def snapshot(force: bool = False) -> _Snapshot:
    """拿一份进程快照（0.5 秒内复用上一次结果）。

    ``force=True`` 时强制重新扫描——退出瞬间需要拿到最新状态时用。
    """
    global _snapshot
    now = time.monotonic()
    if not force and _snapshot is not None:
        if now - _snapshot.taken_at < _SNAPSHOT_TTL:
            return _snapshot
    if sys.platform == "win32":
        pids, names = _windows_process_list()
    else:
        pids, names = _posix_process_list()
    _snapshot = _Snapshot(pids, names)
    return _snapshot


def running_engine_pids() -> list[int]:
    """当前系统里在跑的游戏进程号。查不到就返回空表（不阻塞用户）。"""
    return list(snapshot().pids)


def describe_running() -> str:
    """给界面用的人话描述，例如「jjxf47.exe（PID 1234）」。"""
    snap = snapshot()
    if not snap.names:
        return "游戏进程"
    return "、".join(sorted(set(snap.names)))


def is_game_running(pids: list[int] | None = None) -> bool:
    """游戏是否在运行。``pids`` 可传入已知 pid 做精确判断（不看全表）。"""
    if pids is not None:
        return any(_pid_alive(p) for p in pids)
    return bool(snapshot().pids)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    # Windows：用 OpenProcess 探活，不开子进程
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    STILL_ACTIVE = 259
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 进程已退出时 OpenProcess 会失败；参数无效说明 pid 不存在
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            with contextlib.suppress(Exception):
                kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 探活失败就当它还活着，宁可多锁一次
        return True


def ensure_modifiable(action: str = "修改扩展") -> None:
    """前置校验：游戏在跑就抛 :class:`ModLockedError`。

    调用方（``ExtensionService.set_enabled`` 等）不用自己做判断，异常里带着
    人话原因，界面直接展示即可。
    """
    if is_game_running():
        raise ModLockedError(
            f"游戏正在运行（{describe_running()}），此时不能{action}。\n"
            "请先退出游戏，等进程完全结束后再操作。",
        )
