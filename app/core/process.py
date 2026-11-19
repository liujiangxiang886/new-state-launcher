"""子进程封装：作业对象、进程树终止、输出收集。

计划 §4.9 要求小游戏「作业对象跟踪进程树」，§4.10 要求「存档被改坏」风险可控。
两件事都建立在同一基础能力上：**能可靠地把一个进程和它的子进程全部结束**。

Windows 上 :meth:`subprocess.Popen.terminate` 只杀直接子进程，游戏常会再拉起
子进程（渲染、崩溃上报），留下孤儿进程占着文件句柄，导致「删不掉 / 重建失败」。
这里用 Job Object 把它关进「杀作业即全杀」的容器。

本模块不依赖 Qt，可以被 CLI 与 runner 共用。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------- Job Object

if IS_WINDOWS:  # pragma: no cover - 仅 Windows 走这段
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class JobHandle:
    """把进程关进 Job Object，``kill_all()`` 一次收掉整棵进程树。"""

    def __init__(self, name: str = "jjxf-launcher") -> None:
        self._handle: Any = None
        if not IS_WINDOWS:
            return
        try:  # pragma: no cover - 仅 Windows
            handle = _kernel32.CreateJobObjectW(None, name)
            if not handle:
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _kernel32.SetInformationJobObject(
                handle,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            self._handle = handle
        except Exception:  # noqa: BLE001 - 拿不到作业对象就退化为单进程终止
            self._handle = None

    @property
    def available(self) -> bool:
        return self._handle is not None

    def assign(self, pid: int) -> bool:
        if self._handle is None:
            return False
        try:  # pragma: no cover - 仅 Windows
            process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not process:
                return False
            try:
                return bool(_kernel32.AssignProcessToJobObject(self._handle, process))
            finally:
                _kernel32.CloseHandle(process)
        except Exception:  # noqa: BLE001
            return False

    def kill_all(self) -> bool:
        if self._handle is None:
            return False
        try:  # pragma: no cover - 仅 Windows
            return bool(_kernel32.TerminateJobObject(self._handle, 1))
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        if self._handle is None:
            return
        try:  # pragma: no cover - 仅 Windows
            _kernel32.CloseHandle(self._handle)
        except Exception:  # noqa: BLE001
            pass
        self._handle = None


# ---------------------------------------------------------------- 运行结果


@dataclass
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    killed: bool = False
    command: list[str] = field(default_factory=list)
    pid: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def as_dict(self) -> dict[str, object]:
        return {
            "returncode": self.returncode,
            "seconds": round(self.seconds, 2),
            "timed_out": self.timed_out,
            "killed": self.killed,
            "pid": self.pid,
        }


#: 把一行输出交给回调（实时日志用）
LineSink = Callable[[str, str], None]


def run(
    command: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    creationflags: int | None = None,
    on_line: LineSink | None = None,
    kill_tree: bool = True,
) -> ProcessResult:
    """跑一个进程并收集输出。

    ``on_line(stream, text)`` 会在读到一行时被调用（``stream`` 是 ``out``/``err``），
    用于把游戏输出实时刷到界面日志里。

    超时时先 ``terminate``，宽限 5 秒再 ``kill``；Windows 上顺便把整棵作业树收掉。
    """
    flags = creationflags
    if flags is None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

    started = time.time()
    job = JobHandle() if kill_tree else None
    proc = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
        text=False,
    )
    if job is not None and proc.pid:
        job.assign(proc.pid)

    out_chunks: list[str] = []
    err_chunks: list[str] = []

    def pump(stream, sink: list[str], tag: str) -> None:
        buffer = b""
        try:
            assert stream is not None
            while True:
                block = stream.read(4096)
                if not block:
                    break
                buffer += block
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").rstrip("\r")
                    sink.append(text)
                    if on_line:
                        try:
                            on_line(tag, text)
                        except Exception:  # noqa: BLE001
                            pass
            if buffer:
                text = buffer.decode("utf-8", "replace").rstrip("\r")
                sink.append(text)
                if on_line:
                    try:
                        on_line(tag, text)
                    except Exception:  # noqa: BLE001
                        pass
        except (ValueError, OSError):
            pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, out_chunks, "out"), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err_chunks, "err"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = killed = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(proc, job)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            killed = True
            _kill(proc, job)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        for thread in threads:
            thread.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        if job is not None:
            job.close()

    return ProcessResult(
        returncode=int(proc.returncode if proc.returncode is not None else -9),
        stdout="\n".join(out_chunks),
        stderr="\n".join(err_chunks),
        seconds=time.time() - started,
        timed_out=timed_out,
        killed=killed,
        command=list(command),
        pid=proc.pid,
    )


def _terminate(proc: subprocess.Popen, job: JobHandle | None) -> None:
    """尽量温和地结束：先请它自己退，再退化到强杀。

    为什么不能直接 ``proc.terminate()``
    ----------------------------------

    Windows 上 Python 的 ``Popen.terminate()`` 是 ``TerminateProcess`` —— **硬杀**，
    进程没有任何机会做退出清理。

    Godot 在退出时会把编译好的 GLSL 二进制写进 ``user://shader_cache``。
    硬杀会让这份缓存只写一半，下一次启动读到坏缓存 →
    ``ShaderGLES3::initialize()`` 失败 → **所有材质回退成白色**
    （实测：删缓存立刻恢复，不删重启必白，2026-11-11 复现两次）。

    ``taskkill /PID``（不带 ``/F``）发的是 ``WM_CLOSE``，Godot 会走正常的
    关闭流程、把缓存写完再退出。只有它不响应时，才轮到 ``_kill`` 强杀。
    Job Object 那条路径（真正的 kill_all）同样会硬杀，所以这里放在最后。
    """
    if IS_WINDOWS:
        try:
            done = subprocess.run(
                ["taskkill", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
            if done.returncode == 0:
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    pass
                if proc.poll() is not None:
                    return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.terminate()
        except OSError:
            pass
        return

    if job is not None and job.available and job.kill_all():
        return
    try:
        proc.terminate()
    except OSError:
        pass


def _kill(proc: subprocess.Popen, job: JobHandle | None) -> None:
    if job is not None and job.available and job.kill_all():
        return
    try:
        proc.kill()
    except OSError:
        pass
    if IS_WINDOWS:  # pragma: no cover
        # 兜底：按映像名杀子进程（Job Object 不可用时）
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def spawn(
    command: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen, JobHandle]:
    """启动一个长期运行的进程（游戏/小游戏），返回 (Popen, JobHandle)。

    调用方负责在结束时 ``kill_tree(proc, job)``，否则 Job Object 会一直挂着。
    """
    job = JobHandle()
    proc = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0,
        text=False,
    )
    if proc.pid:
        job.assign(proc.pid)
    return proc, job


def kill_tree(proc: subprocess.Popen, job: JobHandle | None, grace: float = 3.0) -> bool:
    """结束整棵进程树。返回是否走到了「强制」这一步。"""
    if proc.poll() is not None:
        if job is not None:
            job.close()
        return False
    _terminate(proc, job)
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            if job is not None:
                job.close()
            return False
        time.sleep(0.1)
    _kill(proc, job)
    if job is not None:
        job.close()
    return True


def popen_signal(proc: subprocess.Popen, sig: int) -> None:
    """跨平台发送信号（POSIX 用 ``send_signal``，Windows 只支持 terminate/kill）。"""
    try:
        if IS_WINDOWS:
            proc.terminate()
        else:  # pragma: no cover - POSIX
            proc.send_signal(sig if sig != 0 else signal.SIGTERM)
    except OSError:
        pass


def python_executable() -> str:
    """当前解释器路径（子任务里重新拉起自己时用）。"""
    return sys.executable