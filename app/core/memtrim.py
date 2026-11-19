"""启动前释放系统物理内存：把别的进程的「工作集」腾空。

原理
----

Windows 给每个进程维护一个**工作集**（working set）——当前真正驻留在物理
内存里的那部分页。里面既有刚用过的热页，也有很久没碰的冷页。系统默认不会
主动收回冷页（万一下次还要用），所以任务管理器里的占用会一直虚高。

``EmptyWorkingSet(hProcess)``（等价于 ``SetProcessWorkingSetSize(h, -1, -1)``）
就是告诉内核：「把这个进程工作集里的页全部标记为可回收。」内核随后把它们
移到待机列表（standby list），内存压力大时再写进 ``pagefile.sys``。

**它不减少任何进程的提交内存（Commit），只是把物理占用换成页面文件。**
下次访问那些页会触发缺页：还在待机列表里是软缺页（很快），已经落盘是硬缺页
（要读磁盘，机械硬盘上就是肉眼可见的一顿）。PCL 的「内存优化」提示里那句
「机械硬盘可能导致一小段时间严重卡顿」说的就是这个。

和 PCL 那个按钮的区别
--------------------

PCL 的「百宝箱 → 内存优化」是对**全系统所有进程**下手（提示原文：「不仅限于
MC」），所以它能把物理占用整体压低约 1/3。

这里做同样的事，但有两条硬约束：

1. **绝不碰游戏进程**（``jjxf*.exe``）。我们的 4.7 剧情版是 2.5GB 真实数据、
   每帧都在读的热数据，腾走之后立刻硬缺页拉回来，帧率只会更差。
2. **不碰系统关键进程和 dwm**。前者的工作集动不得；后者是桌面合成器，
   腾空会让拖动窗口、切换动画当场卡一下。

一句话：这个功能的价值是「游戏启动时有更多空闲物理页可用」，不是
「让游戏的 7GB 变小」。后者只能靠减数据量，腾内存腾不出来。
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field

__all__ = ["TrimReport", "trim_working_sets", "available_physical", "human"]


# --------------------------------------------------------------------------
# 不碰的进程
# --------------------------------------------------------------------------

#: 名字（小写）命中即跳过。
#:
#: - 前四个是内核态「进程」，本来就没有可腾的工作集；
#: - 中间那批是会话/子系统关键进程，腾它们收益极小、风险不小；
#: - ``dwm.exe`` 是桌面窗口管理器，腾空后拖窗口、Alt+Tab 会当场掉帧；
#: - ``explorer.exe`` 不在列表里 —— 它有 100~300MB 冷页，是收益最大的一块，
#:   且腾空后只是重新读图标缓存，不会闪任务栏（实测 PCL / Mem Reduct 都会清它）。
_NEVER = {
    "system",
    "system idle process",
    "registry",
    "memory compression",
    "secure system",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "fontdrvhost.exe",
    "dwm.exe",
    "audiodg.exe",
}

#: 前缀命中即跳过：游戏引擎（jjxf46.exe / jjxf47.exe）。
#:
#: 这是本模块最重要的一条。4.7 剧情版的 7GB 是热数据，腾走等于每帧硬缺页，
#: 卡顿会比不腾明显得多；而且启动前腾空、启动后立刻又要读回来，纯亏。
_NEVER_PREFIX = ("jjxf",)

#: 工作集小于这个值的进程不值得动 —— 收益几 MB，但每次 OpenProcess 都要钱。
_MIN_WORKING_SET = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# Win32 绑定
# --------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100

if os.name == "nt":  # pragma: no cover - 非 Windows 由调用方短路
    from ctypes import wintypes as _wt

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", _wt.DWORD),
            ("dwMemoryLoad", _wt.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", _wt.DWORD),
            ("PageFaultCount", _wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    _kernel32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
    _kernel32.OpenProcess.restype = _wt.HANDLE
    _kernel32.CloseHandle.argtypes = [_wt.HANDLE]
    _kernel32.CloseHandle.restype = _wt.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        _wt.HANDLE,
        _wt.DWORD,
        _wt.LPWSTR,
        ctypes.POINTER(_wt.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = _wt.BOOL
    _psapi.EmptyWorkingSet.argtypes = [_wt.HANDLE]
    _psapi.EmptyWorkingSet.restype = _wt.BOOL
    _psapi.GetProcessMemoryInfo.argtypes = [
        _wt.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX),
        _wt.DWORD,
    ]
    _psapi.GetProcessMemoryInfo.restype = _wt.BOOL
    _psapi.EnumProcesses.argtypes = [
        ctypes.POINTER(_wt.DWORD),
        _wt.DWORD,
        ctypes.POINTER(_wt.DWORD),
    ]
    _psapi.EnumProcesses.restype = _wt.BOOL
    _kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    _kernel32.GlobalMemoryStatusEx.restype = _wt.BOOL


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


@dataclass
class TrimReport:
    """一次腾内存的结果，够写一行日志、也够排查。"""

    ok: bool = False
    freed: int = 0
    """估算释放的物理字节数：各进程工作集的减少量之和。"""
    estimated: int = 0
    """试运行时的「可腾出总量」估算（不实际执行，所以 ``freed`` 恒为 0）。"""
    before_avail: int = 0
    after_avail: int = 0
    """系统可用物理内存（前 / 后），任务管理器那个口径。"""
    processes: int = 0
    """实际调用了 EmptyWorkingSet 的进程数。"""
    skipped: int = 0
    """因为受保护 / 打不开 / 太小而跳过的进程数。"""
    tops: list[tuple[str, int]] = field(default_factory=list)
    """释放最多的前几个进程，便于判断收益来自哪里。"""
    reason: str = ""

    @property
    def gained(self) -> int:
        """可用物理内存的净增量（比 ``freed`` 更接近玩家观感）。"""
        return max(0, self.after_avail - self.before_avail)

    def summary(self) -> str:
        if not self.ok:
            return self.reason or "未执行"
        if self.processes == 0:
            return f"没有可腾的进程（检查了 {self.skipped} 个）"
        return (
            f"释放 {human(self.freed)}，可用物理内存 "
            f"{human(self.before_avail)} → {human(self.after_avail)}"
            f"（{self.processes} 个进程）"
        )


def human(n: int) -> str:
    """字节转人类可读。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# --------------------------------------------------------------------------
