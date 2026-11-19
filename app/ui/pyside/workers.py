"""后台任务与游戏进程封装。

UI 线程绝不做阻塞事：
- 一次性的阻塞操作（导入扩展、校验基线、重建底座、冒烟）走 :class:`TaskWorker`；
- 游戏本体走 :class:`GameProcess`（QProcess），能实时回显、能停。

启动参数与 CLI 共用 :mod:`app.core.runner` 的 ``build_command`` / ``build_env``，
保证「CLI 能跑」与「界面能跑」是同一条路径。
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QThread, QTimer, Signal

from ...core import runner


# ---------------------------------------------------------------- 一次性任务


class TaskWorker(QThread):
    """在子线程里跑一个函数，结果通过信号回主线程。"""

    done = Signal(object)
    failed = Signal(str, str)   # message, traceback

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:  # pragma: no cover - 依赖线程调度
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - 需要把任意异常带回 UI
            self.failed.emit(str(exc), traceback.format_exc())
            return
        self.done.emit(result)


# ---------------------------------------------------------------- 游戏进程


class GameProcess(QObject):
    """用 QProcess 跑引擎，实时把 stdout/stderr 变成日志行。"""

    line = Signal(str, str)          # text, level
    stateChanged = Signal(str)       # starting / running / stopping / exited
    finished = Signal(dict)          # 结果摘要

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self._read_stdout)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        # 进程真正起来了才发 "running"。QProcess.started 在操作系统确认
        # 成功创建进程后触发——比 emit("starting") 晚，但比游戏窗口出现早，
        # 正好是「界面该显示运行中」的时机。
        #
        # 以前只发 starting / stopping / exited，全库没有一处发 "running"，
        # 于是：界面整局停在「启动中 / 正在挂载资源包…」，
        # 且 _sync_mod_lock() 只在 starting 那一刻查进程表（那时游戏还没起来），
        # 之后不再检查 —— 「游戏运行期间禁止改扩展」的锁整局都没上过。
        self.proc.started.connect(self._on_started)
        self._buf_out = b""
        self._buf_err = b""
        self._started_at = 0.0
        self._run_dir: Path | None = None
        self._line_key = ""
        self._cfg: dict = {}
        self._profile: str | None = None
        self._snap: dict | None = None
        self._killed = False
        self._stopping = False
        self._external = False
        self._finished_emitted = False
        self._running_emitted = False

    def _on_started(self) -> None:
        """操作系统确认进程已创建 —— 发一次 "running"。"""
        if self._running_emitted or self._finished_emitted:
            return
        self._running_emitted = True
        self.stateChanged.emit("running")

    # ------------------------------------------------------------ 启动

    @property
    def running(self) -> bool:
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def start(
        self,
        cfg: dict,
        line: str,
        *,
        packset: str | Path,
        main_scene: str,
        smoke: bool = False,
        profile: str | None = None,
        log_path: str | Path,
        mods_path: str | Path | None = None,
    ) -> None:
        exe, _ = runner.host_paths(line)
        cmd = runner.build_command(
            cfg,
            line,
            packset=packset,
            main_scene=main_scene,
            smoke=smoke,
            headless=False,
            log_path=log_path,
            mods_path=mods_path,
        )
        env, _ = runner.build_env(cfg, profile)
        qenv = QProcessEnvironment()
        for key, value in env.items():
            qenv.insert(key, value)

        # 清着色器缓存：必须在这里做。
        #
        # 玩家点「开始游戏」走的是 QProcess（本类），**不经过** runner.launch，
        # 所以原先写在 launch() 里的清理对界面完全无效 —— CLI 能清、界面不清，
        # 「角色立绘全白」照旧复现。这里是玩家真正的入口。
        #
        # 结果用 info 级写日志：dim 级不会进日志文件，之前「到底清没清」只能靠猜。
        freed, why = runner.purge_shader_cache_ex(cfg, line, profile)
        if freed:
            self.line.emit(
                f"已清着色器缓存 {freed / 1048576:.1f} MB"
                "（上次非正常退出可能留下了坏缓存，不清会导致角色立绘全白）",
                "info",
            )
        else:
            self.line.emit(f"着色器缓存未清：{why}", "info")

        # 可选：启动前腾出系统物理内存（默认关闭，设置里开）。
        #
        # 为什么放在这里而不是 runner.launch：跟清缓存同一个原因 —— 玩家
        # 点「开始游戏」根本不走 launch()。放在 QProcess.start() 之前，
        # 此时游戏进程还没起来，不会误伤。
        if (cfg.get("runtime") or {}).get("trim_working_set"):
            try:
                report = runner.trim_working_set_before_launch()
                self.line.emit(
                    f"已释放系统内存：{report.summary()}",
                    "info" if report.ok else "warn",
                )
            except Exception as exc:  # noqa: BLE001 - 优化失败绝不能拦住启动
                self.line.emit(f"内存优化失败（不影响启动）：{exc}", "warn")

        self._cfg = cfg
        self._profile = profile
        self._line_key = line
        self._run_dir = Path(log_path).parent

        # 启动前给存档做一份快照（备份 + 取指纹）。
        #
        # 和上面清缓存同样的理由：runner.launch 里的存档备份对界面无效，
        # 玩家路径上一直没备份过。游戏自己写的是 offline_roles.json 这些
        # 存档文件，跟 shader_cache 无关；但玩家一旦遇到坏档/误删，
        # 有一份「开游戏前」的备份才有得救。
        self._snap = None
        try:
            from ...core import profiles as profiles_mod

            self._snap = profiles_mod.snapshot(profile or profiles_mod.active_name(cfg))
            if self._snap.get("backup"):
                self.line.emit(f"已备份存档：{Path(self._snap['backup']).name}", "dim")
        except Exception as exc:  # noqa: BLE001 - 备份失败不该拦住启动
            self.line.emit(f"存档备份失败（不影响启动）：{exc}", "warn")

        self._started_at = time.time()
        self._killed = False
        self._stopping = False
        self._external = False
        self._buf_out = b""
        self._buf_err = b""
        self._finished_emitted = False
        self._running_emitted = False

        self.proc.setProcessEnvironment(qenv)
        self.proc.setWorkingDirectory(str(exe.parent))
        self.proc.setProgram(cmd[0])
        self.proc.setArguments(cmd[1:])
        self.stateChanged.emit("starting")
        self.line.emit(f"启动：{Path(cmd[0]).name} {' '.join(cmd[1:])}", "dim")
        self.proc.start()

    def start_external(
        self,
        program: str,
        args: list[str],
        cwd: str,
        env: dict[str, str],
        label: str = "外部程序",
        timeout_sec: int = 0,
    ) -> None:
        """启动小游戏这类与引擎无关的外部进程，复用同一套输出/结束处理。

        ``timeout_sec`` > 0 时到点自动结束（先 terminate，5 秒后 kill），
        用于防「小游戏卡死把启动器也拖住」。
        """
        qenv = QProcessEnvironment()
        for key, value in env.items():
            qenv.insert(key, value)
        self._external = True
        self._line_key = label
        self._run_dir = None
        self._cfg = {}
        self._profile = None
        self._snap = None
        self._started_at = time.time()
        self._killed = False
        self._stopping = False
        self._buf_out = b""
        self._buf_err = b""
        self._finished_emitted = False
        self._running_emitted = False
        self.proc.setProcessEnvironment(qenv)
        self.proc.setWorkingDirectory(cwd)
        self.proc.setProgram(program)
        self.proc.setArguments(list(args))
        self.stateChanged.emit("starting")
        self.line.emit(f"启动：{Path(program).name} {' '.join(args)}".strip(), "dim")
        self.proc.start()
        if timeout_sec and timeout_sec > 0:
            self.line.emit(f"将在 {timeout_sec} 秒后自动结束", "dim")
            QTimer.singleShot(int(timeout_sec) * 1000, self._on_timeout)

    def _on_timeout(self) -> None:
        if not self.running or self._stopping:
            return
        self.line.emit("已达到设定的运行时限，自动结束。", "warn")
        self.stop()

    def stop(self) -> None:
        if not self.running or self._stopping:
            return
        self._stopping = True
        self._killed = True
        self.stateChanged.emit("stopping")
        self.line.emit("正在结束游戏进程…", "warn")
        self.proc.terminate()
        QTimer.singleShot(5000, self._force_kill)

    def _force_kill(self) -> None:
        if self.running:
            self.line.emit("进程未响应，强制结束。", "err")
            self.proc.kill()

    # ------------------------------------------------------------ 输出

    def _emit_lines(self, raw: bytes, level: str) -> bytes:
        buf = raw
        while b"\n" in buf:
            head, buf = buf.split(b"\n", 1)
            text = head.decode("utf-8", "replace").rstrip("\r")
            if text.strip():
                self.line.emit(text, level)
        return buf

    def _read_stdout(self) -> None:
        self._buf_out = self._emit_lines(self._buf_out + bytes(self.proc.readAllStandardOutput()), "info")

    def _read_stderr(self) -> None:
        data = bytes(self.proc.readAllStandardError())
        snippet = data.decode("utf-8", "replace")
        level = "dim"
        low = snippet.lower()
        if "error" in low or "script error" in low:
            level = "err"
        elif "warning" in low:
            level = "warn"
        self._buf_err = self._emit_lines(self._buf_err + data, level)

    # ------------------------------------------------------------ 结束

    def _emit_exit_once(self, summary: dict | None = None) -> None:
        """只复位一次状态机，避免「启动失败」与「进程结束」重复收尾。

        BUG-1 根因：QProcess 启动失败（FailedToStart）时只发 ``errorOccurred``，
        **不会再发 finished**。原来只在 ``_on_finished`` 里复位状态机，于是界面
        永远停在「启动中」，启动按钮再也点不动。这里补上失败路径的复位。
        """
        if self._finished_emitted:
            return
        self._finished_emitted = True
        self.stateChanged.emit("exited")
        if summary is not None:
            self.finished.emit(summary)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.line.emit("进程启动失败：检查运行时 exe 是否存在、是否被杀软拦截。", "err")
            self._emit_exit_once(
                {
                    "ok": False,
                    "returncode": -1,
                    "seconds": 0,
                    "errors": ["FailedToStart"],
                    "killed": False,
                    "mounted": [],
                }
            )

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        # 每次退出后都清缓存，不只是被强杀时。
        #
        # 2026-11-11 实测：正常关窗口（退出码 0）之后，下一次启动照样立绘全白，
        # 而且引擎日志里没有任何着色器报错 —— 说明这份「刚写完」的缓存本身就有
        # 问题（读得出来，但内容坏）。所以不能只防强杀，退出即清最稳。
        # 代价是每次启动多 1–3 秒重编译。
        if self._cfg and not self._external:
            freed, why = runner.purge_shader_cache_ex(
                self._cfg, self._line_key, self._profile
            )
            if freed:
                self.line.emit(
                    f"退出后已清着色器缓存 {freed / 1048576:.1f} MB", "info"
                )
            elif why != "没有缓存目录":
                self.line.emit(f"退出后未能清缓存：{why}", "warn")
        for leftover, level in ((self._buf_out, "info"), (self._buf_err, "dim")):
            text = leftover.decode("utf-8", "replace").strip()
            if text:
                self.line.emit(text, level)
        elapsed = time.time() - self._started_at
        report = _read_json(self._run_dir / "mount_report.json") if self._run_dir else None
        smoke = _read_json(self._run_dir / "smoke_report.json") if self._run_dir else None
        saves = self._finish_saves()
        ok = exit_code == 0 and (True if self._external else (report or {}).get("ok", False))
        summary = {
            "ok": ok,
            "returncode": exit_code,
            "seconds": round(elapsed, 1),
            "mounted": (report or {}).get("mounted", []),
            "errors": (report or {}).get("errors", []),
            "smoke": smoke,
            "saves": saves,
            "killed": self._killed,
        }
        self._emit_exit_once(summary)

    def _finish_saves(self) -> dict | None:
        """退出后比对存档，把结果写进 run_dir/saves-diff.json（界面据此提示）。

        返回结构与 ``runner.launch`` 写出的那份保持一致，界面代码不用区分
        是 CLI 还是界面启动的。
        """
        if not self._snap or self._external:
            return None
        try:
            from ...core import profiles as profiles_mod
            from ...core.fsutil import atomic_write_json

            res = profiles_mod.finish(self._snap)
            payload = {
                "profile": self._snap.get("path", ""),
                "backup": self._snap.get("backup", ""),
                "delta": res.get("delta") or {},
                "summary": res.get("summary", ""),
            }
            if self._run_dir:
                atomic_write_json(self._run_dir / "saves-diff.json", payload)
            if (payload["delta"] or {}).get("total"):
                self.line.emit(payload["summary"] or "存档有变化", "info")
            return payload
        except Exception as exc:  # noqa: BLE001 - 比对失败不影响退出流程
            self.line.emit(f"存档比对失败：{exc}", "warn")
            return None


def _read_json(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- 带进度的任务


class ProgressTaskWorker(QThread):
    """和 :class:`TaskWorker` 一样跑一个函数，但会把 ``progress`` 回调注入进去。

    被调函数签名要求接受 ``progress(done, total, label)`` 关键字参数，
    用于分片打包这类长任务（一片几十秒到几分钟）。
    """

    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str, str)

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:  # pragma: no cover - 依赖线程调度
        try:
            result = self._fn(
                *self._args,
                progress=lambda done, total, label: self.progress.emit(int(done), int(total), str(label)),
                **self._kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc), traceback.format_exc())
            return
        self.done.emit(result)


# ---------------------------------------------------------------- 扩展安装

class InstallWorker(QThread):
    """在子线程里跑完整安装流程，把每一步进度转发给界面。

    跨线程发信号是 Qt 支持的（自动排队到主线程），所以这里可以直接 emit。

    合集包（内含多个扩展 zip）会走 :func:`install_bundle` 逐个安装，
    结果同样是带 ``ok`` / ``message`` / ``issues`` 的对象，界面那边
    的处理逻辑不用分叉。
    """

    progress = Signal(int, str)
    done = Signal(object)          # InstallResult / BundleResult
    failed = Signal(str, str)

    def __init__(
        self,
        cfg: dict,
        package: Path,
        verify_smoke: bool = True,
        target_line: str | None = None,
        allow_partial: bool = False,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._package = Path(package)
        self._verify_smoke = verify_smoke
        self._target_line = target_line
        self._allow_partial = allow_partial

    def run(self) -> None:  # pragma: no cover - 依赖线程调度
        from ...core.ext.service import (
            BundleResult,
            ExtensionService,
            detect_bundle,
            install_bundle,
        )
        try:
            members = detect_bundle(self._package)
            if members:
                result = self._run_bundle(members, install_bundle, BundleResult)
            else:
                svc = ExtensionService(self._cfg)
                result = svc.install(
                    self._package,
                    lambda pct, label: self.progress.emit(int(pct), str(label)),
                    verify_smoke=self._verify_smoke,
                    target_line=self._target_line,
                    allow_partial=self._allow_partial,
                )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc), traceback.format_exc())
            return
        self.done.emit(result)

    def _run_bundle(self, members, install_bundle, BundleResult):  # pragma: no cover
        """合集安装：逐个装。

        状态栏只放**汇总**（成功几个/失败几个），逐个成员的结论放进 issues——
        十几个包的消息全拼进一行会把状态栏撑爆，而且没人会读那么长。
        """
        issues: list[dict] = []

        def on_item(item) -> None:
            if item.ok:
                return
            issues.append(
                {
                    "level": "error",
                    "code": "bundle.member",
                    "message": f"{item.name} — {item.message}",
                }
            )

        result = install_bundle(
            self._cfg,
            self._package,
            progress=lambda pct, label: self.progress.emit(int(pct), str(label)),
            verify_smoke=self._verify_smoke,
            target_line=self._target_line,
            allow_partial=self._allow_partial,
            on_item=on_item,
        )
        # 组合校验 / 预检给出的提示也要进 issues：它们决定「装上是装上了，
        # 但为什么少了几个」，只写在 result.warnings 里界面读不到。
        for text in getattr(result, "warnings", None) or []:
            issues.append(
                {"level": "warn", "code": "bundle.warn", "message": str(text)}
            )
        # 让界面能像读 InstallResult 一样读 issues / message
        result.issues = issues
        return result
