"""UI 日志落盘。

这是「复制日志发给别人」的前提：过去 ``setup()`` 全项目零调用点，
界面上的日志只在内存里，窗口一关就没了，用户只能拍屏幕。
"""

from __future__ import annotations

import logging

import app.core.logging as logging_mod


def _our_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_launcher_log_handler", False)
    ]


def test_setup_is_idempotent_and_path_matches(tmp_path) -> None:
    logging_mod.reset_for_tests()
    try:
        first = logging_mod.setup("INFO", console=False, log_dir=tmp_path)
        second = logging_mod.setup("INFO", console=False, log_dir=tmp_path)

        assert first == second, "重复调用必须幂等，不能每次新建一个文件"
        assert logging_mod.current_log_file() == first
        assert len(_our_handlers()) == 1, "只能有一个文件 handler，否则每行日志写两遍"
    finally:
        logging_mod.reset_for_tests()


def test_ui_text_reaches_file(tmp_path) -> None:
    logging_mod.reset_for_tests()
    try:
        path = logging_mod.setup("INFO", console=False, log_dir=tmp_path)
        logging_mod.get("ui").error("hello-from-ui")
        for handler in _our_handlers():
            handler.flush()

        text = path.read_text(encoding="utf-8")
        assert "hello-from-ui" in text
        # 格式是 "[run_id] logger: message"，logger 名后面跟冒号
        assert "ui:" in text
        assert "[-]" in text, "run_id 占位符要出现在每条记录里"
    finally:
        logging_mod.reset_for_tests()


def test_reset_removes_only_own_handlers(tmp_path) -> None:
    logging_mod.reset_for_tests()
    foreign = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(foreign)
    try:
        logging_mod.setup("INFO", console=False, log_dir=tmp_path)
        logging_mod.reset_for_tests()
        assert foreign in root.handlers, "不能顺手删掉别人挂的 handler"
        assert _our_handlers() == []
    finally:
        if foreign in root.handlers:
            root.removeHandler(foreign)
        logging_mod.reset_for_tests()
