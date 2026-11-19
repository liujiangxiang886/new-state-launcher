"""长任务队列：串行执行、可取消、进度上报。

计划 §5 末尾要求「长任务全部进 jobs.py 串行队列；失败统一『错误码 + 查看日志』」。

为什么需要：分片打包要 2 分钟、剧情导入要 20 秒、发布包组装要复制 3 GB。
这些如果并发跑，会互相抢磁盘 IO，而且共享同一批输出目录时必然互相踩。
串行是这里唯一正确的选择——不是偷懒。

设计要点：

- **单线程执行**：一个 worker 线程 + 队列，任务按提交顺序跑；
- **任务 ID**：提交时返回 id，可用于取消与查询；
- **进度回调**：任务自己调 ``report(percent, label)``，队列负责去重与节流；
- **失败不中断队列**：一个任务抛错只标记它失败，后续任务照常跑；
- **可在无 Qt 环境使用**：不 import PySide6，靠回调把事件抛给上层。
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

Progress = Callable[[int, str], None]


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    title: str
    fn: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    state: State = State.PENDING
    percent: int = 0
    label: str = ""
    result: Any = None
    error: str = ""
    traceback_text: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def seconds(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at) if self.started_at else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state.value,
            "percent": self.percent,
            "label": self.label,
            "seconds": round(self.seconds, 1),
            "error": self.error,
        }


class JobQueue:
    """串行任务队列。

    用法::

        q = JobQueue()
        q.on_progress = lambda job: print(job.percent, job.label)
        job = q.submit("生成分片", build_shards, bid, line, out)
        q.start()
        ...
        q.stop()

    回调在 **worker 线程**里触发。UI 层（Qt）必须自己用信号把它们搬回主线程，
    不要在回调里直接碰控件。
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._cancel: set[str] = set()
        self._thread: threading.Thread | None = None
        self._counter = 0
        self._running = False

        #: 任务状态变化（排队 → 运行 → 完成/失败）
        self.on_change: Callable[[Job], None] | None = None
        #: 进度更新（已节流，同一百分比不重复触发）
        self.on_progress: Callable[[Job], None] | None = None

    # ------------------------------------------------------------ 提交

    def submit(self, title: str, fn: Callable[..., Any], *args, **kwargs) -> Job:
        with self._lock:
            self._counter += 1
            job_id = f"job-{self._counter:04d}"
            job = Job(id=job_id, title=title, fn=fn, args=args, kwargs=kwargs)
            self._jobs[job_id] = job
            self._order.append(job_id)
        self._queue.put(job_id)
        self._emit(self.on_change, job)
        return job

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="job-queue", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._running = False
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def cancel(self, job_id: str) -> bool:
        """请求取消。已运行的任务只能等它自己检查 ``should_cancel``。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state in (State.DONE, State.FAILED, State.CANCELLED):
                return False
            self._cancel.add(job_id)
            if job.state is State.PENDING:
                job.state = State.CANCELLED
                job.finished_at = time.time()
        self._emit(self.on_change, job)
        return True

    def should_cancel(self, job_id: str) -> bool:
        """任务内部轮询用：``if q.should_cancel(job.id): return``。"""
        return job_id in self._cancel

    # ------------------------------------------------------------ 查询

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in self._order if i in self._jobs]

    def pending(self) -> int:
        return sum(1 for j in self.jobs() if j.state in (State.PENDING, State.RUNNING))

    def busy(self) -> bool:
        return self.pending() > 0

    def clear_finished(self) -> int:
        with self._lock:
            dead = [
                i
                for i in self._order
                if self._jobs.get(i) and self._jobs[i].state in (State.DONE, State.FAILED, State.CANCELLED)
            ]
            for job_id in dead:
                self._jobs.pop(job_id, None)
            self._order = [i for i in self._order if i in self._jobs]
            return len(dead)

    # ------------------------------------------------------------ 内部

    def _reporter(self, job: Job) -> Progress:
        def report(percent: int, label: str = "") -> None:
            value = max(0, min(100, int(percent)))
            if value == job.percent and label == job.label:
                return
            job.percent = value
            if label:
                job.label = label
            self._emit(self.on_progress, job)

        return report

    def _emit(self, callback: Callable[[Job], None] | None, job: Job | None) -> None:
        if callback is None or job is None:
            return
        try:
            callback(job)
        except Exception:  # noqa: BLE001 - 回调炸了不能影响队列
            pass

    def _loop(self) -> None:
        while True:
            try:
                job_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                if not self._running:
                    break
                continue
            if job_id is None:
                break

            job = self._jobs.get(job_id)
            if job is None:
                continue
            if job.state is State.CANCELLED:
                continue

            job.state = State.RUNNING
            job.started_at = time.time()
            self._emit(self.on_change, job)

            try:
                report = self._reporter(job)
                # 任务函数的可选参数是约定式的：接受就传，不接受就退化为直接调用。
                # 统一由 _call 处理，避免这里的 try/except 与 run_inline 各写一套。
                job.result = _call(
                    job.fn, job.args, job.kwargs, report,
                    job_id=job.id, queue_=self,
                )
                job.state = State.DONE
                job.percent = 100
            except Exception as exc:  # noqa: BLE001 - 任务失败不影响队列
                job.state = State.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                job.traceback_text = traceback.format_exc()
            finally:
                job.finished_at = time.time()
                self._cancel.discard(job.id)
                self._emit(self.on_change, job)


# ---------------------------------------------------------------- 便捷封装


def _call(
    fn: Callable[..., Any],
    args: tuple,
    kwargs: dict,
    report: Progress,
    **extra: Any,
) -> Any:
    """调用任务函数：先按「带回调参数」调用，函数不接受时逐级退化。

    ``TypeError`` 有两种：签名不匹配（该退化）与函数体内部真的抛了 TypeError
    （不该退化）。用异常信息里的关键字区分——标准库多处也这么做。

    退化顺序：全部回调参数 → 只带 ``report`` → 不带任何回调参数。
    """
    attempts = [
        {**extra, "report": report},
        {"report": report},
        {},
    ]
    last: TypeError | None = None
    for params in attempts:
        try:
            return fn(*args, **kwargs, **params)
        except TypeError as exc:
            message = str(exc)
            if "unexpected keyword argument" not in message and "positional argument" not in message:
                raise               # 函数体内部报错，直接抛
            last = exc
    if last is not None:
        raise last
    raise RuntimeError("unreachable")


def run_inline(title: str, fn: Callable[..., Any], *args, **kwargs) -> Job:
    """同步执行一个任务（测试/CLI 用，不启线程）。"""
    q = JobQueue()
    job = q.submit(title, fn, *args, **kwargs)
    job.state = State.RUNNING
    job.started_at = time.time()
    try:
        job.result = _call(fn, args, kwargs, q._reporter(job))
        job.state = State.DONE
        job.percent = 100
    except Exception as exc:  # noqa: BLE001
        job.state = State.FAILED
        job.error = f"{type(exc).__name__}: {exc}"
        job.traceback_text = traceback.format_exc()
    finally:
        job.finished_at = time.time()
    return job


#: 全局队列（界面共用一份，避免同一时刻跑两个重任务）
SHARED = JobQueue()