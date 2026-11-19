"""日志配置。

- 文件日志写 ``logs/launcher-<时间戳>.log``，UTF-8，单文件按 16 MiB 轮转；
- 控制台日志用于 CLI 与调试，默认 INFO；
- 提供 :func:`bind_run` 把 ``run_id`` 注入所有记录，便于按构建过滤。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path

from . import paths

_CONFIGURED = False
_STAMP: str | None = None
#: 本次会话实际写出的日志文件。过去用 ``_STAMP`` 反推路径，一旦调用方
#: 传了自定义 ``log_dir``（测试、便携模式），反推出来的路径就是错的。
_LOG_FILE: Path | None = None

#: 给本模块自己加的 handler 打标记，``reset_for_tests`` 据此只摘掉自己的，
#: 不会误删调用方（例如 pytest 的 caplog）挂上去的 handler。
_HANDLER_FLAG = "_launcher_log_handler"


class _RunFilter(logging.Filter):
    """把当前 run_id 追加到每条记录。"""

    run_id: str = "-"

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


_RUN_FILTER = _RunFilter()


def setup(level: str = "INFO", *, console: bool = True, log_dir: Path | None = None) -> Path:
    """初始化根日志。重复调用幂等，返回日志文件路径。

    ``console=True`` 时只在 ``sys.stderr`` 真实存在时才挂流处理器：打包成
    windowed exe 后 ``sys.stderr`` 是 ``None``，``StreamHandler(None)`` 会
    直接抛 ``AttributeError``——这也是界面版本过去从不调用本函数的原因之一。
    """
    global _CONFIGURED, _STAMP, _LOG_FILE

    target_dir = log_dir or paths.LOGS
    target_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if _CONFIGURED:
        return current_log_file()

    _STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = target_dir / f"launcher-{_STAMP}.log"

    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=16 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    fh.addFilter(_RUN_FILTER)
    setattr(fh, _HANDLER_FLAG, True)
    root.addHandler(fh)
    _LOG_FILE = log_path

    if console and sys.stderr is not None:
        ch = logging.StreamHandler(stream=sys.stderr)
        ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        ch.addFilter(_RUN_FILTER)
        setattr(ch, _HANDLER_FLAG, True)
        root.addHandler(ch)

    _CONFIGURED = True
    logging.getLogger(__name__).debug("日志已初始化：%s", log_path)
    return log_path


def current_log_file() -> Path:
    """本次会话正在写的日志文件。

    未初始化时不创建文件，只按当前时间戳给出「将会写到哪里」的路径，
    供界面在出错时把路径显示给用户。
    """
    if _LOG_FILE is not None:
        return _LOG_FILE
    stamp = _STAMP or datetime.now().strftime("%Y%m%d-%H%M%S")
    return paths.LOGS / f"launcher-{stamp}.log"


def reset_for_tests() -> None:
    """摘掉本模块挂的 handler 并复位状态（只给测试用）。

    不复位的话，第二个测试进程/用例里 ``setup`` 会因为 ``_CONFIGURED``
    直接返回，新指定的 ``log_dir`` 完全不生效——测试会写进上一个用例的
    临时目录，排查起来非常费劲。
    """
    global _CONFIGURED, _STAMP, _LOG_FILE

    root = logging.getLogger()
    for handler in list(root.handlers):
        if not getattr(handler, _HANDLER_FLAG, False):
            continue
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响复位
            pass
    _CONFIGURED = False
    _STAMP = None
    _LOG_FILE = None


def bind_run(run_id: str) -> None:
    """把后续所有日志记录标记为某个构建/启动批次。"""
    _RUN_FILTER.run_id = run_id or "-"


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