# 实现
# --------------------------------------------------------------------------


def available_physical() -> int:
    """当前可用物理内存（字节）。拿不到返回 0。"""
    if os.name != "nt":
        return 0
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int(status.ullAvailPhys)


def _pids() -> list[int]:
    capacity = 1024
    while capacity <= 65536:
        buf = (ctypes.c_ulong * capacity)()
        needed = ctypes.c_ulong(0)
        if not _psapi.EnumProcesses(
            buf, ctypes.sizeof(buf), ctypes.byref(needed)
        ):
            return []
        count = needed.value // ctypes.sizeof(ctypes.c_ulong)
        if count < capacity:
            return [int(buf[i]) for i in range(count) if buf[i]]
        capacity *= 2  # 进程太多，缓冲区翻倍重来
    return []


def _image_name(handle) -> str:
    """进程映像文件名（小写，不含目录）。拿不到返回空串。"""
    size = 1024
    while size <= 32768:
        buf = ctypes.create_unicode_buffer(size)
        length = ctypes.c_ulong(size)
        if _kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(length)
        ):
            return os.path.basename(buf.value).lower()
        size *= 2
    return ""


def _working_set(handle) -> int:
    counters = _PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX)
    if not _psapi.GetProcessMemoryInfo(
        handle, ctypes.byref(counters), counters.cb
    ):
        return 0
    return int(counters.WorkingSetSize)


def _should_skip(name: str, pid: int, exclude: set[int]) -> bool:
    if pid in exclude or pid <= 4:
        return True
    if not name or name in _NEVER:
        return True
    return name.startswith(_NEVER_PREFIX)


def trim_working_sets(
    *,
    exclude_pids: set[int] | None = None,
    dry_run: bool = False,
    min_bytes: int = _MIN_WORKING_SET,
) -> TrimReport:
    """对系统里其它进程调用 ``EmptyWorkingSet``。

    参数
    ----
    exclude_pids:
        额外排除的进程（调用方自己的 pid、正在跑的游戏实例等）。
        游戏进程本来就会被 ``_NEVER_PREFIX`` 挡住，这里只是双保险。
    dry_run:
        只统计会腾哪些进程、能腾多少，**不实际调用**。给命令行自检用。
    min_bytes:
        工作集小于此值的进程跳过。
    """
    report = TrimReport()
    if os.name != "nt":
        report.reason = "仅支持 Windows"
        return report

    me = os.getpid()
    exclude = set(exclude_pids or ())
    exclude.add(me)

    report.before_avail = available_physical()
    gains: list[tuple[str, int]] = []

    for pid in _pids():
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_SET_QUOTA, False, pid
        )
        if not handle:
            # 跨用户会话的进程要 SeDebugPrivilege，提权才打得开；
            # 打不开就跳过，不该因为一个进程失败就整体放弃。
            report.skipped += 1
            continue
        try:
            name = _image_name(handle)
            if _should_skip(name, pid, exclude):
                report.skipped += 1
                continue
            before = _working_set(handle)
            if before < min_bytes:
                report.skipped += 1
                continue
            if dry_run:
                report.processes += 1
                report.estimated += before
                gains.append((name, before))
                continue
            if not _psapi.EmptyWorkingSet(handle):
                report.skipped += 1
                continue
            after = _working_set(handle)
            report.processes += 1
            report.freed += max(0, before - after)
            gains.append((name, max(0, before - after)))
        finally:
            _kernel32.CloseHandle(handle)

    report.after_avail = available_physical()
    gains.sort(key=lambda item: item[1], reverse=True)
    report.tops = gains[:5]
    report.ok = True
    if dry_run:
        report.reason = "试运行（未实际执行）"
    return report
